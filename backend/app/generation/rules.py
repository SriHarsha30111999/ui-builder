"""Business rules of the generation service as pure functions (no database, no I/O).

services/generations.py gathers the facts from MySQL (row counts, owned screenshot ids, a version's project id, ...) and
calls these functions to decide. Keeping the decisions here makes them unit-testable without a database; the locking
and persistence that make them race-free stay in the service and still need MySQL to be exercised.
"""
import uuid
from collections.abc import Iterable

from ..config import Settings
from ..errors import AppError
from .state import input_mode

BUILD_ERROR_CODES = ("build_failed", "build_timeout", "invalid_build_output")


def normalize_request(
    settings: Settings, prompt: str | None, screenshot_ids: Iterable[uuid.UUID], parent_version_id: uuid.UUID | None
) -> tuple[str | None, list[uuid.UUID], str]:
    """Validate a new generation request. Returns (prompt or None, de-duplicated screenshot ids, input mode)."""
    prompt = (prompt or "").strip() or None
    if prompt and len(prompt) > settings.max_prompt_chars:
        raise AppError(422, "prompt_too_long", f"The prompt is longer than {settings.max_prompt_chars} characters.", {"prompt": "Too long."})
    unique_ids = list(dict.fromkeys(screenshot_ids))
    if len(unique_ids) > settings.max_screenshots_per_prompt:
        raise AppError(422, "too_many_screenshots", "Too many screenshots selected.", {"screenshotIds": "Too many."})
    if not prompt and not unique_ids:
        raise AppError(422, "no_input", "Enter a prompt, or select at least one screenshot.", {"prompt": "A prompt or a screenshot is required."})
    if parent_version_id is not None and not prompt:
        raise AppError(422, "refinement_needs_prompt", "Describe the change you want to make.", {"prompt": "Required for a refinement."})
    return prompt, unique_ids, input_mode(prompt, len(unique_ids))


def assert_screenshots_in_project(requested: list[uuid.UUID], owned_by_project: set[uuid.UUID]) -> None:
    """`owned_by_project` is the subset of `requested` that the database found under this project."""
    if set(requested) != set(owned_by_project):
        raise AppError(422, "screenshot_not_in_project", "A selected screenshot does not belong to this project.", {"screenshotIds": "Invalid."})


def assert_parent_in_project(parent_project_id: uuid.UUID | None, project_id: uuid.UUID) -> None:
    """`parent_project_id` is None when the version does not exist. A Generated Project version can only be refined within the same Application project."""
    if parent_project_id is None or parent_project_id != project_id:
        raise AppError(404, "version_not_found", "The version to refine was not found in this project.")


def assert_no_active_session(active_count: int) -> None:
    if active_count > 0:
        raise AppError(409, "generation_in_progress", "A generation is already running for this project.")


def count_unsent(all_screenshot_ids: list[str], sent_by_sessions: list[list[str]]) -> int:
    """Screenshots of a project that no generation has used yet (the ones waiting for the next message)."""
    sent = {i for ids in sent_by_sessions for i in ids}
    return sum(1 for i in all_screenshot_ids if i not in sent)


def assert_screenshot_unused(screenshot_id: str, sent_by_sessions: list[list[str]]) -> None:
    """A screenshot that a generation used is part of that conversation turn's history and must stay."""
    if any(screenshot_id in ids for ids in sent_by_sessions):
        raise AppError(409, "screenshot_in_use", "This screenshot was sent with a generation and is kept as part of the project history.")


def assert_project_deletable(active_sessions: int, dependent_features: int) -> None:
    if active_sessions > 0:
        raise AppError(409, "generation_in_progress", "A generation is running for this project. Wait for it to finish before deleting.")
    if dependent_features > 0:
        raise AppError(409, "project_has_features", "This Master Project still has Feature Projects. Delete those first.")


def assert_retryable(status: str, retryable: bool, attempts: int, max_attempts: int) -> None:
    if status != "failed" or not retryable:
        raise AppError(409, "not_retryable", "Only failed generations with a temporary cause can be retried. Edit the request and generate again.")
    if attempts >= max_attempts:
        raise AppError(409, "retry_limit_reached", "The retry limit for this generation was reached. Generate again to start a new one.")


def next_version_number(current_max: int | None) -> int:
    """Version numbers are per project, start at 1 and only ever grow (gaps from failed attempts do not exist: only built versions get one)."""
    return (current_max or 0) + 1


def project_status_after_failure(has_version: bool, error_code: str) -> str:
    if has_version:
        return "ready"  # the previous successful version is untouched and still previewable
    return "build_failed" if error_code in BUILD_ERROR_CODES else "draft"


def version_manifest(files: dict[str, str]) -> list[dict]:
    """Stored file list of a version: path and UTF-8 size, sorted by path."""
    return [{"path": p, "size": len(c.encode("utf-8"))} for p, c in sorted(files.items())]
