"""DB-independent tests of the generation service's business rules (app/generation/rules.py + state machine + engine).

services/generations.py calls exactly these functions, so these are the production code paths, not a re-implementation.
What they cannot show: row locking, concurrent requests, JSON/CHAR(32) round-trips, CHECK constraints and
`recover_interrupted`. Those stay in the MySQL-backed tests of test_generation_api.py (skipped until MySQL is usable).
"""
import uuid

import pytest

from app.config import Settings
from app.errors import AppError
from app.generation import rules
from app.generation.engine import EngineInput
from app.generation.providers import ImageInput, ProviderError
from app.generation.sandbox import BuildResult
from app.generation.state import ACTIVE_VALUES, ALL_STATUSES, TRANSITIONS, Status, can_transition
from tests.test_generation_logic import GOOD, FakeProvider, FakeRunner, model_reply, run

S = Settings()
A, B = uuid.uuid4(), uuid.uuid4()


def code_of(fn, *args):
    with pytest.raises(AppError) as e:
        fn(*args)
    return e.value.status_code, e.value.code


# ---------- request validation ----------

def test_request_modes_and_normalisation():
    assert rules.normalize_request(S, "  hi  ", [], None) == ("hi", [], "prompt_only")
    assert rules.normalize_request(S, "  ", [A], None) == (None, [A], "screenshot_only")
    assert rules.normalize_request(S, "hi", [A, A, B], None)[1:] == ([A, B], "prompt_and_screenshot")  # de-duplicated, order kept


def test_request_rejections():
    assert code_of(rules.normalize_request, S, None, [], None) == (422, "no_input")
    assert code_of(rules.normalize_request, S, "   ", [], None) == (422, "no_input")
    assert code_of(rules.normalize_request, S, "x" * (S.max_prompt_chars + 1), [], None) == (422, "prompt_too_long")
    many = [uuid.uuid4() for _ in range(S.max_screenshots_per_prompt + 1)]
    assert code_of(rules.normalize_request, S, "x", many, None) == (422, "too_many_screenshots")
    # a refinement is a change request: a screenshot alone cannot describe it
    assert code_of(rules.normalize_request, S, None, [A], uuid.uuid4()) == (422, "refinement_needs_prompt")
    assert rules.normalize_request(S, "tweak", [], uuid.uuid4())[0] == "tweak"


# ---------- cross-project isolation ----------

def test_screenshots_must_all_belong_to_the_project():
    rules.assert_screenshots_in_project([A, B], {A, B})
    assert code_of(rules.assert_screenshots_in_project, [A, B], {A}) == (422, "screenshot_not_in_project")
    assert code_of(rules.assert_screenshots_in_project, [A], set()) == (422, "screenshot_not_in_project")


def test_refinement_parent_must_exist_in_same_project():
    p, other = uuid.uuid4(), uuid.uuid4()
    rules.assert_parent_in_project(p, p)
    assert code_of(rules.assert_parent_in_project, other, p) == (404, "version_not_found")  # another project's version
    assert code_of(rules.assert_parent_in_project, None, p) == (404, "version_not_found")  # no such version


# ---------- one active session ----------

def test_only_one_active_session_per_project_rule():
    rules.assert_no_active_session(0)
    assert code_of(rules.assert_no_active_session, 1) == (409, "generation_in_progress")
    assert code_of(rules.assert_no_active_session, 2) == (409, "generation_in_progress")


def test_active_states_are_exactly_the_non_terminal_ones():
    assert set(ACTIVE_VALUES) == {"created", "queued", "analyzing", "generating", "building", "repairing"}
    assert "failed" not in ACTIVE_VALUES and "completed" not in ACTIVE_VALUES  # a failed session frees the project


# ---------- retry rules ----------

def test_retry_rules():
    rules.assert_retryable("failed", True, 1, 3)
    rules.assert_retryable("failed", True, 2, 3)
    assert code_of(rules.assert_retryable, "failed", True, 3, 3) == (409, "retry_limit_reached")
    assert code_of(rules.assert_retryable, "failed", True, 9, 3) == (409, "retry_limit_reached")
    assert code_of(rules.assert_retryable, "failed", False, 1, 3) == (409, "not_retryable")
    for st in ALL_STATUSES:
        if st != "failed":  # only a failed session can be retried, even if the flag were set
            assert code_of(rules.assert_retryable, st, True, 1, 3) == (409, "not_retryable")


def test_retry_limit_follows_settings():
    assert code_of(rules.assert_retryable, "failed", True, 1, 1) == (409, "retry_limit_reached")
    assert Settings().max_session_attempts == 3


