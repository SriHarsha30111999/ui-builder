"""Folder-backed records: one JSON file per project / screenshot / generation session / version under a data folder.

Opt-in alternative to MySQL (PERSISTENCE=folder); the MySQL path is unchanged. Generated source code is not stored here: it
stays in the generated-storage folder, and a version record only points at it by id.

Guarantees and limits (see docs/PHASE3.md "Folder store"):
- Writes are atomic per file (temp file + os.replace). A single in-process RLock serialises every read-modify-write, which is
  what the project row lock does in MySQL. An exclusive OS lock on `.lock` refuses a second backend process on the same folder.
- There are no foreign keys, CHECK or UNIQUE constraints: the service rules (and `rules.py`) are the only enforcement.
- Every listing reads the whole folder; fine for a local tool, not for thousands of projects.
"""
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from ..errors import AppError

logger = logging.getLogger("app")


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ProjectRec:
    id: uuid.UUID
    name: str
    type: str
    master_id: uuid.UUID | None
    master_type: str | None
    base_path: str | None
    initial_prompt: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass
class ScreenshotRec:
    id: uuid.UUID
    project_id: uuid.UUID
    storage_key: str
    original_filename: str
    content_type: str
    size_bytes: int
    created_at: datetime


@dataclass
class SessionRec:
    id: uuid.UUID
    project_id: uuid.UUID
    parent_version_id: uuid.UUID | None
    input_mode: str
    prompt: str | None
    screenshot_ids: list[str]
    status: str
    attempts: int
    retryable: bool
    created_at: datetime
    updated_at: datetime
    error_code: str | None = None
    error_message: str | None = None
    build_log: str | None = None
    model_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class VersionRec:
    id: uuid.UUID
    project_id: uuid.UUID
    session_id: uuid.UUID
    parent_version_id: uuid.UUID | None
    version_number: int
    summary: str
    created_at: datetime
    model_id: str
    repaired: bool = False
    uncertainties: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)


_KINDS = {"projects": ProjectRec, "screenshots": ScreenshotRec, "sessions": SessionRec, "versions": VersionRec}
T = TypeVar("T")


def _encode(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def _decode(cls: type, data: dict) -> Any:
    kwargs = {}
    for f in fields(cls):
        v = data.get(f.name)
        ann = str(f.type)
        if v is not None and "UUID" in ann and "list" not in ann:
            v = uuid.UUID(v)
        elif v is not None and "datetime" in ann:
            v = datetime.fromisoformat(v)
        kwargs[f.name] = v
    return cls(**{k: v for k, v in kwargs.items() if k in data})


class FolderStore:
    """Handle used wherever the MySQL code takes a SQLAlchemy Session (commit/rollback/close are no-ops here)."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock = threading.RLock()
        self._lock_file = None

    # -- lifecycle ---------------------------------------------------------------------------------------------
    def open(self) -> None:
        """Create the folders and take the exclusive process lock. Raises RuntimeError if another process holds it."""
        for kind in _KINDS:
            (self.root / kind).mkdir(parents=True, exist_ok=True)
        if self._lock_file is not None:
            return
        handle = open(self.root / ".lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(f"Another backend process is already using the data folder {self.root}.") from exc
        self._lock_file = handle

    def shutdown(self) -> None:
        if self._lock_file is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def check_writable(self) -> None:
        probe = self.root / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()

    # -- Session-compatible no-ops -------------------------------------------------------------------------------
    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None

    # -- records -------------------------------------------------------------------------------------------------
    def _path(self, kind: str, rec_id: uuid.UUID) -> Path:
        return self.root / kind / f"{rec_id}.json"  # rec_id is a typed UUID, never client text

    # Reads take the same lock as writes: on Windows a file cannot be replaced while another thread has it open.
    def get(self, kind: str, rec_id: uuid.UUID) -> Any | None:
        with self.lock:
            path = self._path(kind, rec_id)
            if not path.is_file():
                return None
            return self._load(kind, path)

    def all(self, kind: str) -> list[Any]:
        with self.lock:
            return [self._load(kind, p) for p in sorted((self.root / kind).glob("*.json"))]

    def put(self, kind: str, rec: Any) -> None:
        with self.lock:
            self._put(kind, rec)

    def _put(self, kind: str, rec: Any) -> None:
        path = self._path(kind, rec.id)
        tmp = path.with_suffix(".tmp")
        data = json.dumps({k: _encode(v) for k, v in asdict(rec).items()}, ensure_ascii=False, indent=1)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(8):  # sync clients (OneDrive) and scanners can hold a file briefly on Windows
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.02 * (attempt + 1))

    def delete(self, kind: str, rec_id: uuid.UUID) -> None:
        with self.lock:
            self._path(kind, rec_id).unlink(missing_ok=True)

    def _load(self, kind: str, path: Path) -> Any:
        try:
            return _decode(_KINDS[kind], json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError) as exc:  # never skipped silently: a damaged record must be noticed
            logger.error("Unreadable record in the data folder: %s/%s", kind, path.name)
            raise AppError(500, "store_corrupt", "A stored record could not be read. Check the data folder.") from exc
