"""
Card metadata and small images from Scryfall, fetched lazily and cached on disk.

Why lazy rather than a bulk file: the bulk card dump is hundreds of megabytes
and would need to be held in memory or indexed. A cube draft shows you at most
a few hundred distinct names, and the same cube recurs, so we look cards up as
they appear in the log and remember them forever in SQLite. Small images
(146x204 JPEG, ~10-15KB) are stored alongside.

Etiquette (https://scryfall.com/docs/api): identify ourselves, serialise
requests, keep a gap between them, back off on 429. Names are looked up in
batches through ``POST /cards/collection`` (up to 75 per call), so a fresh
15-card pack costs one metadata request plus one image download per card.
Empirically Scryfall 429s at a steady 10 req/s, so the default gap is 150ms.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

USER_AGENT = "MTGODraftAssistant/0.1 (local read-only draft tracker)"
BASE_URL = "https://api.scryfall.com"
COLLECTION_BATCH = 75

GROUP_ORDER = ["W", "U", "B", "R", "G", "M", "C", "L"]


@dataclass
class CardInfo:
    name: str                               # exactly as MTGO wrote it
    status: str                             # "ok" | "missing"
    scryfall_id: str | None = None
    scryfall_name: str | None = None
    colors: list[str] = field(default_factory=list)
    color_identity: list[str] = field(default_factory=list)
    type_line: str = ""
    mana_cost: str = ""
    cmc: float = 0.0
    image_url: str | None = None
    image_file: str | None = None           # basename inside the image dir

    @property
    def group(self) -> str:
        """W/U/B/R/G single colour, M multicolour, C colourless, L land."""
        if "Land" in self.type_line.split(" // ")[0]:
            return "L"
        if len(self.colors) == 0:
            return "C"
        if len(self.colors) > 1:
            return "M"
        return self.colors[0]

    def to_json(self, image_prefix: str = "/img/") -> dict:
        return {
            "status": self.status,
            "group": self.group if self.status == "ok" else "X",
            "colors": self.colors,
            "color_identity": self.color_identity,
            "type_line": self.type_line,
            "mana_cost": self.mana_cost,
            "cmc": self.cmc,
            "image": f"{image_prefix}{self.image_file}" if self.image_file else None,
        }


def card_from_scryfall(name: str, data: dict) -> CardInfo:
    faces = data.get("card_faces") or []
    front = faces[0] if faces else {}
    image_uris = data.get("image_uris") or front.get("image_uris") or {}
    colors = data.get("colors")
    if colors is None:
        colors = front.get("colors", [])
    return CardInfo(
        name=name,
        status="ok",
        scryfall_id=data.get("id"),
        scryfall_name=data.get("name"),
        colors=sorted(colors, key=lambda c: "WUBRG".find(c)),
        color_identity=sorted(data.get("color_identity", []), key=lambda c: "WUBRG".find(c)),
        type_line=data.get("type_line") or front.get("type_line", ""),
        mana_cost=data.get("mana_cost") or front.get("mana_cost", ""),
        cmc=float(data.get("cmc") or 0.0),
        image_url=image_uris.get("small"),
        image_file=f"{data['id']}.jpg" if data.get("id") and image_uris.get("small") else None,
    )


# -- HTTP client -------------------------------------------------------------


class ScryfallClient:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, min_interval: float = 0.15, timeout: float = 10.0,
                 max_retries: int = 4) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self._last = 0.0
        self._lock = threading.Lock()

    def _request(self, url: str, accept: str, data: bytes | None = None) -> bytes | None:
        """Throttled request with backoff on 429/5xx. Returns None on 404."""
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if data is not None:
            headers["Content-Type"] = "application/json"
        backoff = 1.0
        with self._lock:
            for attempt in range(self.max_retries + 1):
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                req = urllib.request.Request(url, data=data, headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as r:
                        return r.read()
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        return None
                    if e.code in self.RETRY_STATUSES and attempt < self.max_retries:
                        retry_after = e.headers.get("Retry-After") if e.headers else None
                        try:
                            delay = float(retry_after) if retry_after else backoff
                        except ValueError:
                            delay = backoff
                        time.sleep(min(delay, 30.0))
                        backoff = min(backoff * 2, 16.0)
                        continue
                    raise
                finally:
                    self._last = time.monotonic()
        return None  # unreachable; keeps type checkers happy

    def lookup(self, name: str) -> dict | None:
        """One card by exact name, falling back to fuzzy. None if unknown."""
        for mode in ("exact", "fuzzy"):
            url = f"{BASE_URL}/cards/named?" + urllib.parse.urlencode({mode: name})
            body = self._request(url, "application/json")
            if body is not None:
                return json.loads(body)
        return None

    def lookup_many(self, names: list[str]) -> tuple[dict[str, dict], list[str]]:
        """
        Batch lookup via /cards/collection. Returns (found, not_found) where
        found maps the *requested* name to its card JSON.
        """
        found: dict[str, dict] = {}
        not_found: list[str] = []
        for i in range(0, len(names), COLLECTION_BATCH):
            chunk = names[i:i + COLLECTION_BATCH]
            payload = json.dumps({"identifiers": [{"name": n} for n in chunk]}).encode("utf-8")
            body = self._request(f"{BASE_URL}/cards/collection", "application/json", data=payload)
            if body is None:
                not_found.extend(chunk)
                continue
            resp = json.loads(body)
            missing = {ident.get("name") for ident in resp.get("not_found", [])}
            cards = list(resp.get("data", []))
            # data preserves identifier order, with not_found entries skipped.
            for n in chunk:
                if n in missing:
                    not_found.append(n)
                elif cards:
                    found[n] = cards.pop(0)
                else:
                    not_found.append(n)
        return found, not_found

    def download(self, url: str) -> bytes | None:
        return self._request(url, "image/jpeg")


# -- SQLite cache ------------------------------------------------------------


class CardCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                name TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                scryfall_id TEXT, scryfall_name TEXT,
                colors TEXT, color_identity TEXT,
                type_line TEXT, mana_cost TEXT, cmc REAL,
                image_url TEXT, image_file TEXT,
                fetched_at TEXT
            )""")
        self._db.commit()

    def get(self, name: str) -> CardInfo | None:
        with self._lock:
            row = self._db.execute(
                "SELECT name,status,scryfall_id,scryfall_name,colors,color_identity,"
                "type_line,mana_cost,cmc,image_url,image_file FROM cards WHERE name=?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return CardInfo(
            name=row[0], status=row[1], scryfall_id=row[2], scryfall_name=row[3],
            colors=json.loads(row[4] or "[]"), color_identity=json.loads(row[5] or "[]"),
            type_line=row[6] or "", mana_cost=row[7] or "", cmc=row[8] or 0.0,
            image_url=row[9], image_file=row[10],
        )

    def put(self, info: CardInfo) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (info.name, info.status, info.scryfall_id, info.scryfall_name,
                 json.dumps(info.colors), json.dumps(info.color_identity),
                 info.type_line, info.mana_cost, info.cmc, info.image_url, info.image_file,
                 datetime.now().isoformat(timespec="seconds")),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


