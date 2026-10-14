"""API tests that require the isolated MySQL test database (skipped, with a reason, if it is unreachable)."""
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import GIF, PNG, WEBP


def make_master(client, name="Core Banking"):
    r = client.post("/api/projects", json={"name": name, "type": "master"})
    assert r.status_code == 201, r.text
    return r.json()


def make_feature(client, master_id, path="/reports", name="Reports"):
    return client.post(
        "/api/projects", json={"name": name, "type": "feature", "masterId": master_id, "basePath": path}
    )


def test_create_master_and_feature(client):
    m = make_master(client)
    assert m["type"] == "master" and m["basePath"] is None and m["masterId"] is None
    r = make_feature(client, m["id"], "  /Reports//Monthly/ ")
    assert r.status_code == 201
    f = r.json()
    assert f["basePath"] == "/reports/monthly" and f["masterId"] == m["id"] and f["masterName"] == "Core Banking"
    assert {p["id"] for p in client.get("/api/projects").json()} == {m["id"], f["id"]}


def test_feature_requires_existing_master(client):
    r = make_feature(client, "00000000-0000-4000-8000-000000000000")
    assert r.status_code == 422 and r.json()["error"]["code"] == "master_not_found"


def test_feature_cannot_be_master_of_feature(client):
    m = make_master(client)
    f = make_feature(client, m["id"]).json()
    r = make_feature(client, f["id"], "/other")
    assert r.status_code == 422 and r.json()["error"]["code"] == "master_not_a_master"


def test_master_cannot_have_path_or_parent(client):
    m = make_master(client)
    assert client.post("/api/projects", json={"name": "Another", "type": "master", "basePath": "/x"}).status_code == 422
    assert client.post("/api/projects", json={"name": "Another", "type": "master", "masterId": m["id"]}).status_code == 422


@pytest.mark.parametrize("path", ["/", "", "/a b", "/login", "/a?b"])
def test_invalid_paths_rejected(client, path):
    m = make_master(client)
    r = make_feature(client, m["id"], path)
    assert r.status_code == 422 and "basePath" in r.json()["error"]["fields"]


def test_duplicate_path_per_master_rejected_but_ok_under_other_master(client):
    m1, m2 = make_master(client, "Master One"), make_master(client, "Master Two")
    assert make_feature(client, m1["id"], "/reports").status_code == 201
    dup = make_feature(client, m1["id"], "/Reports/")
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "base_path_conflict"
    assert make_feature(client, m2["id"], "/reports").status_code == 201


