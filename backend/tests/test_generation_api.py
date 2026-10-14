"""Generation API behaviour that does not need a database (validation and configuration run before any DB access)
plus DB-backed lifecycle tests (skipped, with a reason, unless MySQL is usable)."""
import io
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.generation.sandbox import BuildResult
from app.main import create_app
from tests.conftest import PNG
from tests.test_generation_logic import APP, GOOD, FakeProvider, FakeRunner, model_reply

PID = str(uuid.uuid4())


@pytest.fixture
def nodb(tmp_path):
    """No database reachable; AI configured and a runner that claims availability, to reach the validation layer."""
    s = Settings(db_host="127.0.0.1", db_port=1, db_connect_timeout_seconds=1, screenshot_storage_dir=tmp_path / "s",
                 generated_storage_dir=tmp_path / "g", ai_api_key=SecretStr("k"))
    with TestClient(create_app(s, provider=FakeProvider(), runner=FakeRunner())) as c:
        yield c


def test_empty_request_rejected_before_database(nodb):
    for body in ({}, {"prompt": ""}, {"prompt": "   ", "screenshotIds": []}):
        r = nodb.post(f"/api/projects/{PID}/generations", json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "no_input"


def test_unknown_fields_and_bad_ids_rejected(nodb):
    assert nodb.post(f"/api/projects/{PID}/generations", json={"prompt": "x", "status": "completed"}).status_code == 422
    assert nodb.post(f"/api/projects/{PID}/generations", json={"screenshotIds": ["nope"]}).status_code == 422
    assert nodb.post("/api/projects/not-a-uuid/generations", json={"prompt": "x"}).status_code == 422


def test_refinement_requires_prompt(nodb):
    r = nodb.post(f"/api/projects/{PID}/generations", json={"screenshotIds": [PID], "parentVersionId": PID})
    assert r.status_code == 422 and r.json()["error"]["code"] == "refinement_needs_prompt"


def test_prompt_too_long_rejected(nodb):
    r = nodb.post(f"/api/projects/{PID}/generations", json={"prompt": "x" * 8001})
    assert r.status_code == 422 and r.json()["error"]["code"] == "prompt_too_long"


def test_status_endpoint_never_exposes_secrets(nodb):
    r = nodb.get("/api/generation/status")
    assert r.status_code == 200
    body = r.json()
    assert body["aiConfigured"] is True and body["buildRunner"] == "fake"
    assert "k" != body.get("apiKey") and "apiKey" not in body and "secret" not in r.text.lower()


def test_missing_ai_credentials_gives_actionable_error(tmp_path):
    s = Settings(db_host="127.0.0.1", db_port=1, db_connect_timeout_seconds=1, screenshot_storage_dir=tmp_path / "s",
                 generated_storage_dir=tmp_path / "g", ai_api_key=SecretStr(""))
    with TestClient(create_app(s, provider=FakeProvider(), runner=FakeRunner())) as c:
        r = c.post(f"/api/projects/{PID}/generations", json={"prompt": "x"})
        assert r.status_code == 503 and r.json()["error"]["code"] == "ai_not_configured"
        assert "AI_API_KEY" in r.json()["error"]["message"]
        assert c.get("/api/generation/status").json()["ready"] is False


def test_default_runner_blocks_generation_without_sandbox(tmp_path):
    s = Settings(db_host="127.0.0.1", db_port=1, db_connect_timeout_seconds=1, screenshot_storage_dir=tmp_path / "s",
                 generated_storage_dir=tmp_path / "g", ai_api_key=SecretStr("k"))
    with TestClient(create_app(s, provider=FakeProvider())) as c:  # default runner = disabled
        r = c.post(f"/api/projects/{PID}/generations", json={"prompt": "x"})
        assert r.status_code == 503 and r.json()["error"]["code"] == "build_sandbox_unavailable"


# ---------------- DB-backed lifecycle (skipped without MySQL) ----------------

@pytest.fixture
def gen_app(test_settings, db_ready):
    from sqlalchemy import text

    s = test_settings.model_copy(update={"ai_api_key": SecretStr("k"), "generated_storage_dir": test_settings.screenshot_storage_dir / "gen"})
    holder = {}

    def make(provider, runner):
        app = create_app(s, provider=provider, runner=runner)
        with app.state.engine.begin() as conn:
            assert conn.execute(text("SELECT DATABASE()")).scalar_one() == s.db_name
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            for t in ("generated_versions", "generation_sessions", "screenshots", "projects"):
                conn.execute(text(f"TRUNCATE TABLE {t}"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        holder["app"] = app
        return app

    yield make
    if "app" in holder:
        holder["app"].state.engine.dispose()


def wait_done(client, gid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        g = client.get(f"/api/generations/{gid}").json()
        if g["status"] in ("completed", "failed"):
            return g
        time.sleep(0.05)
    raise AssertionError("generation did not finish")


def mk_project(client, name="Bank"):
    return client.post("/api/projects", json={"name": name, "type": "master"}).json()


class DistRunner(FakeRunner):
    """Build double that also creates dist/, like a real successful build."""

    def build(self, workspace):
        r = super().build(workspace)
        if r.ok:
            (workspace / "dist").mkdir(exist_ok=True)
            (workspace / "dist" / "index.html").write_text("<html><body>built</body></html>")
        return r


def test_db_prompt_only_session_completes_and_preview_served(gen_app):
    app = gen_app(FakeProvider(model_reply(GOOD)), DistRunner(BuildResult(True, "built ok")))
    with TestClient(app) as c:
        p = mk_project(c)
        r = c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "banking dashboard"})
        assert r.status_code == 202 and r.json()["inputMode"] == "prompt_only"
        g = wait_done(c, r.json()["id"])
        assert g["status"] == "completed" and g["versionId"] and g["startedAt"] and g["completedAt"]
        ver = c.get(f"/api/versions/{g['versionId']}").json()
        assert ver["versionNumber"] == 1 and ver["projectId"] == p["id"]
        assert "src/App.tsx" in [f["path"] for f in ver["files"]]
        assert c.get(f"/api/versions/{ver['id']}/files/src/App.tsx").text.startswith("import")
        pr = c.get(ver["previewUrl"])
        assert pr.status_code == 200 and "built" in pr.text and "connect-src 'none'" in pr.headers["content-security-policy"]
        assert c.get(f"/api/projects/{p['id']}").json()["status"] == "ready"
        assert c.get(f"/api/previews/{ver['id']}/..%2f..%2fsecret").status_code == 404


def test_db_failed_refinement_keeps_previous_version(gen_app):
    fail = BuildResult(False, "error TS1", "build_failed")
    prov = FakeProvider(model_reply(GOOD), model_reply(GOOD), model_reply(GOOD))
    app = gen_app(prov, DistRunner(BuildResult(True, "ok"), fail, fail))
    with TestClient(app) as c:
        p = mk_project(c)
        v1 = wait_done(c, c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "a"}).json()["id"])["versionId"]
        g2 = wait_done(c, c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "b", "parentVersionId": v1}).json()["id"])
        assert g2["status"] == "failed" and g2["versionId"] is None and g2["errorCode"] == "build_failed"
        assert "error TS1" in c.get(f"/api/generations/{g2['id']}/build-log").json()["log"]
        versions = c.get(f"/api/projects/{p['id']}/versions").json()
        assert [v["id"] for v in versions] == [v1]
        assert c.get(f"/api/previews/{v1}/").status_code == 200  # previous version still previewable
        assert c.get(f"/api/projects/{p['id']}").json()["status"] == "ready"


