"""Parsing and static validation of model output, plus the platform-owned scaffold.

The model may only write files under `src/` (tsx/ts/css). The platform owns package.json, tsconfig, the Vite config,
index.html and src/main.tsx, so generated code cannot add dependencies, change the build, or run install scripts.
"""
import re
from dataclasses import dataclass, field

FILE_MARK = re.compile(r"^=== FILE: (.+?) ===\s*$", re.M)
SECTION_MARK = re.compile(r"^=== (SUMMARY|UNCERTAINTIES) ===\s*$", re.M)

PATH_RE = re.compile(r"^src/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.(?:tsx|ts|css)$")
PLATFORM_FILES = ("src/main.tsx", "src/vite-env.d.ts")
# Windows device names: writing "src/CON.tsx" on a Windows host would open the console device instead of a file.
WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})
ALLOWED_PACKAGES = ("react", "react-dom", "react/jsx-runtime")

# APIs that reach the network or run dynamic code. The preview CSP also blocks network access; this is a second layer.
FORBIDDEN_CODE = (
    (re.compile(r"\bfetch\s*\("), "network access (fetch)"),
    (re.compile(r"\bXMLHttpRequest\b"), "network access (XMLHttpRequest)"),
    (re.compile(r"\bWebSocket\b"), "network access (WebSocket)"),
    (re.compile(r"\bEventSource\b"), "network access (EventSource)"),
    (re.compile(r"\bsendBeacon\b"), "network access (sendBeacon)"),
    (re.compile(r"\beval\s*\("), "eval"),
    (re.compile(r"\bnew\s+Function\b"), "new Function"),
    (re.compile(r"\bimport\s*\("), "dynamic import"),
    (re.compile(r"\brequire\s*\("), "require"),
    (re.compile(r"\bimport\.meta\.env\b"), "environment access"),
    (re.compile(r"\bwindow\s*\.\s*open\b"), "window.open"),
    (re.compile(r"\blocation\s*\.\s*(?:href|assign|replace)\b|(?<!const )(?<!let )(?<!var )\blocation\s*=(?!=)"), "navigation (location)"),
)
IMPORT_RE = re.compile(r"""(?:import|export)\s[^'"]*?from\s*['"]([^'"]+)['"]|import\s*['"]([^'"]+)['"]""")
CSS_EXTERNAL = re.compile(r"@import|url\(\s*['\"]?\s*(?:https?:)?//", re.I)