def test_concurrent_duplicate_creation_only_one_wins(app):
    with TestClient(app) as setup:
        m = make_master(setup)
    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker():
        with TestClient(app) as c:
            barrier.wait()
            results.append(make_feature(c, m["id"], "/race").status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [201] + [409] * 7


def _insert(raw_engine, **cols):
    from sqlalchemy import text

    base = {"id": "REPLACE(UUID(), '-', '')", "name": "'bad'", "type": "'feature'", "base_path": "'/x'"}
    base.update(cols)
    names = ", ".join(base)
    values = ", ".join(base.values())
    with raw_engine.begin() as conn:
        conn.execute(text(f"INSERT INTO projects ({names}) VALUES ({values})"))


def test_db_rejects_invalid_hierarchy_when_api_is_bypassed(raw_engine, client):
    """Defense in depth: the database itself refuses Feature-of-Feature and malformed rows."""
    from sqlalchemy.exc import IntegrityError

    m = make_master(client)
    f = make_feature(client, m["id"]).json()
    fid = f["id"].replace("-", "")
    # parent really is a Feature but claims master_type='master' -> composite FK (id, type) has no such row
    with pytest.raises(IntegrityError):
        _insert(raw_engine, master_id=f"'{fid}'", master_type="'master'")
    # parent is a Feature and honestly says so -> CHECK master_feature_shape rejects
    with pytest.raises(IntegrityError):
        _insert(raw_engine, master_id=f"'{fid}'", master_type="'feature'")
    # Feature without a parent / Master with a path -> CHECK
    with pytest.raises(IntegrityError):
        _insert(raw_engine)
    with pytest.raises(IntegrityError):
        _insert(raw_engine, type="'master'", base_path="'/x'")


def test_concurrent_uploads_cannot_exceed_project_limit(app):
    with TestClient(app) as setup:
        m = make_master(setup)
    limit = app.state.settings.max_screenshots_per_prompt  # 4
    n = limit // 2 + 1  # 6 uploads x 2 files against a limit of 10
    results: list[int] = []
    barrier = threading.Barrier(n)

    def worker():
        with TestClient(app) as c:
            barrier.wait()
            r = c.post(
                f"/api/projects/{m['id']}/screenshots",
                files=[("files", ("a.png", PNG, "image/png")), ("files", ("b.png", PNG, "image/png"))],
            )
            results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [201] * (limit // 2) + [409]
    with TestClient(app) as c:
        assert c.get(f"/api/projects/{m['id']}").json()["screenshotCount"] == limit
    assert len(list(app.state.storage.directory.glob("*"))) == limit  # rejected upload left no orphan file


def test_screenshot_upload_retrieve_delete(client, app):
    m = make_master(client)
    r = client.post(
        f"/api/projects/{m['id']}/screenshots",
        files=[("files", ("../../evil name.png", PNG, "text/plain")), ("files", ("b.webp", WEBP, "image/webp"))],
    )
    assert r.status_code == 201, r.text
    shots = r.json()
    assert [s["contentType"] for s in shots] == ["image/png", "image/webp"]  # type from bytes, not client MIME
    assert "/" not in shots[0]["filename"]
    got = client.get(shots[0]["url"])
    assert got.status_code == 200 and got.content == PNG and got.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/api/projects/{m['id']}").json()["screenshotCount"] == 2
    assert client.delete(f"/api/screenshots/{shots[0]['id']}").status_code == 204
    assert client.get(f"/api/screenshots/{shots[0]['id']}").status_code == 404
    assert len(list(app.state.storage.directory.glob("*"))) == 1


def test_upload_rejections_are_all_or_nothing(client, app):
    m = make_master(client)
    url = f"/api/projects/{m['id']}/screenshots"
    bad = client.post(url, files=[("files", ("a.png", PNG, "image/png")), ("files", ("x.png", b"<svg/>", "image/png"))])
    assert bad.status_code == 415
    assert client.post(url, files=[("files", ("e.png", b"", "image/png"))]).status_code == 422
    assert client.get(f"/api/projects/{m['id']}").json()["screenshotCount"] == 0
    assert list(app.state.storage.directory.glob("*")) == []


def test_too_many_and_too_large(client, app):
    m = make_master(client)
    url = f"/api/projects/{m['id']}/screenshots"
    limit = app.state.settings.max_screenshots_per_prompt
    assert client.post(url, files=[("files", (f"{i}.gif", GIF, "image/gif")) for i in range(limit)]).status_code == 201
    assert client.post(url, files=[("files", ("x.gif", GIF, "image/gif"))]).status_code == 409
    big = PNG + b"\x00" * app.state.settings.max_screenshot_bytes
    m2 = make_master(client, "Second Master")
    assert client.post(f"/api/projects/{m2['id']}/screenshots", files=[("files", ("big.png", big, "image/png"))]).status_code == 413


def test_unknown_ids_404(client):
    assert client.get("/api/projects/00000000-0000-4000-8000-000000000000").status_code == 404
    assert client.get("/api/projects/not-a-uuid").status_code == 422
    assert client.post("/api/projects/00000000-0000-4000-8000-000000000000/screenshots", files=[("files", ("a.png", PNG, "image/png"))]).status_code == 404


def test_data_survives_app_restart(test_settings, client):
    m = make_master(client)
    f = make_feature(client, m["id"]).json()
    client.post(f"/api/projects/{m['id']}/screenshots", files=[("files", ("a.png", PNG, "image/png"))])
    second = create_app(test_settings)  # a brand-new app + engine, as after a backend restart
    with TestClient(second) as c2:
        ids = {p["id"] for p in c2.get("/api/projects").json()}
        assert ids == {m["id"], f["id"]}
        detail = c2.get(f"/api/projects/{m['id']}").json()
        assert len(detail["screenshots"]) == 1
        assert c2.get(detail["screenshots"][0]["url"]).content == PNG
    second.state.engine.dispose()