def test_db_refinement_creates_child_version(gen_app):
    prov = FakeProvider(model_reply(GOOD), model_reply({"src/App.tsx": APP}))
    app = gen_app(prov, DistRunner(BuildResult(True, ""), BuildResult(True, "")))
    with TestClient(app) as c:
        p = mk_project(c)
        v1 = wait_done(c, c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "a"}).json()["id"])["versionId"]
        v2 = wait_done(c, c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "make it blue", "parentVersionId": v1}).json()["id"])["versionId"]
        ver2 = c.get(f"/api/versions/{v2}").json()
        assert ver2["versionNumber"] == 2 and ver2["parentVersionId"] == v1
        assert "import './app.css'" in prov.calls[1]["user_text"]  # parent source was given to the model


def test_db_cross_project_isolation(gen_app):
    app = gen_app(FakeProvider(model_reply(GOOD)), DistRunner(BuildResult(True, "")))
    with TestClient(app) as c:
        a, b = mk_project(c, "Alpha"), mk_project(c, "Beta")
        va = wait_done(c, c.post(f"/api/projects/{a['id']}/generations", json={"prompt": "a"}).json()["id"])["versionId"]
        r = c.post(f"/api/projects/{b['id']}/generations", json={"prompt": "x", "parentVersionId": va})
        assert r.status_code == 404 and r.json()["error"]["code"] == "version_not_found"
        assert c.get(f"/api/projects/{b['id']}/versions").json() == []


