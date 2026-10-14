"""
Personal history: every MTGO match log on the machine, grouped into the
decks they were played with, turned into per-card and per-opponent numbers.

Sources (both read-only, DESIGN.md §2.3):
  * ``Match_GameLog_*.dat`` - players, game and match winners, and the cards
    each player showed per game (``matches.py``). Roughly 1,300 unique on
    this machine; each is summarised once and cached in ``history.sqlite``
    keyed by (path, size, mtime), so a restart costs milliseconds. Old MTGO
    deployment folders keep copies of the same logs: deduplicated by id.
  * the draft logs in the log folder - the pod's players and your picks.
    These only exist from the day draft logging was switched on.

**Decks.** A deck is one league entry: the matches you played with one set
of cards. Where a draft log exists, the deck is that draft and its matches
are found by overlap: of the non-basic cards you showed in a match, the
share among a draft's picks is 0.92-1.00 for the right draft and at most
0.50 for any other (67 matches, 2026-10-13). Where no draft log exists
(everything before 2026-09-21), decks are inferred from the matches alone:
walking them in time order, a match joins the latest open deck whose shown
cards it overlaps by >= 0.25, within 24 hours, while that deck has fewer
than three finished matches (a cube league is three matches). Tuned
against the 19 logged drafts: all 58 same-draft pairs reproduced, none
merged or split wrongly. The threshold is lower than for draft logs because
one match shows only ~15 of a deck's ~23 non-basics.

**Constructed** decks are the same cards for weeks, far beyond three
matches. A first pass looks for them: matches within a week that each
overlap the chain's *first* match by >= 0.4 both ways (comparing with the
union instead snowballs: a run of similar cube decks grew to 485 matches and
the whole cube). Five or more finished matches on a pool of <= 45 non-basics
is constructed; those matches count in the all-formats numbers and nowhere
else. On this machine (2026-10-13) there are none: every match is cube.

Per card:
  * **played**: decks (draft or inferred, not constructed) in which you
    showed it, with those decks' match record and trophies (3+ match wins,
    no losses in the deck).
  * **picked**: logged drafts in which you took it, with the same numbers.
  * **games**: games the log shows you with the card, counted once per game
    however often you cast it, and whether you won. MTGO never names your
    draws or hand, so this is "played", not game-in-hand. Two versions:
    cube decks only, and every match on disk.
Per opponent: lifetime match record, all formats.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import matches as M
from .draft_log import parse as parse_draft
from .mtgo_deck import draft_started_at

# draft logs <- matches
LINK_WINDOW = timedelta(days=7)
LINK_THRESHOLD = 0.6
NO_CARDS_WINDOW = timedelta(hours=6)     # a match that showed none of your cards: the draft just before it
# matches -> inferred decks
DECK_THRESHOLD = 0.25
DECK_WINDOW = timedelta(hours=24)
DECK_MAX_MATCHES = 3                     # finished matches in one cube league entry
CONSTRUCTED_THRESHOLD = 0.4              # overlap with the chain's first match, both ways
CONSTRUCTED_WINDOW = timedelta(days=7)
CONSTRUCTED_MIN_FINISHED = 5             # more finished matches than any cube league entry has
CONSTRUCTED_MAX_CARDS = 45               # one deck plus sideboard, not a run of cube decks


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


# -- decks: one league entry each -----------------------------------------------------


@dataclass
class Deck:
    start: datetime
    matches: list[MatchSummary]
    cards: set[str]                   # non-basics you showed across its matches
    source: str                       # "draft" (a logged draft), "inferred" (from the matches), "constructed"
    draft: DraftSummary | None = None
    seed: set[str] = field(default_factory=set)   # constructed detection: the first match's cards

    @property
    def end(self) -> datetime:
        return max((m.start for m in self.matches if m.start), default=self.start)


def _chain(matches: list[MatchSummary], hero: str, threshold: float, window: timedelta,
           max_finished: int | None) -> list[Deck]:
    """Greedy time-ordered grouping of matches by shown-card overlap."""
    decks: list[Deck] = []
    for m in matches:
        if m.start is None:
            continue
        shown = shown_by(m, hero)
        best: tuple[Deck, float] | None = None
        for d in decks[-8:]:
            if m.start - d.end > window or m.start < d.start:
                continue
            if max_finished is not None and sum(1 for x in d.matches if x.winner) >= max_finished:
                continue
            if not shown:                               # nothing shown: the deck just before it
                if m.start - d.end <= NO_CARDS_WINDOW:
                    best = (d, 1.0)
                    break
                continue
            score = len(shown & d.cards) / len(shown)
            if score >= threshold and (best is None or score > best[1]):
                best = (d, score)
        if best is None:
            decks.append(Deck(start=m.start, matches=[m], cards=set(shown), source="inferred"))
        else:
            best[0].matches.append(m)
            best[0].cards |= shown
    return decks


def _seed_chains(matches: list[MatchSummary], hero: str) -> list[Deck]:
    """Group matches that keep showing the cards of the group's first match: one constructed deck."""
    decks: list[Deck] = []
    for m in matches:
        if m.start is None:
            continue
        shown = shown_by(m, hero)
        best: tuple[Deck, float] | None = None
        for d in decks[-12:]:
            if m.start - d.end > CONSTRUCTED_WINDOW or not shown or not d.seed:
                continue
            both = len(shown & d.seed)
            if both / len(shown) >= CONSTRUCTED_THRESHOLD and both / len(d.seed) >= CONSTRUCTED_THRESHOLD:
                score = both / len(shown)
                if best is None or score > best[1]:
                    best = (d, score)
        if best is None:
            decks.append(Deck(start=m.start, matches=[m], cards=set(shown), source="constructed", seed=set(shown)))
        else:
            best[0].matches.append(m)
            best[0].cards |= shown
    return decks


