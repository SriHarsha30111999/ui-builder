"""The generation pipeline proper: model call -> parse -> validate -> sandboxed build -> one bounded repair.

Pure with respect to the database: it takes inputs and a workspace directory and returns a result, so it can be tested
without MySQL. Persistence and status updates are done by services/generations.py through `on_stage`.
"""
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from .artifact import ArtifactError, assemble_workspace_files, parse_model_output, validate_files
from .prompts import SYSTEM, build_repair_text, build_user_text
from .providers import GenerationProvider, ImageInput, ProviderError
from .sandbox import BuildRunner

logger = logging.getLogger("app.generation")
MAX_CONTEXT_CHARS = 150_000


@dataclass
class EngineInput:
    prompt: str | None
    images: list[ImageInput]
    existing_files: dict[str, str] | None = None  # refinement: source of the parent version


@dataclass
class EngineResult:
    ok: bool
    model_id: str
    summary: str = ""
    uncertainties: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # model-written files only
    build_log: str = ""
    repaired: bool = False
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False


def write_workspace(workspace: Path, model_files: dict[str, str]) -> None:
    """Materialise scaffold + model files. Paths were validated (no traversal); re-checked here defensively."""
    if workspace.exists():
        shutil.rmtree(workspace)
    root = workspace.resolve()
    for rel, content in assemble_workspace_files(model_files).items():
        target = (root / rel).resolve()
        if root not in target.parents:
            raise ArtifactError("invalid_artifact", "A file path escaped the workspace.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def run_generation(
    inp: EngineInput,
    *,
    settings: Settings,
    provider: GenerationProvider,
    runner: BuildRunner,
    workspace: Path,
    on_stage: Callable[[str], None],
) -> EngineResult:
    model = settings.ai_vision_model if inp.images else settings.ai_code_model
    res = EngineResult(ok=False, model_id=model)

    ctx = sum(len(c) for c in (inp.existing_files or {}).values())
    if ctx > MAX_CONTEXT_CHARS:
        res.error_code, res.error_message = "context_too_large", "The existing UI is too large to refine in one step."
        return res

    image_bytes = sum(len(i.data) for i in inp.images)
    if image_bytes > settings.max_total_image_bytes:
        res.error_code = "screenshots_too_large"
        res.error_message = f"The selected screenshots total more than {settings.max_total_image_bytes // (1024 * 1024)} MB. Remove some or use smaller images."
        return res

    user_text = build_user_text(
        prompt=inp.prompt, image_count=len(inp.images), existing_files=inp.existing_files,
        image_names=[i.filename for i in inp.images] or None,
    )
    on_stage("analyzing" if inp.images else "generating")

    def call(text: str, images: list[ImageInput]) -> dict[str, str] | tuple[str, str]:
        """Returns validated files, or (code, message) when the output is unusable."""
        raw = provider.complete(model=model, system=SYSTEM, user_text=text, images=images)
        try:
            parsed = parse_model_output(raw)
            # Refinement: files the model leaves out are kept from the parent version rather than silently dropped.
            merged = {**(inp.existing_files or {}), **parsed.files}
            validate_files(merged, settings.max_generated_files, settings.max_generated_file_bytes)
        except ArtifactError as exc:
            return (exc.code, exc.message)
        res.summary, res.uncertainties = parsed.summary or res.summary, parsed.uncertainties or res.uncertainties
        return merged

    try:
        out = call(user_text, inp.images)
        repairs_left = settings.max_repair_attempts
        current_files: dict[str, str] | None = None
        problem: tuple[str, str] | None = None

        while True:
            if isinstance(out, tuple):
                problem = out
            else:
                current_files = out
                write_workspace(workspace, current_files)
                on_stage("building")
                build = runner.build(workspace)
                res.build_log = build.log
                if build.ok:
                    res.ok, res.files = True, current_files
                    return res
                if build.error_code in ("sandbox_unavailable", "build_timeout"):
                    res.error_code = build.error_code
                    res.error_message = build.log if build.error_code == "sandbox_unavailable" else "The build timed out."
                    res.retryable = build.error_code == "build_timeout"
                    return res
                problem = (build.error_code or "build_failed", "The build failed:\n" + build.log)

            if repairs_left <= 0:
                res.error_code = problem[0]
                res.error_message = (
                    "The generated code failed the build." if problem[0] in ("build_failed", "invalid_build_output")
                    else "The model produced output that did not pass validation: " + problem[1]
                )
                res.retryable = problem[0] == "malformed_model_output"
                return res
            repairs_left -= 1
            res.repaired = True
            on_stage("repairing")
            repair_files = current_files or {}
            out = call(build_repair_text(repair_files, problem[1][:8000]), [])
    except ProviderError as exc:
        res.error_code, res.error_message, res.retryable = exc.code, exc.message, exc.retryable
        return res
    except ArtifactError as exc:
        res.error_code, res.error_message = exc.code, exc.message
        return res
