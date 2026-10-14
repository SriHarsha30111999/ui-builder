"""Host build runner (BUILD_RUNNER=host): command shape, environment scrubbing, opt-in, and one real build of a platform fixture."""
import shutil
import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.generation import sandbox
from app.generation.artifact import assemble_workspace_files
from app.generation.sandbox import HostRunner, host_commands, host_env, make_runner


def test_host_is_opt_in_only():
    assert isinstance(make_runner(Settings(build_runner="host")), HostRunner)
    assert make_runner(Settings()).name == "disabled"


def test_host_commands_are_argv_lists_without_shell_text(tmp_path):
    cmds = host_commands("node", tmp_path)
    assert all(isinstance(c, list) for c in cmds)
    assert "--noEmit" in cmds[0] and "build" in cmds[1] and "runner" in cmds[1]
    assert not any(a in ("sh", "cmd", "-c", "/c") for c in cmds for a in c)


def test_host_env_never_carries_secrets(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "secret-value")
    monkeypatch.setenv("DB_PASSWORD", "secret-value")
    env = host_env()
    assert "secret-value" not in " ".join(env.values())
    assert "AI_API_KEY" not in env and "DB_PASSWORD" not in env


def test_host_unavailable_without_toolchain(tmp_path):
    ok, reason = HostRunner(Settings(build_runner="host", build_toolchain_dir=tmp_path)).availability()
    assert not ok and "lacks" in reason


def _real_runner():
    r = HostRunner(Settings(build_runner="host"))
    return r if r.availability()[0] else None


@pytest.mark.skipif(_real_runner() is None, reason="node toolchain not available")
def test_real_host_build_of_fixture_and_type_error_is_reported():
    runner = _real_runner()
    root = Path(__file__).resolve().parents[1] / "storage" / "generated" / "_work" / f"test-{uuid.uuid4().hex[:8]}"
    try:
        files = assemble_workspace_files({"src/App.tsx": "export default function App() { return <h1>Hello</h1> }\n"})
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        res = runner.build(root)
        assert res.ok, res.log
        assert (root / "dist" / "index.html").is_file()

        (root / "src" / "App.tsx").write_text("const n: number = 'x'\nexport default function App() { return <h1>{n}</h1> }\n", encoding="utf-8")
        shutil.rmtree(root / "dist")
        bad = runner.build(root)
        assert not bad.ok and bad.error_code == "build_failed"
    finally:
        shutil.rmtree(root, ignore_errors=True)
