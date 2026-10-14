"""Generation sessions: validation, persistence, background execution, retry and version storage (MySQL-backed)."""
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Protocol
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..errors import AppError
from ..generation.artifact import assemble_workspace_files
from ..generation import rules
from ..generation.engine import EngineInput, EngineResult, run_generation
from ..generation.providers import GenerationProvider, ImageInput
from ..generation.sandbox import BuildRunner
from ..generation.state import ACTIVE_VALUES, InvalidTransition, assert_transition
from ..generation.storage import GeneratedStorage, GeneratedStorageError
from ..models import GeneratedVersion, GenerationSession, Project, Screenshot
from .storage import ScreenshotStorage, StorageError

logger = logging.getLogger("app.generation")


@dataclass
class Deps:
    """Everything the background job needs; built once in create_app."""

    settings: Settings
    factory: sessionmaker[Session]
    screenshots: ScreenshotStorage
    generated: GeneratedStorage
    provider: GenerationProvider
    runner: BuildRunner
    ops: "SessionOps | None" = None  # persistence of background runs; defaults to MySQL

    def __post_init__(self) -> None:
        if self.ops is None:
            self.ops = SqlOps(self.factory)


class InFlight:
    """Sessions this process is executing. A second submission of the same session must not run (or clean up) alongside the first."""

    def __init__(self) -> None:
        self._ids: set[uuid.UUID] = set()
        self._lock = threading.Lock()

    def claim(self, session_id: uuid.UUID) -> bool:
        with self._lock:
            if session_id in self._ids:
                return False
            self._ids.add(session_id)
            return True

    def release(self, session_id: uuid.UUID) -> None:
        with self._lock:
            self._ids.discard(session_id)


_IN_FLIGHT = InFlight()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- readiness --------------------------------------------------------------------------------------------------

def readiness(settings: Settings, runner: BuildRunner) -> dict:
    build_ok, build_msg = runner.availability()
    return {
        "ai_configured": settings.ai_configured,
        "code_model": settings.ai_code_model,
        "vision_model": settings.ai_vision_model,
        "build_runner": runner.name,
        "build_available": build_ok,
        "build_message": build_msg,
        "ready": settings.ai_configured and build_ok,
        "max_session_attempts": settings.max_session_attempts,
    }


def require_ready(settings: Settings, runner: BuildRunner) -> None:
    """Fail fast with an actionable error instead of creating a session that can never complete."""
    if not settings.ai_configured:
        raise AppError(503, "ai_not_configured", "No AI provider token is configured. Set AI_API_KEY (or HF_TOKEN) in .env and restart the backend.")
    ok, msg = runner.availability()
    if not ok:
        raise AppError(503, "build_sandbox_unavailable", msg)


# ---- helpers ----------------------------------------------------------------------------------------------------

def _active_count(session: Session, project_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.count()).select_from(GenerationSession).where(
            GenerationSession.project_id == project_id, GenerationSession.status.in_(ACTIVE_VALUES)
        )
    )


def _lock_project(session: Session, project_id: uuid.UUID) -> Project:
    project = session.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if project is None:
        raise AppError(404, "project_not_found", "Project not found.")
    return project


def get_session_row(session: Session, session_id: uuid.UUID) -> GenerationSession:
    row = session.get(GenerationSession, session_id)
    if row is None:
        raise AppError(404, "generation_not_found", "Generation not found.")
    return row


def version_for_session(session: Session, session_id: uuid.UUID) -> GeneratedVersion | None:
    return session.scalar(select(GeneratedVersion).where(GeneratedVersion.session_id == session_id))


def get_version(session: Session, version_id: uuid.UUID) -> GeneratedVersion:
    row = session.get(GeneratedVersion, version_id)
    if row is None:
        raise AppError(404, "version_not_found", "Version not found.")
    return row


def list_sessions(session: Session, project_id: uuid.UUID, limit: int = 20) -> list[GenerationSession]:
    _ensure_project(session, project_id)
    return list(
        session.scalars(
            select(GenerationSession).where(GenerationSession.project_id == project_id)
            .order_by(GenerationSession.created_at.desc(), GenerationSession.id).limit(limit)
        )
    )


def list_versions(session: Session, project_id: uuid.UUID) -> list[GeneratedVersion]:
    _ensure_project(session, project_id)
    return list(
        session.scalars(
            select(GeneratedVersion).where(GeneratedVersion.project_id == project_id).order_by(GeneratedVersion.version_number.desc())
        )
    )


def _ensure_project(session: Session, project_id: uuid.UUID) -> None:
    if session.get(Project, project_id) is None:
        raise AppError(404, "project_not_found", "Project not found.")


# ---- creating and retrying --------------------------------------------------------------------------------------

