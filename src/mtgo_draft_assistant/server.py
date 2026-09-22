"""
Local web server: serves the single-page UI, a JSON snapshot, and a
Server-Sent Events stream that pushes new state whenever the log changes.

Stdlib only (``http.server``). One page, one local client, one event stream:
a framework wouldn't earn its place here.

    GET /            -> web/index.html
    GET /static/<f>  -> web/<f>
    GET /api/state   -> current state as JSON
    GET /events      -> text/event-stream; one ``state`` event per change
    GET /img/<f>     -> cached Scryfall small image
    POST /api/config -> {"log_dir": "..."} switches the watched directory and
                        saves it to config.toml
"""

from __future__ import annotations

import json
import mimetypes
import threading
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .analysis import Analysis, analyse
from .config import save_log_dir
from .scryfall import CardInfo, CardResolver
from .watcher import DraftWatcher, Update, newest_log

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
SSE_KEEPALIVE_SECONDS = 15.0
CARD_PUSH_DEBOUNCE_SECONDS = 0.3


def draft_card_names(update: Update) -> list[str]:
    """Every distinct card name that has appeared in this draft, in order."""
    seen: dict[str, None] = {}
    for p in update.draft.picks:
        for c in p.available:
            seen.setdefault(c, None)
    return list(seen)


# -- state -------------------------------------------------------------------


