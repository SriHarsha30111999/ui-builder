import logging
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import create_db_engine, create_session_factory, session_scope
from .errors import AppError, error_body, register_error_handlers
from .generation.export import ExportError, build_zip
from .generation.preview import preview_headers
from .generation.providers import GenerationProvider, OpenAICompatibleProvider
from .generation.sandbox import BuildRunner, make_runner
from .generation.storage import GeneratedStorage, GeneratedStorageError
from .schemas import (
    BuildLogOut,
    GenerationCreate,
    GenerationOut,
    GenerationReadinessOut,
    ProjectCreate,
    ProjectDetailOut,
    ProjectOut,
    ScreenshotOut,
    VersionOut,
)
from .services import folder_backend
from .services import generations as sql_generations
from .services import projects as sql_projects
from .services import screenshots as sql_screenshots
from .services.folder_store import FolderStore
from .services.storage import ScreenshotStorage, StorageError


def get_session(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.session_factory)


def get_storage(request: Request) -> ScreenshotStorage:
    return request.app.state.storage


def create_app(
    settings: Settings | None = None,
    provider: GenerationProvider | None = None,
    runner: BuildRunner | None = None,
) -> FastAPI:
    """`provider` / `runner` can be injected (tests); by default they come from configuration."""
    settings = settings or get_settings()
    if settings.persistence == "folder":
        # Explicit opt-in (PERSISTENCE=folder): JSON records under FOLDER_STORE_DIR. Never chosen as a fallback for MySQL.
        folder_store = FolderStore(settings.resolved_folder_store_dir)
        engine = None
        factory = lambda: folder_store  # noqa: E731  (the store plays the role of the Session)
        project_service = screenshot_service = gen_service = folder_backend
        ops = folder_backend.FolderOps(folder_store)
    else:
        folder_store = None
        engine = create_db_engine(settings)
        factory = create_session_factory(engine)
        project_service, screenshot_service, gen_service = sql_projects, sql_screenshots, sql_generations
        ops = None
    storage = ScreenshotStorage(settings.resolved_storage_dir)
    generated = GeneratedStorage(settings.resolved_generated_dir)
    runner = runner or make_runner(settings)
    provider = provider or OpenAICompatibleProvider(
        settings.ai_base_url, settings.ai_api_key.get_secret_value(), settings.ai_timeout_seconds, settings.ai_max_output_tokens
    )
    deps = gen_service.Deps(settings, factory, storage, generated, provider, runner, ops)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="generation")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        storage.ensure_ready()
        generated.ensure_ready()
        if folder_store is not None:
            folder_store.open()  # raises if another backend process already uses this folder
            logging.getLogger("app").warning("Persistence: folder store at %s (single process only)", folder_store.root)
        try:
            n = gen_service.recover_interrupted(factory)
            if n:
                logging.getLogger("app").warning("Marked %d interrupted generation(s) as failed", n)
        except Exception:  # database may be down at startup; the API still starts and reports 503s itself
            logging.getLogger("app").warning("Could not check for interrupted generations at startup")
        yield
        executor.shutdown(wait=False, cancel_futures=True)
        if folder_store is not None:
            folder_store.shutdown()
        if engine is not None:
            engine.dispose()

    app = FastAPI(title="Screenshot-to-React Builder API", version="0.3.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.folder_store = folder_store
    app.state.generation_deps = deps
    app.state.executor = executor
    app.state.storage = storage

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )
    register_error_handlers(app)

    api = APIRouter(prefix="/api")

    @api.get("/health")
    def health():
        return {"status": "ok"}

    @api.get("/health/ready")
    def ready(request: Request):
        if folder_store is not None:
            try:
                folder_store.check_writable()
            except OSError:
                return JSONResponse(error_body("store_unavailable", "The data folder is not writable."), status_code=503)
            return {"status": "ready", "persistence": "folder"}
        try:
            with request.app.state.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse(error_body("database_unavailable", "The database is currently unavailable."), status_code=503)
        return {"status": "ready", "database": "ok"}

    @api.get("/projects", response_model=list[ProjectOut], response_model_by_alias=True)
    def list_projects(session: Session = Depends(get_session)):
        return project_service.list_projects(session)

    @api.post("/projects", response_model=ProjectDetailOut, status_code=201, response_model_by_alias=True)
    def create_project(payload: ProjectCreate, session: Session = Depends(get_session)):
        return project_service.create_project(session, payload)

    @api.get("/projects/{project_id}", response_model=ProjectDetailOut, response_model_by_alias=True)
    def get_project(project_id: uuid.UUID, session: Session = Depends(get_session)):
        return project_service.get_project_detail(session, project_id)

    @api.delete("/projects/{project_id}", status_code=204)
    def delete_project(
        project_id: uuid.UUID,
        session: Session = Depends(get_session),
        storage: ScreenshotStorage = Depends(get_storage),
    ):
        project_service.delete_project(session, storage, generated, project_id)
        return Response(status_code=204)

    @api.get("/projects/{project_id}/screenshots", response_model=list[ScreenshotOut], response_model_by_alias=True)
    def list_screenshots(project_id: uuid.UUID, session: Session = Depends(get_session)):
        return project_service.get_project_detail(session, project_id).screenshots

    @api.post(
        "/projects/{project_id}/screenshots",
        response_model=list[ScreenshotOut],
        status_code=201,
        response_model_by_alias=True,
    )
    async def upload_screenshots(
        project_id: uuid.UUID,
        files: list[UploadFile] = File(...),
        session: Session = Depends(get_session),
        storage: ScreenshotStorage = Depends(get_storage),
    ):
        limit = settings.max_screenshot_bytes
        incoming: list[screenshot_service.IncomingFile] = []
        for f in files:
            data = await f.read(limit + 1)  # never buffer more than limit + 1 bytes per file
            incoming.append(screenshot_service.IncomingFile(filename=f.filename, data=data))
        rows = screenshot_service.add_screenshots(
            session, storage, project_id, incoming, limit, settings.max_screenshots_per_prompt
        )
        return [project_service.screenshot_out(r) for r in rows]

    @api.get("/screenshots/{screenshot_id}/metadata", response_model=ScreenshotOut, response_model_by_alias=True)
    def screenshot_metadata(screenshot_id: uuid.UUID, session: Session = Depends(get_session)):
        return project_service.screenshot_out(screenshot_service.get_screenshot(session, screenshot_id))

    @api.get("/screenshots/{screenshot_id}")
    def screenshot_content(
        screenshot_id: uuid.UUID,
        session: Session = Depends(get_session),
        storage: ScreenshotStorage = Depends(get_storage),
    ):
        shot = screenshot_service.get_screenshot(session, screenshot_id)
        try:
            path = storage.path_for(shot.storage_key)
        except StorageError as exc:
            raise AppError(404, "screenshot_file_missing", "The screenshot file is not available.") from exc
        if not path.is_file():
            raise AppError(404, "screenshot_file_missing", "The screenshot file is not available.")
        return FileResponse(
            path,
            media_type=shot.content_type,
            headers={"X-Content-Type-Options": "nosniff", "Content-Disposition": "inline", "Cache-Control": "private, max-age=300"},
        )

    @api.delete("/screenshots/{screenshot_id}", status_code=204)
    def delete_screenshot(
        screenshot_id: uuid.UUID,
        session: Session = Depends(get_session),
        storage: ScreenshotStorage = Depends(get_storage),
    ):
        screenshot_service.delete_screenshot(session, storage, screenshot_id)
        return Response(status_code=204)

    # ---- Phase 3: AI generation -------------------------------------------------------------------------------

    def generation_out(session: Session, row) -> GenerationOut:
        version = gen_service.version_for_session(session, row.id)
        out = GenerationOut.model_validate(row)
        out.version_id = version.id if version else None
        out.screenshot_ids = [uuid.UUID(i) for i in row.screenshot_ids]
        return out

    def version_out(v) -> VersionOut:
        out = VersionOut.model_validate(v)
        out.preview_url = f"/api/previews/{v.id}/"
        return out

    @api.get("/generation/status", response_model=GenerationReadinessOut, response_model_by_alias=True)
    def generation_status():
        return gen_service.readiness(settings, runner)

    @api.post("/projects/{project_id}/generations", response_model=GenerationOut, status_code=202, response_model_by_alias=True)
    def start_generation(project_id: uuid.UUID, payload: GenerationCreate, session: Session = Depends(get_session)):
        row = gen_service.create_session(
            session, settings, runner, project_id, payload.prompt, payload.screenshot_ids, payload.parent_version_id
        )
        executor.submit(gen_service.run_session, deps, row.id)
        return generation_out(session, row)

    @api.get("/projects/{project_id}/generations", response_model=list[GenerationOut], response_model_by_alias=True)
    def list_generations(project_id: uuid.UUID, session: Session = Depends(get_session)):
        return [generation_out(session, r) for r in gen_service.list_sessions(session, project_id)]

    @api.get("/generations/{generation_id}", response_model=GenerationOut, response_model_by_alias=True)
    def get_generation(generation_id: uuid.UUID, session: Session = Depends(get_session)):
        return generation_out(session, gen_service.get_session_row(session, generation_id))

    @api.post("/generations/{generation_id}/retry", response_model=GenerationOut, status_code=202, response_model_by_alias=True)
    def retry_generation(generation_id: uuid.UUID, session: Session = Depends(get_session)):
        row = gen_service.retry_session(session, settings, runner, generation_id)
        executor.submit(gen_service.run_session, deps, row.id)
        return generation_out(session, row)

    @api.get("/generations/{generation_id}/build-log", response_model=BuildLogOut, response_model_by_alias=True)
    def build_log(generation_id: uuid.UUID, session: Session = Depends(get_session)):
        return BuildLogOut(log=gen_service.get_session_row(session, generation_id).build_log or "")

    @api.get("/projects/{project_id}/versions", response_model=list[VersionOut], response_model_by_alias=True)
    def list_versions(project_id: uuid.UUID, session: Session = Depends(get_session)):
        return [version_out(v) for v in gen_service.list_versions(session, project_id)]

    @api.get("/versions/{version_id}", response_model=VersionOut, response_model_by_alias=True)
    def get_version(version_id: uuid.UUID, session: Session = Depends(get_session)):
        return version_out(gen_service.get_version(session, version_id))

    @api.get("/versions/{version_id}/files/{file_path:path}")
    def version_source(version_id: uuid.UUID, file_path: str, session: Session = Depends(get_session)):
        v = gen_service.get_version(session, version_id)
        try:
            path = generated.source_file(v.project_id, v.id, file_path)
        except GeneratedStorageError as exc:
            raise AppError(404, "file_not_found", "The file was not found in this version.") from exc
        return PlainTextResponse(path.read_text(encoding="utf-8"), headers={"X-Content-Type-Options": "nosniff"})

    @api.get("/versions/{version_id}/download")
    def download_version(version_id: uuid.UUID, session: Session = Depends(get_session)):
        v = gen_service.get_version(session, version_id)
        try:
            data = build_zip(generated.version_dir(v.project_id, v.id), v.version_number)
        except (ExportError, GeneratedStorageError, OSError) as exc:
            raise AppError(404, "source_missing", "The source of this version could not be exported.") from exc
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="generated-ui-v{v.version_number}.zip"', "X-Content-Type-Options": "nosniff"},
        )

    def serve_preview(version_id: uuid.UUID, file_path: str, session: Session):
        v = gen_service.get_version(session, version_id)  # only successfully built versions exist
        try:
            path = generated.preview_file(v.project_id, v.id, file_path)
        except GeneratedStorageError as exc:
            raise AppError(404, "file_not_found", "The preview file was not found.") from exc
        headers = preview_headers(settings.cors_origin_list, path.name)
        return FileResponse(path, media_type=headers.pop("Content-Type"), headers=headers)

    @api.get("/previews/{version_id}/")
    def preview_index(version_id: uuid.UUID, session: Session = Depends(get_session)):
        return serve_preview(version_id, "index.html", session)

    @api.get("/previews/{version_id}/{file_path:path}")
    def preview_file(version_id: uuid.UUID, file_path: str, session: Session = Depends(get_session)):
        return serve_preview(version_id, file_path, session)

    app.include_router(api)
    return app


app = create_app()
