"""Test fixtures.

DB-backed tests run ONLY against an isolated MySQL test database (name must end in `_test` and differ from the
dev database). If MySQL is unreachable they are skipped with the reason; they are never replaced by a
mock or an alternate database engine. The folder backend has its own explicit tests (test_folder_backend.py).
"""
import os

os.environ.setdefault("PERSISTENCE", "mysql")  # the MySQL test suite must never pick up a folder-mode backend/.env
os.environ.setdefault("BUILD_RUNNER", "disabled")  # tests must never pick up a host/docker runner from backend/.env
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pymysql
import pytest
from sqlalchemy import text

from app.config import BACKEND_DIR, Settings
from app.db import create_db_engine
from app.main import create_app

pytestmark_db = pytest.mark.db


def _settings(storage: Path) -> Settings:
    base = Settings()
    test_db = base.test_db_name
    if not test_db.endswith("_test"):
        raise RuntimeError("TEST_DB_NAME must end with '_test'.")
    if test_db == base.db_name:
        raise RuntimeError("TEST_DB_NAME must differ from the development database name.")
    return base.model_copy(update={"db_name": test_db, "screenshot_storage_dir": storage})


def _connect(settings: Settings, dbname: str | None):
    return pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=dbname,
        connect_timeout=3,
        autocommit=True,
        charset="utf8mb4",
    )


@pytest.fixture(scope="session")
def storage_root():
    root = Path(tempfile.mkdtemp(prefix="screenshots_test_"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def test_settings(storage_root) -> Settings:
    return _settings(storage_root)


@pytest.fixture(scope="session")
def db_ready(test_settings):
    """Create the test DB if missing (never touches any other DB), then migrate it."""
    try:
        conn = _connect(test_settings, None)
    except pymysql.err.OperationalError as exc:
        pytest.skip(f"MySQL is not reachable/usable at {test_settings.db_host}:{test_settings.db_port}: error {exc.args[0]}")
    with conn:
        with conn.cursor() as cur:
            # Only ever creates the *_test database (never drops or alters anything that already exists).
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{test_settings.db_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
    env = {**os.environ, "MIGRATION_DB_NAME": test_settings.db_name}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND_DIR, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr[-2000:]}"
    return True


@pytest.fixture
def app(test_settings, db_ready):
    application = create_app(test_settings)
    engine = application.state.engine
    with engine.begin() as conn:
        assert conn.execute(text("SELECT DATABASE()")).scalar_one() == test_settings.db_name  # never wipe another DB
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        conn.execute(text("TRUNCATE TABLE generated_versions"))
        conn.execute(text("TRUNCATE TABLE generation_sessions"))
        conn.execute(text("TRUNCATE TABLE screenshots"))
        conn.execute(text("TRUNCATE TABLE projects"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    for f in test_settings.resolved_storage_dir.glob("*"):
        f.unlink()
    yield application
    engine.dispose()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture
def raw_engine(test_settings, db_ready):
    engine = create_db_engine(test_settings)
    yield engine
    engine.dispose()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
