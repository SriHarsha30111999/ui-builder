"""Manual Preview probe (no Docker, no AI, no database).

Builds a HAND-WRITTEN fixture app (not model output) with the platform scaffold using the host's Node toolchain, then serves it
with the exact production preview headers (generation/preview.py) and the same storage layout, and a host page whose iframe has the
same attributes as the Preview panel (sandbox="allow-scripts"). Open http://127.0.0.1:5173/ in your own browser.

Expected: "Fixture Preview OK", a working counter button, and the probe list showing localStorage/parent DOM/cookie/network all "blocked"
(the page tries to fetch a live endpoint on :8000, so "network: REACHED" would mean the CSP/sandbox failed).
Run from the repo root:  cd backend && .venv/Scripts/python tools/preview_probe.py      (Ctrl+C to stop)
Never feed model-generated code to this tool; generated code must only be built inside the Docker sandbox.
"""

import json, os, pathlib, subprocess, sys, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # so `app` imports work when run as tools/preview_probe.py

from app.generation.artifact import assemble_workspace_files
from app.generation.preview import preview_headers
from app.generation.storage import GeneratedStorage, GeneratedStorageError

import tempfile
repo = pathlib.Path(__file__).resolve().parents[2]  # frontend node_modules provide tsc/vite/react for the host build
out = pathlib.Path(tempfile.mkdtemp(prefix="preview_probe_"))
out.mkdir(parents=True, exist_ok=True)

APP = """import { useEffect, useState } from 'react'
import './app.css'
export default function App() {
  const [n, setN] = useState(0)
  const [probes, setProbes] = useState<string[]>([])
  useEffect(() => {
    const r: string[] = []
    try { localStorage.getItem('x'); r.push('localStorage: ACCESSIBLE') } catch { r.push('localStorage: blocked') }
    try { r.push('parent DOM: ' + (window.parent.document.title || 'ACCESSIBLE')) } catch { r.push('parent DOM: blocked') }
    try { r.push('cookie: ' + (document.cookie === '' ? 'empty' : 'ACCESSIBLE')) } catch { r.push('cookie: blocked') }
    fetch('http://127.0.0.1:8000/api/health').then(() => { r.push('network: REACHED'); setProbes([...r]) }).catch(() => { r.push('network: blocked'); setProbes([...r]) })
  }, [])
  return (
    <main className="app">
      <h1>Fixture Preview OK</h1>
      <button onClick={() => setN(n + 1)}>Clicked {n}</button>
      <ul>{probes.map((p) => <li key={p}>{p}</li>)}</ul>
    </main>
  )
}
"""
CSS = ".app { font-family: sans-serif; padding: 16px; background: #eef6ff; min-height: 100vh } h1 { color: #0b5cab }\n"

ws = out / "ws"
ws.mkdir(exist_ok=True)
for k, v in assemble_workspace_files({"src/App.tsx": APP, "src/app.css": CSS}).items():
    p = ws / k; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(v, encoding="utf-8")
import shutil
shutil.rmtree(ws / "dist", ignore_errors=True)
if not (ws / "node_modules").exists():
    os.symlink(repo / "node_modules", ws / "node_modules", target_is_directory=True)
r = subprocess.run(["cmd", "/c", "npx", "tsc", "--noEmit", "-p", "tsconfig.json", "&&", "npx", "vite", "build", "--config", "vite.config.ts", "--configLoader", "runner"],
                   cwd=ws, capture_output=True, text=True)
print("build rc", r.returncode, r.stdout[-300:], r.stderr[-300:])
assert r.returncode == 0
os.remove(ws / "node_modules") if (ws / "node_modules").is_symlink() else None

store = GeneratedStorage(out / "store")
pid, vid = uuid.uuid4(), uuid.uuid4()
target = store.version_dir(pid, vid); target.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(ws, target)
ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
seen = []


class Api(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a): pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            seen.append("health requested")
            body = b'{"status":"ok"}'
            self.send_response(200); self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        prefix = f"/api/previews/{vid}/"
        if path.startswith(prefix):
            rel = path[len(prefix):] or "index.html"
            try:
                f = store.preview_file(pid, vid, rel)
            except GeneratedStorageError:
                self.send_response(404); self.end_headers(); return
            data = f.read_bytes(); h = preview_headers(ORIGINS, f.name)
            self.send_response(200)
            for k, v in h.items(): self.send_header(k, v)
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        self.send_response(404); self.end_headers()


HOST = f"""<!doctype html><html><body style="margin:0;font-family:sans-serif">
<h2 style="margin:8px">Application host page (mirrors PreviewPanel: sandbox="allow-scripts")</h2>
<iframe title="Preview" sandbox="allow-scripts" src="http://127.0.0.1:8000/api/previews/{vid}/" style="width:880px;height:420px;border:2px solid #333"></iframe>
</body></html>""".encode()


class Host(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a): pass

    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(HOST))); self.end_headers(); self.wfile.write(HOST)


s1, s2 = ThreadingHTTPServer(("127.0.0.1", 8000), Api), ThreadingHTTPServer(("127.0.0.1", 5173), Host)
for s in (s1, s2):
    threading.Thread(target=s.serve_forever, daemon=True).start()
print("Preview probe running. Open http://127.0.0.1:5173/ in your browser. Ctrl+C to stop.", flush=True)
try:
    threading.Event().wait()
except KeyboardInterrupt:
    pass
finally:
    s1.shutdown(); s2.shutdown()
