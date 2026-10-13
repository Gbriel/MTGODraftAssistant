"""
Personal history: every MTGO match log on the machine, tied to the draft it
came from, turned into per-card and per-opponent numbers.

Sources (both read-only, DESIGN.md §2.3):
  * ``Match_GameLog_*.dat`` - players, game and match winners, and the cards
    each player showed per game (``matches.py``). Roughly 2,500 on this
    machine; each is summarised once and cached in ``history.sqlite`` keyed
    by (path, size, mtime), so a restart costs milliseconds, not a minute.
  * the draft logs in the log folder - the pod's players and your picks.

A match belongs to the draft whose picks it was played with: of the
non-basic cards you showed in the match, the share that are among a draft's
picks, over the drafts started in the week before the match. Verified on
2026-10-13 over the 67 matches since draft logging began: the right draft
scores 0.92-1.00 and the runner-up at most 0.50, so the threshold is 0.6.
Pod membership is **not** the link - MTGO cube is a league, you play other
entrants, and only 4 of those 67 opponents were in your pod. Match logs
carry no event or format marker, so constructed matches simply score low
against every draft and stay unlinked.

Old MTGO deployment folders keep copies of the same logs, so matches are
deduplicated by id before anything is counted.

Per card:
  * **picked**: drafts in which you took it, and those drafts' match record
    and trophy count (a trophy is 3+ match wins and no losses in the draft).
  * **GIH**: games in which the log shows you with it (cast, played,
    discarded, milled...), and how many you won. MTGO never names your draws
    or hand, so "in hand" here means "became public". Two versions: games of
    linked drafts only (cube), and every match on disk (all formats).
Per opponent: lifetime match record, all formats.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import matches as M
from .draft_log import parse as parse_draft
from .mtgo_deck import draft_started_at

LINK_WINDOW = timedelta(days=7)


# -- matches, summarised and cached ---------------------------------------------


@dataclass
class MatchSummary:
    match_id: str
    path: str
    started_at: str | None            # ISO, the first record's time
    players: list[str]
    winner: str | None
    score: str | None
    # per game: {"winner": name|None, "seen": {player: [card names]}}
    games: list[dict] = field(default_factory=list)

    @property
    def start(self) -> datetime | None:
        return datetime.fromisoformat(self.started_at) if self.started_at else None


def summarise(m: M.MatchLog) -> MatchSummary:
    first = next((g.started_at for g in m.games if g.started_at), None) or m.last_at
    games = []
    for g in m.games:
        seen = {}
        for who, d in g.seen.items():
            names = sorted({n for n in d.values() if not M.is_token(n)})
            if names:
                seen[who] = names
        games.append({"winner": g.winner, "seen": seen})
    return MatchSummary(match_id=m.match_id, path=str(m.path) if m.path else "",
                        started_at=first.isoformat(timespec="seconds") if first else None,
                        players=list(m.players), winner=m.winner, score=m.score, games=games)


class MatchIndex:
    """Summaries of every match log matching ``pattern``, cached by (size, mtime)."""

    def __init__(self, cache_dir: str | Path | None, pattern: str | None = None) -> None:
        self.pattern = pattern
        self._mem: dict[str, tuple[int, int, MatchSummary]] = {}
        self._db: sqlite3.Connection | None = None
        if cache_dir is not None:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(Path(cache_dir) / "history.sqlite"), check_same_thread=False)
            self._db.execute("CREATE TABLE IF NOT EXISTS matches (path TEXT PRIMARY KEY, size INTEGER, "
                             "mtime_ns INTEGER, data TEXT)")
            self._db.commit()
        self._lock = threading.Lock()

    def _cached(self, path: str) -> tuple[int, int, MatchSummary] | None:
        hit = self._mem.get(path)
        if hit is not None:
            return hit
        if self._db is None:
            return None
        row = self._db.execute("SELECT size, mtime_ns, data FROM matches WHERE path=?", (path,)).fetchone()
        if row is None:
            return None
        hit = (row[0], row[1], MatchSummary(**json.loads(row[2])))
        self._mem[path] = hit
        return hit

    def _store(self, path: str, size: int, mtime_ns: int, s: MatchSummary) -> None:
        self._mem[path] = (size, mtime_ns, s)
        if self._db is not None:
            self._db.execute("INSERT OR REPLACE INTO matches VALUES (?,?,?,?)",
                             (path, size, mtime_ns, json.dumps(asdict(s))))

    def scan(self) -> list[MatchSummary]:
        out: list[MatchSummary] = []
        with self._lock:
            dirty = False
            for p in M.match_log_paths(self.pattern):
                try:
                    st = p.stat()
                except OSError:
                    continue
                key = str(p)
                hit = self._cached(key)
                if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
                    out.append(hit[2])
                    continue
                try:
                    s = summarise(M.parse_match(p.read_bytes(), M.match_id_of(p), p))
                except OSError:
                    continue
                self._store(key, st.st_size, st.st_mtime_ns, s)
                dirty = True
                out.append(s)
            if dirty and self._db is not None:
                self._db.commit()
        # the same match can sit in several deployment folders: keep the fullest copy
        best: dict[str, MatchSummary] = {}
        for s in out:
            cur = best.get(s.match_id)
            if cur is None or (len(s.games), s.winner is not None) > (len(cur.games), cur.winner is not None):
                best[s.match_id] = s
        return sorted(best.values(), key=lambda s: s.started_at or "")

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None


# -- drafts ------------------------------------------------------------------------


@dataclass
class DraftSummary:
    path: str
    event_id: str | None
    started_at: datetime
    hero: str | None
    players: list[str]
    picks: list[str]
    set_name: str | None


def scan_drafts(log_dir: str | Path | None) -> list[DraftSummary]:
    """Every MTGO draft log in ``log_dir`` that has a start time and a pod."""
    if log_dir is None:
        return []
    out: list[DraftSummary] = []
    try:
        paths = sorted(Path(log_dir).glob("*.txt"))
    except OSError:
        return []
    for p in paths:
        try:
            d = parse_draft(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        started = draft_started_at(d.timestamp)
        if started is None or not d.players:
            continue
        picks = [c for pk in d.picks if pk.complete for c in pk.picked_all]
        out.append(DraftSummary(path=str(p), event_id=d.event_id, started_at=started, hero=d.hero,
                                players=list(d.players), picks=picks, set_name=d.set_name))
    out.sort(key=lambda d: d.started_at)
    return out


LINK_THRESHOLD = 0.6
NO_CARDS_WINDOW = timedelta(hours=6)     # a match that showed none of your cards: the draft just before it


def shown_by(m: MatchSummary, hero: str) -> set[str]:
    """Non-basic cards ``hero`` showed anywhere in the match."""
    out: set[str] = set()
    for g in m.games:
        out.update(n for n in g["seen"].get(hero, []) if n not in M.BASICS)
    return out


def link_matches(drafts: list[DraftSummary], matches: list[MatchSummary], hero: str | None) -> dict[str, list[MatchSummary]]:
    """Draft path -> the matches played with its picks (see the module docstring)."""
    linked: dict[str, list[MatchSummary]] = defaultdict(list)
    if hero is None:
        return linked
    for m in matches:
        if hero not in m.players or len(m.players) != 2:
            continue
        start = m.start
        if start is None:
            continue
        cands = [d for d in drafts if d.started_at <= start <= d.started_at + LINK_WINDOW]
        if not cands:
            continue
        shown = shown_by(m, hero)
        if not shown:
            latest = max(cands, key=lambda d: d.started_at)
            if start - latest.started_at <= NO_CARDS_WINDOW:
                linked[latest.path].append(m)
            continue
        best, best_score = None, 0.0
        for d in cands:                                   # ties go to the latest draft
            score = len(shown & set(d.picks)) / len(shown)
            if score >= best_score:
                best, best_score = d, score
        if best is not None and best_score >= LINK_THRESHOLD:
            linked[best.path].append(m)
    return linked


def hero_of(drafts: list[DraftSummary], pattern: str | None = None) -> str | None:
    names = {d.hero for d in drafts if d.hero}
    if len(names) == 1:
        return next(iter(names))
    return M.guess_hero(pattern)


# -- the numbers --------------------------------------------------------------------


def _result(m: MatchSummary, hero: str) -> str | None:
    if m.winner == hero:
        return "W"
    if m.winner:
        return "L"
    return None


def compute(drafts: list[DraftSummary], matches: list[MatchSummary], hero: str | None,
            pattern: str | None = None) -> dict:
    hero = hero or hero_of(drafts, pattern)
    linked = link_matches(drafts, matches, hero)
    opponents: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    with_result = 0
    for m in matches:
        if hero is None or hero not in m.players or len(m.players) != 2:
            continue
        r = _result(m, hero)
        if r is None:
            continue
        with_result += 1
        opp = next(p for p in m.players if p != hero)
        opponents[opp][0 if r == "W" else 1] += 1

    cards: dict[str, dict] = defaultdict(lambda: {
        "name": "", "picked": 0, "pick_w": 0, "pick_l": 0, "trophies": 0, "drafts_with_matches": 0,
        "gih_g": 0, "gih_w": 0, "gih_m": 0, "gih_mw": 0, "all_g": 0, "all_w": 0,
    })

    def card(name: str) -> dict:
        c = cards[name]
        c["name"] = name
        return c

    draft_rows = []
    for d in drafts:
        ms = linked.get(d.path, [])
        w = sum(1 for m in ms if _result(m, hero) == "W")
        l = sum(1 for m in ms if _result(m, hero) == "L")
        trophy = w >= 3 and l == 0
        for name in set(d.picks):
            c = card(name)
            c["picked"] += 1
            if ms:
                c["drafts_with_matches"] += 1
                c["pick_w"] += w
                c["pick_l"] += l
                c["trophies"] += int(trophy)
        for m in ms:
            seen_in_match: set[str] = set()
            for g in m.games:
                if not g["winner"]:
                    continue
                for name in g["seen"].get(hero, []):
                    c = card(name)
                    c["gih_g"] += 1
                    c["gih_w"] += int(g["winner"] == hero)
                    seen_in_match.add(name)
            r = _result(m, hero)
            if r:
                for name in seen_in_match:
                    card(name)["gih_m"] += 1
                    card(name)["gih_mw"] += int(r == "W")
        draft_rows.append({
            "path": Path(d.path).name, "event_id": d.event_id, "started_at": d.started_at.isoformat(timespec="seconds"),
            "set_name": d.set_name, "players": d.players, "matches": len(ms), "w": w, "l": l, "trophy": trophy,
            "opponents": [{"name": next(p for p in m.players if p != hero), "result": _result(m, hero), "score": m.score}
                          for m in ms],
        })
    # every match on disk, all formats
    if hero is not None:
        for m in matches:
            if hero not in m.players:
                continue
            for g in m.games:
                if not g["winner"]:
                    continue
                for name in g["seen"].get(hero, []):
                    c = card(name)
                    c["all_g"] += 1
                    c["all_w"] += int(g["winner"] == hero)
    rows = sorted(cards.values(), key=lambda c: (-c["picked"], -c["gih_g"], c["name"]))
    return {
        "hero": hero,
        "matches": len(matches),
        "matches_with_result": with_result,
        "drafts": len(drafts),
        "drafts_linked": sum(1 for d in drafts if linked.get(d.path)),
        "cards": rows,
        "opponents": dict(opponents),
        "draft_rows": draft_rows,
    }


# -- server-facing ------------------------------------------------------------------


class History:
    """Keeps the computed history current; recomputes in a background thread on request."""

    def __init__(self, log_dir: str | Path | None, cache_dir: str | Path | None,
                 pattern: str | None = None, on_change: Callable[[], None] | None = None) -> None:
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.index = MatchIndex(cache_dir, pattern)
        self.pattern = pattern
        self.on_change = on_change
        self.version = 0
        self.error: str | None = None
        self._snapshot: dict | None = None
        self._lock = threading.Lock()
        self._pending = False
        self._thread: threading.Thread | None = None

    def set_log_dir(self, log_dir: str | Path | None) -> None:
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.request_refresh()

    def refresh(self) -> dict:
        """Scan and recompute now (blocking). Safe to call from any thread."""
        try:
            matches = self.index.scan()
            drafts = scan_drafts(self.log_dir)
            snap = compute(drafts, matches, None, self.pattern)
            snap["status"] = "ready"
            snap["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.error = None
        except Exception as e:  # noqa: BLE001 - a history problem must never take the server down
            self.error = f"{type(e).__name__}: {e}"
            snap = dict(self._snapshot or self._empty())
            snap["status"] = "error"
        snap["error"] = self.error
        with self._lock:
            self._snapshot = snap
            self.version += 1
        if self.on_change is not None:
            self.on_change()
        return snap

    def request_refresh(self) -> None:
        """Recompute in the background; coalesces requests made while one runs."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._pending = True
                return
            self._thread = threading.Thread(target=self._run, name="history", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            self.refresh()
            with self._lock:
                if not self._pending:
                    return
                self._pending = False

    def start(self) -> None:
        self.request_refresh()

    def wait(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    @staticmethod
    def _empty() -> dict:
        return {"hero": None, "matches": 0, "matches_with_result": 0, "drafts": 0, "drafts_linked": 0,
                "cards": [], "opponents": {}, "draft_rows": []}

    def snapshot(self) -> dict:
        with self._lock:
            if self._snapshot is None:
                return {**self._empty(), "status": "scanning", "error": None, "updated_at": None, "version": self.version}
            return {**self._snapshot, "version": self.version}

    def records_for(self, names: list[str]) -> dict[str, list[int]]:
        """Lifetime match record against each of ``names`` that you have played before."""
        with self._lock:
            opp = (self._snapshot or {}).get("opponents", {})
            return {n: opp[n] for n in names if n in opp}

    def close(self) -> None:
        self.index.close()
