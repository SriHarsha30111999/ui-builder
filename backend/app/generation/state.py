"""Generation-session lifecycle. Only the server moves a session between states, and only along these edges."""
from enum import Enum


class Status(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    ANALYZING = "analyzing"  # screenshot modes: the vision model call (analysis + generation happen in one call)
    GENERATING = "generating"  # prompt-only mode: the code model call
    BUILDING = "building"
    REPAIRING = "repairing"  # one bounded repair model call
    COMPLETED = "completed"
    FAILED = "failed"


ALL_STATUSES = tuple(s.value for s in Status)
ACTIVE = frozenset({Status.CREATED, Status.QUEUED, Status.ANALYZING, Status.GENERATING, Status.BUILDING, Status.REPAIRING})
ACTIVE_VALUES = tuple(s.value for s in ACTIVE)

TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.CREATED: frozenset({Status.QUEUED, Status.FAILED}),
    Status.QUEUED: frozenset({Status.ANALYZING, Status.GENERATING, Status.FAILED}),
    Status.ANALYZING: frozenset({Status.BUILDING, Status.REPAIRING, Status.FAILED}),
    Status.GENERATING: frozenset({Status.BUILDING, Status.REPAIRING, Status.FAILED}),
    Status.BUILDING: frozenset({Status.REPAIRING, Status.COMPLETED, Status.FAILED}),
    Status.REPAIRING: frozenset({Status.BUILDING, Status.FAILED}),
    Status.COMPLETED: frozenset(),
    Status.FAILED: frozenset({Status.QUEUED}),  # retry of a retryable failure
}

INPUT_MODES = ("prompt_only", "screenshot_only", "prompt_and_screenshot")


class InvalidTransition(Exception):
    pass


def can_transition(current: str, target: str) -> bool:
    try:
        return Status(target) in TRANSITIONS[Status(current)]
    except (ValueError, KeyError):
        return False


def assert_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(f"Cannot move a generation from '{current}' to '{target}'.")


def input_mode(prompt: str | None, screenshot_count: int) -> str:
    has_prompt = bool(prompt and prompt.strip())
    if has_prompt and screenshot_count:
        return "prompt_and_screenshot"
    if has_prompt:
        return "prompt_only"
    if screenshot_count:
        return "screenshot_only"
    raise ValueError("A prompt or at least one screenshot is required.")
