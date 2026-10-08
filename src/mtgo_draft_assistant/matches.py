"""
MTGO match logs: ``Match_GameLog_<uuid>.dat`` under the client's AppFiles.

Format, verified 2026-10-08 on a real log (349 records, none unparsed; see
DESIGN.md §2.3): a header, then one record per game-log line:

    [8 bytes]  .NET DateTime ticks, little-endian, local time
    [1 byte]   kind (always 0 so far)
    [7-bit]    length of the text, in the .NET BinaryWriter 7-bit encoding
    [text]     UTF-8, starting with "@P" (sometimes "@P@P")

Card references inside the text look like ``@[Name@:catalogId,instanceId:@]``.
Lines name a card only when it becomes public (played, cast, discarded,
revealed, fetched); your own draws are "draws a card" with no name, and
there are no life totals. The last line of a match is "X wins the match 2-1".

What this gives a tracker: whose turn it is, cards each player has shown,
game and match results. It cannot give your hand or library; "still in
your library" is therefore the exported deck minus the cards of yours seen
so far, and it is blind to sideboarding unless the user tells the app.

Location: the client's AppFiles directory changes with every client update.
Discovered by glob, newest by mtime. Read-only, always.
"""

from __future__ import annotations

import glob
import os
import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

LOG_GLOB = os.path.expandvars(r"%LOCALAPPDATA%\Apps\2.0\Data\*\*\mtgo*\Data\AppFiles\*\Match_GameLog_*.dat")
TICKS_EPOCH = datetime(1, 1, 1)
CARD_REF = re.compile(r"@\[(?P<name>.*?)@:(?P<cat>\d+),(?P<inst>\d+):@\]")
TURN = re.compile(r"^Turn (\d+): (.+)$")
JOINED = re.compile(r"^(.+?) joined the game\.$")
OPENING = re.compile(r"^(.+?) (?:puts? .*? and )?begins the game with (\w+) cards? in hand\.$")
WINS_GAME = re.compile(r"^(.+?) wins the game\.?$")
WINS_MATCH = re.compile(r"^(.+?) wins the match (\d+)-(\d+)")
CONCEDE = re.compile(r"^(.+?) has conceded")
# verbs after which the first card reference is the subject's own card
OWN_VERBS = ("plays", "casts", "discards", "reveals", "cycles", "activates an ability of",
             "puts a triggered ability from", "exiles", "sacrifices", "returns", "attacks with")
WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}


@dataclass
class Record:
    at: datetime
    text: str