# -- resolver: background worker ---------------------------------------------


class CardResolver:
    """
    Hand it card names; it fills the cache in a background thread and calls
    ``on_change`` whenever new information landed. ``lookup`` is always
    non-blocking and returns whatever is known right now.
    """

    RETRY_AFTER = 60.0     # seconds before retrying a name that errored

    def __init__(self, cache_dir: str | Path, client: ScryfallClient | None = None,
                 on_change: Callable[[], None] | None = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.image_dir = self.cache_dir / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.cache = CardCache(self.cache_dir / "cards.sqlite")
        self.client = client or ScryfallClient()
        self.on_change = on_change
        self._mem: dict[str, CardInfo] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._pending: set[str] = set()
        self._failed: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors: list[str] = []

    # -- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._worker, name="scryfall", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.cache.close()

    # -- queries
    def _known(self, name: str) -> CardInfo | None:
        info = self._mem.get(name)
        if info is None:
            info = self.cache.get(name)
            if info is not None:
                self._mem[name] = info
        return info

    def _has_image(self, info: CardInfo) -> bool:
        return bool(info.image_file) and (self.image_dir / info.image_file).is_file()

    def _needs_work(self, info: CardInfo | None) -> bool:
        if info is None:
            return True
        return info.status == "ok" and bool(info.image_url) and not self._has_image(info)

    def lookup(self, names: Iterable[str]) -> dict[str, CardInfo]:
        out: dict[str, CardInfo] = {}
        for n in names:
            info = self._known(n)
            if info is not None:
                if info.image_file and not self._has_image(info):
                    # metadata cached but image not on disk yet: don't advertise it
                    info = CardInfo(**{**info.__dict__, "image_file": None})
                out[n] = info
        return out

    def request(self, names: Iterable[str]) -> int:
        """Queue any names we don't fully have. Returns how many were queued."""
        queued = 0
        now = time.monotonic()
        with self._lock:
            for n in names:
                if n in self._pending:
                    continue
                if n in self._failed and now - self._failed[n] < self.RETRY_AFTER:
                    continue
                if not self._needs_work(self._known(n)):
                    continue
                self._pending.add(n)
                self._queue.put(n)
                queued += 1
        return queued

    @property
    def pending(self) -> int:
        return len(self._pending)

    # -- worker
    def _drain(self) -> list[str]:
        """Block for one name, then grab whatever else is queued (up to a batch)."""
        try:
            names = [self._queue.get(timeout=0.25)]
        except queue.Empty:
            return []
        while len(names) < COLLECTION_BATCH:
            try:
                names.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return names

    def _worker(self) -> None:
        while not self._stop.is_set():
            names = self._drain()
            if not names:
                continue
            try:
                self._resolve_batch(names)
            finally:
                with self._lock:
                    for n in names:
                        self._pending.discard(n)
            self._notify()

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()

    def _fail(self, name: str, exc: Exception) -> None:
        with self._lock:
            self._failed[name] = time.monotonic()
        self.errors.append(f"{name}: {exc}")
        del self.errors[:-20]

    def _resolve_batch(self, names: list[str]) -> None:
        # 1. metadata for anything we've never seen
        unknown = [n for n in names if self._known(n) is None]
        if unknown:
            try:
                found, not_found = self.client.lookup_many(unknown)
            except Exception as e:
                for n in unknown:
                    self._fail(n, e)
                found, not_found = {}, []
            for n, data in found.items():
                self._store(card_from_scryfall(n, data))
            for n in not_found:
                # Exact-name miss: MTGO may format split/DFC names differently.
                try:
                    data = self.client.lookup(n)
                except Exception as e:
                    self._fail(n, e)
                    continue
                self._store(card_from_scryfall(n, data) if data else CardInfo(name=n, status="missing"))
            if found or not_found:
                self._notify()

        # 2. images, one at a time
        for n in names:
            if self._stop.is_set():
                return
            info = self._known(n)
            if info is None or info.status != "ok" or not info.image_url or not info.image_file:
                continue
            if self._has_image(info):
                continue
            try:
                body = self.client.download(info.image_url)
            except Exception as e:
                self._fail(n, e)
                continue
            if body:
                tmp = self.image_dir / (info.image_file + ".part")
                tmp.write_bytes(body)
                tmp.replace(self.image_dir / info.image_file)
                self._notify()

    def _store(self, info: CardInfo) -> None:
        self.cache.put(info)
        self._mem[info.name] = info
