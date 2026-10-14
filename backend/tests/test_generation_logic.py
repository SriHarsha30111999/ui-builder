"""Database-independent Phase 3 tests: state machine, artifact validation, provider HTTP mapping, engine, sandbox flags.

The model provider and build runner are test doubles here. These tests prove the orchestration logic; they do NOT prove
that a real model or a real sandboxed build works (see docs/PHASE3.md).
"""
import json
import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.generation import artifact, prompts, sandbox
from app.generation.engine import EngineInput, run_generation
from app.generation.providers import ImageInput, OpenAICompatibleProvider, ProviderError
from app.generation.sandbox import BuildResult, DisabledRunner
from app.generation.state import InvalidTransition, assert_transition, can_transition, input_mode
from app.generation.storage import GeneratedStorage, GeneratedStorageError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 32
APP = "import './app.css'\nexport default function App() { return <main><h1>Hello</h1></main> }\n"


def model_reply(files: dict[str, str], summary="Built it.", unc="") -> str:
    out = f"=== SUMMARY ===\n{summary}\n=== UNCERTAINTIES ===\n{unc}\n"
    for p, c in files.items():
        out += f"=== FILE: {p} ===\n{c}\n"
    return out


GOOD = {"src/App.tsx": APP, "src/app.css": "main { color: navy; }\n"}


# ---------------- state machine ----------------

def test_valid_transitions():
    for a, b in [("created", "queued"), ("queued", "generating"), ("queued", "analyzing"), ("generating", "building"),
                 ("analyzing", "building"), ("building", "repairing"), ("repairing", "building"), ("building", "completed"),
                 ("building", "failed"), ("failed", "queued")]:
        assert can_transition(a, b), (a, b)


def test_invalid_transitions_rejected():
    for a, b in [("completed", "queued"), ("completed", "failed"), ("created", "completed"), ("queued", "completed"),
                 ("generating", "completed"), ("failed", "completed"), ("repairing", "completed"), ("queued", "bogus")]:
        assert not can_transition(a, b), (a, b)
    with pytest.raises(InvalidTransition):
        assert_transition("generating", "completed")  # completion requires a build step first


def test_input_mode_rules():
    assert input_mode("hi", 0) == "prompt_only"
    assert input_mode(None, 2) == "screenshot_only"
    assert input_mode("  ", 1) == "screenshot_only"
    assert input_mode("hi", 1) == "prompt_and_screenshot"
    with pytest.raises(ValueError):
        input_mode("   ", 0)


# ---------------- parsing and validation ----------------

def test_parse_extracts_files_summary_uncertainties():
    p = artifact.parse_model_output(model_reply(GOOD, "A page.", "- logo unreadable\n- font unknown"))
    assert set(p.files) == set(GOOD) and p.summary == "A page."
    assert p.uncertainties == ["logo unreadable", "font unknown"]


def test_parse_rejects_description_without_files():
    with pytest.raises(artifact.ArtifactError) as e:
        artifact.parse_model_output("Here is a nice dashboard design, with a sidebar...")
    assert e.value.code == "malformed_model_output"


def test_parse_tolerates_code_fences():
    p = artifact.parse_model_output("=== FILE: src/App.tsx ===\n```tsx\nexport default function A(){return null}\n```\n")
    assert p.files["src/App.tsx"].startswith("export default")


def v(files):
    artifact.validate_files(files, 30, 200_000)


def test_valid_artifact_passes():
    v(GOOD)


@pytest.mark.parametrize("bad", ["../evil.tsx", "/etc/passwd", "src/../x.tsx", "src\\a.tsx", "package.json", "vite.config.ts",
                                 "src/main.tsx", "src/vite-env.d.ts", "src/a.js", "public/x.tsx", "src//a.tsx"])
def test_bad_paths_rejected(bad):
    with pytest.raises(artifact.ArtifactError):
        v({"src/App.tsx": APP, bad: "export {}\n"})