def build_decks(drafts: list[DraftSummary], matches: list[MatchSummary], hero: str | None) -> list[Deck]:
    """Every deck you played: logged drafts first, then inferred and constructed ones from the rest."""
    if hero is None:
        return []
    mine = [m for m in matches if hero in m.players and len(m.players) == 2 and m.start is not None]
    linked = link_matches(drafts, mine, hero)
    decks: list[Deck] = []
    taken: set[str] = set()
    for d in drafts:
        ms = linked.get(d.path, [])
        cards: set[str] = set()
        for m in ms:
            cards |= shown_by(m, hero)
            taken.add(m.match_id)
        decks.append(Deck(start=d.started_at, matches=ms, cards=cards, source="draft", draft=d))
    rest = [m for m in mine if m.match_id not in taken]
    # pass 1: one small pool of cards played five or more times in a week is a constructed deck
    constructed: set[str] = set()
    for chain in _seed_chains(rest, hero):
        finished = sum(1 for m in chain.matches if m.winner)
        if finished >= CONSTRUCTED_MIN_FINISHED and len(chain.cards) <= CONSTRUCTED_MAX_CARDS:
            decks.append(chain)
            constructed.update(m.match_id for m in chain.matches)
    # pass 2: the rest are league entries of up to three finished matches
    decks.extend(_chain([m for m in rest if m.match_id not in constructed], hero,
                        DECK_THRESHOLD, DECK_WINDOW, DECK_MAX_MATCHES))
    decks.sort(key=lambda d: d.start)
    return decks


# -- the numbers --------------------------------------------------------------------


def _result(m: MatchSummary, hero: str) -> str | None:
    if m.winner == hero:
        return "W"
    if m.winner:
        return "L"
    return None


def _new_card(name: str) -> dict:
    return {"name": name,
            # decks (draft or inferred) you showed the card in
            "played": 0, "play_w": 0, "play_l": 0, "play_trophies": 0,
            # logged drafts you took the card in
            "picked": 0, "picked_with_matches": 0, "pick_w": 0, "pick_l": 0, "pick_trophies": 0,
            # games the log shows you with it: cube decks / every match on disk
            "cube_g": 0, "cube_w": 0, "all_g": 0, "all_w": 0}


