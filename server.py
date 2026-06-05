"""Simple web server for the StepMania chart generator.

Run it from the repo root:

    .venv\\Scripts\\python.exe server.py

then open http://localhost:8000 in a browser. Paste a YouTube URL and it will
download the audio, generate charts with every available generator (our
TempoSync + FootGraph, and AutoStepper if installed), score them on objective
metrics, and let you download the playable song folders.

Uses only the Python standard library (plus the generator's own audio deps), so
there is no web framework to install.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from sm_generator import pipeline  # noqa: E402

HOST = "127.0.0.1"
PORT = 8000
ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
OUTPUT_BASE = os.path.join(ROOT, "output", "web")

# In-memory job store.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(fields)


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _run_job(job_id: str, url: str, title, artist: str, difficulties, autostepper: bool):
    def progress(msg: str, pct: float = 0.0):
        _set_job(job_id, status="running", message=msg, pct=pct)

    try:
        result = pipeline.run_pipeline(
            url=url, title=title or None, artist=artist or "Unknown",
            difficulties=difficulties or None,
            include_autostepper=autostepper, progress=progress,
        )
        _set_job(job_id, status="done", message="Done", pct=100,
                 result=result.to_dict())
    except Exception as exc:  # noqa: BLE001
        _set_job(job_id, status="error", message=str(exc), pct=0)


def _safe_output_path(rel: str) -> str | None:
    """Resolve a path under OUTPUT_BASE, blocking traversal."""
    full = os.path.abspath(os.path.join(OUTPUT_BASE, rel))
    base = os.path.abspath(OUTPUT_BASE)
    if full == base or full.startswith(base + os.sep):
        return full
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "SMGen/1.0"

    def log_message(self, fmt, *args):  # quieter logging
        pass

    # -- helpers -------------------------------------------------------
    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, obj):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _send_file(self, path, ctype):
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    # -- routing -------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/" or route == "/index.html":
            return self._send_file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
        if route == "/app.js":
            return self._send_file(os.path.join(WEB_DIR, "app.js"), "text/javascript; charset=utf-8")
        if route == "/style.css":
            return self._send_file(os.path.join(WEB_DIR, "style.css"), "text/css; charset=utf-8")

        if route == "/api/status":
            qs = parse_qs(parsed.query)
            job_id = (qs.get("job") or [""])[0]
            job = _get_job(job_id)
            if not job:
                return self._send_json(404, {"error": "unknown job"})
            return self._send_json(200, job)

        if route == "/api/capabilities":
            import shutil
            return self._send_json(200, {
                "yt_dlp": shutil.which("yt-dlp") is not None,
                "autostepper": pipeline.autostepper_jar() is not None
                and shutil.which("java") is not None,
                "difficulties": pipeline.footgraph.DIFFICULTY_ORDER,
            })

        if route == "/api/metadata":
            qs = parse_qs(parsed.query)
            url = (qs.get("url") or [""])[0].strip()
            if not url:
                return self._send_json(400, {"error": "missing url"})
            meta = pipeline.fetch_metadata(url)
            return self._send_json(200, meta)

        if route == "/download":
            qs = parse_qs(parsed.query)
            rel = (qs.get("path") or [""])[0]
            full = _safe_output_path(rel)
            if not full or not os.path.isfile(full):
                return self._send_json(404, {"error": "not found"})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(full)}"')
            self.send_header("Content-Length", str(os.path.getsize(full)))
            self.end_headers()
            with open(full, "rb") as f:
                self.wfile.write(f.read())
            return

        if route == "/download_zip":
            qs = parse_qs(parsed.query)
            rel = (qs.get("dir") or [""])[0]
            full = _safe_output_path(rel)
            if not full or not os.path.isdir(full):
                return self._send_json(404, {"error": "not found"})
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for r, _d, files in os.walk(full):
                    for fn in files:
                        fp = os.path.join(r, fn)
                        arc = os.path.relpath(fp, os.path.dirname(full))
                        zf.write(fp, arc)
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(full)}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/generate":
            return self._send_json(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send_json(400, {"error": "invalid JSON"})

        url = (payload.get("url") or "").strip()
        if not url:
            return self._send_json(400, {"error": "missing url"})
        title = (payload.get("title") or "").strip()
        artist = (payload.get("artist") or "Unknown").strip()
        difficulties = payload.get("difficulties") or None
        autostepper = bool(payload.get("autostepper", True))

        job_id = uuid.uuid4().hex
        _set_job(job_id, status="queued", message="Queued", pct=0)
        t = threading.Thread(
            target=_run_job,
            args=(job_id, url, title, artist, difficulties, autostepper),
            daemon=True,
        )
        t.start()
        return self._send_json(202, {"job": job_id})


def main():
    os.makedirs(OUTPUT_BASE, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"StepMania chart generator running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