def test_requires_app_with_default_export():
    with pytest.raises(artifact.ArtifactError):
        v({"src/other.tsx": "export {}\n"})
    with pytest.raises(artifact.ArtifactError):
        v({"src/App.tsx": "export const App = () => null\n"})


@pytest.mark.parametrize("code", [
    "fetch('https://x.test')", "new XMLHttpRequest()", "new WebSocket('ws://x')", "eval('1')", "new Function('1')",
    "import('./x')", "require('fs')", "navigator.sendBeacon('/x')",
])
def test_forbidden_apis_rejected(code):
    with pytest.raises(artifact.ArtifactError):
        v({"src/App.tsx": f"export default function A(){{ {code}; return null }}\n"})


def test_imports_limited_to_react_and_existing_files():
    with pytest.raises(artifact.ArtifactError):
        v({"src/App.tsx": "import axios from 'axios'\nexport default function A(){return null}\n"})
    with pytest.raises(artifact.ArtifactError):
        v({"src/App.tsx": "import X from './missing'\nexport default function A(){return null}\n"})
    with pytest.raises(artifact.ArtifactError):
        v({"src/App.tsx": "import X from '../../outside'\nexport default function A(){return null}\n"})
    v({"src/App.tsx": "import { useState } from 'react'\nimport B from './components/B'\nexport default function A(){return <B/>}\n",
       "src/components/B.tsx": "export default function B(){return null}\n"})


def test_css_external_resources_rejected():
    with pytest.raises(artifact.ArtifactError):
        v({**GOOD, "src/app.css": "@import url('https://x.test/a.css');"})
    with pytest.raises(artifact.ArtifactError):
        v({**GOOD, "src/app.css": "a{background:url(https://x.test/a.png)}"})


def test_limits_enforced():
    with pytest.raises(artifact.ArtifactError):
        artifact.validate_files({"src/App.tsx": APP, **{f"src/f{i}.ts": "export {}\n" for i in range(5)}}, 3, 1000)
    with pytest.raises(artifact.ArtifactError):
        artifact.validate_files({"src/App.tsx": APP + "x" * 5000}, 30, 1000)


def test_scaffold_cannot_be_overridden_and_has_no_deps_beyond_react():
    files = artifact.assemble_workspace_files(GOOD)
    assert files["package.json"] == artifact.SCAFFOLD["package.json"]
    deps = json.loads(files["package.json"])["dependencies"]
    assert set(deps) == {"react", "react-dom"}


# ---------------- provider (HTTP mapped with httpx.MockTransport) ----------------

def provider_with(handler, key="secret-token-123"):
    return OpenAICompatibleProvider("https://api.test/v1", key, 5, 100, client=httpx.Client(transport=httpx.MockTransport(handler)))


def ok_handler(captured):
    def h(request: httpx.Request):
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "reply"}}]})
    return h


def test_provider_sends_prompt_and_image_bytes_to_endpoint():
    cap: list[httpx.Request] = []
    p = provider_with(ok_handler(cap))
    out = p.complete(model="vision-x", system="sys", user_text="make it blue", images=[ImageInput("image/png", PNG, "a.png")])
    assert out == "reply"
    body = json.loads(cap[0].content)
    assert body["model"] == "vision-x"
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "make it blue"}
    import base64
    assert parts[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()  # real image bytes
    assert cap[0].headers["authorization"] == "Bearer secret-token-123"


def test_provider_prompt_only_has_no_image_part():
    cap: list[httpx.Request] = []
    provider_with(ok_handler(cap)).complete(model="m", system="s", user_text="hello", images=[])
    assert len(json.loads(cap[0].content)["messages"][1]["content"]) == 1


@pytest.mark.parametrize("status,code,retryable", [(401, "ai_auth_failed", False), (403, "ai_auth_failed", False),
                                                    (429, "ai_rate_limited", True), (503, "ai_unavailable", True),
                                                    (400, "ai_request_rejected", False)])
def test_provider_error_mapping_no_secret_or_body_leak(status, code, retryable):
    p = provider_with(lambda r: httpx.Response(status, text="echo secret-token-123 and internals"))
    with pytest.raises(ProviderError) as e:
        p.complete(model="m", system="s", user_text="x", images=[])
    assert (e.value.code, e.value.retryable) == (code, retryable)
    assert "secret-token-123" not in e.value.message and "internals" not in e.value.message


def test_provider_timeout_and_bad_payload():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)
    with pytest.raises(ProviderError) as e:
        provider_with(boom).complete(model="m", system="s", user_text="x", images=[])
    assert e.value.code == "ai_timeout" and e.value.retryable
    with pytest.raises(ProviderError) as e2:
        provider_with(lambda r: httpx.Response(200, json={"nope": 1})).complete(model="m", system="s", user_text="x", images=[])
    assert e2.value.code == "ai_bad_response"


