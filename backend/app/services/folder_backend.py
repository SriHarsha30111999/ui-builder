"""Service functions of the folder backend, with the same names and behaviour as services/projects.py, screenshots.py and
generations.py (MySQL). main.py picks one set by PERSISTENCE. Business rules come from the shared pure modules
(rules.py, project_rules.py, state.py); only the reads and writes differ.

The first parameter is a FolderStore where the MySQL functions take a SQLAlchemy Session. All read-modify-write sequences hold
`store.lock`, the in-process stand-in for MySQL's project row lock.
"""
import logging
import uuid
from collections.abc import Callable

from ..config import Settings
from ..errors import AppError
from ..generation import rules
from ..generation.artifact import assemble_workspace_files
from ..generation.engine import EngineResult
from ..generation.sandbox import BuildRunner
from ..generation.state import ACTIVE_VALUES, InvalidTransition, assert_transition
from . import project_rules
from .folder_store import FolderStore, ProjectRec, ScreenshotRec, SessionRec, VersionRec, now
from .generations import Deps, SessionFacts, readiness, require_ready, run_session  # noqa: F401  (re-exported for main.py)
from .projects import _to_out, screenshot_out
from .screenshots import IncomingFile, validate_incoming  # noqa: F401
from .storage import ScreenshotStorage, StorageError, sanitize_display_name

logger = logging.getLogger("app.generation")

# ---- projects ---------------------------------------------------------------------------------------------------


def _project(store: FolderStore, project_id: uuid.UUID) -> ProjectRec:
    rec = store.get("projects", project_id)
    if rec is None:
        raise AppError(404, "project_not_found", "Project not found.")
    return rec


def _touch_project(store: FolderStore, project: ProjectRec, status: str | None = None) -> None:
    if status is not None:
        project.status = status
    project.updated_at = now()
    store.put("projects", project)


def _project_out(store: FolderStore, project: ProjectRec, projects: dict | None = None, counts: dict | None = None):
    projects = projects if projects is not None else {p.id: p for p in store.all("projects")}
    counts = counts if counts is not None else _screenshot_counts(store)
    master = projects.get(project.master_id) if project.master_id else None
    return _to_out(project, master.name if master else None, counts.get(project.id, 0))


def _screenshot_counts(store: FolderStore) -> dict:
    counts: dict = {}
    for s in store.all("screenshots"):
        counts[s.project_id] = counts.get(s.project_id, 0) + 1
    return counts


def list_projects(store: FolderStore):
    with store.lock:
        projects = store.all("projects")
        by_id, counts = {p.id: p for p in projects}, _screenshot_counts(store)
        projects.sort(key=lambda p: (p.updated_at, str(p.id)), reverse=True)  # newest first, like ORDER BY updated_at DESC
        return [_project_out(store, p, by_id, counts) for p in projects]


def get_project_detail(store: FolderStore, project_id: uuid.UUID):
    from ..schemas import ProjectDetailOut

    with store.lock:
        project = _project(store, project_id)
        shots = sorted((s for s in store.all("screenshots") if s.project_id == project_id), key=lambda s: (s.created_at, str(s.id)))
        base = _project_out(store, project)
        return ProjectDetailOut(**base.model_dump(), screenshots=[screenshot_out(s) for s in shots])


def delete_project(store: FolderStore, storage, generated, project_id: uuid.UUID) -> None:
    """Remove the project and everything it owns: versions, generation sessions, screenshots (records and files), generated source."""
    with store.lock:
        _project(store, project_id)
        sessions = [s for s in store.all("sessions") if s.project_id == project_id]
        features = sum(1 for p in store.all("projects") if p.master_id == project_id)
        rules.assert_project_deletable(sum(1 for s in sessions if s.status in ACTIVE_VALUES), features)
        versions = [v for v in store.all("versions") if v.project_id == project_id]
        shots = [s for s in store.all("screenshots") if s.project_id == project_id]
        # The project record goes last, so an interrupted delete can simply be repeated.
        for kind, recs in (("versions", versions), ("sessions", sessions), ("screenshots", shots)):
            for r in recs:
                store.delete(kind, r.id)
        store.delete("projects", project_id)
    for s in shots:
        storage.remove_quietly(s.storage_key)
    generated.discard(generated.root / str(project_id))