def create_session(
    session: Session,
    settings: Settings,
    runner: BuildRunner,
    project_id: uuid.UUID,
    prompt: str | None,
    screenshot_ids: list[uuid.UUID],
    parent_version_id: uuid.UUID | None,
) -> GenerationSession:
    prompt, unique_ids, mode = rules.normalize_request(settings, prompt, screenshot_ids, parent_version_id)

    require_ready(settings, runner)

    project = _lock_project(session, project_id)
    if unique_ids:
        found = set(session.scalars(select(Screenshot.id).where(Screenshot.project_id == project_id, Screenshot.id.in_(unique_ids))))
        rules.assert_screenshots_in_project(unique_ids, found)
    if parent_version_id is not None:
        parent = session.get(GeneratedVersion, parent_version_id)
        rules.assert_parent_in_project(parent.project_id if parent else None, project_id)
    rules.assert_no_active_session(_active_count(session, project_id))

    row = GenerationSession(
        project_id=project_id,
        parent_version_id=parent_version_id,
        input_mode=mode,
        prompt=prompt,
        screenshot_ids=[str(i) for i in unique_ids],
        status="created",
        attempts=1,
        retryable=False,
    )
    assert_transition("created", "queued")
    row.status = "queued"
    session.add(row)
    project.status = "generating"
    session.commit()
    return row


def retry_session(session: Session, settings: Settings, runner: BuildRunner, session_id: uuid.UUID) -> GenerationSession:
    require_ready(settings, runner)
    row = session.scalar(select(GenerationSession).where(GenerationSession.id == session_id).with_for_update())
    if row is None:
        raise AppError(404, "generation_not_found", "Generation not found.")
    project = _lock_project(session, row.project_id)
    rules.assert_retryable(row.status, row.retryable, row.attempts, settings.max_session_attempts)
    rules.assert_no_active_session(_active_count(session, row.project_id))
    assert_transition(row.status, "queued")
    row.status = "queued"
    row.attempts += 1
    row.error_code = row.error_message = row.build_log = None
    row.retryable = False
    row.completed_at = None
    project.status = "generating"
    session.commit()
    return row


# ---- background execution ---------------------------------------------------------------------------------------

@dataclass
class SessionFacts:
    """What the worker needs to run a session. `screenshots` holds (storage_key, content_type, filename) or None for a missing one."""

    project_id: uuid.UUID
    prompt: str | None
    parent_id: uuid.UUID | None
    screenshots: list[tuple[str, str, str] | None]


class SessionOps(Protocol):
    """The persistence steps of a background run. Implemented for MySQL (SqlOps) and for the folder store."""

    def load(self, session_id: uuid.UUID) -> SessionFacts | None:
        """Facts of a session that is `queued`; None if it is missing or already handled."""
        ...

    def set_status(self, session_id: uuid.UUID, target: str) -> None: ...

    def fail(self, session_id: uuid.UUID, code: str, message: str, retryable: bool, build_log: str | None = None) -> None: ...

    def complete(self, session_id: uuid.UUID, facts: SessionFacts, version_id: uuid.UUID, result: EngineResult) -> None:
        """Record the version and mark the session completed, atomically. Called only after a successful build."""
        ...


class SqlOps:
    def __init__(self, factory: sessionmaker[Session]):
        self._factory = factory

    def load(self, session_id: uuid.UUID) -> SessionFacts | None:
        with self._factory() as s:
            row = get_session_row(s, session_id)
            if row.status != "queued":
                return None  # duplicate submission or already handled
            project_id = row.project_id
            shot_rows = [s.get(Screenshot, uuid.UUID(i)) for i in row.screenshot_ids]
            # a screenshot of another project is treated as missing (ownership was checked at creation; re-checked here)
            shots = [(r.storage_key, r.content_type, r.original_filename) if r and r.project_id == project_id else None for r in shot_rows]
            return SessionFacts(project_id, row.prompt, row.parent_version_id, shots)

    def set_status(self, session_id: uuid.UUID, target: str) -> None:
        with self._factory() as s:
            row = s.scalar(select(GenerationSession).where(GenerationSession.id == session_id).with_for_update())
            if row is None:
                raise InvalidTransition("Session disappeared.")
            assert_transition(row.status, target)
            row.status = target
            if row.started_at is None and target != "queued":
                row.started_at = _now()
            s.commit()

    def fail(self, session_id: uuid.UUID, code: str, message: str, retryable: bool, build_log: str | None = None) -> None:
        with self._factory() as s:
            row = s.scalar(select(GenerationSession).where(GenerationSession.id == session_id).with_for_update())
            if row is None or row.status in ("completed", "failed"):
                return
            assert_transition(row.status, "failed")
            row.status, row.error_code, row.error_message, row.retryable = "failed", code, message[:2000], retryable
            row.build_log = build_log[-20000:] if build_log else None
            row.completed_at = _now()
            project = _lock_project(s, row.project_id)
            project.status = _project_status_after_failure(s, row.project_id, code)
            s.commit()

    def complete(self, session_id: uuid.UUID, facts: SessionFacts, version_id: uuid.UUID, result: EngineResult) -> None:
        with self._factory() as s:
            row = s.scalar(select(GenerationSession).where(GenerationSession.id == session_id).with_for_update())
            project = _lock_project(s, facts.project_id)
            number = rules.next_version_number(
                s.scalar(select(func.max(GeneratedVersion.version_number)).where(GeneratedVersion.project_id == facts.project_id))
            )
            manifest = rules.version_manifest(assemble_workspace_files(result.files))
            s.add(GeneratedVersion(
                id=version_id, project_id=facts.project_id, session_id=session_id, parent_version_id=facts.parent_id,
                version_number=number, summary=result.summary, uncertainties=result.uncertainties, files=manifest,
                model_id=result.model_id, repaired=result.repaired,
            ))
            s.flush()
            assert_transition(row.status, "completed")
            row.status, row.completed_at, row.model_id, row.build_log = "completed", _now(), result.model_id, result.build_log[-20000:]
            project.status = "ready"
            s.commit()


