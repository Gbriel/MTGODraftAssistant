"""
17Lands card ratings for the Arena cube, fetched once a day and cached on disk.

What 17Lands actually serves for ``Cube - Powered`` (checked 2026-09-24, see
DESIGN.md §2.4):

- One flat list of ~540 cards for the *current* Arena cube run. The
  ``start_date``/``end_date`` parameters are required but do not narrow the
  cube data; every window returns the same all-time pool for that run.
- Win-rate fields (``ever_drawn_win_rate`` = GIH WR, ``win_rate`` = GP WR, ...)
  are ``null`` below 500 games. Cube games are spread thin, so early in a run
  almost every card is null. Count fields are always populated.
- ``avg_seen`` (ALSA, average last-seen-at pick) is present for nearly every
  card and is the one signal that is always usable. Low ALSA = the pod takes
  it early. ``avg_pick`` (ATA) is mostly null.

So the UI leads with ALSA, shows GIH WR only when 17Lands publishes it, and
always renders the game count so nobody reads a thin number as a solid one.

Etiquette: one request, serialised, with a User-Agent, backoff on failure,
cached in SQLite, refreshed at most once per ``refresh_hours``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

USER_AGENT = "MTGODraftAssistant/0.1 (local read-only draft tracker)"
RATINGS_URL = "https://www.17lands.com/card_ratings/data"

DEFAULT_EXPANSION = "Cube - Powered"    # the powered/Vintage cube; plain "Cube" is unpowered
DEFAULT_FORMAT = "PremierDraft"
DEFAULT_START_DATE = "2019-01-01"       # dates don't filter cube data, but must be sent
DEFAULT_REFRESH_HOURS = 24.0
DEFAULT_MIN_GAMES = 500                 # 17Lands's own null threshold; keep ours no lower
RETRY_AFTER_FAILURE = 600.0             # seconds before another attempt after an error


# -- names -------------------------------------------------------------------


def normalise(name: str) -> str:
    """Join key: trimmed, single-spaced, ``///`` -> ``//``, case-folded."""
    s = " ".join(name.replace("///", "//").split())
    return s.casefold()


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def front_face(key: str) -> str:
    return key.split("//", 1)[0].strip()


# -- one card ----------------------------------------------------------------


@dataclass(frozen=True)
class CardRating:
    name: str                      # as 17Lands spells it
    color: str = ""
    rarity: str = ""
    alsa: float | None = None      # avg_seen: average last-seen-at pick
    ata: float | None = None       # avg_pick: average taken-at pick
    gih_wr: float | None = None    # ever_drawn_win_rate, None below 17Lands's threshold
    gih_games: int = 0             # ever_drawn_game_count
    gp_wr: float | None = None     # win_rate (games played)
    games: int = 0                 # game_count
    oh_wr: float | None = None     # opening_hand_win_rate
    iwd: float | None = None       # drawn_improvement_win_rate
    play_rate: float | None = None
    seen_count: int = 0
    pick_count: int = 0
    alsa_pct: float | None = None  # 0-100: share of the dataset taken *later* than this card
    gih_pct: float | None = None   # 0-100 among cards that have a GIH WR

    def to_json(self, min_games: int = DEFAULT_MIN_GAMES) -> dict:
        has_wr = self.gih_wr is not None and self.gih_games >= min_games
        return {
            "name": self.name,
            "alsa": self.alsa,
            "ata": self.ata,
            "gih_wr": self.gih_wr if has_wr else None,
            "gih_games": self.gih_games,
            "gp_wr": self.gp_wr if self.games >= min_games else None,
            "games": self.games,
            "oh_wr": self.oh_wr if has_wr else None,
            "iwd": self.iwd if has_wr else None,
            "play_rate": self.play_rate,
            "seen_count": self.seen_count,
            "pick_count": self.pick_count,
            "alsa_pct": self.alsa_pct,
            "gih_pct": self.gih_pct if has_wr else None,
        }


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def rating_from_17lands(row: dict) -> CardRating:
    return CardRating(
        name=str(row.get("name", "")).strip(),
        color=str(row.get("color") or ""),
        rarity=str(row.get("rarity") or ""),
        alsa=_num(row.get("avg_seen")),
        ata=_num(row.get("avg_pick")),
        gih_wr=_num(row.get("ever_drawn_win_rate")),
        gih_games=_int(row.get("ever_drawn_game_count")),
        gp_wr=_num(row.get("win_rate")),
        games=_int(row.get("game_count")),
        oh_wr=_num(row.get("opening_hand_win_rate")),
        iwd=_num(row.get("drawn_improvement_win_rate")),
        play_rate=_num(row.get("play_rate")),
        seen_count=_int(row.get("seen_count")),
        pick_count=_int(row.get("pick_count")),
    )