def create_project(store: FolderStore, data):
    new = project_rules.normalize_project(data)
    with store.lock:
        projects = store.all("projects")
        if new.type == "feature":
            master = next((p for p in projects if p.id == new.master_id), None)
            project_rules.assert_master(master.type if master else None)
            if any(p.master_id == new.master_id and p.base_path == new.base_path for p in projects):  # case-sensitive, like utf8mb4_bin
                raise project_rules.base_path_conflict(new.base_path)
        stamp = now()
        rec = ProjectRec(
            id=uuid.uuid4(), name=new.name, type=new.type,
            master_id=new.master_id, master_type="master" if new.type == "feature" else None,
            base_path=new.base_path, initial_prompt=new.initial_prompt, status="draft", created_at=stamp, updated_at=stamp,
        )
        store.put("projects", rec)
        return get_project_detail(store, rec.id)


# ---- screenshots ------------------------------------------------------------------------------------------------

def add_screenshots(
    store: FolderStore, storage: ScreenshotStorage, project_id: uuid.UUID, files: list[IncomingFile], max_bytes: int, max_per_project: int
) -> list[ScreenshotRec]:
    """All-or-nothing, like the MySQL version: either every file is stored and recorded, or none is."""
    if not files:
        raise AppError(422, "no_files", "Attach at least one image.")
    with store.lock:
        _project(store, project_id)
        types = [validate_incoming(f, max_bytes) for f in files]
        existing = rules.count_unsent(
            [str(s.id) for s in store.all("screenshots") if s.project_id == project_id],
            [s.screenshot_ids for s in store.all("sessions") if s.project_id == project_id],
        )
        if existing + len(files) > max_per_project:
            raise AppError(409, "too_many_screenshots", f"A message can have at most {max_per_project} screenshots ({existing} already waiting).")
        written_keys: list[str] = []
        written_recs: list[ScreenshotRec] = []
        try:
            for incoming, content_type in zip(files, types, strict=True):
                key = storage.new_key(content_type)
                storage.write(key, incoming.data)
                written_keys.append(key)
                rec = ScreenshotRec(
                    id=uuid.uuid4(), project_id=project_id, storage_key=key,
                    original_filename=sanitize_display_name(incoming.filename), content_type=content_type,
                    size_bytes=len(incoming.data), created_at=now(),
                )
                store.put("screenshots", rec)
                written_recs.append(rec)
        except StorageError as exc:
            _undo_screenshots(store, storage, written_keys, written_recs)
            raise AppError(500, "storage_failed", "The upload could not be saved. Nothing was stored.") from exc
        except Exception:
            _undo_screenshots(store, storage, written_keys, written_recs)
            raise
        return written_recs


def _undo_screenshots(store: FolderStore, storage: ScreenshotStorage, keys: list[str], recs: list[ScreenshotRec]) -> None:
    for r in recs:
        store.delete("screenshots", r.id)
    for k in keys:
        storage.remove_quietly(k)


def get_screenshot(store: FolderStore, screenshot_id: uuid.UUID) -> ScreenshotRec:
    rec = store.get("screenshots", screenshot_id)
    if rec is None:
        raise AppError(404, "screenshot_not_found", "Screenshot not found.")
    return rec


def delete_screenshot(store: FolderStore, storage: ScreenshotStorage, screenshot_id: uuid.UUID) -> None:
    with store.lock:
        shot = get_screenshot(store, screenshot_id)
        rules.assert_screenshot_unused(str(shot.id), [s.screenshot_ids for s in store.all("sessions") if s.project_id == shot.project_id])
        store.delete("screenshots", shot.id)
    storage.remove_quietly(shot.storage_key)  # the file goes only after the record, so no record points at a missing file