def test_provider_without_key_makes_no_request():
    called = []
    p = provider_with(lambda r: called.append(r) or httpx.Response(200), key="  ")
    with pytest.raises(ProviderError) as e:
        p.complete(model="m", system="s", user_text="x", images=[])
    assert e.value.code == "ai_not_configured" and not called


def test_api_key_is_secret_in_settings_repr():
    s = Settings(ai_api_key=SecretStr("hf_abc123"))
    assert "hf_abc123" not in repr(s) and "hf_abc123" not in str(s.model_dump())
    assert s.ai_configured


# ---------------- engine (provider/runner doubles) ----------------

class FakeProvider:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, *, model, system, user_text, images):
        self.calls.append({"model": model, "user_text": user_text, "images": images, "system": system})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeRunner:
    name = "fake"

    def __init__(self, *results):
        self.results = list(results)
        self.built: list[Path] = []

    def availability(self):
        return True, "ok"

    def build(self, workspace):
        self.built.append(workspace)
        assert (workspace / "src" / "App.tsx").is_file() and (workspace / "package.json").is_file()
        return self.results.pop(0)


def run(inp, provider, runner, tmp_path, settings=None):
    stages: list[str] = []
    res = run_generation(inp, settings=settings or Settings(), provider=provider, runner=runner,
                         workspace=tmp_path / "ws", on_stage=stages.append)
    return res, stages


def test_prompt_only_calls_code_model_and_produces_source(tmp_path):
    prov = FakeProvider(model_reply(GOOD))
    res, stages = run(EngineInput("Create a banking dashboard", []), prov, FakeRunner(BuildResult(True, "built")), tmp_path)
    assert res.ok and set(res.files) == set(GOOD)
    assert prov.calls[0]["model"] == Settings().ai_code_model and prov.calls[0]["images"] == []
    assert "Create a banking dashboard" in prov.calls[0]["user_text"]
    assert stages == ["generating", "building"]  # no screenshot-analysis stage in prompt-only mode
    assert (tmp_path / "ws" / "package.json").is_file()


def test_different_prompts_reach_the_model_unchanged_and_output_follows_model(tmp_path):
    other = {"src/App.tsx": "export default function Shop(){return <h1>Shop</h1>}\n"}
    for prompt, files in [("a dark banking dashboard", GOOD), ("an e-commerce product page", other)]:
        prov = FakeProvider(model_reply(files))
        res, _ = run(EngineInput(prompt, []), prov, FakeRunner(BuildResult(True, "")), tmp_path)
        assert prompt in prov.calls[0]["user_text"] and res.files == {k: v.rstrip() + "\n" for k, v in files.items()}


def test_screenshot_only_uses_vision_model_and_sends_image(tmp_path):
    prov = FakeProvider(model_reply(GOOD, unc="- small text unreadable"))
    img = ImageInput("image/png", PNG, "ui.png")
    res, stages = run(EngineInput(None, [img]), prov, FakeRunner(BuildResult(True, "")), tmp_path)
    assert res.ok and prov.calls[0]["model"] == Settings().ai_vision_model
    assert prov.calls[0]["images"] == [img]
    assert stages[0] == "analyzing" and res.uncertainties == ["small text unreadable"]
    assert len(prov.calls) == 1  # one model call, not a separate analysis + generation


