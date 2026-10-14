"""Full HTTP lifecycle against the FOLDER backend (PERSISTENCE=folder), with a fake model and a fake build runner.

These run the real routes, services, rules, run_session orchestration and generated-storage code, so they cover the service
lifecycle that the MySQL-backed tests cannot cover on this machine. They do NOT prove MySQL behaviour, a real model, or a real
sandboxed build (the runner double only writes a placeholder dist/index.html).
"""
import json
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.generation.providers import ProviderError
from app.generation.sandbox import BuildResult
from app.main import create_app
from app.services.folder_store import FolderStore, SessionRec, now
from tests.conftest import PNG
from tests.test_generation_logic import GOOD, FakeProvider, FakeRunner, model_reply


class DistRunner(FakeRunner):
    """Build double that also creates dist/, like a real successful build."""

    def build(self, workspace):
        r = super().build(workspace)
        if r.ok:
            (workspace / "dist").mkdir(exist_ok=True)
            (workspace / "dist" / "index.html").write_text("<html><body>built</body></html>")
        return r


class GatedProvider(FakeProvider):
    """Blocks inside the model call until released, so a session can be observed while active."""

    def __init__(self, *replies):
        super().__init__(*replies)
        self.entered, self.release = threading.Event(), threading.Event()

    def complete(self, **kw):
        self.entered.set()
        assert self.release.wait(10)
        return super().complete(**kw)


def settings_for(tmp_path, **extra) -> Settings:
    return Settings(persistence="folder", folder_store_dir=tmp_path / "data", screenshot_storage_dir=tmp_path / "shots",
                    generated_storage_dir=tmp_path / "gen", ai_api_key=SecretStr("k"), **extra)


@pytest.fixture
def make(tmp_path):
    clients: list[TestClient] = []

    def factory(provider, runner, **extra):
        c = TestClient(create_app(settings_for(tmp_path, **extra), provider=provider, runner=runner))
        c.__enter__()
        clients.append(c)
        return c

    yield factory
    for c in reversed(clients):
        c.__exit__(None, None, None)


