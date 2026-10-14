"""Filesystem layout for generated versions:  <root>/<project_id>/<version_id>/{src/, dist/, package.json, ...}

Every path is built from UUIDs (typed, never client text) plus a relative file path that is checked to stay inside
the version directory. Scratch workspaces live under <root>/_work and are removed after each session.
"""
import shutil
import uuid
from pathlib import Path


class GeneratedStorageError(Exception):
    pass


def _inside(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise GeneratedStorageError("Path escapes the storage directory.")
    return resolved


class GeneratedStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def ensure_ready(self) -> None:
        """Create the root and clear scratch workspaces left behind by a crashed run (no session survives a restart)."""
        work = self.root / "_work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)

    def work_dir(self, session_id: uuid.UUID) -> Path:
        return _inside(self.root, self.root / "_work" / str(session_id))

    def version_dir(self, project_id: uuid.UUID, version_id: uuid.UUID) -> Path:
        return _inside(self.root, self.root / str(project_id) / str(version_id))

    def promote(self, workspace: Path, project_id: uuid.UUID, version_id: uuid.UUID) -> Path:
        """Move a successfully built workspace into its permanent, project-scoped location."""
        target = self.version_dir(project_id, version_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(workspace), str(target))
        return target

    def discard(self, path: Path) -> None:
        """Remove a directory, but only if it is inside the storage root."""
        try:
            shutil.rmtree(_inside(self.root, path), ignore_errors=True)
        except GeneratedStorageError:
            pass

    def read_sources(self, project_id: uuid.UUID, version_id: uuid.UUID) -> dict[str, str]:
        """Model-visible source files (src/**) of a stored version."""
        base = self.version_dir(project_id, version_id)
        out: dict[str, str] = {}
        src = base / "src"
        if not src.is_dir():
            raise GeneratedStorageError("The stored source for this version is missing.")
        for p in sorted(src.rglob("*")):
            if p.is_file() and not p.is_symlink():
                rel = p.relative_to(base).as_posix()
                if rel not in ("src/main.tsx", "src/vite-env.d.ts"):
                    out[rel] = p.read_text(encoding="utf-8")
        return out

    def source_file(self, project_id: uuid.UUID, version_id: uuid.UUID, rel: str) -> Path:
        return self._file_in(project_id, version_id, "src", rel, anywhere_in_version=True)

    def preview_file(self, project_id: uuid.UUID, version_id: uuid.UUID, rel: str) -> Path:
        return self._file_in(project_id, version_id, "dist", rel or "index.html")

    def _file_in(self, project_id, version_id, sub: str, rel: str, anywhere_in_version: bool = False) -> Path:
        if "\\" in rel or "\x00" in rel or ":" in rel or rel.startswith("/") or any(seg in ("..", "") for seg in rel.split("/")):
            raise GeneratedStorageError("Invalid path.")
        base = self.version_dir(project_id, version_id)
        container = base if anywhere_in_version else base / sub
        path = container / rel
        if path.is_symlink():
            raise GeneratedStorageError("Invalid path.")
        resolved = _inside(base / sub if not anywhere_in_version else base, path)
        if anywhere_in_version and not (rel.startswith("src/") or rel in ("package.json", "tsconfig.json", "vite.config.ts", "index.html")):
            raise GeneratedStorageError("Invalid path.")
        if not resolved.is_file():
            raise GeneratedStorageError("File not found.")
        return resolved