def test_prompt_and_screenshot_sends_both_and_marks_reference(tmp_path):
    prov = FakeProvider(model_reply(GOOD))
    img = ImageInput("image/png", PNG, "ui.png")
    run(EngineInput("change the theme to blue and add a search bar", [img]), prov, FakeRunner(BuildResult(True, "")), tmp_path)
    text = prov.calls[0]["user_text"]
    assert "change the theme to blue and add a search bar" in text and "VISUAL REFERENCE" in text
    assert prov.calls[0]["images"] == [img]


def test_refinement_includes_existing_source_and_distinguishes_reference(tmp_path):
    prov = FakeProvider(model_reply(GOOD))
    existing = {"src/App.tsx": "export default function Old(){return <b>OLD_MARKER</b>}\n"}
    res, _ = run(EngineInput("Move the sidebar to the right", [], existing), prov, FakeRunner(BuildResult(True, "")), tmp_path)
    text = prov.calls[0]["user_text"]
    assert res.ok and "OLD_MARKER" in text and "REFINEMENT" in text and "Move the sidebar to the right" in text
    t2 = prompts.build_user_text(prompt="x", image_count=1, existing_files=existing)
    assert "visual guidance only" in t2


def test_build_failure_triggers_one_repair_then_succeeds(tmp_path):
    prov = FakeProvider(model_reply(GOOD), model_reply({**GOOD, "src/app.css": "main{color:red}\n"}))
    runner = FakeRunner(BuildResult(False, "src/App.tsx(1,1): error TS2304", "build_failed"), BuildResult(True, "ok"))
    res, stages = run(EngineInput("x", []), prov, runner, tmp_path)
    assert res.ok and res.repaired and stages == ["generating", "building", "repairing", "building"]
    assert "TS2304" in prov.calls[1]["user_text"] and len(prov.calls) == 2


def test_repair_is_bounded_and_failure_is_genuine(tmp_path):
    prov = FakeProvider(model_reply(GOOD), model_reply(GOOD), model_reply(GOOD))
    fail = BuildResult(False, "error", "build_failed")
    res, _ = run(EngineInput("x", []), prov, FakeRunner(fail, fail, fail), tmp_path)
    assert not res.ok and res.error_code == "build_failed" and res.files == {}
    assert len(prov.calls) == 2  # original + exactly one repair


def test_invalid_model_files_never_reach_the_build(tmp_path):
    bad = model_reply({"src/App.tsx": "import x from 'axios'\nexport default function A(){return null}\n"})
    prov = FakeProvider(bad, bad)
    runner = FakeRunner()
    res, _ = run(EngineInput("x", []), prov, runner, tmp_path)
    assert not res.ok and res.error_code == "invalid_artifact" and runner.built == []


def test_unavailable_sandbox_is_failure_not_success(tmp_path):
    prov = FakeProvider(model_reply(GOOD))
    res, _ = run(EngineInput("x", []), prov, DisabledRunner(), tmp_path)
    assert not res.ok and res.error_code == "sandbox_unavailable" and len(prov.calls) == 1


def test_provider_failure_is_reported_accurately(tmp_path):
    prov = FakeProvider(ProviderError("ai_rate_limited", "slow down", True))
    res, _ = run(EngineInput("x", []), prov, FakeRunner(), tmp_path)
    assert not res.ok and res.error_code == "ai_rate_limited" and res.retryable


def test_malformed_output_is_reported(tmp_path):
    prov = FakeProvider("just prose", "still prose")
    res, _ = run(EngineInput("x", []), prov, FakeRunner(), tmp_path)
    assert not res.ok and res.error_code == "malformed_model_output" and res.retryable


# ---------------- sandbox flags and dist checks ----------------