def wait_done(c, gid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        g = c.get(f"/api/generations/{gid}").json()
        if g["status"] in ("completed", "failed"):
            return g
        time.sleep(0.05)
    raise AssertionError("generation did not finish")


def mk(c, name="Bank"):
    r = c.post("/api/projects", json={"name": name, "type": "master"})
    assert r.status_code == 201, r.text
    return r.json()


def gen(c, pid, **body):
    r = c.post(f"/api/projects/{pid}/generations", json=body)
    assert r.status_code == 202, r.text
    return wait_done(c, r.json()["id"])


def ok_runner(n=1):
    return DistRunner(*[BuildResult(True, "built ok")] * n)


# ---------------- projects and screenshots ----------------

def test_project_rules_match_the_mysql_backend(make):
    c = make(FakeProvider(), FakeRunner())
    assert c.get("/api/health/ready").json() == {"status": "ready", "persistence": "folder"}
    m = mk(c, "Master One")
    assert m["status"] == "draft" and m["screenshotCount"] == 0 and m["type"] == "master"
    f = c.post("/api/projects", json={"name": "Login", "type": "feature", "masterId": m["id"], "basePath": "/accounts"})
    assert f.status_code == 201 and f.json()["masterName"] == "Master One" and f.json()["basePath"] == "/accounts"
    dup = c.post("/api/projects", json={"name": "Login 2", "type": "feature", "masterId": m["id"], "basePath": "/accounts"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "base_path_conflict"
    assert c.post("/api/projects", json={"name": "Login 3", "type": "feature", "masterId": m["id"], "basePath": "/ACCOUNTS/"}).status_code == 409  # normalised first
    assert c.post("/api/projects", json={"name": "Reserved", "type": "feature", "masterId": m["id"], "basePath": "/login"}).json()["error"]["code"] == "invalid_base_path"
    on_feature = c.post("/api/projects", json={"name": "Nested", "type": "feature", "masterId": f.json()["id"], "basePath": "/x"})
    assert on_feature.status_code == 422 and on_feature.json()["error"]["code"] == "master_not_a_master"
    assert c.post("/api/projects", json={"name": "Orphan", "type": "feature", "masterId": str(uuid.uuid4()), "basePath": "/x"}).json()["error"]["code"] == "master_not_found"
    assert c.post("/api/projects", json={"name": "ab", "type": "master"}).status_code == 422
    assert c.post("/api/projects", json={"name": "Valid", "type": "master", "basePath": "/no"}).status_code == 422
    assert {p["name"] for p in c.get("/api/projects").json()} == {"Master One", "Login"}
    assert c.get(f"/api/projects/{uuid.uuid4()}").json()["error"]["code"] == "project_not_found"


def test_screenshot_upload_limit_delete_and_no_orphans(make, tmp_path):
    c = make(FakeProvider(), FakeRunner())
    p = mk(c)
    up = c.post(f"/api/projects/{p['id']}/screenshots", files=[("files", ("a.png", PNG, "image/png")), ("files", ("b.png", PNG, "image/png"))])
    assert up.status_code == 201 and len(up.json()) == 2
    assert c.get(f"/api/projects/{p['id']}").json()["screenshotCount"] == 2
    assert c.get(up.json()[0]["url"]).content == PNG
    too_many = [("files", (f"{i}.png", PNG, "image/png")) for i in range(9)]
    r = c.post(f"/api/projects/{p['id']}/screenshots", files=too_many)
    assert r.status_code == 409 and r.json()["error"]["code"] == "too_many_screenshots"
    assert len(list((tmp_path / "shots").glob("*"))) == 2  # the rejected batch left nothing behind
    bad = c.post(f"/api/projects/{p['id']}/screenshots", files=[("files", ("x.txt", b"hello world", "text/plain"))])
    assert bad.status_code == 415
    assert c.delete(f"/api/screenshots/{up.json()[0]['id']}").status_code == 204
    assert c.get(f"/api/projects/{p['id']}").json()["screenshotCount"] == 1
    assert len(list((tmp_path / "shots").glob("*"))) == 1


# ---------------- generation lifecycle ----------------

def test_prompt_only_generation_completes_and_preview_is_served(make, tmp_path):
    c = make(FakeProvider(model_reply(GOOD)), ok_runner())
    p = mk(c)
    g = gen(c, p["id"], prompt="banking dashboard")
    assert g["status"] == "completed" and g["inputMode"] == "prompt_only" and g["versionId"] and g["startedAt"] and g["completedAt"]
    ver = c.get(f"/api/versions/{g['versionId']}").json()
    assert ver["versionNumber"] == 1 and ver["projectId"] == p["id"] and ver["parentVersionId"] is None
    assert "src/App.tsx" in [f["path"] for f in ver["files"]] and "package.json" in [f["path"] for f in ver["files"]]
    assert c.get(f"/api/versions/{ver['id']}/files/src/App.tsx").text.startswith("import")
    pr = c.get(ver["previewUrl"])
    assert pr.status_code == 200 and "built" in pr.text and "connect-src 'none'" in pr.headers["content-security-policy"]
    assert c.get(f"/api/previews/{ver['id']}/..%2f..%2fsecret").status_code == 404
    assert c.get(f"/api/projects/{p['id']}").json()["status"] == "ready"
    assert (tmp_path / "gen" / p["id"] / ver["id"] / "dist" / "index.html").is_file()
    assert list((tmp_path / "gen" / "_work").iterdir()) == []  # scratch workspace cleaned up


def test_download_is_a_runnable_project_zip_without_build_output(make):
    import io
    import json
    import zipfile

    c = make(FakeProvider(model_reply(GOOD)), ok_runner())
    p = mk(c)
    g = gen(c, p["id"], prompt="banking dashboard")
    r = c.get(f"/api/versions/{g['versionId']}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"] and "generated-ui-v1.zip" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert {n.split("/")[0] for n in names} == {"generated-ui-v1"}
    rel = {n.split("/", 1)[1] for n in names}
    assert {"package.json", "tsconfig.json", "vite.config.ts", "index.html", "src/App.tsx", "src/main.tsx", "README.md", ".gitignore"} <= rel
    assert not any(n.startswith(("dist/", "node_modules/")) for n in rel)
    pkg = json.loads(z.read("generated-ui-v1/package.json"))
    assert pkg["scripts"]["dev"] == "vite" and "vite" in pkg["devDependencies"]
    assert c.get("/api/versions/00000000-0000-0000-0000-000000000000/download").status_code == 404


def test_refinement_creates_child_version_and_numbers_increase(make):
    c = make(FakeProvider(model_reply(GOOD), model_reply({**GOOD, "src/app.css": "main { color: red; }\n"})), ok_runner(2))
    p = mk(c)
    v1 = gen(c, p["id"], prompt="first")["versionId"]
    g2 = gen(c, p["id"], prompt="make it red", parentVersionId=v1)
    assert g2["status"] == "completed" and g2["parentVersionId"] == v1
    v2 = c.get(f"/api/versions/{g2['versionId']}").json()
    assert v2["versionNumber"] == 2 and v2["parentVersionId"] == v1
    assert "red" in c.get(f"/api/versions/{v2['id']}/files/src/app.css").text
    assert "navy" in c.get(f"/api/versions/{v1}/files/src/app.css").text  # the earlier version is unchanged
    assert [v["versionNumber"] for v in c.get(f"/api/projects/{p['id']}/versions").json()] == [2, 1]


def test_failed_refinement_keeps_previous_version_and_status(make):
    fail = BuildResult(False, "error TS1", "build_failed")
    prov = FakeProvider(model_reply(GOOD), model_reply(GOOD), model_reply(GOOD))
    c = make(prov, DistRunner(BuildResult(True, "ok"), fail, fail))
    p = mk(c)
    v1 = gen(c, p["id"], prompt="first")["versionId"]
    g2 = gen(c, p["id"], prompt="break it", parentVersionId=v1)
    assert g2["status"] == "failed" and g2["versionId"] is None and g2["errorCode"] == "build_failed"
    assert [v["id"] for v in c.get(f"/api/projects/{p['id']}/versions").json()] == [v1]
    assert c.get(f"/api/previews/{v1}/").status_code == 200
    assert c.get(f"/api/projects/{p['id']}").json()["status"] == "ready"
    assert "error TS1" in c.get(f"/api/generations/{g2['id']}/build-log").json()["log"]


def test_failed_first_generation_leaves_project_without_a_version(make):
    fail = BuildResult(False, "error", "build_failed")
    c = make(FakeProvider(model_reply(GOOD), model_reply(GOOD)), DistRunner(fail, fail))
    p = mk(c)
    g = gen(c, p["id"], prompt="x")
    assert g["status"] == "failed" and g["versionId"] is None
    assert c.get(f"/api/projects/{p['id']}/versions").json() == []
    assert c.get(f"/api/projects/{p['id']}").json()["status"] == "build_failed"


def test_cross_project_references_are_rejected(make):
    c = make(FakeProvider(model_reply(GOOD)), ok_runner())
    a, b = mk(c, "Alpha"), mk(c, "Bravo")
    va = gen(c, a["id"], prompt="alpha ui")["versionId"]
    r = c.post(f"/api/projects/{b['id']}/generations", json={"prompt": "steal", "parentVersionId": va})
    assert r.status_code == 404 and r.json()["error"]["code"] == "version_not_found"
    shot = c.post(f"/api/projects/{a['id']}/screenshots", files=[("files", ("a.png", PNG, "image/png"))]).json()[0]
    r = c.post(f"/api/projects/{b['id']}/generations", json={"screenshotIds": [shot["id"]]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "screenshot_not_in_project"
    assert c.get(f"/api/projects/{b['id']}/versions").json() == []


def test_screenshot_only_sends_the_image_to_the_vision_model(make):
    prov = FakeProvider(model_reply(GOOD))
    c = make(prov, ok_runner())
    p = mk(c)
    shot = c.post(f"/api/projects/{p['id']}/screenshots", files=[("files", ("a.png", PNG, "image/png"))]).json()[0]
    g = gen(c, p["id"], screenshotIds=[shot["id"]])
    assert g["status"] == "completed" and g["inputMode"] == "screenshot_only"
    assert prov.calls[0]["images"][0].data == PNG and prov.calls[0]["model"] == Settings().ai_vision_model


def test_deleted_screenshot_cannot_be_used(make):
    c = make(FakeProvider(), ok_runner())
    p = mk(c)
    shot = c.post(f"/api/projects/{p['id']}/screenshots", files=[("files", ("a.png", PNG, "image/png"))]).json()[0]
    c.delete(f"/api/screenshots/{shot['id']}")
    r = c.post(f"/api/projects/{p['id']}/generations", json={"screenshotIds": [shot["id"]]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "screenshot_not_in_project"


def test_only_one_active_generation_per_project(make):
    gate = GatedProvider(model_reply(GOOD), model_reply(GOOD))
    c = make(gate, ok_runner(2))
    p, other = mk(c, "Alpha"), mk(c, "Bravo")
    first = c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "one"})
    assert first.status_code == 202
    assert gate.entered.wait(10)
    second = c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "two"})
    assert second.status_code == 409 and second.json()["error"]["code"] == "generation_in_progress"
    assert c.get(f"/api/generations/{first.json()['id']}").json()["status"] in ("generating", "building")
    assert c.get(f"/api/projects/{p['id']}").json()["status"] == "generating"
    assert c.get(f"/api/projects/{other['id']}").json()["status"] == "draft"  # other projects are unaffected
    gate.release.set()
    assert wait_done(c, first.json()["id"])["status"] == "completed"
    third = c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "three"})  # the project is free again
    assert third.status_code == 202 and wait_done(c, third.json()["id"])["status"] == "completed"


def test_retry_rules_and_limits(make):
    transient = ProviderError("ai_unavailable", "down", True)
    prov = FakeProvider(transient, transient, transient, model_reply(GOOD))
    c = make(prov, ok_runner(), max_session_attempts=3)
    p = mk(c)
    g = gen(c, p["id"], prompt="x")
    assert g["status"] == "failed" and g["errorCode"] == "ai_unavailable" and g["retryable"] and g["attempts"] == 1
    for attempt in (2, 3):
        r = c.post(f"/api/generations/{g['id']}/retry")
        assert r.status_code == 202 and r.json()["attempts"] == attempt and r.json()["errorCode"] is None
        g = wait_done(c, g["id"])
        assert g["status"] == "failed" and g["attempts"] == attempt
    limit = c.post(f"/api/generations/{g['id']}/retry")
    assert limit.status_code == 409 and limit.json()["error"]["code"] == "retry_limit_reached"
    assert c.get(f"/api/projects/{p['id']}/versions").json() == []


def test_retry_succeeds_and_completed_sessions_cannot_be_retried(make):
    c = make(FakeProvider(ProviderError("ai_rate_limited", "slow", True), model_reply(GOOD)), ok_runner())
    p = mk(c)
    g = gen(c, p["id"], prompt="x")
    assert g["status"] == "failed"
    assert c.post(f"/api/generations/{g['id']}/retry").status_code == 202
    done = wait_done(c, g["id"])
    assert done["status"] == "completed" and done["attempts"] == 2 and done["versionId"]
    again = c.post(f"/api/generations/{g['id']}/retry")
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_retryable"


def test_non_retryable_failure_cannot_be_retried(make):
    c = make(FakeProvider(ProviderError("ai_auth_failed", "bad token", False)), ok_runner())
    p = mk(c)
    g = gen(c, p["id"], prompt="x")
    assert g["status"] == "failed" and not g["retryable"]
    r = c.post(f"/api/generations/{g['id']}/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_retryable"


# ---------------- persistence across restarts ----------------

def test_records_survive_a_restart_and_are_plain_json(make, tmp_path):
    c1 = make(FakeProvider(model_reply(GOOD)), ok_runner())
    p = mk(c1)
    v1 = gen(c1, p["id"], prompt="x")["versionId"]
    c1.__exit__(None, None, None)  # stop the first backend (releases the folder lock)
    c2 = make(FakeProvider(), FakeRunner())
    assert [x["id"] for x in c2.get("/api/projects").json()] == [p["id"]]
    assert [v["id"] for v in c2.get(f"/api/projects/{p['id']}/versions").json()] == [v1]
    assert c2.get(f"/api/previews/{v1}/").status_code == 200
    rec = json.loads((tmp_path / "data" / "projects" / f"{p['id']}.json").read_text(encoding="utf-8"))
    assert rec["name"] == "Bank" and rec["created_at"].endswith("+00:00")  # readable, with an explicit UTC offset


def _seed_session(tmp_path, project_id, status):
    store = FolderStore(tmp_path / "data")
    stamp = now()
    rec = SessionRec(id=uuid.uuid4(), project_id=uuid.UUID(project_id), parent_version_id=None, input_mode="prompt_only", prompt="x",
                     screenshot_ids=[], status=status, attempts=1, retryable=False, created_at=stamp, updated_at=stamp)
    store.put("sessions", rec)
    return str(rec.id)


def test_interrupted_sessions_are_failed_and_retryable_after_restart(make, tmp_path):
    c1 = make(FakeProvider(), FakeRunner())
    p = mk(c1)
    c1.__exit__(None, None, None)
    sid = _seed_session(tmp_path, p["id"], "building")  # as if the backend died mid-build
    c2 = make(FakeProvider(model_reply(GOOD)), ok_runner())
    g = c2.get(f"/api/generations/{sid}").json()
    assert g["status"] == "failed" and g["errorCode"] == "interrupted" and g["retryable"] and g["completedAt"]
    assert c2.get(f"/api/projects/{p['id']}").json()["status"] == "draft"
    assert c2.post(f"/api/generations/{sid}/retry").status_code == 202
    assert wait_done(c2, sid)["status"] == "completed"


def test_second_backend_process_on_the_same_folder_is_refused(tmp_path):
    first, second = FolderStore(tmp_path / "data"), FolderStore(tmp_path / "data")
    first.open()
    try:
        with pytest.raises(RuntimeError, match="already using"):
            second.open()
    finally:
        first.shutdown()
    second.open()  # free again once the first one stopped
    second.shutdown()


def test_damaged_record_is_reported_not_skipped(make, tmp_path):
    c = make(FakeProvider(), FakeRunner())
    p = mk(c)
    (tmp_path / "data" / "projects" / f"{p['id']}.json").write_text("{not json", encoding="utf-8")
    r = c.get("/api/projects")
    assert r.status_code == 500 and r.json()["error"]["code"] == "store_corrupt"
    assert "not json" not in r.text and str(tmp_path) not in r.text


def test_default_persistence_is_mysql_never_folder(tmp_path):
    assert Settings(_env_file=None).persistence == "mysql"


def test_screenshot_used_by_a_generation_cannot_be_deleted(make):
    c = make(FakeProvider(model_reply(GOOD)), ok_runner())
    p = mk(c)
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    shot = c.post(f"/api/projects/{p['id']}/screenshots", files=[("files", ("a.png", png, "image/png"))]).json()[0]
    g = gen(c, p["id"], prompt="same as image", screenshotIds=[shot["id"]])
    assert g["status"] == "completed"
    r = c.delete(f"/api/screenshots/{shot['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "screenshot_in_use"
    assert c.get(f"/api/screenshots/{shot["id"]}").status_code == 200


def test_delete_project_removes_records_screenshots_and_source(make, tmp_path):
    c = make(FakeProvider(model_reply(GOOD)), ok_runner())
    p = mk(c)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    shot = c.post(f"/api/projects/{p['id']}/screenshots", files=[("files", ("a.png", png, "image/png"))]).json()[0]
    g = gen(c, p["id"], prompt="x", screenshotIds=[shot["id"]])
    assert g["status"] == "completed"
    assert (tmp_path / "gen" / p["id"]).is_dir()
    other = mk(c)
    assert c.delete(f"/api/projects/{p['id']}").status_code == 204
    assert c.get(f"/api/projects/{p['id']}").status_code == 404
    assert c.get(f"/api/screenshots/{shot['id']}").status_code == 404
    assert c.get(f"/api/versions/{g['versionId']}").status_code == 404
    assert c.get(f"/api/generations/{g['id']}").status_code == 404
    assert not (tmp_path / "gen" / p["id"]).exists()
    assert [x["id"] for x in c.get("/api/projects").json()] == [other["id"]]  # nothing else touched
    assert c.delete(f"/api/projects/{p['id']}").status_code == 404


def test_master_with_features_cannot_be_deleted(make):
    c = make(FakeProvider(model_reply(GOOD)), ok_runner())
    m = mk(c)
    f = c.post("/api/projects", json={"name": "Login", "type": "feature", "masterId": m["id"], "basePath": "/accounts"})
    assert f.status_code == 201, f.text
    f = f.json()
    r = c.delete(f"/api/projects/{m['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "project_has_features"
    assert c.delete(f"/api/projects/{f['id']}").status_code == 204
    assert c.delete(f"/api/projects/{m['id']}").status_code == 204


def test_four_screenshots_per_message_and_sent_ones_do_not_count_against_the_next(make):
    c = make(FakeProvider(model_reply(GOOD)), ok_runner())
    p = mk(c)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    def upload(n):
        return c.post(f"/api/projects/{p['id']}/screenshots", files=[("files", (f"{i}.png", png, "image/png")) for i in range(n)])

    assert upload(5).status_code == 409  # more than 4 in one message
    first = upload(4)
    assert first.status_code == 201
    assert upload(1).status_code == 409  # 4 already waiting
    g = gen(c, p["id"], prompt="x", screenshotIds=[s["id"] for s in first.json()])
    assert g["status"] == "completed"
    assert upload(4).status_code == 201  # the next message gets its own 4
    too_many = c.post(f"/api/projects/{p['id']}/generations", json={"prompt": "y", "screenshotIds": [str(uuid.uuid4()) for _ in range(5)]})
    assert too_many.status_code == 422 and too_many.json()["error"]["code"] == "too_many_screenshots"
