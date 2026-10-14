"""Local filesystem storage for screenshot files. Metadata lives in MySQL."""

import os
import re
import unicodedata
import uuid
from pathlib import Path

# Content type is decided from the file's own bytes, never from the client-provided MIME type or name.
_EXT_BY_TYPE = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
_STORAGE_KEY = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")


class StorageError(Exception):
    """File could not be written or read."""


class UnsafePathError(StorageError):
    """A storage key did not resolve to a file inside the storage directory."""


def detect_image_type(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def sanitize_display_name(name: str | None) -> str:
    """Display-only name: no directories, no control characters, bounded length."""
    base = (name or "").replace("\\", "/").split("/")[-1]
    base = "".join(c for c in unicodedata.normalize("NFC", base) if unicodedata.category(c)[0] != "C").strip()
    if base in ("", ".", ".."):
        base = "screenshot"
    if len(base) > 120:
        stem, dot, ext = base.rpartition(".")
        base = (stem[:100] + dot + ext[:15]) if dot else base[:120]
    return base


class ScreenshotStorage:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()

    def ensure_ready(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def new_key(self, content_type: str) -> str:
        return f"{uuid.uuid4().hex}.{_EXT_BY_TYPE[content_type]}"

    def path_for(self, key: str) -> Path:
        """Resolve a storage key to a path, refusing anything outside the storage directory."""
        if not _STORAGE_KEY.match(key):
            raise UnsafePathError("Invalid storage key.")
        path = (self.directory / key).resolve()
        if path.parent != self.directory:
            raise UnsafePathError("Storage key escapes the storage directory.")
        return path

    def write(self, key: str, data: bytes) -> None:
        """Create the file exclusively (never overwrites an existing file)."""
        path = self.path_for(key)
        created = False
        try:
            self.ensure_ready()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            created = True
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            if created:  # only clean up a partial file we made; never delete a pre-existing one
                self.remove_quietly(key)
            raise StorageError("Could not write the uploaded file.") from exc

    def remove_quietly(self, key: str) -> None:
        try:
            self.path_for(key).unlink(missing_ok=True)
        except (OSError, UnsafePathError):
            pass
