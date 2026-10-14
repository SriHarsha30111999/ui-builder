"""Pure-logic tests: no database needed."""
import pytest

from app.services.paths import BasePathError, normalize_base_path, validate_base_path
from app.services.storage import (
    ScreenshotStorage,
    StorageError,
    UnsafePathError,
    detect_image_type,
    sanitize_display_name,
)

from .conftest import GIF, JPEG, PNG, WEBP


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  /Reports//Monthly/ ", "/reports/monthly"),
        ("reports", "/reports"),
        ("/a_b/c-d", "/a_b/c-d"),
    ],
)
def test_normalizes_to_canonical(raw, expected):
    assert validate_base_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "/", "/a b", "/a?x", "/a#x", "/a:b", "/a*", "/a\\b", "/a%20b", "/-a", "/a--b", "/a-", "/login",
     "/api/x", "/dashboard", "/" + "a" * 120],
)
def test_rejects_invalid_paths(raw):
    with pytest.raises(BasePathError):
        validate_base_path(raw)


def test_normalize_is_idempotent():
    once = normalize_base_path("/Foo//Bar/")
    assert normalize_base_path(once) == once


def test_magic_byte_detection():
    assert detect_image_type(PNG) == "image/png"
    assert detect_image_type(JPEG) == "image/jpeg"
    assert detect_image_type(GIF) == "image/gif"
    assert detect_image_type(WEBP) == "image/webp"
    assert detect_image_type(b"<svg xmlns=...>") is None
    assert detect_image_type(b"MZ\x90\x00") is None
    assert detect_image_type(b"") is None


@pytest.mark.parametrize("name", ["../../etc/passwd", "..\\..\\x.png", "a/b/c.png", "", None, "..", "bad\x00name.png"])
def test_display_name_has_no_directories_or_control_chars(name):
    out = sanitize_display_name(name)
    assert "/" not in out and "\\" not in out and "\x00" not in out and out not in ("", ".", "..")


@pytest.mark.parametrize(
    "key", ["../x.png", "..\\x.png", "x.png", "a" * 32 + ".exe", "a" * 32 + ".png/../b", "/etc/passwd", "A" * 32 + ".png", ""]
)
def test_storage_rejects_unsafe_keys(tmp_path, key):
    with pytest.raises(UnsafePathError):
        ScreenshotStorage(tmp_path).path_for(key)


def test_storage_write_is_exclusive_and_inside_dir(tmp_path):
    storage = ScreenshotStorage(tmp_path)
    key = storage.new_key("image/png")
    storage.write(key, PNG)
    assert storage.path_for(key).read_bytes() == PNG
    assert storage.path_for(key).parent == tmp_path.resolve()
    with pytest.raises(StorageError):
        storage.write(key, b"overwrite")
    assert storage.path_for(key).read_bytes() == PNG  # failed write did not clobber or delete the original
    storage.remove_quietly(key)
    assert not storage.path_for(key).exists()
