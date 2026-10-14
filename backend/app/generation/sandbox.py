"""Isolated build execution.

Generated code is untrusted. It is NEVER built or run in the FastAPI process or directly on the host. The only
supported runner is a disposable Docker container with no network, a read-only root filesystem, dropped capabilities,
no privileges and CPU/memory/process/time limits. If Docker (or the sandbox image) is not available the runner reports
"unavailable" and builds are refused; there is deliberately no unsandboxed fallback.
"""
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import Settings

LOG_LIMIT = 20_000
# Fixed command: contains no user-controlled text.
# --configLoader runner: the default loader writes a temp file under the nearest node_modules (/node_modules -> read-only image layer),
# which fails with EROFS under --read-only. Verified from the Vite 8 source and a host build; not yet run in the container.
CONTAINER_COMMAND = ["sh", "-c", "tsc --noEmit -p /work/tsconfig.json && vite build --config /work/vite.config.ts --configLoader runner"]


@dataclass
class BuildResult:
    ok: bool
    log: str
    error_code: str | None = None  # build_failed | build_timeout | sandbox_unavailable | invalid_build_output


class BuildRunner(Protocol):
    name: str

    def availability(self) -> tuple[bool, str]: ...

    def build(self, workspace: Path) -> BuildResult:
        """Build `workspace`; on success the output is in `workspace/dist`."""
        ...


class DisabledRunner:
    name = "disabled"

    def __init__(self, reason: str = "No build sandbox is configured (BUILD_RUNNER=disabled)."):
        self._reason = reason

    def availability(self) -> tuple[bool, str]:
        return False, self._reason

    def build(self, workspace: Path) -> BuildResult:
        return BuildResult(False, self._reason, "sandbox_unavailable")


def docker_command(settings: Settings, workspace: Path, container_name: str) -> list[str]:
    """argv for `docker run` (never a shell string). Pure function so the isolation flags are unit-testable."""
    return [
        "docker", "run", "--rm", "--name", container_name,
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256",
        "--memory", settings.build_memory, "--memory-swap", settings.build_memory,
        "--cpus", settings.build_cpus,
        "--user", "1000:1000",
        "-e", "HOME=/tmp", "-e", "CI=1",
        "-v", f"{workspace}:/work:rw",
        "-w", "/work",
        settings.build_image,
        *CONTAINER_COMMAND,
    ]


class DockerRunner:
    name = "docker"

    def __init__(self, settings: Settings):
        self._s = settings

    def availability(self) -> tuple[bool, str]:
        if shutil.which("docker") is None:
            return False, "Docker is not installed or not on PATH, so generated code cannot be built in isolation."
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", self._s.build_image], capture_output=True, timeout=15, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return False, "Docker did not respond (is the daemon running?)."
        if r.returncode != 0:
            return False, f"The sandbox image '{self._s.build_image}' is not built. See docs/PHASE3.md."
        return True, "ok"

    def build(self, workspace: Path) -> BuildResult:
        ok, reason = self.availability()
        if not ok:
            return BuildResult(False, reason, "sandbox_unavailable")
        name = f"sb-{uuid.uuid4().hex[:12]}"
        cmd = docker_command(self._s, workspace.resolve(), name)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=self._s.build_timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(["docker", "kill", name], capture_output=True, timeout=15, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass  # the build is reported as timed out either way
            return BuildResult(False, f"The build exceeded {self._s.build_timeout_seconds}s and was stopped.", "build_timeout")
        except OSError:
            return BuildResult(False, "Docker could not be started.", "sandbox_unavailable")
        log = _clean_log(proc.stdout + proc.stderr, workspace)
        if proc.returncode != 0:
            return BuildResult(False, log, "build_failed")
        problem = check_workspace_tree(workspace) or check_dist(workspace / "dist", self._s.build_max_dist_bytes)
        if problem:
            return BuildResult(False, log + "\n" + problem, "invalid_build_output")
        return BuildResult(True, log)


def _clean_log(raw: bytes, workspace: Path) -> str:
    text = raw.decode("utf-8", errors="replace").replace(str(workspace.resolve()), "<workspace>").replace("/work/", "")
    return text[-LOG_LIMIT:]


def check_dist(dist: Path, max_bytes: int) -> str | None:
    """Output must be regular files only (no symlinks), contain index.html, and stay within the size limit."""
    if not dist.is_dir() or not (dist / "index.html").is_file():
        return "The build produced no index.html."
    total = 0
    for p in dist.rglob("*"):
        if p.is_symlink():
            return "The build output contains a symbolic link and was rejected."
        if p.is_file():
            total += p.stat().st_size
    if total > max_bytes:
        return "The build output is larger than the allowed size."
    return None


def check_workspace_tree(workspace: Path, max_entries: int = 20_000) -> str | None:
    """The container wrote into this host directory, which is then kept and served: reject links and runaway trees anywhere in it."""
    count = 0
    for root, dirs, files in os.walk(workspace, followlinks=False):
        for name in dirs + files:
            count += 1
            if count > max_entries:
                return "The build workspace contains too many files and was rejected."
            if os.path.islink(os.path.join(root, name)):
                return "The build workspace contains a symbolic link and was rejected."
    return None


def host_commands(node: str, toolchain: Path) -> list[list[str]]:
    """argv lists (never a shell string) for the host build: type-check, then bundle. Pure so it is unit-testable."""
    return [
        [node, str(toolchain / "typescript" / "bin" / "tsc"), "--noEmit", "-p", "tsconfig.json"],
        [node, str(toolchain / "vite" / "bin" / "vite.js"), "build", "--config", "vite.config.ts", "--configLoader", "runner"],
    ]


def host_env() -> dict[str, str]:
    """Minimal environment for the build. Deliberately NOT os.environ: the AI key and other secrets must never reach it."""
    keep = ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"CI": "1", "NODE_ENV": "production", "npm_config_offline": "true"})
    return env


