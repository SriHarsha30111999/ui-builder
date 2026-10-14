"""Feature base-path rules. Mirrors src/validation.ts in the frontend; this module is the authority."""

import re

# Routes owned by the Master shell. Compared against the normalized path (exact match or as a prefix).
# "/" is rejected separately (it normalizes to an empty string).
RESERVED_BASE_PATHS = ("/login", "/logout", "/dashboard", "/auth", "/api", "/404")
MAX_BASE_PATH_LENGTH = 100

_SEGMENT = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_FORBIDDEN_CHARS = re.compile(r"[\s?#:*\\%]")


class BasePathError(ValueError):
    """Raised with a user-facing message when a base path is not acceptable."""


def normalize_base_path(raw: str) -> str:
    """Trim, lowercase, collapse repeated slashes, ensure one leading slash and no trailing slash.

    Returns "" when nothing usable remains (empty input or "/").
    """
    segments = [s for s in raw.strip().lower().split("/") if s]
    return "/" + "/".join(segments) if segments else ""


def validate_base_path(raw: str) -> str:
    """Return the canonical base path or raise BasePathError.

    Does not check uniqueness; that is enforced per Master by the database (uq_projects_master_base_path).
    """
    if not raw or not raw.strip():
        raise BasePathError("Enter the base path for this feature, e.g. /accounts.")
    if _FORBIDDEN_CHARS.search(raw.strip()):  # surrounding whitespace is trimmed; interior whitespace is not allowed
        raise BasePathError("Base path cannot contain spaces or the characters ? # : * \\ %")
    normalized = normalize_base_path(raw)
    if not normalized:
        raise BasePathError("Base path must name a route below the root, e.g. /accounts.")
    if len(normalized) > MAX_BASE_PATH_LENGTH:
        raise BasePathError(f"Base path must be {MAX_BASE_PATH_LENGTH} characters or fewer.")
    if not all(_SEGMENT.match(seg) for seg in normalized[1:].split("/")):
        raise BasePathError('Use letters, numbers, "-" and "_" in each path segment (no empty or "." segments).')
    for reserved in RESERVED_BASE_PATHS:
        if normalized == reserved or normalized.startswith(reserved + "/"):
            raise BasePathError(f'"{normalized}" is reserved for the Master application. Choose another path.')
    return normalized
