import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

ProjectType = Literal["master", "feature"]
ProjectStatus = Literal["draft", "generating", "build_failed", "ready", "published"]


class ApiModel(BaseModel):
    """camelCase on the wire, to match the frontend's existing field names."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class ProjectCreate(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    name: str = Field(max_length=200)  # precise 3-60 rule (after trimming) is applied in the service
    type: ProjectType
    initial_prompt: str = Field(default="", max_length=10_000)
    master_id: uuid.UUID | None = None
    base_path: str | None = Field(default=None, max_length=500)


class ScreenshotOut(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    created_at: datetime
    url: str


class ProjectOut(ApiModel):
    id: uuid.UUID
    name: str
    type: ProjectType
    master_id: uuid.UUID | None
    master_name: str | None
    base_path: str | None
    initial_prompt: str
    status: ProjectStatus
    screenshot_count: int
    created_at: datetime
    updated_at: datetime


class ProjectDetailOut(ProjectOut):
    screenshots: list[ScreenshotOut]


# ---- Phase 3: generation ----

class GenerationCreate(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    prompt: str | None = Field(default=None, max_length=20_000)  # precise limit applied in the service
    screenshot_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    parent_version_id: uuid.UUID | None = None  # set to refine an existing version


class GenerationOut(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    parent_version_id: uuid.UUID | None
    version_id: uuid.UUID | None = None  # filled by the route (not an ORM attribute)
    input_mode: Literal["prompt_only", "screenshot_only", "prompt_and_screenshot"]
    prompt: str | None
    screenshot_ids: list[uuid.UUID]
    status: Literal["created", "queued", "analyzing", "generating", "building", "repairing", "completed", "failed"]
    attempts: int
    error_code: str | None
    error_message: str | None
    retryable: bool
    model_id: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class VersionFileOut(ApiModel):
    path: str
    size: int


class VersionOut(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    session_id: uuid.UUID
    parent_version_id: uuid.UUID | None
    version_number: int
    summary: str
    uncertainties: list[str]
    files: list[VersionFileOut]
    model_id: str
    repaired: bool
    created_at: datetime
    preview_url: str = ""  # filled by the route


class BuildLogOut(ApiModel):
    log: str


class GenerationReadinessOut(ApiModel):
    ai_configured: bool
    code_model: str
    vision_model: str
    build_runner: str
    build_available: bool
    build_message: str
    ready: bool
    max_session_attempts: int = 3
