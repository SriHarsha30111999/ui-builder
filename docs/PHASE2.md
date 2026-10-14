# Phase 2 — Local MySQL 8.0, FastAPI backend and project persistence

**Database:** MySQL **8.0.16 or newer** (the local MySQL 8.0 service). PostgreSQL was the original target; it was replaced by MySQL
at the project owner's explicit request for this temporary development setup. MariaDB is not supported.

**Status:** code complete and ported to MySQL. See [Verification status](#verification-status) for exactly what has and
has not been run against a live database.

## Layout

```
MyApplication/            frontend (Vite + React + TypeScript) — Phase 1 UI, API-backed
  src/api/                projectsApi.ts (contract + ApiError), httpProjectsApi.ts (only code that calls fetch)
  src/test/               fakeProjectsApi.ts — TEST DOUBLE, not used by the app
  backend/                FastAPI app
    app/                  config, db, models, schemas, errors, main (routes), services/{paths,storage,projects,screenshots}
    migrations/           Alembic (0001_initial, MySQL)
    tests/                pytest
    storage/screenshots/  uploaded files (git-ignored, created on startup)
  .env.example            placeholders only; copy to .env (git-ignored)
```

## Prerequisites and one-time setup

- Node 20+, Python 3.12, MySQL 8.0.16+ running locally (Windows service `MySQL80`, port 3306).
- A MySQL account the app can use. **The app never uses `root` implicitly and never guesses credentials.** Suggested
  (run as an administrator account, once, in `mysqlsh`/`mysql`; choose your own password):

  ```sql
  CREATE DATABASE screenshot_react_builder       CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
  CREATE DATABASE screenshot_react_builder_test  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
  CREATE USER 'ui_builder'@'localhost' IDENTIFIED BY '<choose-a-password>';
  GRANT ALL PRIVILEGES ON screenshot_react_builder.*       TO 'ui_builder'@'localhost';
  GRANT ALL PRIVILEGES ON screenshot_react_builder_test.*  TO 'ui_builder'@'localhost';
  ```
  The grants are limited to these two databases, so the app and its tests cannot touch any other database.
  (The test suite creates the `_test` database itself only if the account has `CREATE` on it; creating it up front is simpler.)
- `copy .env.example .env`, then set `DB_USER` and `DB_PASSWORD`. Never commit `.env`.
- Backend: `cd backend`, `python -m venv .venv`, `.venv\Scripts\pip install -r requirements-dev.txt`,
  `.venv\Scripts\python -m alembic upgrade head`.
- Frontend: `npm install` in the workspace root.

## Run

```
cd backend && .venv\Scripts\python -m uvicorn app.main:app --port 8000     # API on http://localhost:8000
npm run dev                                                                  # UI  on http://localhost:5173
```

- `GET /api/health` — process is up. `GET /api/health/ready` — database reachable (503 `database_unavailable` otherwise).
- Transient lock contention (deadlock / lock timeout) returns 503 `database_busy`; the project-create path retries it internally first.
- If the backend or database is down, the UI shows an explicit error; it never shows an empty list or fake success.
- CORS allows only `CORS_ORIGINS` (default `http://localhost:5173,http://127.0.0.1:5173`).
- The frontend reads `VITE_API_BASE_URL` (default `http://localhost:8000/api`).

## Configuration (env vars)

`DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD TEST_DB_NAME CORS_ORIGINS SCREENSHOT_STORAGE_DIR MAX_SCREENSHOT_BYTES MAX_SCREENSHOTS_PER_PROJECT VITE_API_BASE_URL`.
Passwords are never logged or returned by the API.

## API

Unchanged by the database switch. See the table below.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/projects` | list, newest first, with `masterName`, `screenshotCount` |
| POST | `/api/projects` | create Master or Feature (JSON, camelCase) |
| GET | `/api/projects/{id}` | project + screenshot metadata |
| POST | `/api/projects/{id}/screenshots` | multipart `files`; all-or-nothing |
| GET | `/api/projects/{id}/screenshots` | metadata list |
| GET | `/api/screenshots/{id}` | image bytes (`nosniff`) |
| GET | `/api/screenshots/{id}/metadata` | metadata |
| DELETE | `/api/screenshots/{id}` | used by the Builder "Remove"/"Replace" |

Errors: `{"error": {"code", "message", "fields"}}` — 404 not found, 409 duplicate path / too many screenshots,
413 too large, 415 not an image, 422 validation, 503 database unavailable/busy. No stack traces are returned.

## Schema (migration `0001`, InnoDB, utf8mb4)

- `projects`: Master = no parent, no base path; Feature = parent Master + base path (CHECK `master_feature_shape`).
  Composite FK `(master_id, master_type) → projects(id, type)` with `master_type = 'master'` means the database itself rejects a
  Feature whose parent is not a Master. `UNIQUE (master_id, base_path)` makes per-Master path uniqueness concurrency-safe; the API
  maps the duplicate-key error (1062) to 409 by reading the constraint name from the error message.
- `screenshots`: metadata only (`storage_key` unique, `size_bytes > 0`); files live on local disk.

### MySQL-specific decisions

| Topic | Decision |
|---|---|
| UUIDs | SQLAlchemy `Uuid` → `CHAR(32)`, generated by the app |
| Timestamps | `DATETIME(6)`, server default `CURRENT_TIMESTAMP(6)` (`updated_at` also `ON UPDATE`); connections run in UTC and values are returned timezone-aware so JSON carries an explicit offset |
| CHECK constraints | Enforced only from 8.0.16. The migration refuses to run on older servers/MariaDB, and a test asserts violations are rejected |
| Collation | Tables default to `utf8mb4_0900_ai_ci` (case/accent-insensitive); `base_path` and `storage_key` use `utf8mb4_bin` so uniqueness is exact |
| `initial_prompt` | `TEXT` cannot have a literal default in MySQL; the application always supplies it |
| SQL mode | `STRICT_ALL_TABLES` is added per connection so bad data errors instead of truncating |
| Migrations | MySQL DDL is not transactional: a migration that fails part-way is **not** rolled back and may need manual cleanup |
| Screenshot limit | Project row is locked (`SELECT … FOR UPDATE`), then existing screenshots are counted with a *locking* read, so concurrent uploads cannot exceed the limit under REPEATABLE READ |
| Deadlocks | Project creation retries up to 3 times on deadlock / lock-wait timeout |

## Base-path rules (backend authoritative, mirrored in the UI)

Trim, lowercase, collapse `//`, one leading `/`, no trailing `/`. Segments `[a-z0-9]+([-_][a-z0-9]+)*`. No whitespace or
`? # : * \ %`. `/` rejected. Max 100. Reserved (exact or prefix): `/login /logout /dashboard /auth /api /404`.
Unique per Master; the same path may exist under different Masters.

## Screenshots

Local filesystem + MySQL metadata. Type is decided from file bytes (PNG/JPEG/GIF/WebP), not the client MIME type or name.
Max 10 MB each, 10 per project. Stored under generated `<uuid>.<ext>` keys validated by regex and resolved inside the
storage directory (no path traversal). Files are never executed. Deleting a screenshot removes its row, then its file.

## Tests

```
backend\.venv\Scripts\python -m pytest -rs     # DB tests skip with a reason if MySQL cannot be used
npm run typecheck && npm run lint && npm test && npm run build
```

Backend DB tests use an isolated database named `TEST_DB_NAME` (must end in `_test`, must differ from `DB_NAME`). It is created if
missing and migrated with Alembic; before each test the fixture asserts `SELECT DATABASE()` is the test database and then truncates
its two tables (foreign-key checks are disabled only for that truncate, on that connection). The development database is never touched.

## Verification status

Real-MySQL verification is **still pending**: no MySQL account/`.env` has been configured, so migrations `0001` and `0002` have
never been applied and all DB-backed tests skip (MySQL error 1045). Latest full backend run: 109 passed, 33 skipped.
Do not treat persistence, restart-survival or the constraints as verified until those tests run against MySQL.

## Limitations

No authentication, no project deletion or editing, prompt edits in the Builder are not saved, publish (later phase). AI generation and builds: see docs/PHASE3.md. Local-development security only; this is not production-hardened.