# ---- generations ------------------------------------------------------------------------------------------------

def _session(store: FolderStore, session_id: uuid.UUID) -> SessionRec:
    rec = store.get("sessions", session_id)
    if rec is None:
        raise AppError(404, "generation_not_found", "Generation not found.")
    return rec


get_session_row = _session


def _active_count(store: FolderStore, project_id: uuid.UUID) -> int:
    return sum(1 for s in store.all("sessions") if s.project_id == project_id and s.status in ACTIVE_VALUES)


def version_for_session(store: FolderStore, session_id: uuid.UUID) -> VersionRec | None:
    return next((v for v in store.all("versions") if v.session_id == session_id), None)


def get_version(store: FolderStore, version_id: uuid.UUID) -> VersionRec:
    rec = store.get("versions", version_id)
    if rec is None:
        raise AppError(404, "version_not_found", "Version not found.")
    return rec


def list_sessions(store: FolderStore, project_id: uuid.UUID, limit: int = 20) -> list[SessionRec]:
    with store.lock:
        _project(store, project_id)
        rows = [s for s in store.all("sessions") if s.project_id == project_id]
        rows.sort(key=lambda s: (s.created_at, str(s.id)), reverse=True)
        return rows[:limit]


def list_versions(store: FolderStore, project_id: uuid.UUID) -> list[VersionRec]:
    with store.lock:
        _project(store, project_id)
        return sorted((v for v in store.all("versions") if v.project_id == project_id), key=lambda v: v.version_number, reverse=True)


def create_session(
    store: FolderStore,
    settings: Settings,
    runner: BuildRunner,
    project_id: uuid.UUID,
    prompt: str | None,
    screenshot_ids: list[uuid.UUID],
    parent_version_id: uuid.UUID | None,
) -> SessionRec:
    prompt, unique_ids, mode = rules.normalize_request(settings, prompt, screenshot_ids, parent_version_id)
    require_ready(settings, runner)
    with store.lock:
        project = _project(store, project_id)
        if unique_ids:
            found = {s.id for s in store.all("screenshots") if s.project_id == project_id and s.id in set(unique_ids)}
            rules.assert_screenshots_in_project(unique_ids, found)
        if parent_version_id is not None:
            parent = store.get("versions", parent_version_id)
            rules.assert_parent_in_project(parent.project_id if parent else None, project_id)
        rules.assert_no_active_session(_active_count(store, project_id))
        assert_transition("created", "queued")
        stamp = now()
        row = SessionRec(
            id=uuid.uuid4(), project_id=project_id, parent_version_id=parent_version_id, input_mode=mode, prompt=prompt,
            screenshot_ids=[str(i) for i in unique_ids], status="queued", attempts=1, retryable=False, created_at=stamp, updated_at=stamp,
        )
        store.put("sessions", row)
        _touch_project(store, project, "generating")
        return row


def retry_session(store: FolderStore, settings: Settings, runner: BuildRunner, session_id: uuid.UUID) -> SessionRec:
    require_ready(settings, runner)
    with store.lock:
        row = _session(store, session_id)
        project = _project(store, row.project_id)
        rules.assert_retryable(row.status, row.retryable, row.attempts, settings.max_session_attempts)
        rules.assert_no_active_session(_active_count(store, row.project_id))
        assert_transition(row.status, "queued")
        row.status = "queued"
        row.attempts += 1
        row.error_code = row.error_message = row.build_log = None
        row.retryable = False
        row.completed_at = None
        row.updated_at = now()
        store.put("sessions", row)
        _touch_project(store, project, "generating")
        return row


def _status_after_failure(store: FolderStore, project_id: uuid.UUID, code: str) -> str:
    has_version = any(v.project_id == project_id for v in store.all("versions"))
    return rules.project_status_after_failure(has_version, code)