def build_state(update: Update | None, analysis: Analysis | None,
                log_dir: Path | None, version: int,
                cards: dict[str, CardInfo] | None = None,
                cards_pending: int = 0,
                card_errors: list[str] | None = None) -> dict:
    """JSON-serialisable view of everything the UI needs."""
    base = {
        "version": version,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "log_dir": str(log_dir) if log_dir is not None else None,
        "log_dir_ok": bool(log_dir is not None and log_dir.is_dir()),
        "file": None,
        "draft": None,
        "position": {"pack": None, "pick": None, "status": "idle"},
        "current_pack": None,
        "picks": [],
        "wheels": [],
        "in_flight": [],
        "warnings": [],
        "cards": {name: info.to_json() for name, info in (cards or {}).items()},
        "cards_pending": cards_pending,
        "card_errors": list(card_errors or []),
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
        elif path.startswith("/img/"):
            self._image(path[len("/img/"):])
        else:
            self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path != "/api/config":
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, json.JSONDecodeError) as e:
            self._json({"ok": False, "error": f"bad request: {e}"}, HTTPStatus.BAD_REQUEST)
            return
        if self.server.on_config is None:
            self._json({"ok": False, "error": "configuration is read-only"}, HTTPStatus.FORBIDDEN)
            return
        result = self.server.on_config(payload)
        self._json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)

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

    def _json(self, obj: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

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

    def _image(self, rel: str) -> None:
        image_dir = self.server.image_dir
        if image_dir is None:
            self._error(HTTPStatus.NOT_FOUND, "images disabled")
            return
        target = (image_dir / rel).resolve()
        if image_dir.resolve() not in target.parents or not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        # Image files are keyed by Scryfall id, so their content never changes.
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self.end_headers()
        self.wfile.write(body)

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

    def __init__(self, address: tuple[str, int], store: StateStore, verbose: bool = False,
                 image_dir: Path | None = None,
                 on_config: Callable[[dict], dict] | None = None) -> None:
        super().__init__(address, Handler)
        self.store = store
        self.verbose = verbose
        self.image_dir = image_dir
        self.on_config = on_config


# -- orchestration -----------------------------------------------------------


class DraftServer:
    """
    Watcher thread + HTTP server thread sharing a StateStore, plus an optional
    :class:`CardResolver` that fills in colours and images in the background.
    Pass ``resolver=None`` for a text-only server (tests, offline use).
    """

    def __init__(self, log_dir: str | Path | None, host: str = "127.0.0.1", port: int = 8765,
                 interval: float = 0.5, verbose: bool = False,
                 resolver: CardResolver | None = None,
                 config_path: Path | None = None,
                 allow_config: bool = True) -> None:
        self.log_dir: Path | None = Path(log_dir) if log_dir is not None else None
        self.interval = interval
        self.resolver = resolver
        self.config_path = config_path          # None -> default config.toml location
        if resolver is not None:
            resolver.on_change = self._on_cards_changed
        self.store = StateStore(build_state(None, None, self.log_dir, 0))
        self.watcher = DraftWatcher(self.log_dir, interval=interval)
        self.httpd = DraftHTTPServer(
            (host, port), self.store, verbose=verbose,
            image_dir=resolver.image_dir if resolver else None,
            on_config=self._on_config if allow_config else None,
        )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last: tuple[Update, Analysis] | None = None
        self._last_lock = threading.Lock()
        self._card_timer: threading.Timer | None = None

    @property
    def host(self) -> str:
        return self.httpd.server_address[0]

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def _publish(self, update: Update, analysis: Analysis) -> None:
        cards: dict[str, CardInfo] = {}
        pending = 0
        errors: list[str] = []
        if self.resolver is not None:
            names = draft_card_names(update)
            self.resolver.request(names)
            cards = self.resolver.lookup(names)
            pending = self.resolver.pending
            errors = self.resolver.errors[-5:]
        self.store.set(build_state(
            update, analysis, self.log_dir, self.store.version + 1,
            cards=cards, cards_pending=pending, card_errors=errors,
        ))

    def _publish_idle(self) -> None:
        with self._last_lock:
            self._last = None
        self.store.set(build_state(None, None, self.log_dir, self.store.version + 1))

    def _on_update(self, update: Update) -> None:
        analysis = analyse(update.draft)
        with self._last_lock:
            self._last = (update, analysis)
        self._publish(update, analysis)

    # -- configuration from the UI
    def set_log_dir(self, value: str) -> dict:
        """Switch the watched directory, persist it, and re-publish state."""
        raw = (value or "").strip().strip('"')
        if not raw:
            return {"ok": False, "error": "enter a directory path"}
        path = Path(raw).expanduser()
        if not path.is_dir():
            return {"ok": False, "error": f"not a directory: {path}"}
        self.log_dir = path
        self.watcher.set_log_dir(path)
        try:
            saved = save_log_dir(path, self.config_path)
        except OSError as e:
            saved = None
            save_error = str(e)
        else:
            save_error = None
        first = self.watcher.poll()
        if first is not None:
            self._on_update(first)
        else:
            self._publish_idle()
        logs = len(list(path.glob(self.watcher.pattern)))
        result = {
            "ok": True,
            "log_dir": str(path),
            "logs_found": logs,
            "newest": newest_log(path).name if logs else None,
            "saved_to": str(saved) if saved else None,
        }
        if save_error:
            result["warning"] = f"watching the new directory, but could not save config: {save_error}"
        return result

    def _on_config(self, payload: dict) -> dict:
        if "log_dir" in payload:
            return self.set_log_dir(str(payload["log_dir"]))
        return {"ok": False, "error": "nothing to change (expected log_dir)"}

    def _on_cards_changed(self) -> None:
        """Called from the resolver thread per card; coalesce into one push."""
        with self._last_lock:
            if self._card_timer is not None:
                return
            self._card_timer = threading.Timer(CARD_PUSH_DEBOUNCE_SECONDS, self._republish)
            self._card_timer.daemon = True
            self._card_timer.start()

    def _republish(self) -> None:
        with self._last_lock:
            self._card_timer = None
            last = self._last
        if last is not None and not self._stop.is_set():
            self._publish(*last)

    def start(self) -> None:
        if self.resolver is not None:
            self.resolver.start()
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
        with self._last_lock:
            if self._card_timer is not None:
                self._card_timer.cancel()
        self.store.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        for t in self._threads:
            t.join(timeout=2)
        if self.resolver is not None:
            self.resolver.stop()

    def wait(self) -> None:
        """Block the main thread until interrupted."""
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        except KeyboardInterrupt:
            pass
