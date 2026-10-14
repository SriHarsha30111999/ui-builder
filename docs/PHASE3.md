# Phase 3 — AI-powered React UI generation pipeline

## Terminology
- **Application**: this platform (the Screenshot-to-React UI Generation Platform).
- **Generated Project**: the React + TypeScript project the AI creates from the prompt and optional screenshot.
- **Generated Source Code**: the permitted `.tsx`/`.ts`/`.css` files of the Generated Project (`src/**`).
- **Preview**: the Generated Project's rendered UI, shown in the Application's Preview panel (sandboxed iframe).

Workflow: prompt + optional screenshot -> AI writes a Generated Project -> built in the Docker sandbox -> Preview. A follow-up prompt
modifies the existing Generated Project as a new version; the Preview changes only after that version builds. The AI never edits the Application's own UI.
Naming note: the Application's own "project" records (Master/Feature) and the database/API names (`generation`, `version`) are unchanged;
in this document "Generated Project" always means the AI output.

Status labels used here: **Implemented and verified**, **Implemented but unverified**, **Blocked**, **Not implemented**.
Nothing in this phase has run against a real model, a real sandbox or a real MySQL yet (see "Status").

## Input modes
All three are first-class; a screenshot is never required.

| Mode | Model used | Behaviour |
|---|---|---|
| `prompt_only` | code model | Generates from the prompt. |
| `screenshot_only` | vision model | Recreates the UI from the image bytes. |
| `prompt_and_screenshot` | vision model | The screenshot is the visual reference, the prompt is applied as changes. |

A request with neither prompt nor screenshots is rejected (422). Prompt length, screenshot ownership and the existing upload
limits are enforced server-side. Screenshots are read from disk and sent as base64 `data:` URLs; the original upload is preserved.

## Model and provider
Evaluated: `Qwen/Qwen3-VL-8B-Instruct`, `Qwen/Qwen3-VL-32B-Instruct`, `Qwen/Qwen3-Coder-30B-A3B-Instruct`.
Defaults: code model `Qwen/Qwen3-Coder-30B-A3B-Instruct`, vision model `Qwen/Qwen3-VL-32B-Instruct`, via an OpenAI-compatible
`chat/completions` endpoint (default `https://router.huggingface.co/v1`). Rationale: the Coder model is the strongest code option
for prompt-only requests; the 32B VL model is the better vision choice, and the 8B VL is the lower-hardware alternative
(set `AI_VISION_MODEL`). Availability, licence and pricing at your provider are **not verified** here: no token was available, so
nothing was called. No weights are downloaded; self-hosting 32B/30B-class models needs substantial GPU memory.
One model call per generation (analysis and code in the same call for screenshot modes) plus at most one bounded repair call.
Refinement keeps any parent file the model omits (the model output overrides, never deletes). A reply cut off by the output limit
(`finish_reason: length`) is rejected as `ai_output_truncated` instead of being parsed as a partial artifact.

### Provider/model evidence (read-only public metadata, no token, no inference)
The Hub lists featherless-ai as `live` for `Qwen3-VL-32B-Instruct` and `Qwen3-VL-8B-Instruct`, and featherless-ai + scaleway for
`Qwen3-Coder-30B-A3B-Instruct`. The router `/v1/models` listing contained the Coder model and `Qwen3-VL-30B-A3B-Instruct`, but **not**
the 32B/8B VL models. The VL default is therefore **unconfirmed**; verify with `GET https://router.huggingface.co/v1/models` using your
token and set `AI_VISION_MODEL` to a listed VL model. Default timeout is 300 s because large outputs from 30B-class models are slow.

## Environment variables (see `.env.example`, placeholders only)
`AI_API_KEY` (alias `HF_TOKEN`), `AI_BASE_URL`, `AI_CODE_MODEL`, `AI_VISION_MODEL`, `AI_TIMEOUT_SECONDS`, `AI_MAX_OUTPUT_TOKENS`,
`MAX_PROMPT_CHARS`, `MAX_TOTAL_IMAGE_BYTES`, `MAX_GENERATED_FILES`, `MAX_GENERATED_FILE_BYTES`, `MAX_REPAIR_ATTEMPTS`, `MAX_SESSION_ATTEMPTS`,
`GENERATED_STORAGE_DIR`, `BUILD_RUNNER` (`disabled`|`docker`|`host`), `BUILD_TOOLCHAIN_DIR`, `BUILD_IMAGE`, `BUILD_TIMEOUT_SECONDS`, `BUILD_MEMORY`, `BUILD_CPUS`.
The token is a backend-only `SecretStr`: never logged, never returned by any endpoint, never sent to the frontend.