def test_retryable_flags_of_engine_failures(tmp_path):
    """Which failures the engine marks retryable (this value is what the service stores and retry_session checks)."""
    cases = [
        (ProviderError("ai_rate_limited", "slow", True), True),
        (ProviderError("ai_unavailable", "down", True), True),
        (ProviderError("ai_auth_failed", "bad token", False), False),
        (ProviderError("ai_request_rejected", "no", False), False),
        (ProviderError("ai_output_truncated", "cut", False), False),
    ]
    for exc, expected in cases:
        res, _ = run(EngineInput("x", []), FakeProvider(exc), FakeRunner(), tmp_path / exc.code)
        assert not res.ok and res.error_code == exc.code and res.retryable is expected, exc.code
    # rule-violating artifacts are never retryable: the same request would fail the same way
    bad = model_reply({"src/App.tsx": "import x from 'axios'\nexport default function A(){return null}\n"})
    res, _ = run(EngineInput("x", []), FakeProvider(bad, bad), FakeRunner(), tmp_path / "bad")
    assert res.error_code == "invalid_artifact" and not res.retryable
    res, _ = run(EngineInput("x", []), FakeProvider("prose", "prose"), FakeRunner(), tmp_path / "prose")
    assert res.error_code == "malformed_model_output" and res.retryable


# ---------- lifecycle transitions ----------

def test_transition_table_is_complete_and_terminal_states_hold():
    assert set(TRANSITIONS) == set(Status)
    assert TRANSITIONS[Status.COMPLETED] == frozenset()  # completed is terminal, no retry or failure afterwards
    assert TRANSITIONS[Status.FAILED] == {Status.QUEUED}
    assert not any(can_transition(s, "completed") for s in ALL_STATUSES if s != "building")  # completion only after a build


def test_every_engine_stage_sequence_is_a_legal_lifecycle(tmp_path):
    """Replays the stages the real engine emits and checks each hop against the production transition table."""
    ok, fail = BuildResult(True, "ok"), BuildResult(False, "err", "build_failed")
    png = [ImageInput("image/png", b"\x89PNG" + b"1" * 8, "a.png")]
    scenarios = {
        "prompt ok": (EngineInput("x", []), [model_reply(GOOD)], [ok]),
        "screenshot ok": (EngineInput(None, png), [model_reply(GOOD)], [ok]),
        "repair ok": (EngineInput("x", []), [model_reply(GOOD)] * 2, [fail, ok]),
        "repair exhausted": (EngineInput("x", []), [model_reply(GOOD)] * 2, [fail, fail]),
        "provider error": (EngineInput("x", []), [ProviderError("ai_unavailable", "d", True)], []),
        "invalid artifact": (EngineInput("x", []), ["prose"] * 2, []),
    }
    for name, (inp, replies, builds) in scenarios.items():
        res, stages = run(inp, FakeProvider(*replies), FakeRunner(*builds), tmp_path / name.replace(" ", "_"))
        path = ["queued", *stages, "completed" if res.ok else "failed"]
        for a, b in zip(path, path[1:]):
            assert can_transition(a, b), (name, a, b, path)
        if res.ok:
            assert path[-2] == "building", name  # success is only ever recorded straight after a successful build


# ---------- versions ----------

def test_version_numbers_are_per_project_sequential():
    assert rules.next_version_number(None) == 1
    assert rules.next_version_number(0) == 1
    assert rules.next_version_number(1) == 2
    assert rules.next_version_number(41) == 42


def test_failed_generation_yields_no_files_for_a_version(tmp_path):
    """The service creates a version only from a successful result; a failed result carries no files at all."""
    fail = BuildResult(False, "err", "build_failed")
    res, _ = run(EngineInput("x", []), FakeProvider(*[model_reply(GOOD)] * 2), FakeRunner(fail, fail), tmp_path)
    assert not res.ok and res.files == {}
    res, _ = run(EngineInput("x", []), FakeProvider(ProviderError("ai_timeout", "t", True)), FakeRunner(), tmp_path / "p")
    assert not res.ok and res.files == {}


def test_failed_refinement_leaves_parent_source_untouched(tmp_path):
    parent = dict(GOOD)
    snapshot = dict(parent)
    fail = BuildResult(False, "err", "build_failed")
    reply = model_reply({**GOOD, "src/app.css": "x{}\n"})
    res, _ = run(EngineInput("tweak", [], existing_files=parent), FakeProvider(reply, reply), FakeRunner(fail, fail), tmp_path)
    assert not res.ok and res.files == {} and parent == snapshot  # input not mutated, nothing to store


def test_project_status_after_failure_preserves_ready_state():
    assert rules.project_status_after_failure(True, "build_failed") == "ready"  # earlier version still previewable
    assert rules.project_status_after_failure(True, "ai_unavailable") == "ready"
    assert rules.project_status_after_failure(False, "build_failed") == "build_failed"
    assert rules.project_status_after_failure(False, "build_timeout") == "build_failed"
    assert rules.project_status_after_failure(False, "invalid_build_output") == "build_failed"
    assert rules.project_status_after_failure(False, "ai_unavailable") == "draft"
    assert rules.project_status_after_failure(False, "interrupted") == "draft"


def test_version_manifest_lists_every_file_with_utf8_size():
    m = rules.version_manifest({"src/b.css": "é", "src/App.tsx": "abc"})
    assert m == [{"path": "src/App.tsx", "size": 3}, {"path": "src/b.css", "size": 2}]
