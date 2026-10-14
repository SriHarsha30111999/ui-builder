"""DB-independent regression tests for the Phase 3 security/reliability audit.

Everything here uses the production functions directly. Nothing starts Docker, calls a model, or touches MySQL, so none of it
proves the container sandbox or a real provider behaves correctly; it proves the host-side checks and headers are in place.
"""
import json
from pathlib import Path
import os
import subprocess
import uuid

import httpx
import pytest

from app.config import Settings
from app.generation import artifact, sandbox
from app.generation.artifact import ArtifactError
from app.generation.preview import content_type, preview_csp
from app.generation.providers import MAX_RESPONSE_BYTES, ProviderError
from app.generation.sandbox import DockerRunner, check_workspace_tree
from app.generation.storage import GeneratedStorage, GeneratedStorageError
from app.services.generations import InFlight, readiness
from tests.test_generation_logic import APP, GOOD, provider_with

TOKEN = "secret-token-123"


def call(provider):
    return provider.complete(model="m", system="s", user_text="u", images=[])


# ---------- generated file names (host-side writes) ----------

@pytest.mark.parametrize("path", ["src/CON.tsx", "src/nul.ts", "src/Aux.css", "src/com1.tsx", "src/LPT9.ts", "src/lib/prn.tsx"])
def test_windows_device_names_rejected(path):
    with pytest.raises(ArtifactError, match="reserved"):
        artifact.validate_files({"src/App.tsx": APP, path: "export const a = 1\n"}, 30, 200_000)


def test_names_differing_only_by_case_rejected():
    files = {"src/App.tsx": APP, "src/app.tsx": "export const a = 1\n"}
    with pytest.raises(ArtifactError, match="letter case"):
        artifact.validate_files(files, 30, 200_000)
    artifact.validate_files(GOOD, 30, 200_000)  # normal output is unaffected


# ---------- preview headers ----------

def test_preview_csp_blocks_network_and_same_origin():
    csp = preview_csp(["http://localhost:5173"])
    assert csp.startswith("sandbox allow-scripts;") and "allow-same-origin" not in csp
    for d in ("default-src 'none'", "connect-src 'none'", "base-uri 'none'", "form-action 'none'", "frame-ancestors http://localhost:5173"):
        assert d in csp
    assert "unsafe-eval" not in csp and "script-src 'self'" in csp
    assert preview_csp([]).endswith("frame-ancestors 'none'")


def test_preview_content_types_do_not_depend_on_the_os_registry():
    assert content_type("assets/index-abc.js").startswith("text/javascript")
    assert content_type("a.CSS").startswith("text/css") and content_type("index.html").startswith("text/html")
    assert content_type("x.unknown") == "application/octet-stream"  # never guessed as something executable


# ---------- storage paths and cross-project access ----------

@pytest.mark.parametrize("rel", ["../x", "a/../../x", "/etc/passwd", "a\\b", "src//App.tsx", "", "index.html:stream", "a\x00b"])
def test_preview_and_source_paths_reject_escapes(tmp_path, rel):
    st, p, v = GeneratedStorage(tmp_path), uuid.uuid4(), uuid.uuid4()
    d = st.version_dir(p, v)
    (d / "dist").mkdir(parents=True)
    (d / "dist" / "index.html").write_text("ok")
    (tmp_path / "secret.txt").write_text("secret")
    if rel:  # an empty preview path means index.html by design
        with pytest.raises(GeneratedStorageError):
            st.preview_file(p, v, rel)
    with pytest.raises(GeneratedStorageError):
        st.source_file(p, v, rel)


def test_version_files_are_scoped_to_their_own_project(tmp_path):
    st, pa, pb, v = GeneratedStorage(tmp_path), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    (st.version_dir(pa, v) / "dist").mkdir(parents=True)
    (st.version_dir(pa, v) / "dist" / "index.html").write_text("A")
    assert st.preview_file(pa, v, "index.html").read_text() == "A"
    with pytest.raises(GeneratedStorageError):
        st.preview_file(pb, v, "index.html")  # same version id under another project does not resolve
    with pytest.raises(GeneratedStorageError):
        st.read_sources(pb, v)


def test_symlinked_file_is_not_served_or_fed_back_to_the_model(tmp_path):
    st, p, v = GeneratedStorage(tmp_path), uuid.uuid4(), uuid.uuid4()
    d = st.version_dir(p, v)
    (d / "src").mkdir(parents=True)
    (d / "src" / "App.tsx").write_text(APP)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    try:
        os.symlink(outside, d / "src" / "leak.ts")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks cannot be created in this environment")
    with pytest.raises(GeneratedStorageError):
        st.source_file(p, v, "src/leak.ts")
    assert set(st.read_sources(p, v)) == {"src/App.tsx"}