def test_docker_command_isolation_flags(tmp_path):
    s = Settings()
    cmd = sandbox.docker_command(s, tmp_path, "sb-test")
    joined = " ".join(cmd)
    for flag in ["--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--pids-limit", "--memory", "--cpus", "--user 1000:1000"]:
        assert flag in joined, flag
    assert "--privileged" not in cmd and "/var/run/docker.sock" not in joined
    assert cmd.count("-v") == 1 and cmd[cmd.index("-v") + 1] == f"{tmp_path}:/work:rw"
    assert cmd[-3:] == sandbox.CONTAINER_COMMAND  # fixed command, no user-controlled text


def test_prompt_never_appears_in_docker_argv(tmp_path):
    assert "ignore previous; rm -rf /" not in " ".join(sandbox.docker_command(Settings(), tmp_path, "n"))


def test_disabled_runner_refuses():
    ok, reason = DisabledRunner().availability()
    assert not ok and "sandbox" in reason.lower()
    assert DisabledRunner().build(Path(".")).error_code == "sandbox_unavailable"


def test_default_runner_is_disabled():
    assert sandbox.make_runner(Settings()).name == "disabled"


def test_check_dist(tmp_path):
    assert sandbox.check_dist(tmp_path / "none", 1000)
    d = tmp_path / "dist"
    d.mkdir()
    assert "index.html" in sandbox.check_dist(d, 1000)
    (d / "index.html").write_text("<html></html>")
    assert sandbox.check_dist(d, 1000) is None
    (d / "big.js").write_text("x" * 2000)
    assert "larger" in sandbox.check_dist(d, 1000)


# ---------------- storage paths ----------------

def test_storage_paths_are_scoped_and_reject_traversal(tmp_path):
    st = GeneratedStorage(tmp_path)
    st.ensure_ready()
    pid, vid = uuid.uuid4(), uuid.uuid4()
    base = st.version_dir(pid, vid)
    (base / "dist" / "assets").mkdir(parents=True)
    (base / "src").mkdir()
    (base / "dist" / "index.html").write_text("<html/>")
    (base / "dist" / "assets" / "a.js").write_text("1")
    (base / "src" / "App.tsx").write_text("x")
    (tmp_path / "secret.txt").write_text("top secret")
    assert st.preview_file(pid, vid, "").name == "index.html"
    assert st.preview_file(pid, vid, "assets/a.js").name == "a.js"
    assert st.source_file(pid, vid, "src/App.tsx").name == "App.tsx"
    for bad in ["../../secret.txt", "..", "/etc/passwd", "assets/../../../secret.txt", "a\\b", "assets//a.js", "nope.js"]:
        with pytest.raises(GeneratedStorageError):
            st.preview_file(pid, vid, bad)
    for bad in ["../../secret.txt", "src/../../../secret.txt", "dist/index.html", "/x"]:
        with pytest.raises(GeneratedStorageError):
            st.source_file(pid, vid, bad)
    other = uuid.uuid4()
    with pytest.raises(GeneratedStorageError):  # a version is only readable under its own project
        st.preview_file(other, vid, "index.html")


def test_discard_refuses_paths_outside_root(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    GeneratedStorage(root).discard(outside)
    assert (outside / "keep.txt").exists()


def test_promote_moves_workspace(tmp_path):
    st = GeneratedStorage(tmp_path)
    st.ensure_ready()
    sid, pid, vid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ws = st.work_dir(sid)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "App.tsx").write_text("export default 1")
    target = st.promote(ws, pid, vid)
    assert (target / "src" / "App.tsx").is_file() and not ws.exists()
    assert st.read_sources(pid, vid) == {"src/App.tsx": "export default 1"}


# ---------------- audit follow-ups (all with provider/runner DOUBLES; no real model or sandbox) ----------------

