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
import re
import threading
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .analysis import Analysis, analyse
from .arena_cards import ArenaCardDb
from .arena_log import DEFAULT_LOG as arena_default_log
from .arena_log import ArenaWatcher
from .arena_log import log_status as arena_log_status
from .auto_watcher import AutoWatcher
from .config import save_arena_log, save_log_dir


def arena_default() -> Path:
    return arena_default_log
from .cube_list import CubeList, from_names
from .pool import PoolReport, pool_state
from .ratings import CardRating, RatingsPool, RatingsProvider, dataset_for_event
from .scryfall import CardInfo, CardResolver
from .watcher import DraftWatcher, Update, newest_log

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
SSE_KEEPALIVE_SECONDS = 15.0
CARD_PUSH_DEBOUNCE_SECONDS = 0.3


def ui_version() -> str:
    """
    Fingerprint of the page files. Sent in every state; the page reloads
    itself when it changes, so a restarted server never talks to a stale
    tab that is still running the previous JavaScript.
    """
    parts = []
    for name in ("index.html", "app.js", "style.css"):
        try:
            parts.append(str((WEB_DIR / name).stat().st_mtime_ns))
        except OSError:
            parts.append("0")
    return "-".join(parts)


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
                card_errors: list[str] | None = None,
                ratings: dict[str, CardRating] | None = None,
                ratings_meta: dict | None = None,
                ratings_unmatched: list[str] | None = None,
                pool: PoolReport | None = None,
                ratings_sets: dict[str, dict[str, CardRating]] | None = None,
                arena_game: dict | None = None) -> dict:
    """JSON-serialisable view of everything the UI needs."""
    min_games = (ratings_meta or {}).get("min_games", 0)
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
        # 17Lands: per-card numbers for names in this draft, dataset status,
        # and the names that have no Arena data (real information, not noise)
        "ratings": {name: r.to_json(min_games) for name, r in (ratings or {}).items()},
        # the same cards in the other period / player-group combinations, keyed
        # "PERIOD|group" (e.g. "LATEST_EVENT|top"); the page's toggles pick one
        "ratings_sets": {key: {name: r.to_json(min_games) for name, r in cards.items()}
                         for key, cards in (ratings_sets or {}).items()},
        "ratings_meta": ratings_meta,           # None when ratings are disabled
        "ratings_unmatched": list(ratings_unmatched or []),
        "pool": pool.to_json() if pool is not None else None,
        # Arena only: the submitted deck and, during a game, what is still in the library
        "arena_game": arena_game,
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
            "pod_size_source": analysis.pod_size_source,
            "pack_sizes": {str(k): v for k, v in analysis.pack_sizes.items()},
            "cards_per_pick": analysis.cards_per_pick,
            "source": d.source,
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
                "picked_all": p.picked_all,
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
    :class:`CardResolver` that fills in colours and images in the background
    and an optional :class:`RatingsProvider` for 17Lands numbers.
    Pass ``resolver=None`` for a text-only server (tests, offline use).
    """

    def __init__(self, log_dir: str | Path | None, host: str = "127.0.0.1", port: int = 8765,
                 interval: float = 0.5, verbose: bool = False,
                 resolver: CardResolver | None = None,
                 ratings: RatingsProvider | None = None,
                 config_path: Path | None = None,
                 allow_config: bool = True,
                 watcher: DraftWatcher | ArenaWatcher | AutoWatcher | None = None,
                 cube: CubeList | None = None,
                 arena_cube: RatingsProvider | None = None,
                 ratings_sets: dict[str, RatingsProvider] | None = None,
                 arena_db: ArenaCardDb | None = None,
                 ratings_pool: RatingsPool | None = None) -> None:
        self.log_dir: Path | None = Path(log_dir) if log_dir is not None else None
        self.interval = interval
        self.verbose = verbose
        self._ui_version = ui_version()
        self.arena_db = arena_db                # Arena's own card database: names every id, offline
        self.resolver = resolver
        # 17Lands datasets: one per (expansion, event type, period, group), chosen per
        # draft from its event name (a set draft wants that set, a cube draft the cube)
        if ratings_pool is None:
            ratings_pool = RatingsPool(default=(ratings.expansion, ratings.format) if ratings else
                                       (RatingsPool.__init__.__defaults__[4]),  # type: ignore[index]
                                       time_period=ratings.time_period if ratings else "ALL_TIME")
        self.pool = ratings_pool
        self.pool.on_change = self._on_cards_changed
        for p in [ratings, arena_cube, *(ratings_sets or {}).values()]:
            if p is not None:
                self.pool.add(p)
        self.ratings_enabled = ratings is not None or ratings_pool.cache_dir is not None \
            or bool(self.pool.providers())
        self.cube = cube                        # explicit list; else 17Lands's for Arena drafts
        self._cube_from_ratings: CubeList | None = None
        self._cube_key: tuple | None = None
        self.config_path = config_path          # None -> default config.toml location
        if resolver is not None:
            resolver.on_change = self._on_cards_changed
        if watcher is None:
            watcher = DraftWatcher(self.log_dir, interval=interval)
        self.watcher = watcher
        self.arena = isinstance(watcher, ArenaWatcher)          # Arena only
        self.auto = isinstance(watcher, AutoWatcher)            # both, follow the latest
        if self.arena:
            watcher.namer = self._arena_name
            self.log_dir = watcher.log_dir
        elif self.auto:
            watcher.arena.namer = self._arena_name
        self.config_locked = not allow_config
        self._settings_version = 0
        self.store = StateStore(self._idle_state(0))
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

    def _dataset_key(self, source: str, update: Update | None) -> tuple[str, str]:
        """
        (expansion, event_type) for the draft on screen. An Arena event names
        the expansion; the event type is always the configured default
        (PremierDraft), because that is where the data is: a pick-two or
        quick draft of a set is best served by the set's premier-draft numbers.
        """
        if source == "arena" and update is not None:
            found = dataset_for_event(update.draft.set_name)
            if found is not None:
                return found[0], self.pool.default[1]
        return self.pool.default

    def _ratings_for(self, source: str, update: Update | None):
        """(primary provider, alternates, meta) for the active draft; (None, {}, None) if disabled."""
        if not self.ratings_enabled:
            return None, {}, None
        expansion, fmt = self._dataset_key(source, update)
        primary, alts = self.pool.sets_for(expansion, fmt)
        if primary is None:
            # nothing for this dataset and no way to create one: say so honestly
            return None, {}, {"source": "17Lands", "expansion": expansion, "format": fmt,
                              "time_period": self.pool.time_period, "user_group": "",
                              "status": "empty", "fetching": False, "fetched_at": None,
                              "age_hours": None, "cards": 0, "with_win_rate": 0,
                              "min_games": self.pool.min_games, "error": None,
                              "retry_in": None, "sets": {}}
        meta = primary.status()
        meta["sets"] = {key: p.status() for key, p in alts.items()}
        return primary, alts, meta

    def _idle_state(self, version: int) -> dict:
        _, _, meta = self._ratings_for("mtgo", None)
        return self._decorate(build_state(None, None, self.log_dir, version, ratings_meta=meta))

    def _decorate(self, state: dict, source: str | None = None) -> dict:
        if source is None:
            source = "arena" if self.arena else "auto" if self.auto else "mtgo"
        state["source"] = source
        state["config_locked"] = self.config_locked
        state["ui_version"] = self._ui_version
        state["arena_log"] = str(self.watcher.arena_log) if self.auto else (
            str(self.watcher.log_path) if self.arena else None)
        state["sources"] = self._sources()
        return state

    def _active_source(self, update: Update) -> str:
        return update.draft.source if (self.auto or self.arena) else "mtgo"

    def _arena_watcher(self) -> ArenaWatcher | None:
        if self.arena:
            return self.watcher
        if self.auto:
            return self.watcher.arena
        return None

    def _mtgo_watcher(self) -> DraftWatcher | None:
        if self.auto:
            return self.watcher.mtgo
        if not self.arena:
            return self.watcher
        return None

    def _sources(self) -> dict:
        """
        Per client: where the log is expected, whether it is there, and what
        the user can do about it. Rendered by the settings panel.
        """
        mtgo_w = self._mtgo_watcher()
        mtgo: dict = {"watched": mtgo_w is not None, "log_dir": str(self.log_dir) if self.log_dir else None,
                      "exists": False, "logs_found": 0, "status": "not_set"}
        if self.log_dir is not None:
            if self.log_dir.is_dir():
                mtgo["exists"] = True
                try:
                    mtgo["logs_found"] = len(list(self.log_dir.glob("*.txt")))
                except OSError:
                    pass
                mtgo["status"] = "ok" if mtgo["logs_found"] else "no_logs"
            else:
                mtgo["status"] = "missing"
        arena_w = self._arena_watcher()
        arena: dict = {"watched": arena_w is not None}
        path = arena_w.log_path if arena_w is not None else arena_default()
        arena.update(arena_log_status(path))
        if not arena["exists"]:
            arena["status"] = "missing"
        elif arena["detailed_logs"] is False:
            arena["status"] = "detailed_logs_off"
        else:
            arena["status"] = "ok"
        return {"mtgo": mtgo, "arena": arena}

    def _arena_name(self, grp_id: int) -> str | None:
        """
        Arena card id -> name. Arena's own database first (complete, offline),
        then 17Lands (no network), then Scryfall's arena lookup (cached, async;
        it does not know Arena-only printings such as id 101033, Dismember).
        """
        if self.arena_db is not None:
            name = self.arena_db.name(grp_id)
            if name:
                return name
        for provider in self._providers():
            name = provider.arena_name(grp_id)
            if name:
                return name
        if self.resolver is not None:
            return self.resolver.arena_name(grp_id)
        return None

    def current_cube(self, source: str = "mtgo", update: Update | None = None) -> CubeList | None:
        """
        The explicit cube list, or for an Arena draft the 17Lands card list of
        its dataset: the latest event's, so a cube's retired cards stay out.
        """
        if self.cube is not None:
            return self.cube
        if source != "arena" or not self.ratings_enabled:
            return None
        expansion, fmt = self._dataset_key(source, update)
        provider = self.pool.get(expansion, fmt, RatingsPool.PERIOD_ALT, "", create=False) \
            or self.pool.get(expansion, fmt, None, "", create=False)
        if provider is None:
            return None
        ds = provider.dataset
        if ds is None:
            return None
        key = (expansion, fmt, provider.time_period, ds.fetched_at, len(ds))
        if self._cube_key != key:
            label = f"17Lands {ds.expansion}" + (" current run" if provider.time_period == RatingsPool.PERIOD_ALT else "")
            cl = from_names(list(ds.cards), label)
            cl.fetched_at = ds.fetched_at
            self._cube_from_ratings = cl
            self._cube_key = key
        return self._cube_from_ratings

    def _arena_game_state(self, source: str) -> dict | None:
        """Deck and live game for an Arena draft, with card ids named."""
        if source != "arena" or not hasattr(self.watcher, "deck"):
            return None
        deck = self.watcher.deck()
        game = self.watcher.game()
        if deck is None and game is None:
            return None

        def card(grp: int, n: int) -> dict:
            name = self._arena_name(grp)
            return {"grp": grp, "name": name or f"#{grp}", "n": n, "named": name is not None}

        out: dict = {"deck": None, "game": None}
        if deck is not None:
            out["deck"] = {
                "event": deck.event_name, "submitted_at": deck.submitted_at,
                "main": [card(g, q) for g, q in deck.main],
                "side": [card(g, q) for g, q in deck.side],
                "main_count": deck.main_count, "side_count": deck.side_count,
            }
        if game is not None:
            out["game"] = {
                **{k: game[k] for k in ("match_id", "game_number", "stage", "over", "turn", "my_seat",
                                        "opponent", "life", "library", "seat_known")},
                "remaining": [card(g, n) for g, n in sorted((game["remaining"] or {}).items())]
                             if game["remaining"] is not None else None,
                "seen_mine": [card(g, n) for g, n in game["seen_mine"].items()],
                "unexpected": [card(g, n) for g, n in game["unexpected"].items()],
                "seen_theirs": [card(g, n) for g, n in game["seen_theirs"].items()],
            }
        return out

    @staticmethod
    def _game_names(state: dict | None) -> list[str]:
        if not state:
            return []
        names: list[str] = []
        for part in (state.get("deck") or {}).get("main", []), (state.get("deck") or {}).get("side", []):
            names += [c["name"] for c in part if c["named"]]
        g = state.get("game") or {}
        for key in ("remaining", "seen_mine", "seen_theirs", "unexpected"):
            names += [c["name"] for c in (g.get(key) or []) if c["named"]]
        return names

    def _name_errors(self, errors: list[str]) -> list[str]:
        """Scryfall error lines mention Arena ids; add the card's name when we know it."""
        def sub(m) -> str:
            grp = int(m.group(1))
            name = self._arena_name(grp)
            return f"arena {grp} ({name})" if name else f"arena {grp} (name unknown)"
        return [re.sub(r"arena (\d+)", sub, e) for e in errors]

    def _publish(self, update: Update, analysis: Analysis) -> None:
        names = draft_card_names(update)
        source = self._active_source(update)
        cube = self.current_cube(source, update)
        pool = pool_state(update.draft, analysis, cube)
        arena_game = self._arena_game_state(source)
        cards: dict[str, CardInfo] = {}
        pending = 0
        errors: list[str] = []
        if self.resolver is not None:
            wanted = names + [n for n in self._game_names(arena_game) if n not in names]
            self.resolver.request(wanted)
            cards = self.resolver.lookup(wanted)
            pending = self.resolver.pending
            errors = self._name_errors(self.resolver.errors[-5:])
        ratings: dict[str, CardRating] = {}
        unmatched: list[str] = []
        primary, alts, meta = self._ratings_for(source, update)
        if primary is not None:
            ratings, unmatched = primary.lookup(names)
        elif meta is not None:
            unmatched = list(names)
        sets = {key: p.lookup(names)[0] for key, p in alts.items()}
        log_dir = update.path.parent if source == "arena" else self.log_dir
        self.store.set(self._decorate(build_state(
            update, analysis, log_dir, self.store.version + 1,
            cards=cards, cards_pending=pending, card_errors=errors,
            ratings=ratings, ratings_meta=meta, ratings_unmatched=unmatched,
            pool=pool, ratings_sets=sets, arena_game=arena_game,
        ), source))

    def _publish_idle(self) -> None:
        with self._last_lock:
            self._last = None
        self.store.set(self._idle_state(self.store.version + 1))

    def _on_update(self, update: Update) -> None:
        analysis = analyse(update.draft)
        with self._last_lock:
            self._last = (update, analysis)
        if self.verbose:
            d = update.draft
            live = d.current_pack
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {d.source} "
                  f"{'NEW DRAFT ' if update.new_draft else ''}P{analysis.current_pack}P{analysis.current_pick} "
                  f"{'on screen' if live else 'waiting'} - {sum(p.complete for p in d.picks)} picks, "
                  f"{len(d.picks)} blocks", flush=True)
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
        if self._mtgo_watcher() is not None:
            self.watcher.set_log_dir(path)
        try:
            saved = save_log_dir(path, self.config_path)
        except OSError as e:
            saved = None
            save_error = str(e)
        else:
            save_error = None
        first = self.watcher.poll() if self._mtgo_watcher() is not None else None
        if first is not None:
            self._on_update(first)
        else:
            self._publish_idle()
        logs = len(list(path.glob("*.txt")))
        result = {
            "ok": True,
            "log_dir": str(path),
            "logs_found": logs,
            "newest": newest_log(path).name if logs else None,
            "saved_to": str(saved) if saved else None,
            "watched": self._mtgo_watcher() is not None,
        }
        if save_error:
            result["warning"] = f"watching the new directory, but could not save config: {save_error}"
        elif not result["watched"]:
            result["warning"] = "saved, but this run watches Arena only (started with --arena)"
        return result

    def set_arena_log(self, value: str) -> dict:
        """
        Re-point Arena's Player.log and persist it. Empty = the default
        location. The file need not exist yet (Arena may not have run), but a
        warning says so. A directory means the Player.log inside it.
        """
        raw = (value or "").strip().strip('"')
        path: Path | None = Path(raw).expanduser() if raw else None
        if path is not None and path.is_dir():
            path = path / "Player.log"
        if path is not None and not path.is_absolute():
            return {"ok": False, "error": f"enter a full path to Player.log, not {raw!r}"}
        if path is not None and path.name.lower() != "player.log":
            return {"ok": False, "error": f"the Arena log is called Player.log; got {path.name!r}"}
        if self.auto:
            self.watcher.set_arena_log(path)
        elif self.arena:
            self.watcher.set_log_dir(path)
        try:
            saved = save_arena_log(path, self.config_path)
            save_error = None
        except OSError as e:
            saved, save_error = None, str(e)
        first = self.watcher.poll() if (self.auto or self.arena) else None
        if first is not None:
            self._on_update(first)
        else:
            self._publish_idle()
        status = arena_log_status(path or arena_default())
        result = {"ok": True, "arena_log": status["path"], "is_default": status["is_default"],
                  "exists": status["exists"], "detailed_logs": status["detailed_logs"],
                  "saved_to": str(saved) if saved else None, "watched": self.auto or self.arena}
        if not status["exists"]:
            result["warning"] = "no Player.log there yet; it appears once Arena runs with Detailed Logs on"
        elif status["detailed_logs"] is False:
            result["warning"] = "found, but Arena wrote it with Detailed Logs off"
        if save_error:
            result["warning"] = (result.get("warning", "") + " · could not save config: " + save_error).strip(" ·")
        return result

    def _on_config(self, payload: dict) -> dict:
        if "log_dir" in payload:
            return self.set_log_dir(str(payload["log_dir"]))
        if "arena_log" in payload:
            return self.set_arena_log(str(payload["arena_log"] or ""))
        return {"ok": False, "error": "nothing to change (expected log_dir or arena_log)"}

    def _on_cards_changed(self) -> None:
        """Called from the resolver/ratings threads; coalesce into one push."""
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
        if self._stop.is_set():
            return
        if last is not None:
            if self.arena or self.auto:
                # card names may have arrived: re-render the draft from the log
                rebuilt = self.watcher.rebuild()
                if rebuilt is not None:
                    self._on_update(rebuilt)
                    return
            self._publish(*last)
        else:
            # no draft yet, but the ratings status line still wants updating
            self.store.set(self._idle_state(self.store.version + 1))

    def start(self) -> None:
        if self.resolver is not None:
            self.resolver.start()
        self.pool.start()
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
        self.pool.stop()

    def _providers(self) -> list[RatingsProvider]:
        return self.pool.providers()

    def wait(self) -> None:
        """Block the main thread until interrupted."""
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        except KeyboardInterrupt:
            pass
