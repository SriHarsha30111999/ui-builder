"""Project-creation rules that need no storage (shared by the MySQL and folder backends)."""
import uuid
from dataclasses import dataclass

from ..errors import AppError
from ..schemas import ProjectCreate
from .paths import BasePathError, validate_base_path


@dataclass
class NewProject:
    name: str
    type: str
    initial_prompt: str
    master_id: uuid.UUID | None
    base_path: str | None


def normalize_project(data: ProjectCreate) -> NewProject:
    name = data.name.strip()
    if not name:
        raise AppError(422, "invalid_request", "The request is not valid.", {"name": "Enter a project name."})
    if len(name) < 3:
        raise AppError(422, "invalid_request", "The request is not valid.", {"name": "Project name must be at least 3 characters."})
    if len(name) > 60:
        raise AppError(422, "invalid_request", "The request is not valid.", {"name": "Project name must be 60 characters or fewer."})
    prompt = data.initial_prompt.strip()

    if data.type == "master":
        if data.master_id is not None or (data.base_path is not None and data.base_path.strip()):
            raise AppError(
                422,
                "invalid_request",
                "A Master Project cannot have a parent Master or a base path.",
                {"masterId": "Not allowed for a Master Project."} if data.master_id else {"basePath": "Not allowed for a Master Project."},
            )
        return NewProject(name, "master", prompt, None, None)

    if data.master_id is None:
        raise AppError(422, "invalid_request", "The request is not valid.", {"masterId": "Select the Master Project this feature belongs to."})
    try:
        base_path = validate_base_path(data.base_path or "")
    except BasePathError as exc:
        raise AppError(422, "invalid_base_path", str(exc), {"basePath": str(exc)}) from exc
    return NewProject(name, "feature", prompt, data.master_id, base_path)


def assert_master(master_type: str | None) -> None:
    """`master_type` is None when the referenced project does not exist."""
    if master_type is None:
        raise AppError(422, "master_not_found", "The selected Master Project does not exist.", {"masterId": "The selected Master Project does not exist."})
    if master_type != "master":
        raise AppError(422, "master_not_a_master", "A Feature cannot be attached to another Feature.", {"masterId": "The selected project is a Feature, not a Master Project."})


def base_path_conflict(base_path: str | None) -> AppError:
    msg = f'Another feature under this Master already uses "{base_path}".'
    return AppError(409, "base_path_conflict", msg, {"basePath": msg})
