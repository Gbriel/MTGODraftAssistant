"""
Local web server: serves the single-page UI, a JSON snapshot, and a
Server-Sent Events stream that pushes new state whenever the log changes.

Stdlib only (``http.server``). One page, one local client, one event stream:
a framework wouldn't earn its place here.

    GET /            -> web/index.html
    GET /static/<f>  -> web/<f>
    GET /api/state   -> current state as JSON
    GET /events      -> text/event-stream; one ``state`` event per change
"""

from __future__ import annotations

import json
import mimetypes
import threading
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .analysis import Analysis, analyse
from .watcher import DraftWatcher, Update

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
SSE_KEEPALIVE_SECONDS = 15.0


# -- state -------------------------------------------------------------------


def build_state(update: Update | None, analysis: Analysis | None,
                log_dir: Path, version: int) -> dict:
    """JSON-serialisable view of everything the UI needs."""
    base = {
        "version": version,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "log_dir": str(log_dir),
        "file": None,
        "draft": None,
        "position": {"pack": None, "pick": None, "status": "idle"},
        "current_pack": None,
        "picks": [],
        "wheels": [],
        "in_flight": [],
        "warnings": [],
    }
    if update is None or analysis is None:
        return base

    d = update.draft
    live = d.current_pack
    if live is not None:
        status = "on_screen"
    elif d.picks:
        status = "waiting"
    else:
        status = "idle"

    base.update({
        "file": update.path.name,
        "draft": {
            "event_id": d.event_id,
            "timestamp": d.timestamp,
            "hero": d.hero,
            "players": d.players,
            "set_name": d.set_name,
            "pod_size": analysis.pod_size,
            "pack_sizes": {str(k): v for k, v in analysis.pack_sizes.items()},
        },
        "position": {
            "pack": analysis.current_pack,
            "pick": analysis.current_pick,
            "status": status,
        },
        "current_pack": (
            {"pack": live.pack, "pick": live.pick, "cards": list(live.available)}
            if live is not None else None
        ),
        "picks": [
            {
                "pack": p.pack, "pick": p.pick, "picked": p.picked,
                "available": list(p.available), "complete": p.complete,
            }
            for p in d.picks
        ],
        "wheels": [asdict(w) for w in analysis.wheels],
        "in_flight": [asdict(f) for f in analysis.in_flight],
        "warnings": list(analysis.warnings),
    })
    return base


class StateStore:
    """Versioned state with a condition variable for SSE waiters."""

    def __init__(self, initial: dict) -> None:
        self._cond = threading.Condition()
        self.version = 0
        self.state = dict(initial, version=0)
        self.closed = False

    def set(self, state: dict) -> None:
        with self._cond:
            self.version += 1
            self.state = dict(state, version=self.version)
            self._cond.notify_all()

    def get(self) -> dict:
        with self._cond:
            return self.state

    def wait_for_change(self, seen_version: int, timeout: float) -> tuple[int, dict] | None:
        with self._cond:
            if self.version != seen_version:
                return self.version, self.state
            self._cond.wait(timeout)
            if self.version != seen_version:
                return self.version, self.state
            return None

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()


# -- HTTP --------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "DraftHTTPServer"

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        if self.server.verbose:
            super().log_message(fmt, *args)

    # routing
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._file(WEB_DIR / "index.html")
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        elif path == "/api/state":
            self._json(self.server.store.get())
        elif path == "/events":
            self._sse()
        else:
            self._error(HTTPStatus.NOT_FOUND, "not found")

    # responses
    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _json(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")

    def _file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, f"missing {path.name}")
            return
        ctype, _ = mimetypes.guess_type(str(path))
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, body, ctype)

    def _static(self, rel: str) -> None:
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        self._file(target)

    def _sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        store = self.server.store
        seen = -1
        try:
            while not store.closed:
                result = store.wait_for_change(seen, SSE_KEEPALIVE_SECONDS)
                if result is None:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    seen, state = result
                    payload = json.dumps(state, ensure_ascii=False)
                    self.wfile.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.close_connection = True


class DraftHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: StateStore, verbose: bool = False) -> None:
        super().__init__(address, Handler)
        self.store = store
        self.verbose = verbose


# -- orchestration -----------------------------------------------------------


class DraftServer:
    """Watcher thread + HTTP server thread sharing a StateStore."""

    def __init__(self, log_dir: str | Path, host: str = "127.0.0.1", port: int = 8765,
                 interval: float = 0.5, verbose: bool = False) -> None:
        self.log_dir = Path(log_dir)
        self.interval = interval
        self.store = StateStore(build_state(None, None, self.log_dir, 0))
        self.watcher = DraftWatcher(self.log_dir, interval=interval)
        self.httpd = DraftHTTPServer((host, port), self.store, verbose=verbose)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def host(self) -> str:
        return self.httpd.server_address[0]

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def _on_update(self, update: Update) -> None:
        analysis = analyse(update.draft)
        self.store.set(build_state(update, analysis, self.log_dir, self.store.version + 1))

    def start(self) -> None:
        # Prime synchronously so the first page load has data.
        first = self.watcher.poll()
        if first is not None:
            self._on_update(first)
        t_watch = threading.Thread(
            target=self.watcher.run, args=(self._on_update, self._stop),
            name="draft-watcher", daemon=True,
        )
        t_http = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.25},
            name="draft-http", daemon=True,
        )
        self._threads = [t_watch, t_http]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        self.store.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        for t in self._threads:
            t.join(timeout=2)

    def wait(self) -> None:
        """Block the main thread until interrupted."""
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        except KeyboardInterrupt:
            pass