class FolderOps:
    """SessionOps for the folder store (see services/generations.py)."""

    def __init__(self, store: FolderStore):
        self._store = store

    def load(self, session_id: uuid.UUID) -> SessionFacts | None:
        with self._store.lock:
            row = self._store.get("sessions", session_id)
            if row is None or row.status != "queued":
                return None
            shots = []
            for i in row.screenshot_ids:
                r = self._store.get("screenshots", uuid.UUID(i))
                shots.append((r.storage_key, r.content_type, r.original_filename) if r and r.project_id == row.project_id else None)
            return SessionFacts(row.project_id, row.prompt, row.parent_version_id, shots)

    def set_status(self, session_id: uuid.UUID, target: str) -> None:
        with self._store.lock:
            row = self._store.get("sessions", session_id)
            if row is None:
                raise InvalidTransition("Session disappeared.")
            assert_transition(row.status, target)
            row.status = target
            if row.started_at is None and target != "queued":
                row.started_at = now()
            row.updated_at = now()
            self._store.put("sessions", row)

    def fail(self, session_id: uuid.UUID, code: str, message: str, retryable: bool, build_log: str | None = None) -> None:
        with self._store.lock:
            row = self._store.get("sessions", session_id)
            if row is None or row.status in ("completed", "failed"):
                return
            assert_transition(row.status, "failed")
            row.status, row.error_code, row.error_message, row.retryable = "failed", code, message[:2000], retryable
            row.build_log = build_log[-20000:] if build_log else None
            row.completed_at = row.updated_at = now()
            self._store.put("sessions", row)
            project = self._store.get("projects", row.project_id)
            if project is not None:
                _touch_project(self._store, project, _status_after_failure(self._store, row.project_id, code))

    def complete(self, session_id: uuid.UUID, facts: SessionFacts, version_id: uuid.UUID, result: EngineResult) -> None:
        with self._store.lock:
            row = _session(self._store, session_id)
            project = _project(self._store, facts.project_id)
            assert_transition(row.status, "completed")  # checked before anything is written
            numbers = [v.version_number for v in self._store.all("versions") if v.project_id == facts.project_id]
            version = VersionRec(
                id=version_id, project_id=facts.project_id, session_id=session_id, parent_version_id=facts.parent_id,
                version_number=rules.next_version_number(max(numbers) if numbers else None), summary=result.summary,
                created_at=now(), model_id=result.model_id, repaired=result.repaired, uncertainties=result.uncertainties,
                files=rules.version_manifest(assemble_workspace_files(result.files)),
            )
            self._store.put("versions", version)
            try:
                row.status, row.completed_at, row.model_id, row.build_log = "completed", now(), result.model_id, result.build_log[-20000:]
                row.updated_at = now()
                self._store.put("sessions", row)
                _touch_project(self._store, project, "ready")
            except Exception:
                self._store.delete("versions", version_id)
                raise


def recover_interrupted(factory: Callable[[], FolderStore]) -> int:
    """After a restart, sessions left active can no longer progress. If their version record was already written (a crash
    between the two writes of `complete`) the session is completed; otherwise it is failed as retryable. Returns the failed count."""
    store = factory()
    failed = 0
    with store.lock:
        versions = {v.session_id: v for v in store.all("versions")}
        for row in store.all("sessions"):
            if row.status not in ACTIVE_VALUES:
                continue
            project = store.get("projects", row.project_id)
            if row.id in versions:
                row.status, row.completed_at, row.model_id = "completed", row.completed_at or now(), versions[row.id].model_id
                row.updated_at = now()
                store.put("sessions", row)
                if project is not None:
                    _touch_project(store, project, "ready")
                continue
            row.status, row.error_code, row.retryable = "failed", "interrupted", True
            row.error_message = "The backend restarted while this generation was running."
            row.completed_at = row.updated_at = now()
            store.put("sessions", row)
            if project is not None:
                _touch_project(store, project, _status_after_failure(store, row.project_id, "interrupted"))
            failed += 1
    return failed