class ArtifactError(Exception):
    """Model output could not be turned into a valid artifact. `message` is safe to show to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ParsedOutput:
    summary: str
    uncertainties: list[str]
    files: dict[str, str] = field(default_factory=dict)


def parse_model_output(text: str) -> ParsedOutput:
    """Format: optional `=== SUMMARY ===` / `=== UNCERTAINTIES ===` sections, then `=== FILE: src/x.tsx ===` blocks."""
    text = text.replace("\r\n", "\n")
    marks = list(FILE_MARK.finditer(text))
    if not marks:
        raise ArtifactError("malformed_model_output", "The model reply did not contain any source files.")
    files: dict[str, str] = {}
    for i, m in enumerate(marks):
        path = m.group(1).strip().strip("`")
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end() : end].strip("\n")
        body = re.sub(r"^```[a-zA-Z]*\n", "", body)  # tolerate a code fence around the file
        body = re.sub(r"\n```\s*$", "", body)
        if path in files:
            raise ArtifactError("invalid_artifact", f"The model returned '{path}' more than once.")
        files[path] = body.rstrip() + "\n"
    head = text[: marks[0].start()]
    summary, uncertainties = "", []
    sections = list(SECTION_MARK.finditer(head))
    for i, m in enumerate(sections):
        end = sections[i + 1].start() if i + 1 < len(sections) else len(head)
        content = head[m.end() : end].strip()
        if m.group(1) == "SUMMARY":
            summary = content[:2000]
        else:
            uncertainties = [ln.lstrip("-* ").strip() for ln in content.splitlines() if ln.strip().lstrip("-* ").strip()][:20]
            uncertainties = [u[:300] for u in uncertainties]
    return ParsedOutput(summary=summary, uncertainties=uncertainties, files=files)


def _resolve_relative(from_path: str, spec: str) -> list[str]:
    parts = from_path.split("/")[:-1]
    for seg in spec.split("/"):
        if seg == "..":
            if len(parts) <= 1:
                return []  # would leave src/
            parts.pop()
        elif seg not in ("", "."):
            parts.append(seg)
    base = "/".join(parts)
    if re.search(r"\.(tsx|ts|css)$", base):
        return [base]
    return [base + ".tsx", base + ".ts", base + "/index.tsx", base + "/index.ts"]


def validate_files(files: dict[str, str], max_files: int, max_file_bytes: int) -> None:
    """Static checks on model-written files. Raises ArtifactError with a message that is also used as repair feedback."""
    if not files:
        raise ArtifactError("invalid_artifact", "No files were generated.")
    if len(files) > max_files:
        raise ArtifactError("invalid_artifact", f"Too many files ({len(files)}; limit {max_files}).")
    folded: dict[str, str] = {}
    for path in files:
        if folded.setdefault(path.lower(), path) != path:  # case-insensitive filesystems would merge these into one file
            raise ArtifactError("invalid_artifact", f"'{path}' and '{folded[path.lower()]}' differ only by letter case.")
        if any(seg.split(".")[0].lower() in WINDOWS_RESERVED for seg in path.split("/")):
            raise ArtifactError("invalid_artifact", f"File path '{path[:80]}' uses a reserved name.")
    for path, content in files.items():
        if not PATH_RE.match(path) or path.endswith(".d.ts") or ".." in path.split("/"):
            raise ArtifactError("invalid_artifact", f"File path '{path[:80]}' is not allowed (only src/**/*.tsx, .ts, .css).")
        if path in PLATFORM_FILES:
            raise ArtifactError("invalid_artifact", f"'{path}' is managed by the platform and must not be generated.")
        if len(content.encode("utf-8")) > max_file_bytes:
            raise ArtifactError("invalid_artifact", f"'{path}' is larger than {max_file_bytes // 1000} KB.")
        if "\x00" in content:
            raise ArtifactError("invalid_artifact", f"'{path}' contains binary data.")
    if "src/App.tsx" not in files:
        raise ArtifactError("invalid_artifact", "src/App.tsx is required.")
    if not re.search(r"export\s+default\b", files["src/App.tsx"]):
        raise ArtifactError("invalid_artifact", "src/App.tsx must have a default export (the root component).")

    known = set(files) | set(PLATFORM_FILES)
    for path, content in files.items():
        if path.endswith(".css"):
            if CSS_EXTERNAL.search(content):
                raise ArtifactError("invalid_artifact", f"'{path}' references external resources (@import / remote url()).")
            continue
        for pattern, label in FORBIDDEN_CODE:
            if pattern.search(content):
                raise ArtifactError("invalid_artifact", f"'{path}' uses {label}, which is not allowed in Generated Source Code.")
        for m in IMPORT_RE.finditer(content):
            spec = m.group(1) or m.group(2)
            if spec.startswith("."):
                if not any(c in known for c in _resolve_relative(path, spec)):
                    raise ArtifactError("invalid_artifact", f"'{path}' imports '{spec}', which does not exist.")
            elif spec not in ALLOWED_PACKAGES:
                raise ArtifactError("invalid_artifact", f"'{path}' imports '{spec}'. Only react and react-dom are available.")


# ---- platform-owned scaffold -------------------------------------------------------------------------------------

SCAFFOLD: dict[str, str] = {
    "package.json": """{
  "name": "generated-ui",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": { "build": "tsc --noEmit && vite build" },
  "dependencies": { "react": "^19.3.0", "react-dom": "^19.3.0" }
}
""",
    "tsconfig.json": """{
  "compilerOptions": {
    "target": "ES2022", "lib": ["ES2022", "DOM", "DOM.Iterable"], "module": "ESNext", "moduleResolution": "bundler",
    "jsx": "react-jsx", "strict": true, "noEmit": true, "skipLibCheck": true, "isolatedModules": true, "types": ["vite/client"]
  },
  "include": ["src"]
}
""",
    "vite.config.ts": """import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({ base: './', plugins: [react()], build: { outDir: 'dist', sourcemap: false } })
""",
    "index.html": """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Generated UI</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
""",
    "src/main.tsx": """import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
""",
    "src/vite-env.d.ts": '/// <reference types="vite/client" />\n',
}


def assemble_workspace_files(model_files: dict[str, str]) -> dict[str, str]:
    """Scaffold + model files. Model files can never replace scaffold files (validated beforehand)."""
    return {**SCAFFOLD, **model_files}