# -- the whole dataset -------------------------------------------------------


def _percentiles(values: list[tuple[str, float]], higher_is_better: bool) -> dict[str, float]:
    """Map name -> 0..100 share of *other* cards this one beats."""
    if len(values) < 2:
        return {name: 50.0 for name, _ in values}
    ordered = sorted(values, key=lambda kv: kv[1], reverse=not higher_is_better)
    # ordered[0] is the worst; ties share the lowest rank of their group
    out: dict[str, float] = {}
    n = len(ordered) - 1
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        pct = round(100.0 * i / n, 1)
        for k in range(i, j + 1):
            out[ordered[k][0]] = pct
        i = j + 1
    return out


class Dataset:
    """One expansion+format pull, indexed for name lookup."""

    def __init__(self, rows: Iterable[dict], expansion: str, fmt: str,
                 fetched_at: str, min_games: int = DEFAULT_MIN_GAMES) -> None:
        self.expansion = expansion
        self.format = fmt
        self.fetched_at = fetched_at
        self.min_games = min_games
        base = [rating_from_17lands(r) for r in rows]
        base = [r for r in base if r.name]
        alsa_pct = _percentiles([(r.name, r.alsa) for r in base if r.alsa is not None],
                                higher_is_better=False)
        gih_pct = _percentiles(
            [(r.name, r.gih_wr) for r in base if r.gih_wr is not None and r.gih_games >= min_games],
            higher_is_better=True,
        )
        self.cards: dict[str, CardRating] = {}
        for r in base:
            self.cards[r.name] = CardRating(**{
                **r.__dict__, "alsa_pct": alsa_pct.get(r.name), "gih_pct": gih_pct.get(r.name),
            })
        # exact key, then front face, then accent-stripped: first writer wins
        self._by_key: dict[str, CardRating] = {}
        self._by_face: dict[str, CardRating] = {}
        self._by_ascii: dict[str, CardRating] = {}
        for r in self.cards.values():
            key = normalise(r.name)
            self._by_key.setdefault(key, r)
            self._by_face.setdefault(front_face(key), r)
            self._by_ascii.setdefault(strip_accents(key), r)

    def __len__(self) -> int:
        return len(self.cards)

    @property
    def with_win_rate(self) -> int:
        return sum(1 for r in self.cards.values()
                   if r.gih_wr is not None and r.gih_games >= self.min_games)

    def get(self, name: str) -> CardRating | None:
        key = normalise(name)
        hit = self._by_key.get(key)
        if hit is None:
            hit = self._by_face.get(front_face(key))
        if hit is None:
            hit = self._by_ascii.get(strip_accents(key))
        return hit

    def lookup(self, names: Iterable[str]) -> tuple[dict[str, CardRating], list[str]]:
        found: dict[str, CardRating] = {}
        missing: list[str] = []
        for n in names:
            r = self.get(n)
            if r is None:
                missing.append(n)
            else:
                found[n] = r
        return found, missing


# -- HTTP --------------------------------------------------------------------


class RatingsClient:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, min_interval: float = 1.0, timeout: float = 30.0,
                 max_retries: int = 3) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self._last = 0.0
        self._lock = threading.Lock()

    def fetch(self, expansion: str, fmt: str, start_date: str, end_date: str) -> list[dict]:
        url = RATINGS_URL + "?" + urllib.parse.urlencode({
            "expansion": expansion, "format": fmt,
            "start_date": start_date, "end_date": end_date,
        })
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
        backoff = 2.0
        with self._lock:
            for attempt in range(self.max_retries + 1):
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as r:
                        body = r.read()
                except urllib.error.HTTPError as e:
                    if e.code in self.RETRY_STATUSES and attempt < self.max_retries:
                        time.sleep(min(backoff, 30.0))
                        backoff *= 2
                        continue
                    raise
                finally:
                    self._last = time.monotonic()
                data = json.loads(body)
                if not isinstance(data, list):
                    raise ValueError(f"unexpected 17Lands response: {type(data).__name__}")
                return data
        raise RuntimeError("unreachable")


# -- disk cache --------------------------------------------------------------


class RatingsCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS datasets (
                key TEXT PRIMARY KEY,
                expansion TEXT NOT NULL, format TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                body TEXT NOT NULL
            )""")
        self._db.commit()

    @staticmethod
    def key(expansion: str, fmt: str) -> str:
        return f"{expansion}|{fmt}"

    def get(self, expansion: str, fmt: str) -> tuple[list[dict], str] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT body, fetched_at FROM datasets WHERE key=?",
                (self.key(expansion, fmt),),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0]), row[1]

    def put(self, expansion: str, fmt: str, rows: list[dict], fetched_at: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO datasets VALUES (?,?,?,?,?)",
                (self.key(expansion, fmt), expansion, fmt, fetched_at, json.dumps(rows)),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


# -- provider: background refresh --------------------------------------------


class RatingsProvider:
    """
    Owns one dataset. Loads it from the disk cache at construction, refreshes
    it in a background thread when it is older than ``refresh_hours``, and
    calls ``on_change`` when new data lands. ``lookup`` never blocks.
    """

    POLL_SECONDS = 60.0

    def __init__(self, cache_dir: str | Path, expansion: str = DEFAULT_EXPANSION,
                 fmt: str = DEFAULT_FORMAT, client: RatingsClient | None = None,
                 on_change: Callable[[], None] | None = None,
                 refresh_hours: float = DEFAULT_REFRESH_HOURS,
                 min_games: int = DEFAULT_MIN_GAMES,
                 start_date: str = DEFAULT_START_DATE,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self.cache_dir = Path(cache_dir)
        self.expansion = expansion
        self.format = fmt
        self.client = client or RatingsClient()
        self.on_change = on_change
        self.refresh_hours = refresh_hours
        self.min_games = min_games
        self.start_date = start_date
        self._now = now
        self.cache = RatingsCache(self.cache_dir / "ratings.sqlite")
        self._lock = threading.Lock()
        self._dataset: Dataset | None = None
        self._fetching = False
        self._last_attempt: float | None = None       # monotonic
        self._force = False
        self.error: str | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._load_cached()

    # -- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._worker, name="17lands", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.cache.close()

    def refresh_now(self) -> None:
        """Ask the worker to fetch on its next wake regardless of age."""
        with self._lock:
            self._last_attempt = None
            self._force = True
        self._wake.set()

    # -- state
    @property
    def dataset(self) -> Dataset | None:
        with self._lock:
            return self._dataset

    def _load_cached(self) -> None:
        cached = self.cache.get(self.expansion, self.format)
        if cached is None:
            return
        rows, fetched_at = cached
        ds = Dataset(rows, self.expansion, self.format, fetched_at, self.min_games)
        with self._lock:
            self._dataset = ds

    def _age_hours(self, ds: Dataset | None) -> float | None:
        if ds is None or not ds.fetched_at:
            return None
        try:
            then = datetime.fromisoformat(ds.fetched_at)
        except ValueError:
            return None
        return (self._now() - then).total_seconds() / 3600.0

    def is_stale(self) -> bool:
        age = self._age_hours(self.dataset)
        return age is None or age >= self.refresh_hours

    def _due(self) -> bool:
        with self._lock:
            force = self._force
            last = self._last_attempt
        if not force and not self.is_stale():
            return False
        return last is None or time.monotonic() - last >= RETRY_AFTER_FAILURE

    # -- queries
    def lookup(self, names: Iterable[str]) -> tuple[dict[str, CardRating], list[str]]:
        ds = self.dataset
        if ds is None:
            return {}, list(names)
        return ds.lookup(names)

    def status(self) -> dict:
        ds = self.dataset
        with self._lock:
            fetching = self._fetching
            error = self.error
        if ds is not None:
            state = "ok"
        elif fetching:
            state = "fetching"
        elif error:
            state = "error"
        else:
            state = "empty"
        age = self._age_hours(ds)
        return {
            "source": "17Lands",
            "expansion": self.expansion,
            "format": self.format,
            "status": state,
            "fetching": fetching,
            "fetched_at": ds.fetched_at if ds else None,
            "age_hours": round(age, 1) if age is not None else None,
            "cards": len(ds) if ds else 0,
            "with_win_rate": ds.with_win_rate if ds else 0,
            "min_games": self.min_games,
            "error": error,
        }

    # -- worker
    def _worker(self) -> None:
        while not self._stop.is_set():
            if self._due():
                self._fetch_once()
            self._wake.wait(self.POLL_SECONDS)
            self._wake.clear()

    def _fetch_once(self) -> None:
        with self._lock:
            self._fetching = True
            self._force = False
            self._last_attempt = time.monotonic()
        self._notify()
        try:
            end = self._now().date().isoformat()
            rows = self.client.fetch(self.expansion, self.format, self.start_date, end)
            fetched_at = self._now().isoformat(timespec="seconds")
            ds = Dataset(rows, self.expansion, self.format, fetched_at, self.min_games)
            if len(ds) == 0:
                raise ValueError(f"17Lands returned no cards for {self.expansion!r}/{self.format!r}")
            self.cache.put(self.expansion, self.format, rows, fetched_at)
            with self._lock:
                self._dataset = ds
                self.error = None
        except Exception as e:  # network, HTTP, JSON, empty: all recorded, all retried later
            with self._lock:
                self.error = f"{type(e).__name__}: {e}"
        finally:
            with self._lock:
                self._fetching = False
        self._notify()

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()