def _project_status_after_failure(s: Session, project_id: uuid.UUID, error_code: str) -> str:
    count = s.scalar(select(func.count()).select_from(GeneratedVersion).where(GeneratedVersion.project_id == project_id))
    return rules.project_status_after_failure(count > 0, error_code)


def run_session(deps: Deps, session_id: uuid.UUID) -> None:
    """Executed in a worker thread. Never raises: every outcome is recorded on the session. Storage-agnostic via `deps.ops`."""
    if not _IN_FLIGHT.claim(session_id):
        logger.warning("Generation session %s is already being processed; duplicate submission ignored", session_id)
        return
    ops = deps.ops
    workspace = deps.generated.work_dir(session_id)
    try:
        facts = ops.load(session_id)
        if facts is None:
            return

        images: list[ImageInput] = []
        for item in facts.screenshots:
            if item is None:
                ops.fail(session_id, "screenshot_missing", "A selected screenshot no longer exists.", False)
                return
            key, ctype, fname = item
            try:
                images.append(ImageInput(ctype, deps.screenshots.path_for(key).read_bytes(), fname))
            except (StorageError, OSError):
                ops.fail(session_id, "screenshot_file_missing", "A screenshot file could not be read.", False)
                return

        existing = None
        if facts.parent_id is not None:
            try:
                existing = deps.generated.read_sources(facts.project_id, facts.parent_id)
            except (GeneratedStorageError, OSError):
                ops.fail(session_id, "parent_source_missing", "The source of the version to refine is missing.", False)
                return

        result = run_generation(
            EngineInput(prompt=facts.prompt, images=images, existing_files=existing),
            settings=deps.settings, provider=deps.provider, runner=deps.runner, workspace=workspace,
            on_stage=lambda st: ops.set_status(session_id, st),
        )
        if not result.ok:
            ops.fail(session_id, result.error_code or "generation_failed", result.error_message or "Generation failed.", result.retryable, result.build_log)
            return

        version_id = uuid.uuid4()
        target = deps.generated.version_dir(facts.project_id, version_id)
        try:
            deps.generated.promote(workspace, facts.project_id, version_id)
            ops.complete(session_id, facts, version_id, result)
        except Exception:
            deps.generated.discard(target)
            raise
    except Exception:
        logger.exception("Generation session %s failed unexpectedly", session_id)
        try:
            ops.fail(session_id, "internal_error", "An unexpected error stopped the generation.", True)
        except Exception:
            logger.exception("Could not record failure for session %s", session_id)
    finally:
        deps.generated.discard(workspace)
        _IN_FLIGHT.release(session_id)


def recover_interrupted(factory: sessionmaker[Session]) -> int:
    """After a restart, sessions left in an active state can no longer progress: mark them failed (retryable)."""
    count = 0
    with factory() as s:
        rows = s.scalars(select(GenerationSession).where(GenerationSession.status.in_(ACTIVE_VALUES)).with_for_update()).all()
        for row in rows:
            row.status, row.error_code, row.retryable = "failed", "interrupted", True
            row.error_message = "The backend restarted while this generation was running."
            row.completed_at = _now()
            project = s.get(Project, row.project_id)
            if project is not None:
                project.status = _project_status_after_failure(s, row.project_id, "interrupted")
            count += 1
        s.commit()
    return count