def read7(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
        shift += 7


def parse_records(b: bytes) -> list[Record]:
    """Every "@P" text record with its timestamp. Tolerates junk between records."""
    out: list[Record] = []
    i = 0
    n = len(b)
    while i < n:
        j = b.find(b"@P", i)
        if j < 0:
            break
        rec = None
        for lp in (1, 2, 3):
            if j - lp - 9 < 0:
                continue
            try:
                length, k = read7(b, j - lp)
            except IndexError:
                continue
            if k == j and length >= 2 and j + length <= n:
                ticks = struct.unpack("<q", b[j - lp - 9: j - lp - 1])[0]
                try:
                    at = TICKS_EPOCH + timedelta(microseconds=ticks // 10)
                except (OverflowError, ValueError):
                    at = TICKS_EPOCH
                text = b[j:j + length].decode("utf-8", errors="replace")
                rec = Record(at=at, text=text.lstrip("@P").strip() if text.startswith("@P") else text)
                i = j + length
                break
        if rec is None:
            i = j + 2
            continue
        out.append(rec)
    return out


def strip_refs(text: str) -> str:
    return CARD_REF.sub(lambda m: m.group("name"), text)


@dataclass
class GameLog:
    number: int
    players: list[str] = field(default_factory=list)
    turn: int | None = None
    active: str | None = None
    winner: str | None = None
    first: str | None = None
    hand_sizes: dict[str, int] = field(default_factory=dict)
    # cards seen per player: instance id -> name (a permanent keeps its id across zones)
    seen: dict[str, dict[int, str]] = field(default_factory=dict)
    started_at: datetime | None = None


@dataclass
class MatchLog:
    match_id: str
    path: Path | None
    players: list[str] = field(default_factory=list)
    games: list[GameLog] = field(default_factory=list)
    winner: str | None = None
    score: str | None = None
    last_at: datetime | None = None

    @property
    def current(self) -> GameLog | None:
        return self.games[-1] if self.games else None

    @property
    def over(self) -> bool:
        return self.score is not None


def parse_match(b: bytes, match_id: str = "", path: Path | None = None) -> MatchLog:
    m = MatchLog(match_id=match_id, path=path)
    game: GameLog | None = None
    for rec in parse_records(b):
        m.last_at = rec.at
        raw = rec.text
        text = strip_refs(raw)
        mj = JOINED.match(text)
        if mj:
            if game is None or game.winner is not None:
                game = GameLog(number=len(m.games) + 1, started_at=rec.at)
                m.games.append(game)
            who = mj.group(1)
            if who not in game.players:
                game.players.append(who)
            if who not in m.players:
                m.players.append(who)
            continue
        if game is None:
            continue
        mt = TURN.match(text)
        if mt:
            game.turn, game.active = int(mt.group(1)), mt.group(2)
            continue
        mo = OPENING.match(text)
        if mo:
            game.hand_sizes[mo.group(1)] = WORDS.get(mo.group(2).lower(), 7)
            continue
        if text.endswith("chooses to play first."):
            game.first = text[: -len(" chooses to play first.")]
            continue
        mw = WINS_MATCH.match(text)
        if mw:
            m.winner, m.score = mw.group(1), f"{mw.group(2)}-{mw.group(3)}"
            continue
        mg = WINS_GAME.match(text)
        if mg:
            game.winner = mg.group(1)
            continue
        mc = CONCEDE.match(text)
        if mc and game.winner is None:
            others = [p for p in game.players if p != mc.group(1)]
            game.winner = others[0] if len(others) == 1 else None
            continue
        # a card becoming public: the first reference after an owning verb
        refs = list(CARD_REF.finditer(raw))
        if not refs:
            continue
        for who in game.players:
            prefix = who + " "
            if raw.startswith(prefix) and any(raw[len(prefix):].startswith(v) for v in OWN_VERBS):
                r = refs[0]
                game.seen.setdefault(who, {})[int(r.group("inst"))] = r.group("name")
                break
    return m


# -- the view the Deck pane shows ---------------------------------------------


BASICS = {"Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
          "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
          "Snow-Covered Mountain", "Snow-Covered Forest"}


def is_token(name: str) -> bool:
    return name.endswith(" Token") or " Token " in name or name == "Token"


def seen_counts(game: GameLog, who: str | None) -> Counter:
    """
    Cards of ``who`` seen this game, by name. A card gets a new instance id
    every time it changes zone, so ids overcount; a cube deck is singleton,
    so a non-basic counts once per name. Basic lands count by instance id,
    which can overcount a bounced land by one. Tokens are not cards.
    """
    out: Counter = Counter()
    if who is None:
        return out
    basics_seen: set[int] = set()
    for inst, name in game.seen.get(who, {}).items():
        if is_token(name):
            continue
        if name in BASICS:
            basics_seen.add(inst)
            out[name] += 1
        else:
            out[name] = 1
    return out


def match_view(m: MatchLog, hero: str | None, deck_main: list[tuple[str, int]] | None) -> dict | None:
    """Same shape as the Arena game view, minus what MTGO does not log."""
    g = m.current
    if g is None:
        return None
    if hero is None or hero not in g.players:
        hero = guess_hero_from_players(g.players, hero)
    opp = next((p for p in g.players if p != hero), None)
    mine = seen_counts(g, hero)
    theirs = seen_counts(g, opp)
    remaining: dict[str, int] | None = None
    unexpected: dict[str, int] = {}
    if deck_main is not None:
        counts: Counter = Counter()
        for name, q in deck_main:
            counts[name] += q
        remaining = {}
        for name, q in counts.items():
            left = q - mine.get(name, 0)
            if left > 0:
                remaining[name] = left
        for name, n in mine.items():
            extra = n - counts.get(name, 0)
            if extra > 0:
                unexpected[name] = extra
    return {
        "match_id": m.match_id,
        "game_number": g.number,
        "stage": "over" if m.over else ("game_over" if g.winner else "play"),
        "over": m.over,
        "turn": g.turn,
        "my_seat": None,
        "opponent": opp,
        "life": {},
        "library": None,
        "seen_mine": dict(mine),
        "remaining": remaining,
        "unexpected": unexpected,
        "seen_theirs": dict(theirs),
        "seat_known": hero is not None and hero in g.players,
        "hero": hero,
        "winner": m.winner or g.winner,
        "score": m.score,
        "games": [{"number": x.number, "winner": x.winner} for x in m.games],
    }


def guess_hero_from_players(players: list[str], hint: str | None) -> str | None:
    if hint and hint in players:
        return hint
    return None


# -- finding the logs -----------------------------------------------------------


def match_log_paths(pattern: str | None = None) -> list[Path]:
    # LOG_GLOB is read at call time so tests can point it elsewhere
    return [Path(p) for p in glob.glob(pattern or LOG_GLOB)]


def match_id_of(path: Path) -> str:
    stem = path.stem
    return stem.split("Match_GameLog_", 1)[-1]


def newest_match_after(start: datetime | None, pattern: str | None = None) -> Path | None:
    """The most recently modified match log written after ``start``."""
    best: tuple[float, Path] | None = None
    for p in match_log_paths(pattern):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if start is not None and mtime < start.timestamp():
            continue
        if best is None or mtime > best[0]:
            best = (mtime, p)
    return best[1] if best else None


def guess_hero(pattern: str | None = None, sample: int = 6) -> str | None:
    """The one player name present in every recent match log: the account."""
    paths = sorted(match_log_paths(pattern), key=lambda p: p.stat().st_mtime if p.exists() else 0)[-sample:]
    common: set[str] | None = None
    for p in paths:
        try:
            m = parse_match(p.read_bytes(), match_id_of(p), p)
        except OSError:
            continue
        names = set(m.players)
        if not names:
            continue
        common = names if common is None else common & names
    return next(iter(common)) if common and len(common) == 1 else None


class MatchWatcher:
    """
    Follows the newest match log written since the draft started, re-reading
    it when it changes. Match logs are small (tens of KB), so a change means
    a whole re-parse. Read-only.
    """

    def __init__(self, pattern: str | None = None) -> None:
        self.pattern = pattern                 # None: the module's LOG_GLOB, read at call time
        self._stamp: tuple[Path, int, int] | None = None
        self._match: MatchLog | None = None
        self.hero: str | None = None

    def match(self, after: datetime | None) -> MatchLog | None:
        path = newest_match_after(after, self.pattern)
        if path is None:
            self._stamp, self._match = None, None
            return None
        try:
            st = path.stat()
        except OSError:
            return self._match
        stamp = (path, st.st_size, st.st_mtime_ns)
        if stamp != self._stamp:
            try:
                self._match = parse_match(path.read_bytes(), match_id_of(path), path)
            except OSError:
                return self._match
            self._stamp = stamp
        return self._match