## Pipeline
validate → create session → resolve project/screenshots/parent version → (vision/code) model call → parse → static validation →
sandboxed build → at most one repair → promote files → record version → mark `completed` **only after a successful build**.
Output is model-generated (no template or canned UI). The platform owns the scaffold (`package.json`, `tsconfig.json`,
`vite.config.ts`, `index.html`, `src/main.tsx`); the model writes only `src/**/*.tsx|ts|css` and may import only `react`/`react-dom`.
Static checks reject (also `window.open` and `location` navigation): path traversal, `.d.ts`, too many/large files, `fetch`/XHR/WebSocket/EventSource/`sendBeacon`, `eval`/`new Function`,
dynamic `import()`, `require`, `import.meta.env`, unresolved relative imports, remote CSS `@import`/`url()`.

## Sessions and versions
Lifecycle (transitions enforced server-side): `created → queued → analyzing|generating → building ⇄ repairing → completed|failed`;
`failed → queued` on retry (only for retryable errors, ≤ `MAX_SESSION_ATTEMPTS`); `completed` is terminal.
One active session per project (409 `generation_in_progress`). On startup, sessions left active by a crash are marked failed
(`interrupted`, retryable). Versions exist only for successful builds, with per-project numbers and a `parent_version_id` for
refinements, so a failed refinement never removes or replaces the previous version.

API: `GET /api/generation/status`, `POST|GET /api/projects/{id}/generations`, `GET /api/generations/{id}`,
`POST /api/generations/{id}/retry`, `GET /api/generations/{id}/build-log`, `GET /api/projects/{id}/versions`,
`GET /api/versions/{id}`, `GET /api/versions/{id}/files/{path}`, `GET /api/previews/{version_id}/{path}`.
Errors are structured `{error:{code,message}}` with safe messages (e.g. `ai_not_configured`, `build_sandbox_unavailable`,
`ai_timeout`, `ai_rate_limited`, `invalid_model_output`, `build_failed`, `build_timeout`, `interrupted`).

