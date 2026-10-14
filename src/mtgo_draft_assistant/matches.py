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
revealed, milled, fetched); your own draws are "draws a card" with no name,
and there are no life totals. The last line of a match is "X wins the match 2-1".

**Nothing logs a card leaving the battlefield.** Measured over 2,521 real logs
(2026-10-11): no "dies", "is destroyed", damage or combat-death line exists.
"X puts [card] into their graveyard" is surveil and similar library-to-
graveyard moves only. So a tracker can say how a card was last seen (cast,
milled, exiled, bounced...) but never which zone it is in now.

Who owns a named card is decided per line shape, from the same corpus (share
of lines where the card was cast or played by the acting player elsewhere in
the match): casts/plays/discards/cycles/sacrifices 100%, "puts triggered
ability from" 99%, "activates an ability of" 99%, "reveals" 99%, "mills" 94%,
"puts [card] into their graveyard" 95%, "P's [card]" 99%; "P is being
attacked by [cards]" names the *other* player's cards 98.6% of the time. The
first card of "exiles ... with", "returns ... to its owner's hand with" and
"counters ... with" is a coin flip, so those lines only update the last event
of a card whose owner is already known. Rules rejected by measurement are
listed next to OWN_RULES. Result over the corpus: 0.6% of attributions
contradict cast/play evidence (stolen permanents, constructed decks sharing
a card), and 10% of attributed cards were never cast or played by anyone -
milled, surveiled, revealed, fetched, attacking tokens' makers - which the
cast/play rules alone would miss.

What this gives a tracker: whose turn it is, cards each player has shown and
how, game and match results. It cannot give your hand or library; "still in
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
LEADS = re.compile(r"^(.+?) leads the match (\d+)-(\d+)")
LEFT = re.compile(r"^(.+?) has (?:left the game|lost connection to the game)")
CONCEDE = re.compile(r"^(.+?) has conceded")
WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}

# How a card was last seen. Not a zone: MTGO never logs a permanent leaving the
# battlefield (see the module docstring), so "cast" may well be dead by now.
EV_CAST, EV_PLAYED, EV_DISCARDED, EV_CYCLED, EV_SACRIFICED = "cast", "played", "discarded", "cycled", "sacrificed"
EV_MILLED, EV_TO_GRAVEYARD, EV_EXILED, EV_BOUNCED, EV_TO_LIBRARY = "milled", "to graveyard", "exiled", "bounced", "to library"
EV_REVEALED, EV_BATTLEFIELD, EV_ATTACKING, EV_COUNTERED, EV_ABILITY = "revealed", "on battlefield", "attacking", "countered", "ability"

# (verb the line starts with after "P ", event) - the acting player's own card(s):
# every reference before any " with ", " into ", " onto ", " targeting " or " beneath ".
# Order matters: longer prefixes first where one is a prefix of another.
OWN_RULES: tuple[tuple[str, str], ...] = (
    ("casts ability from ", EV_ABILITY),
    ("casts ", EV_CAST),
    ("plays ", EV_PLAYED),
    ("discards ", EV_DISCARDED),
    ("cycles ", EV_CYCLED),
    ("sacrifices ", EV_SACRIFICED),
    ("mills ", EV_MILLED),
    ("reveals ", EV_REVEALED),
    ("activates an ability of ", EV_ABILITY),
    ("puts triggered ability from ", EV_ABILITY),
    ("puts a triggered ability from ", EV_ABILITY),
)
# Not rules, on purpose (measured): "draws N cards with [card]" - both players
# draw off a Timetwister; "puts a counter on [card]" - stun counters go on the
# other side; "chooses to use [card]'s ability" - 7% wrong; the "with [card]"
# of a reveal - Oath of Druids is yours, the Duress making you reveal is not.
# "P puts [card] <where>": the card is P's, the event depends on the destination
PUTS_DEST = ((" into their graveyard", EV_TO_GRAVEYARD), (" onto the battlefield", EV_BATTLEFIELD),
             (" beneath ", EV_TO_LIBRARY), (" on top of ", EV_TO_LIBRARY), (" on the bottom of ", EV_TO_LIBRARY),
             (" on the top of ", EV_TO_LIBRARY))