def test_truncated_model_reply_is_an_error_not_a_partial_artifact():
    reply = {"choices": [{"message": {"content": "=== FILE: src/App.tsx ===\nexport default function A() {"}, "finish_reason": "length"}]}
    with pytest.raises(ProviderError) as e:
        provider_with(lambda r: httpx.Response(200, json=reply)).complete(model="m", system="s", user_text="x", images=[])
    assert e.value.code == "ai_output_truncated" and not e.value.retryable


def test_normal_finish_reason_is_accepted():
    reply = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    assert provider_with(lambda r: httpx.Response(200, json=reply)).complete(model="m", system="s", user_text="x", images=[]) == "ok"


def test_refinement_keeps_parent_files_the_model_omitted(tmp_path):
    parent = {
        "src/App.tsx": "import Card from './Card'\nexport default function App(){return <Card/>}\n",
        "src/Card.tsx": "export default function Card(){return <i>card</i>}\n",
    }
    # the model returns only App.tsx (a partial answer); Card.tsx must survive and the import must still resolve
    reply = model_reply({"src/App.tsx": "import Card from './Card'\nexport default function App(){return <main><Card/></main>}\n"})
    res, _ = run(EngineInput("wrap in main", [], parent), FakeProvider(reply), FakeRunner(BuildResult(True, "")), tmp_path)
    assert res.ok and set(res.files) == set(parent)
    assert "<main>" in res.files["src/App.tsx"] and res.files["src/Card.tsx"] == parent["src/Card.tsx"]
    assert (tmp_path / "ws" / "src" / "Card.tsx").is_file()  # what gets built includes the preserved file


def test_refinement_model_changes_override_parent_files(tmp_path):
    parent = {"src/App.tsx": "export default function Old(){return <b>old</b>}\n"}
    res, _ = run(EngineInput("change", [], parent), FakeProvider(model_reply(GOOD)), FakeRunner(BuildResult(True, "")), tmp_path)
    assert res.ok and res.files["src/App.tsx"] == APP.rstrip() + "\n"


def test_oversized_screenshots_rejected_before_any_model_call(tmp_path):
    prov = FakeProvider()
    big = ImageInput("image/png", b"x" * 2048, "big.png")
    res, stages = run(EngineInput(None, [big]), prov, FakeRunner(), tmp_path, settings=Settings(max_total_image_bytes=1024))
    assert not res.ok and res.error_code == "screenshots_too_large" and not prov.calls and stages == []


@pytest.mark.parametrize("code", [
    "window.open('https://x.test')",
    "location.href = 'https://x.test'",
    "window.location.assign('https://x.test')",
    "location = 'https://x.test'",
])
def test_navigation_apis_rejected(code):
    with pytest.raises(artifact.ArtifactError):
        artifact.validate_files({"src/App.tsx": f"export default function A(){{ {code}; return null }}\n"}, 30, 200_000)


def test_comparison_with_location_name_is_not_flagged():
    artifact.validate_files({"src/App.tsx": "export default function A(){ const location = 'a'; return <p>{location === 'a' ? 'x' : 'y'}</p> }\n"}, 30, 200_000)


def test_ensure_ready_clears_stale_scratch_but_keeps_versions(tmp_path):
    st = GeneratedStorage(tmp_path)
    st.ensure_ready()
    stale = st.work_dir(uuid.uuid4())
    stale.mkdir(parents=True)
    (stale / "leftover").write_text("x")
    pid, vid = uuid.uuid4(), uuid.uuid4()
    (st.version_dir(pid, vid) / "src").mkdir(parents=True)
    st.ensure_ready()
    assert not stale.exists() and (st.version_dir(pid, vid) / "src").is_dir()


def test_failed_refinement_returns_no_files_so_parent_is_untouched(tmp_path):
    parent = {"src/App.tsx": "export default function Old(){return <b>old</b>}\n"}
    res, _ = run(EngineInput("change", [], parent), FakeProvider(ProviderError("ai_unavailable", "down", True)), FakeRunner(), tmp_path)
    assert not res.ok and res.files == {} and res.error_code == "ai_unavailable"