def test_discard_never_leaves_the_storage_root(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    GeneratedStorage(root).discard(outside)
    assert (outside / "keep.txt").exists()


def test_build_workspace_with_symlink_is_rejected(tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("ok")
    assert check_workspace_tree(tmp_path) is None
    try:
        os.symlink(tmp_path / "dist", tmp_path / "src_link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks cannot be created in this environment")
    assert "symbolic link" in check_workspace_tree(tmp_path)
    assert "too many" in check_workspace_tree(tmp_path, max_entries=1)


# ---------- provider: secrets and bounds ----------

def test_oversized_provider_reply_is_rejected_not_buffered():
    big = b"x" * (MAX_RESPONSE_BYTES + 1)
    prov = provider_with(lambda req: httpx.Response(200, content=big), TOKEN)
    with pytest.raises(ProviderError) as e:
        call(prov)
    assert e.value.code == "ai_bad_response" and TOKEN not in e.value.message


def test_provider_does_not_follow_redirects_with_the_token():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    with pytest.raises(ProviderError) as e:
        call(provider_with(handler, TOKEN))
    assert len(seen) == 1 and e.value.code == "ai_bad_response"


def test_provider_error_bodies_and_token_never_reach_messages():
    body = {"error": f"bad key {TOKEN}"}
    for status in (400, 401, 429, 500):
        with pytest.raises(ProviderError) as e:
            call(provider_with(lambda req, s=status: httpx.Response(s, json=body), TOKEN))
        assert TOKEN not in e.value.message and "bad key" not in e.value.message and TOKEN not in repr(e.value)


def test_readiness_payload_never_contains_the_token():
    s = Settings(ai_api_key=TOKEN)
    out = readiness(s, sandbox.DisabledRunner())
    assert TOKEN not in json.dumps(out) and out["ai_configured"] is True
    assert TOKEN not in repr(s) and TOKEN not in str(s.model_dump())


# ---------- reliability ----------

def test_a_session_cannot_be_processed_twice_concurrently():
    f, sid = InFlight(), uuid.uuid4()
    assert f.claim(sid)
    assert not f.claim(sid)  # duplicate submission is refused
    assert f.claim(uuid.uuid4())  # other sessions are unaffected
    f.release(sid)
    assert f.claim(sid)  # released after completion, so a later retry can run


def test_docker_kill_failure_after_timeout_is_still_reported_as_timeout(tmp_path, monkeypatch):
    runner = DockerRunner(Settings())
    monkeypatch.setattr(runner, "availability", lambda: (True, "ok"))

    def fake_run(cmd, **kw):
        if cmd[1] == "run":
            raise subprocess.TimeoutExpired(cmd, 1)
        raise OSError("docker vanished")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    res = runner.build(tmp_path)
    assert not res.ok and res.error_code == "build_timeout"


def test_docker_command_mounts_only_the_workspace_and_has_no_shell_text(tmp_path):
    cmd = sandbox.docker_command(Settings(), tmp_path, "sb-x")
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert mounts == [f"{tmp_path}:/work:rw"]
    assert "--privileged" not in cmd and "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
    assert cmd[-3:] == sandbox.CONTAINER_COMMAND


def test_container_build_command_does_not_need_a_writable_node_modules():
    """Vite's default config loader writes <nearest node_modules>/.vite-temp, which is read-only in the sandbox image."""
    joined = " ".join(sandbox.CONTAINER_COMMAND)
    assert "--configLoader runner" in joined and "--noEmit" in joined
    assert "--read-only" in sandbox.docker_command(Settings(), Path("/w"), "n")


def test_preview_headers_bundle_is_what_the_route_sends():
    from app.generation.preview import preview_headers

    h = preview_headers(["http://localhost:5173"], "assets/index-abc.js")
    assert h["Content-Type"].startswith("text/javascript") and h["X-Content-Type-Options"] == "nosniff"
    assert h["Cache-Control"] == "no-store" and h["Access-Control-Allow-Origin"] == "*"
    assert h["Content-Security-Policy"] == preview_csp(["http://localhost:5173"])


def test_preflight_reports_missing_setup_without_printing_secrets(capsys):
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from preflight import preflight

    rows = preflight(Settings(db_user="", db_password="", ai_api_key="", build_runner="disabled"), False)
    assert [ok for _, ok, _ in rows] == [False] * 5
    folder = preflight(Settings(persistence="folder", ai_api_key="", build_runner="disabled"), False)
    assert [ok for _, ok, _ in folder] == [True, False, False, False]  # folder mode does not ask for MySQL credentials
    rows = preflight(Settings(db_user="u", db_password=TOKEN, ai_api_key=TOKEN, build_runner="docker"), True)
    assert all(ok for _, ok, _ in rows) and TOKEN not in repr(rows)