## Build sandbox and preview serving
Generated code is untrusted and is never executed in the FastAPI process or directly on the host. The only runner is Docker:
`--network none`, read-only root, `--cap-drop ALL`, `no-new-privileges`, pids/memory/CPU limits, user `1000:1000`, tmpfs `/tmp`,
fixed command, argv list (no shell). `BUILD_RUNNER=disabled` (default) refuses to build; requests fail fast with 503
`build_sandbox_unavailable` — there is no unrestricted fallback. Build the toolchain image once with
`docker build -t screenshot-builder-sandbox:1 backend/sandbox`.
Previews are static `dist` files served under `/api/previews/{version_id}/` with a `sandbox allow-scripts` CSP (no network,
`connect-src 'none'`), `nosniff`, `no-store`; the frontend embeds them in an `<iframe sandbox="allow-scripts">` (no
`allow-same-origin`, so generated code gets an opaque origin and cannot touch the app's storage or API). Paths are UUID-scoped
and traversal-checked. For production, serve previews from a separate origin.

## Local development
1. Configure MySQL and `.env` (see `docs/PHASE2.md`), then `alembic upgrade head` (applies `0001` and `0002`).
2. Set `AI_API_KEY`, install Docker, build the sandbox image, set `BUILD_RUNNER=docker`.
3. Backend: `uvicorn app.main:app` from `backend/`; frontend: `npm run dev`.
Without 1–2 the builder shows an actionable "Generation is not available" banner; nothing is faked.

## Status
| Capability | Label |
|---|---|
| Session state machine, artifact parsing/validation, prompts, provider HTTP/error mapping (mock transport), engine orchestration (doubles), docker command flags, storage path safety | Implemented and verified (unit tests, doubles) |
| Refinement keeps omitted parent files; truncated replies rejected; combined screenshot size capped; navigation APIs rejected; stale scratch dirs cleared at startup | Implemented and verified (unit tests, doubles) |
| Frontend: three modes, readiness error, failure/retry, refinement, version/source viewer, sandboxed iframe | Implemented and verified against a test double of the API only |
| Platform scaffold compiles and builds | Verified once on the host with a hand-written App (not the sandbox, not model output) |
| DB models + migration `0002`, session/version API lifecycle | Implemented but unverified (migration never applied; DB tests skipped, MySQL error 1045) |
| Real model inference (any mode), screenshot analysis | Blocked: no AI token configured |
| Sandboxed build, repair loop on real build errors, preview of a real artifact | Blocked: Docker is not installed (WSL absent) |
| Refinement against a real model | Blocked (as above) |
| Persistence across backend restarts on MySQL | Implemented but unverified |
| Production auth, cloud deploy, separate preview origin, cancellation | Not implemented |

## Tests
Backend: `pytest` — 165 passed, 33 skipped (all skips are MySQL-backed tests, error 1045). Frontend: Vitest with a fake API (47 passed); typecheck, lint and build clean.
No test here exercises a real provider, sandbox or MySQL.

### Service-lifecycle coverage without MySQL
The service's decisions live in `backend/app/generation/rules.py` (pure functions); `services/generations.py` gathers facts from the
database and calls them, so the tests below run the production code (no second implementation, no SQLite, no fake DB).
`backend/tests/test_generation_rules.py` (16 tests) covers:
- request validation: prompt trim/length, de-duplicated screenshots, screenshot cap, `no_input`, refinement needs a prompt, input mode;
- cross-project isolation: screenshots not owned by the project (422) and a parent version of another project or missing (404);
- one active session per project (409 `generation_in_progress`), and that `failed`/`completed` are not active;
- retry: only `failed` + retryable, bounded by `MAX_SESSION_ATTEMPTS` (409 `not_retryable` / `retry_limit_reached`); which engine failures are marked retryable;
- lifecycle: transition table completeness, `completed` terminal, completion only from `building`; every stage sequence the real engine emits
  (prompt, screenshot, repair ok, repair exhausted, provider error, invalid output) is a legal path under `can_transition`;
- versions: per-project number = max+1; a failed generation/refinement returns no files (so no version is stored) and does not mutate the parent source;
  project status after failure stays `ready` when an earlier version exists; stored file manifest.
Existing engine tests (`test_generation_logic.py`) cover refinement keeping parent files and the repair bound.

### What needs MySQL (kept in `test_generation_api.py`, skipped until MySQL is usable)
Not provable without MySQL, and deliberately not faked: the project row lock that makes "one active session" and version numbering
race-free under concurrent requests; the unique `(project_id, version_number)` constraint; that parent/screenshot facts are actually read
from rows; `parent_version_id` persistence and `ON DELETE` behaviour; JSON and CHAR(32) round-trips; CHECK constraints;
`recover_interrupted` on restart; a failed refinement leaving the stored parent version row and files in place end to end.

## Security and reliability audit (source review + targeted tests)
Fixed after reading the actual code paths:
- **Windows device names** (`src/CON.tsx`, `nul`, `com1`...) and **case-colliding paths** (`App.tsx` vs `app.tsx`) are now rejected by `validate_files`; on a Windows host the first would open a device and the second would merge two validated files into one built file.
- **Preview content types** no longer come from `mimetypes` (the Windows registry can map `.js` to `text/plain`, which `nosniff` turns into a broken preview). CSP and types live in `generation/preview.py`.
- **Provider response** is streamed with an 8 MB cap and a total wall-clock deadline (httpx timeouts are per read only). Redirects are not followed, so the token is never forwarded.
- **Duplicate processing**: a session id already running in this process is refused (`InFlight`); previously a second worker would have marked the running session failed and deleted its workspace.
- **Promotion**: a failed move to the permanent version directory now cleans up the partial directory.
- **Post-build tree check**: symlinks or runaway file counts anywhere in the workspace (not only `dist`) reject the build.
- `docker kill` errors after a timeout no longer turn a timeout into an internal error; `:` is rejected in served paths; screenshots are re-checked against the session's project at run time.

Verified by tests (`tests/test_generation_security.py`, no DB/Docker/network): the items above, preview CSP contents (no `allow-same-origin`, `connect-src 'none'`), path traversal and cross-project file access in storage, symlink refusal, `discard` confined to the storage root, token absent from provider errors, readiness payload and settings repr, docker argv (single workspace mount, `--network none`).
Verified by inspection only: failed generations/refinements never touch earlier versions (versions are rows plus UUID directories created only after a successful build; lock order session -> project is consistent in all paths); the frontend iframe uses `sandbox="allow-scripts"` without `allow-same-origin`; no token reaches the frontend bundle; error handlers return generic messages without stack traces; total session time is bounded (<= 2 model calls x timeout + 2 builds x build timeout).
**Not verified (needs runtime):** that the Docker flags actually isolate a build, that the toolchain image builds and `tsc`/`vite` run under `--read-only`/`noexec` tmpfs and uid 1000 (on Linux hosts the bind-mounted workspace must be writable by uid 1000), that CSP `script-src 'self'` loads the module from an opaque-origin iframe in a browser, MySQL locking. The static forbidden-API list is a second layer and is bypassable (e.g. `globalThis['fetch']`); the real controls are the CSP, the iframe sandbox and the no-network build container.
- **Sandbox build command** now uses `vite build --configLoader runner`: by reading the Vite 8 source, the default loader does `mkdir /node_modules/.vite-temp` (a read-only layer under `--read-only`, error EROFS, which Vite rethrows), so every container build would have failed. A host build with the flag succeeded; it has not been run in the container.
Known limits, not changed: run a single backend process (startup recovery marks all active sessions interrupted, which would break sessions of another process); there is no authentication, so anyone who can reach the API and knows a UUID can read that version's preview/source; a crash between promoting files and the database commit can leave an orphan version directory; `docker run` output is captured in memory before truncation.

## Remaining blockers and exact next steps
1. MySQL: create the dedicated account and `backend/.env` (SQL in the Phase 3 chat), confirm, then a read-only connection test; migrations `0001`/`0002` and DB tests only after explicit approval.
2. AI token (`AI_API_KEY`) in `backend/.env`; confirm VL model via `GET /v1/models`.
3. Docker Desktop (needs WSL2; likely IT approval), then `docker build -t screenshot-builder-sandbox:1 backend/sandbox` and `BUILD_RUNNER=docker`.
4. First real run: prompt-only, refine, screenshot-only, prompt+screenshot, failure paths, restart persistence.
Real E2E minimum: items 1-3 above, then run the skipped DB tests (after approval) and one real generation per mode.

## Feasibility audit of the current laptop (evidence, no installs)
- **Isolated runtimes:** none usable. `docker`, `podman`, `nerdctl`, `multipass`, VirtualBox, QEMU and Windows Sandbox are absent; WSL is not installed; the Windows features Hyper-V, VirtualMachinePlatform, Containers and WSL are all *Disabled* (enabling them needs admin/IT). Node 22 is present, but its permission model does not isolate the network and the host must not run generated code, so it is not an acceptable build sandbox.
- **Browsers:** Edge and Chrome are installed. Headless Edge exits silently in this elevated shell and Chrome headless would need `--no-sandbox`, which is not acceptable, so no automated screenshot was taken. Playwright's browser cache exists but the Playwright package is not installed in the project.
- **Offline verification possible:** everything in "Tests" above, plus `backend/tools/preview_probe.py`: builds a hand-written fixture (not model output) with the host toolchain, serves it with the production preview headers on :8000 and a host page with the Preview panel's iframe attributes on :5173. Checked here over HTTP: headers, content types, traversal 404. **Seen in a browser (user-run, Chrome, 2026-10-09):** the fixture rendered inside the sandboxed iframe, React state worked (button counter), and localStorage, parent DOM, cookie and network probes all reported "blocked". Not checked: Edge/Firefox, and the server-side request log for the network probe.
- **Needs real inference:** whether the model emits valid files for real prompts, repair quality, refinement quality and the vision model's behaviour. **Needs MySQL:** persistence/locking tests. **Needs Docker:** the sandboxed build.

## First real model call (2026-10-11, Groq via the OpenAI-compatible provider)
Provider switched by configuration only (`backend/.env`: `AI_BASE_URL=https://api.groq.com/openai/v1`, `AI_CODE_MODEL=openai/gpt-oss-120b`); no code change. `GET /models` returned 200 and does not list the Qwen defaults or any model I could confirm as vision-capable, so screenshot requests are **unverified** (`AI_VISION_MODEL` still points at a model Groq does not serve).
One prompt-only generation ("pricing page with three plan cards, a monthly/yearly toggle, and an FAQ accordion") ran through the production engine in 5.9 s: real reply parsed into 4 components + CSS + App, all static validation passed, stage reached `building`. A recording stand-in for the runner kept the text files and refused to build (no sandbox). **Not verified:** that the files type-check or build, how they render, refinement and repair against a real model. Key rotation advised: the key was pasted into a chat transcript.

## Folder store (PERSISTENCE=folder) — added 2026-10-11, approved by the user
**What it is.** An explicit opt-in (`PERSISTENCE=folder`, `FOLDER_STORE_DIR`, default `backend/storage/data`) that keeps records as one JSON file each under `projects/`, `screenshots/`, `sessions/`, `versions/`. Generated source stays in `backend/storage/generated/<project>/<version>/` and screenshots in `backend/storage/screenshots/`. The default stays `mysql`; the app never falls back from one to the other. The MySQL code path is unchanged apart from two refactors that both backends share: `run_session` is storage-agnostic (`SessionOps`: `SqlOps` = the old code moved, `FolderOps`), and project validation moved to `services/project_rules.py`.
**Same interface.** `services/folder_backend.py` has the same function names and behaviour as the MySQL services (`create_project`, `add_screenshots`, `create_session`, `retry_session`, `recover_interrupted`, ...); `main.py` selects one set. Business rules are shared (`rules.py`, `project_rules.py`, `state.py`), not copied.

| | MySQL | Folder store |
|---|---|---|
| Setup on this laptop | needs a dedicated account + `.env` + migrations | none |
| Atomicity | transactions across rows | atomic per file; a version is written before its session is marked completed, and `recover_interrupted` completes such a session after a crash |
| Concurrency | row locks, any number of backend processes | one in-process lock; an OS lock on `data/.lock` refuses a second backend on the same folder |
| Integrity | FKs, UNIQUE, CHECK constraints | none: only the service rules enforce it (e.g. base-path uniqueness is checked under the lock) |
| Scale | indexed queries | every listing reads the whole folder; fine for a local tool |
| Inspectability / backup | needs MySQL tools | plain JSON; copy the folder (keep it out of OneDrive sync if possible: sync can briefly lock files, the store retries `os.replace`) |
| Verified here | 33 tests skipped (MySQL error 1045) | 18 HTTP lifecycle tests in `tests/test_folder_backend.py` run for real (fake model/runner) |

Not verified: MySQL behaviour; the folder store under heavy concurrent writers or on a network drive; a real model or real build through it.

## Groq provider (configuration only)
Working local config: `AI_BASE_URL=https://api.groq.com/openai/v1`, `AI_CODE_MODEL=openai/gpt-oss-120b`, `AI_VISION_MODEL=qwen/qwen3.8-27b`. One tiny image call to `qwen/qwen3.8-27b` described a solid red image as "Red" (vision input works); `gpt-oss-120b` rejects images (HTTP 400), so it is the code model only.

## Host build runner (`BUILD_RUNNER=host`) - explicit, unsandboxed opt-in

Approved by the user because Docker is unavailable on this laptop. Docker is needed for neither the Application nor the Generated Project's Preview; it was only an isolation choice for the build step.

- Runs `tsc --noEmit` then `vite build` (node, argv lists, no shell) in the temp workspace, using the Application's own `node_modules` as the toolchain (`BUILD_TOOLCHAIN_DIR` to override). A directory junction `generated/_work/node_modules` provides module resolution; nothing is installed.
- The generated code is never started by the platform. The built `dist` is served as static files into the sandboxed Preview iframe (URL not shown).
- **Not a sandbox.** Build tools read model-written source with your own OS permissions, and there is no network or filesystem isolation. Mitigations: validated sources only, platform-owned configs, scrubbed environment (AI key and DB secrets are never passed), temp workspace, timeout with process-tree kill, same output checks as Docker.
- `docker` and `disabled` runners are unchanged; default stays `disabled`. Tests force `BUILD_RUNNER=disabled` and include one real fixture build plus a type-error build.
