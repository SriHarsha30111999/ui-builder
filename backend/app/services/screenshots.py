import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import AppError
from ..generation import rules
from ..models import GenerationSession, Project, Screenshot
from .storage import ScreenshotStorage, StorageError, detect_image_type, sanitize_display_name

logger = logging.getLogger("app")


@dataclass
class IncomingFile:
    filename: str | None
    data: bytes


def validate_incoming(file: IncomingFile, max_bytes: int) -> str:
    """Return the detected content type or raise AppError."""
    shown = sanitize_display_name(file.filename)
    if len(file.data) == 0:
        raise AppError(422, "empty_file", f'"{shown}" is empty.')
    if len(file.data) > max_bytes:
        raise AppError(413, "file_too_large", f'"{shown}" is larger than {max_bytes // (1024 * 1024)} MB.')
    content_type = detect_image_type(file.data[:16])
    if content_type is None:
        raise AppError(415, "unsupported_file_type", f'"{shown}" is not a supported image (use PNG, JPEG, WebP or GIF).')
    return content_type


def add_screenshots(
    session: Session,
    storage: ScreenshotStorage,
    project_id: uuid.UUID,
    files: list[IncomingFile],
    max_bytes: int,
    max_per_project: int,
) -> list[Screenshot]:
    """All-or-nothing: either every file is stored and recorded, or none is."""
    if not files:
        raise AppError(422, "no_files", "Attach at least one image.")

    # Lock the project row so the per-project count check cannot be raced by a parallel upload.
    project = session.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if project is None:
        raise AppError(404, "project_not_found", "Project not found.")

    types = [validate_incoming(f, max_bytes) for f in files]

    # Locking read (not a snapshot read): under MySQL's default REPEATABLE READ a plain COUNT could miss rows
    # committed by an upload that finished just before we acquired the project lock.
    # Only screenshots not yet sent with a generation count: sent ones belong to earlier messages in the conversation.
    all_ids = [str(i) for i in session.scalars(select(Screenshot.id).where(Screenshot.project_id == project_id).with_for_update()).all()]
    sent = list(session.scalars(select(GenerationSession.screenshot_ids).where(GenerationSession.project_id == project_id)))
    existing = rules.count_unsent(all_ids, sent)
    if existing + len(files) > max_per_project:
        raise AppError(
            409,
            "too_many_screenshots",
            f"A message can have at most {max_per_project} screenshots ({existing} already waiting).",
        )

    written: list[str] = []
    rows: list[Screenshot] = []
    try:
        for incoming, content_type in zip(files, types, strict=True):
            key = storage.new_key(content_type)
            storage.write(key, incoming.data)
            written.append(key)
            rows.append(
                Screenshot(
                    project_id=project_id,
                    storage_key=key,
                    original_filename=sanitize_display_name(incoming.filename),
                    content_type=content_type,
                    size_bytes=len(incoming.data),
                )
            )
        session.add_all(rows)
        session.commit()
        for row in rows:
            session.refresh(row)  # server-generated created_at (MySQL has no RETURNING)
    except StorageError as exc:
        session.rollback()
        for key in written:
            storage.remove_quietly(key)
        raise AppError(500, "storage_failed", "The upload could not be saved. Nothing was stored.") from exc
    except Exception:
        session.rollback()
        for key in written:
            storage.remove_quietly(key)
        raise
    return rows


def get_screenshot(session: Session, screenshot_id: uuid.UUID) -> Screenshot:
    shot = session.get(Screenshot, screenshot_id)
    if shot is None:
        raise AppError(404, "screenshot_not_found", "Screenshot not found.")
    return shot


def delete_screenshot(session: Session, storage: ScreenshotStorage, screenshot_id: uuid.UUID) -> None:
    shot = get_screenshot(session, screenshot_id)
    rules.assert_screenshot_unused(
        str(shot.id), list(session.scalars(select(GenerationSession.screenshot_ids).where(GenerationSession.project_id == shot.project_id)))
    )
    key = shot.storage_key
    session.delete(shot)
    session.commit()
    # Remove the file only after the row is gone, so no row ever points at a missing file.
    storage.remove_quietly(key)