def test_db_screenshot_must_belong_to_project(gen_app):
    app = gen_app(FakeProvider(), DistRunner())
    with TestClient(app) as c:
        a, b = mk_project(c, "Alpha"), mk_project(c, "Beta")
        shot = c.post(f"/api/projects/{a['id']}/screenshots", files=[("files", ("a.png", io.BytesIO(PNG), "image/png"))]).json()[0]
        r = c.post(f"/api/projects/{b['id']}/generations", json={"screenshotIds": [shot["id"]]})
        assert r.status_code == 422 and r.json()["error"]["code"] == "screenshot_not_in_project"


def test_db_screenshot_only_sends_image_to_provider(gen_app):
    prov = FakeProvider(model_reply(GOOD))
    app = gen_app(prov, DistRunner(BuildResult(True, "")))
    with TestClient(app) as c:
        p = mk_project(c)
        shot = c.post(f"/api/projects/{p['id']}/screenshots", files=[("files", ("a.png", io.BytesIO(PNG), "image/png"))]).json()[0]
        r = c.post(f"/api/projects/{p['id']}/generations", json={"screenshotIds": [shot["id"]]})
        assert r.json()["inputMode"] == "screenshot_only"
        assert wait_done(c, r.json()["id"])["status"] == "completed"
        assert prov.calls[0]["images"][0].data == PNG


def test_db_retry_only_for_retryable_and_bounded(gen_app):
    from app.generation.providers import ProviderError

    prov = FakeProvider(ProviderError("ai_rate_limited", "wait", True), model_reply(GOOD))
    app = gen_app(prov, DistRunner(BuildResult(True, "")))
    with TestClient(app) as c:
        p = mk_project(c)
        g = wait_done(c, c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "a"}).json()["id"])
        assert g["status"] == "failed" and g["retryable"] and g["errorCode"] == "ai_rate_limited"
        r = c.post(f"/api/generations/{g['id']}/retry")
        assert r.status_code == 202 and r.json()["attempts"] == 2
        assert wait_done(c, g["id"])["status"] == "completed"
        assert c.post(f"/api/generations/{g['id']}/retry").status_code == 409  # completed: not retryable


def test_db_only_one_active_generation_per_project(gen_app):
    import threading

    gate = threading.Event()

    class Slow(FakeProvider):
        def complete(self, **kw):
            gate.wait(5)
            return super().complete(**kw)

    app = gen_app(Slow(model_reply(GOOD)), DistRunner(BuildResult(True, "")))
    with TestClient(app) as c:
        p = mk_project(c)
        first = c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "a"})
        dup = c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "b"})
        gate.set()
        assert first.status_code == 202 and dup.status_code == 409 and dup.json()["error"]["code"] == "generation_in_progress"
        wait_done(c, first.json()["id"])


def test_db_interrupted_sessions_recovered_on_startup(gen_app):
    from app.models import GenerationSession
    from app.services.generations import recover_interrupted

    app = gen_app(FakeProvider(), DistRunner())
    with TestClient(app) as c:
        p = mk_project(c)
        with app.state.session_factory() as s:
            row = GenerationSession(project_id=uuid.UUID(p["id"]), input_mode="prompt_only", prompt="x", screenshot_ids=[], status="building", attempts=1, retryable=False)
            s.add(row)
            s.commit()
            gid = row.id
        assert recover_interrupted(app.state.session_factory) == 1
        g = c.get(f"/api/generations/{gid}").json()
        assert g["status"] == "failed" and g["errorCode"] == "interrupted" and g["retryable"]


def test_db_check_constraints_reject_bad_status(raw_engine):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    with raw_engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        conn.execute(text("TRUNCATE TABLE generation_sessions"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    with pytest.raises(DBAPIError):
        with raw_engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            conn.execute(text(
                "INSERT INTO generation_sessions (id, project_id, input_mode, screenshot_ids, status, attempts, retryable) "
                "VALUES ('a', 'b', 'prompt_only', '[]', 'hacked', 1, 0)"
            ))