# first card(s) of these lines may belong to either player: only update a known card's event
EITHER_RULES: tuple[tuple[str, str], ...] = (
    ("exiles ", EV_EXILED),
    ("returns ", EV_BOUNCED),                     # "returns [card] to its owner's hand with [card]"
    ("counters ", EV_COUNTERED),
)
SPLIT_WORDS = (" with ", " into ", " onto ", " targeting ", " beneath ", " on top of ", " on the bottom of ",
               " on the top of ", " to its owner's hand", " to the battlefield", ":")


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
    # cards seen per player: instance id -> name (a card gets a new id per zone change)
    seen: dict[str, dict[int, str]] = field(default_factory=dict)
    # per player: card name -> how it was last seen (an EV_* string, not a zone)
    last: dict[str, dict[str, str]] = field(default_factory=dict)
    started_at: datetime | None = None


@dataclass
class MatchLog:
    match_id: str
    path: Path | None
    players: list[str] = field(default_factory=list)
    games: list[GameLog] = field(default_factory=list)
    winner: str | None = None
    score: str | None = None
    # "match line" (X wins the match), "game wins" (two game wins, no match line),
    # "opponent left" (led the match, the other player left or lost connection), or None
    result_source: str | None = None
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
    leader: str | None = None            # "X leads the match 1-0"
    left: set[str] = set()               # players who left or lost connection after the last lead line
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
            m.winner, m.score, m.result_source = mw.group(1), f"{mw.group(2)}-{mw.group(3)}", "match line"
            continue
        ml = LEADS.match(text)
        if ml:
            leader, left = ml.group(1), set()
            continue
        mleft = LEFT.match(text)
        if mleft:
            left.add(mleft.group(1))
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
        if CARD_REF.search(raw):
            _attribute(game, raw)
    if m.winner is None:
        _infer_result(m, leader, left)
    return m


def _infer_result(m: MatchLog, leader: str | None, left: set[str]) -> None:
    """
    No "wins the match" line. MTGO writes none when the loser leaves or drops
    connection and never comes back, so the match is decided by game wins
    (two of them) or by the lead at the moment the other player left. A 1-1
    with nobody leaving stays undecided.
    """
    wins = Counter(g.winner for g in m.games if g.winner)
    for who, n in wins.items():
        if n >= 2:
            others = sum(k for w, k in wins.items() if w != who)
            m.winner, m.score, m.result_source = who, f"{n}-{others}", "game wins"
            return
    if leader is not None and left and all(p != leader for p in left) and len(m.players) == 2:
        others = sum(k for w, k in wins.items() if w != leader)
        m.winner, m.score, m.result_source = leader, f"{wins.get(leader, 0)}-{others}", "opponent left"


# -- who a named card belongs to, and how it was last seen ----------------------


def _record(game: GameLog, who: str, ref: re.Match, event: str, *, update_only: bool = False) -> None:
    name = ref.group("name")
    last = game.last.setdefault(who, {})
    if update_only:
        if name in last:
            last[name] = event
        return
    game.seen.setdefault(who, {}).setdefault(int(ref.group("inst")), name)
    # an ability can be used from any zone, so it never overrides a real event
    if event != EV_ABILITY or name not in last:
        last[name] = event


def _subject_end(masked: str, start: int) -> int:
    """Where the line's subject ends: the first split word at or after ``start``."""
    end = len(masked)
    for w in SPLIT_WORDS + (" (",):
        i = masked.find(w, start)
        if 0 <= i < end:
            end = i
    return end


def _after_with(game: GameLog, who: str, raw: str, masked: str, rest: int, event: str,
                credit_instrument: bool = False) -> None:
    """
    The tail of a line after its subject. ``with [card]`` is the instrument:
    credited to the acting player only where measured safe (``credit_instrument``).
    ``: [a], [b]`` after it lists more of the acting player's own cards
    ("reveals 3 cards with Thoughtseize: ...": the revealer's hand).
    """
    tail = masked[rest:]
    i, p = tail.find(" with "), tail.find(" (")
    if i < 0 or 0 <= p < i:                      # no "with", or only inside an ability's text
        return
    rest += i
    refs = list(CARD_REF.finditer(raw, rest))
    if not refs:
        return
    colon = masked.find(":", rest)
    if colon >= 0:
        for r in refs:
            if r.start() > colon:
                _record(game, who, r, event)
        refs = [r for r in refs if r.start() < colon]
    if refs and credit_instrument:
        _record(game, who, refs[0], EV_ABILITY)


