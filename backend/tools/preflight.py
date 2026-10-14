"""Offline setup check: reports which configuration is missing for a real generation. Prints no secret values.

Makes NO network or database connection and starts nothing. Run:  cd backend && .venv/Scripts/python tools/preflight.py
"""
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import BACKEND_DIR, PROJECT_DIR, Settings  # noqa: E402


def preflight(s: Settings, docker_on_path: bool) -> list[tuple[str, bool, str]]:
    """(item, ok, what to do). Pure, so it is unit-tested."""
    db = (
        [("PERSISTENCE=folder (no MySQL needed)", True, "")]
        if s.persistence == "folder"
        else [
            ("DB_USER set", bool(s.db_user), "Set DB_USER in backend/.env (dedicated least-privilege MySQL account, not root)."),
            ("DB_PASSWORD set", bool(s.db_password), "Set DB_PASSWORD in backend/.env (typed locally, never in chat)."),
        ]
    )
    rows = db + [
        ("AI token set (AI_API_KEY / HF_TOKEN)", s.ai_configured, "Set AI_API_KEY in backend/.env."),
        ("BUILD_RUNNER=docker or host", s.build_runner in ("docker", "host"), "Set BUILD_RUNNER=host (unsandboxed, opt-in) or docker."),
    ]
    if s.build_runner != "host":  # the host runner needs no Docker
        rows.append(("docker on PATH", docker_on_path, "Docker Desktop (needs WSL2; IT approval) must be installed."))
    return rows

if __name__ == "__main__":
    envs = [p for p in (PROJECT_DIR / ".env", BACKEND_DIR / ".env") if p.is_file()]
    print("env files found:", [str(p) for p in envs] or "none")
    rows = preflight(Settings(), shutil.which("docker") is not None)
    for item, ok, todo in rows:
        print(f"[{'ok' if ok else 'MISSING'}] {item}" + ("" if ok else f"  -> {todo}"))
    sys.exit(0 if all(ok for _, ok, _ in rows) else 1)
