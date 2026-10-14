"""The API must start and report honestly when MySQL cannot be reached (uses a closed local port)."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def down_client(tmp_path):
    s = Settings(db_host="127.0.0.1", db_port=1, db_connect_timeout_seconds=1, screenshot_storage_dir=tmp_path)
    with TestClient(create_app(s)) as c:
        yield c


def test_health_ok_without_database(down_client):
    assert down_client.get("/api/health").json() == {"status": "ok"}


def test_ready_and_projects_report_503_without_leaking_details(down_client):
    for path in ("/api/health/ready", "/api/projects"):
        r = down_client.get(path)
        assert r.status_code == 503
        body = r.json()
        assert body["error"]["code"] == "database_unavailable"
        text = r.text.lower()
        assert "password" not in text and "traceback" not in text and "127.0.0.1" not in text


def test_validation_errors_do_not_need_database(down_client):
    r = down_client.post("/api/projects", json={"name": "x"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


def test_cors_only_for_configured_origins(down_client):
    ok = down_client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = down_client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in bad.headers