def _attribute(game: GameLog, raw: str) -> None:
    """Apply the line-shape rules in the module docstring to one record."""
    if "\n" in raw:                 # a combat record can hold two lines ("B is being attacked by X" + "B's Y is being attacked by Z")
        for line in raw.split("\n"):
            if CARD_REF.search(line):
                _attribute(game, line.strip())
        return
    players = game.players
    masked = CARD_REF.sub(lambda mm: "\0" * (mm.end() - mm.start()), raw)
    for who in players:
        other = next((p for p in players if p != who), None) if len(players) == 2 else None
        if masked.startswith(who + "'s "):
            start = len(who) + 3
            plain = masked[start:].split(" (", 1)[0]
            # "P's Skyclave Apparition creates an Illusion Token under P's control": the
            # possessive is the token's recipient, not the card's owner. Skip those.
            if " under " in plain and "'s control" in plain:
                return
            attacked = masked.find(" is being attacked by ", start)
            end = min(_subject_end(masked, start), attacked if attacked >= 0 else len(masked))
            own = list(CARD_REF.finditer(raw, start, end))
            if own:
                _record(game, who, own[0], EV_BATTLEFIELD)       # "P's [card] creates / is being attacked by"
            if other is not None and attacked >= 0:
                for r in CARD_REF.finditer(raw, attacked):
                    _record(game, other, r, EV_ATTACKING)
            return
        if not masked.startswith(who + " "):
            continue
        start = len(who) + 1
        body = masked[start:]
        if body.startswith("is being attacked by "):
            if other is not None:
                for r in CARD_REF.finditer(raw, start):
                    _record(game, other, r, EV_ATTACKING)
            return
        if body.startswith("reveals their hand to "):
            # "... to [Duress]('s ability), containing [a], [b]": the hand is the revealer's,
            # the card making them reveal is not
            i = masked.find(", containing ", start)
            if i >= 0:
                for r in CARD_REF.finditer(raw, i):
                    _record(game, who, r, EV_REVEALED)
            return
        for verb, event in OWN_RULES:
            if body.startswith(verb):
                end = _subject_end(masked, start + len(verb))
                for r in CARD_REF.finditer(raw, start + len(verb), end):
                    _record(game, who, r, event)
                _after_with(game, who, raw, masked, end, event)
                return
        if body.startswith("puts "):
            plain = body.split(" (", 1)[0]
            for phrase, event in PUTS_DEST:
                i = plain.find(phrase)
                if i >= 0:
                    for r in CARD_REF.finditer(raw, start + 5, start + i):
                        _record(game, who, r, event)
                    return
            return
        for verb, event in EITHER_RULES:
            if body.startswith(verb):
                end = _subject_end(masked, start + len(verb))
                for r in CARD_REF.finditer(raw, start + len(verb), end):
                    _record(game, who, r, event, update_only=True)
                    if other is not None:
                        _record(game, other, r, event, update_only=True)
                # the bouncing card is the actor's (measured 99%); the exiling or countering one is not safe
                _after_with(game, who, raw, masked, end, event, credit_instrument=(verb == "returns "))
                return
        return


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


def shown_in_match(m: MatchLog, who: str | None) -> list[dict]:
    """
    Every card of ``who`` seen anywhere in the match, one entry per name:
    ``n`` (the most copies seen in one game), ``last`` (how it was last seen),
    ``game`` (the latest game it appeared in) and ``current`` (seen in the game
    on screen). Sorted: this game's cards first, then by name.
    """
    if who is None or not m.games:
        return []
    cur = m.current
    out: dict[str, dict] = {}
    for g in m.games:
        counts = seen_counts(g, who)
        last = g.last.get(who, {})
        for name, n in counts.items():
            e = out.setdefault(name, {"name": name, "n": 0, "last": None, "game": g.number, "current": False})
            e["n"] = max(e["n"], n)
            e["game"] = g.number
            e["last"] = last.get(name)
            e["current"] = g is cur
    return sorted(out.values(), key=lambda e: (not e["current"], e["name"]))


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
        # the whole match, not just this game, with how each card was last seen
        "shown_theirs": shown_in_match(m, opp),
        "last_mine": dict(g.last.get(hero, {})) if hero else {},
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

    def _current(self, after: datetime | None) -> tuple[Path, int, int] | None:
        path = newest_match_after(after, self.pattern)
        if path is None:
            return None
        try:
            st = path.stat()
        except OSError:
            return self._stamp
        return (path, st.st_size, st.st_mtime_ns)

    def changed(self, after: datetime | None) -> bool:
        """True when ``match()`` would now read a different file or version
        (including a log appearing or disappearing). Only stats, never reads."""
        return self._current(after) != self._stamp

    def match(self, after: datetime | None) -> MatchLog | None:
        stamp = self._current(after)
        if stamp is None:
            self._stamp, self._match = None, None
            return None
        if stamp == self._stamp:
            return self._match
        path = stamp[0]
        try:
            self._match = parse_match(path.read_bytes(), match_id_of(path), path)
        except OSError:
            return self._match
        self._stamp = stamp
        return self._match