def compute(drafts: list[DraftSummary], matches: list[MatchSummary], hero: str | None,
            pattern: str | None = None) -> dict:
    hero = hero or hero_of(drafts, pattern)
    decks = build_decks(drafts, matches, hero)
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

    cards: dict[str, dict] = {}

    def card(name: str) -> dict:
        c = cards.get(name)
        if c is None:
            c = cards[name] = _new_card(name)
        return c

    deck_rows = []
    for d in decks:
        w = sum(1 for m in d.matches if _result(m, hero) == "W")
        l = sum(1 for m in d.matches if _result(m, hero) == "L")
        trophy = w >= 3 and l == 0
        cube = d.source != "constructed"
        if d.draft is not None:
            for name in set(d.draft.picks):
                c = card(name)
                c["picked"] += 1
                if d.matches:
                    c["picked_with_matches"] += 1
                    c["pick_w"] += w
                    c["pick_l"] += l
                    c["pick_trophies"] += int(trophy)
        if cube and d.matches:
            for name in d.cards:
                c = card(name)
                c["played"] += 1
                c["play_w"] += w
                c["play_l"] += l
                c["play_trophies"] += int(trophy)
        for m in d.matches:
            for g in m.games:
                if not g["winner"]:
                    continue
                won = int(g["winner"] == hero)
                for name in g["seen"].get(hero, []):
                    if name in M.BASICS:                # every deck has them; a basic's "win rate" is yours
                        continue
                    c = card(name)
                    c["all_g"] += 1
                    c["all_w"] += won
                    if cube:
                        c["cube_g"] += 1
                        c["cube_w"] += won
        deck_rows.append({
            "started_at": d.start.isoformat(timespec="seconds"), "source": d.source,
            "draft": Path(d.draft.path).name if d.draft else None, "set_name": d.draft.set_name if d.draft else None,
            "players": d.draft.players if d.draft else [], "cards": len(d.cards),
            "matches": len(d.matches), "w": w, "l": l, "trophy": trophy,
            "opponents": [{"name": next(p for p in m.players if p != hero), "result": _result(m, hero), "score": m.score}
                          for m in d.matches],
        })
    rows = sorted(cards.values(), key=lambda c: (-c["played"], -c["cube_g"], c["name"]))
    cube_decks = [d for d in decks if d.source != "constructed" and d.matches]
    # your averages, to read a card's numbers against: cube decks, and every match on disk
    totals = {"cube": {"decks": len(cube_decks), "trophies": 0, "m_w": 0, "m_l": 0, "g_w": 0, "g": 0},
              "all": {"decks": None, "trophies": None, "m_w": 0, "m_l": 0, "g_w": 0, "g": 0}}
    for d in decks:
        cube = d.source != "constructed"
        w = sum(1 for m in d.matches if _result(m, hero) == "W")
        l = sum(1 for m in d.matches if _result(m, hero) == "L")
        gw = sum(1 for m in d.matches for g in m.games if g["winner"] == hero)
        gl = sum(1 for m in d.matches for g in m.games if g["winner"] and g["winner"] != hero)
        for key in (("cube", "all") if cube else ("all",)):
            t = totals[key]
            t["m_w"] += w
            t["m_l"] += l
            t["g_w"] += gw
            t["g"] += gw + gl
        if cube and d.matches:
            totals["cube"]["trophies"] += int(w >= 3 and l == 0)
    return {
        "hero": hero,
        "matches": len(matches),
        "matches_with_result": with_result,
        "drafts": len(drafts),
        "decks": len(cube_decks),
        "decks_from_drafts": sum(1 for d in cube_decks if d.source == "draft"),
        "decks_inferred": sum(1 for d in cube_decks if d.source == "inferred"),
        "constructed_matches": sum(len(d.matches) for d in decks if d.source == "constructed"),
        "cards": rows,
        "opponents": dict(opponents),
        "deck_rows": deck_rows,
        "totals": totals,
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
        return {"hero": None, "matches": 0, "matches_with_result": 0, "drafts": 0, "decks": 0,
                "decks_from_drafts": 0, "decks_inferred": 0, "constructed_matches": 0,
                "cards": [], "opponents": {}, "deck_rows": [], "totals": None}

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
