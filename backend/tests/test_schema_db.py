"""Inspects the real MySQL schema produced by the Alembic migration."""
from sqlalchemy import text


def _rows(engine, sql, **params):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params).all()]


def test_server_is_mysql_with_enforced_checks(raw_engine):
    version = _rows(raw_engine, "SELECT VERSION()")[0][0]
    major, minor, patch = (int(p) for p in version.split("-")[0].split(".")[:3])
    assert (major, minor, patch) >= (8, 0, 16), f"CHECK constraints are not enforced on {version}"


def test_expected_constraints_exist(raw_engine):
    rows = _rows(
        raw_engine,
        "SELECT table_name, constraint_name, constraint_type FROM information_schema.table_constraints "
        "WHERE table_schema = DATABASE()",
    )
    found = {(t, n): kind for t, n, kind in rows}
    expected = {
        ("projects", "pk_projects"): "PRIMARY KEY",
        ("projects", "uq_projects_id_type"): "UNIQUE",
        ("projects", "uq_projects_master_base_path"): "UNIQUE",
        ("projects", "fk_projects_master_is_master"): "FOREIGN KEY",
        ("projects", "ck_projects_type_valid"): "CHECK",
        ("projects", "ck_projects_status_valid"): "CHECK",
        ("projects", "ck_projects_master_feature_shape"): "CHECK",
        ("screenshots", "pk_screenshots"): "PRIMARY KEY",
        ("screenshots", "uq_screenshots_storage_key"): "UNIQUE",
        ("screenshots", "fk_screenshots_project_id_projects"): "FOREIGN KEY",
        ("screenshots", "ck_screenshots_size_positive"): "CHECK",
    }
    for key, kind in expected.items():
        assert found.get(key) == kind, f"missing or wrong: {key} (found {found.get(key)})"


def test_composite_fk_targets_id_and_type(raw_engine):
    cols = _rows(
        raw_engine,
        "SELECT column_name, referenced_column_name FROM information_schema.key_column_usage "
        "WHERE table_schema = DATABASE() AND constraint_name = 'fk_projects_master_is_master' ORDER BY ordinal_position",
    )
    assert cols == [("master_id", "id"), ("master_type", "type")]


def test_engine_is_innodb_and_paths_are_case_sensitive(raw_engine):
    rows = _rows(
        raw_engine,
        "SELECT table_name, engine FROM information_schema.tables WHERE table_schema = DATABASE() "
        "AND table_name IN ('projects','screenshots')",
    )
    assert {e for _, e in rows} == {"InnoDB"}
    coll = _rows(
        raw_engine,
        "SELECT collation_name FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name='projects' AND column_name='base_path'",
    )
    assert coll == [("utf8mb4_bin",)]


def test_timestamps_are_utc_and_timezone_aware_in_api(client):
    r = client.post("/api/projects", json={"name": "Core Banking", "type": "master"})
    assert r.status_code == 201
    created = r.json()["createdAt"]
    assert created.endswith("Z") or created.endswith("+00:00"), created