class HostRunner:
    """UNSANDBOXED build on this machine, explicitly opted in with BUILD_RUNNER=host.

    The build only type-checks and bundles (tsc, vite build); it never starts the generated code, and the result is served
    to the Preview as static files inside a sandboxed iframe. It still runs build tools over model-written source with the
    user's own OS permissions, which Docker would prevent. Mitigations: validated sources only, platform-owned configs, a
    scrubbed environment, a temp workspace, a hard timeout with process-tree kill, and the same output checks as Docker.
    """

    name = "host"

    def __init__(self, settings: Settings):
        self._s = settings

    def _toolchain(self) -> Path:
        return self._s.resolved_build_toolchain_dir

    def availability(self) -> tuple[bool, str]:
        if shutil.which("node") is None:
            return False, "Node.js is not on PATH, so the host build cannot run."
        tc = self._toolchain()
        missing = [p for p in ("typescript/bin/tsc", "vite/bin/vite.js", "react", "react-dom", "@vitejs/plugin-react") if not (tc / p).exists()]
        if missing:
            return False, f"The build toolchain folder {tc} lacks: {', '.join(missing)}."
        return True, "ok (unsandboxed host build)"

    def _link_toolchain(self, workspace: Path) -> Path:
        """Node resolves packages from an ancestor node_modules. A junction next to the workspace root (never inside it) provides it."""
        link = workspace.resolve().parent / "node_modules"
        if not link.exists():
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self._toolchain())], capture_output=True, timeout=15, check=False)
            if r.returncode != 0 and not link.exists():
                raise OSError("Could not link the build toolchain.")
        return link

    def build(self, workspace: Path) -> BuildResult:
        ok, reason = self.availability()
        if not ok:
            return BuildResult(False, reason, "sandbox_unavailable")
        try:
            self._link_toolchain(workspace)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return BuildResult(False, str(exc), "sandbox_unavailable")
        node = shutil.which("node")
        deadline = self._s.build_timeout_seconds
        logs: list[str] = []
        for cmd in host_commands(node, self._toolchain()):
            try:
                proc = subprocess.Popen(
                    cmd, cwd=workspace, env=host_env(), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
            except OSError:
                return BuildResult(False, "The build tool could not be started.", "sandbox_unavailable")
            try:
                out, _ = proc.communicate(timeout=deadline)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15, check=False)
                proc.kill()
                return BuildResult(False, f"The build exceeded {deadline}s and was stopped.", "build_timeout")
            logs.append(_clean_log(out, workspace))
            if proc.returncode != 0:
                return BuildResult(False, "\n".join(logs)[-LOG_LIMIT:], "build_failed")
        log = "\n".join(logs)[-LOG_LIMIT:]
        problem = check_workspace_tree(workspace) or check_dist(workspace / "dist", self._s.build_max_dist_bytes)
        if problem:
            return BuildResult(False, log + "\n" + problem, "invalid_build_output")
        return BuildResult(True, log)


def make_runner(settings: Settings) -> BuildRunner:
    if settings.build_runner == "docker":
        return DockerRunner(settings)
    if settings.build_runner == "host":
        return HostRunner(settings)
    return DisabledRunner()
