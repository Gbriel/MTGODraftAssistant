"""
MTG Arena draft events from ``Player.log``, parsed into the same :class:`Draft`
the MTGO parser produces so the analysis and UI are shared.

Verified 2026-09-24 against six complete and one live Arena powered-cube draft
(DESIGN.md §2.5). The facts this parser relies on:

  * ``[UnityCrossThreadLogger]Draft.Notify {...}`` carries the pack on screen:
    ``draftId``, ``SelfPack``, ``SelfPick`` and ``PackCards`` as a comma-joined
    string of Arena card ids (grpIds). Unlike MTGO, **the pick number here is
    trustworthy**; still cross-checked against the card count.
  * ``==> EventPlayerDraftMakePick {"request": "<json string>"}`` is the pick:
    ``DraftId``, ``GrpIds`` (one id), ``Pack``, ``Pick``. The request is
    JSON-in-JSON: ``request`` is a string that must be decoded again.
  * A notify may be logged several times for the same pick (three copies each
    in the live capture) — key on (pack, pick), last copy wins. A pick may be
    requested twice for the same position; only a request whose card is in the
    notified pack counts, and the last valid one wins.
  * The next notify can be logged *before* the ``<==`` response to the
    previous pick. Ordering between requests and responses is not reliable;
    the pick request itself is the commit.
  * ``==> EventJoin`` before the draft names the event (``EventName``); the
    draft id first appears in the first notify. ``==> DraftCompleteDraft``
    ends it. There is no player list except in one anomalous response
    (``TableInfo.Players``), which is used when seen and never relied on.
  * Timestamps are separate ``[UnityCrossThreadLogger]M/D/YYYY h:mm:ss AM``
    lines; the most recent one dates the events that follow.
  * The file is CRLF, UTF-8, rewritten on every client launch (the previous
    one becomes ``Player-prev.log``), and grows to ~100MB. Read it
    incrementally by byte offset; a shrink means a new launch.

Read-only, always: this module opens the file for reading and nothing else.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .arena_game import ArenaGameParser, DeckList, MatchState, game_view
from .draft_log import Draft, Pick
from .watcher import FileStamp, Update

DEFAULT_LOG = Path(os.environ.get("USERPROFILE", "~")).expanduser() / "AppData" / "LocalLow" \
    / "Wizards Of The Coast" / "MTGA" / "Player.log"
ARENA_POD_SIZE_HINT = 8          # human drafts seat 8; only used until a lap confirms it

NOTIFY = re.compile(r"Draft\.Notify (\{.*\})\s*$")
MAKE_PICK = re.compile(r"==> EventPlayerDraftMakePick (\{.*\})\s*$")
EVENT_JOIN = re.compile(r"==> EventJoin (\{.*\})\s*$")
COMPLETE = re.compile(r"==> DraftCompleteDraft ")
TIMESTAMP = re.compile(r"^\[UnityCrossThreadLogger\](\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} [AP]M)\s*$")
RESPONSE = re.compile(r"^<== (\w+)\(")

Namer = Callable[[int], "str | None"]


@dataclass
class ArenaPick:
    pack: int
    pick: int
    available: list[int]            # grpIds as notified, in Arena's order
    picked: int | None = None
    complete: bool = False
    also_picked: list[int] = field(default_factory=list)   # second card in a pick-two draft


@dataclass
class ArenaDraft:
    draft_id: str
    event_name: str | None = None
    started_at: str | None = None
    players: list[str] = field(default_factory=list)
    picks: dict[tuple[int, int], ArenaPick] = field(default_factory=dict)
    complete: bool = False
    warnings: list[str] = field(default_factory=list)
    last_event_at: str | None = None   # log timestamp preceding the latest draft message
    cards_per_pick: int = 1            # 2 once a pick request names two cards (PickTwoDraft)

    @property
    def last_event_time(self) -> datetime | None:
        return parse_timestamp(self.last_event_at)

    @property
    def ordered_picks(self) -> list[ArenaPick]:
        return [self.picks[k] for k in sorted(self.picks)]


def parse_timestamp(s: str | None) -> datetime | None:
    """``9/24/2026 7:55:43 PM`` (local time) -> datetime, or None."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y %I:%M:%S %p")
    except ValueError:
        return None


def placeholder(grp_id: int) -> str:
    """Name used until the card id has been resolved. Unique per id."""
    return f"#{grp_id}"


def to_draft(ad: ArenaDraft, namer: Namer | None = None) -> Draft:
    """Render an Arena draft as the shared :class:`Draft`, naming cards via ``namer``."""
    def name(g: int) -> str:
        n = namer(g) if namer else None
        return n if n else placeholder(g)

    d = Draft(
        event_id=ad.draft_id,
        timestamp=ad.started_at,
        hero=None,
        players=list(ad.players),
        set_name=ad.event_name,
        pod_size_hint=ARENA_POD_SIZE_HINT,
        source="arena",
        cards_per_pick=ad.cards_per_pick,
    )
    for p in ad.ordered_picks:
        d.picks.append(Pick(
            pack=p.pack, pick=p.pick,
            picked=name(p.picked) if p.picked is not None else None,
            available=[name(g) for g in p.available],
            complete=p.complete,
            also_picked=[name(g) for g in p.also_picked],
        ))
    return d


def grp_ids(draft: ArenaDraft) -> list[int]:
    seen: dict[int, None] = {}
    for p in draft.ordered_picks:
        for g in p.available:
            seen.setdefault(g, None)
    return list(seen)


# -- streaming parser --------------------------------------------------------


class ArenaLogParser:
    """
    Feed it lines (any order of arrival, one call per line). ``current`` is
    the most recently started draft; ``version`` bumps whenever it changes.
    """

    def __init__(self) -> None:
        self.drafts: dict[str, ArenaDraft] = {}
        self.current: ArenaDraft | None = None
        self.version = 0
        self._last_event: str | None = None
        self._last_ts: str | None = None
        self._expect_json = False       # the line after a "<== X(id)" is its JSON body

    def feed_text(self, text: str) -> None:
        for line in text.split("\n"):
            self.feed_line(line.rstrip("\r"))

    def feed_line(self, line: str) -> None:
        if self._expect_json:
            self._expect_json = False
            if line.startswith("{"):
                self._response_body(line)
                return
        m = TIMESTAMP.match(line)
        if m:
            self._last_ts = m.group(1)
            return
        if RESPONSE.match(line):
            self._expect_json = True
            return
        if "Draft.Notify" in line:
            m = NOTIFY.search(line)
            if m:
                self._notify(m.group(1))
            return
        if "EventPlayerDraftMakePick" in line:
            m = MAKE_PICK.search(line)
            if m:
                self._pick(m.group(1))
            return
        if "==> EventJoin" in line:
            m = EVENT_JOIN.search(line)
            if m:
                try:
                    req = json.loads(json.loads(m.group(1))["request"])
                    self._last_event = req.get("EventName") or self._last_event
                except (ValueError, KeyError, TypeError):
                    pass
            return
        if COMPLETE.search(line) and self.current is not None and not self.current.complete:
            self.current.complete = True
            self._bump()

    # -- messages
    def _draft(self, draft_id: str) -> ArenaDraft:
        d = self.drafts.get(draft_id)
        if d is None:
            d = ArenaDraft(draft_id=draft_id, event_name=self._last_event, started_at=self._last_ts)
            # cards_per_pick is learned from the first pick request, not the event name
            self.drafts[draft_id] = d
            self.current = d
        return d

    def _notify(self, body: str) -> None:
        try:
            msg = json.loads(body)
            draft_id = str(msg["draftId"])
            pack = int(msg["SelfPack"])
            pick = int(msg["SelfPick"])
            raw = str(msg.get("PackCards") or "")
            cards = [int(x) for x in raw.split(",") if x.strip()]
        except (ValueError, KeyError, TypeError) as e:
            self._warn(f"unparseable Draft.Notify: {e}")
            return
        d = self._draft(draft_id)
        existing = d.picks.get((pack, pick))
        if existing is not None and existing.available == cards:
            return                      # repeated notification, identical content
        if existing is not None and existing.complete:
            d.warnings.append(f"P{pack}P{pick}: pack re-notified with different cards after the pick")
        d.picks[(pack, pick)] = ArenaPick(pack=pack, pick=pick, available=cards,
                                          picked=existing.picked if existing else None,
                                          complete=existing.complete if existing else False)
        self._bump(d)

    def _pick(self, body: str) -> None:
        try:
            req = json.loads(json.loads(body)["request"])
            draft_id = str(req["DraftId"])
            pack = int(req["Pack"])
            pick = int(req["Pick"])
            ids = [int(g) for g in req.get("GrpIds") or []]
        except (ValueError, KeyError, TypeError) as e:
            self._warn(f"unparseable EventPlayerDraftMakePick: {e}")
            return
        d = self._draft(draft_id)
        p = d.picks.get((pack, pick))
        if p is None:
            d.warnings.append(f"P{pack}P{pick}: pick logged before its pack was notified")
            return
        if not ids or any(g not in p.available for g in ids):
            d.warnings.append(f"P{pack}P{pick}: pick request for card {ids} not in the notified pack; ignored")
            return
        p.picked = ids[0]
        p.also_picked = list(ids[1:])
        p.complete = True
        if len(ids) > d.cards_per_pick:
            d.cards_per_pick = len(ids)         # a pick-two draft names two cards per request
        self._bump(d)

    def _response_body(self, line: str) -> None:
        if "TableInfo" not in line or self.current is None:
            return
        try:
            info = json.loads(line).get("TableInfo") or {}
            names = [str(pl.get("ScreenName")) for pl in info.get("Players", []) if pl.get("ScreenName")]
        except (ValueError, AttributeError):
            return
        if names and names != self.current.players:
            self.current.players = names
            self._bump()

    def _warn(self, msg: str) -> None:
        if self.current is not None:
            self.current.warnings.append(msg)

    def _bump(self, d: ArenaDraft | None = None) -> None:
        target = d or self.current
        if target is not None:
            target.last_event_at = self._last_ts
        if d is None or d is self.current:
            self.version += 1


# -- tail watcher ------------------------------------------------------------


class ArenaWatcher:
    """
    Poll ``Player.log`` by size and read only what was appended. A shrink
    means Arena was relaunched and the file rewritten: start over.

    Same contract as :class:`watcher.DraftWatcher`: ``poll`` returns an
    :class:`Update` when the draft changed, ``run`` loops, ``rebuild`` renders
    the current draft again (after card names were resolved).
    """

    pattern = "Player.log"

    def __init__(self, log_path: str | os.PathLike[str] | None = None, interval: float = 0.5,
                 namer: Namer | None = None) -> None:
        self.log_path = Path(log_path) if log_path is not None else DEFAULT_LOG
        self.log_dir = self.log_path.parent
        self.interval = interval
        self.namer = namer
        self._lock = threading.Lock()
        self._parser = ArenaLogParser()
        self._games = ArenaGameParser()      # deck submissions and live game state
        self._offset = 0
        self._partial = b""
        self._tail = b""                 # last bytes consumed, to detect a rewrite in place
        self._seen_version = -1
        self._seen_game_version = -1
        self._seen_draft: str | None = None
        self.current: Update | None = None
        self._seed_from_previous()

    def set_log_dir(self, log_dir: str | os.PathLike[str] | None) -> None:
        """Kept for interface parity; Arena has one fixed file."""
        if log_dir is not None:
            self.log_path = Path(log_dir) / "Player.log" if Path(log_dir).is_dir() else Path(log_dir)
            self.log_dir = self.log_path.parent
        with self._lock:
            self._reset()

    def _reset(self) -> None:
        self._parser = ArenaLogParser()
        self._games = ArenaGameParser()
        self._offset = 0
        self._partial = b""
        self._tail = b""
        self._seen_version = -1
        self._seen_game_version = -1
        self._seen_draft = None
        self._seed_from_previous()

    PREVIOUS_NAME = "Player-prev.log"

    def _seed_from_previous(self) -> None:
        """
        Arena rewrites Player.log on every launch and keeps the old one as
        Player-prev.log. Read that first, so the last draft and the deck you
        submitted survive a relaunch. Anything in the live log supersedes it
        (a newer draft becomes current). A match left "playing" in the old
        log is over by definition, so it is dropped.
        """
        prev = self.log_path.with_name(self.PREVIOUS_NAME)
        try:
            with open(prev, "rb") as fh:
                data = fh.read()
        except OSError:
            return
        for raw in data.split(b"\n"):
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            self._parser.feed_line(line)
            self._games.feed_line(line)
        self._games.match = None
        self._seen_version = -1          # the seeded draft counts as new on the first poll

    TAIL_CHECK = 256

    def _rewritten(self, fh) -> bool:
        """
        Arena rewrites Player.log on launch. Usually that shows as a shrink,
        but if the new file has already outgrown our offset, compare the
        bytes just before the offset with what we consumed last time.
        """
        if not self._tail:
            return False
        fh.seek(self._offset - len(self._tail))
        return fh.read(len(self._tail)) != self._tail

    # -- single step
    def poll(self) -> Update | None:
        with self._lock:
            return self._poll_locked()

    def _poll_locked(self) -> Update | None:
        try:
            st = self.log_path.stat()
        except OSError:
            # no live log, but a previous one may have been seeded
            if self._offset == 0 and self._seen_version != self._parser.version:
                return self._emit(FileStamp(path=self.log_path, size=0, mtime_ns=0))
            return None
        if st.st_size < self._offset:
            self._reset()                        # rewritten: new client launch
        if st.st_size == self._offset:
            # nothing new in the live log; the seeded previous log may still be unreported
            if self._seen_version != self._parser.version or self._seen_game_version != self._games.version:
                return self._emit(FileStamp(path=self.log_path, size=st.st_size, mtime_ns=st.st_mtime_ns))
            return None
        try:
            with open(self.log_path, "rb") as fh:
                if self._rewritten(fh):
                    self._reset()
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            return None
        self._offset += len(chunk)
        self._tail = (self._tail + chunk)[-self.TAIL_CHECK:]
        data = self._partial + chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            self._partial = data
            return None
        self._partial = data[cut + 1:]
        for raw in data[:cut].split(b"\n"):
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            self._parser.feed_line(line)
            self._games.feed_line(line)
        return self._emit(FileStamp(path=self.log_path, size=st.st_size, mtime_ns=st.st_mtime_ns))

    def _emit(self, stamp: FileStamp) -> Update | None:
        cur = self._parser.current
        draft_changed = self._parser.version != self._seen_version
        game_changed = self._games.version != self._seen_game_version
        if not draft_changed and not game_changed:
            return None
        if cur is None and self._games.match is None and self._games.latest_deck is None:
            return None
        self._seen_version = self._parser.version
        self._seen_game_version = self._games.version
        draft_id = cur.draft_id if cur is not None else None
        new_draft = draft_id != self._seen_draft
        self._seen_draft = draft_id
        draft = to_draft(cur, self.namer) if cur is not None else Draft(source="arena",
                                                                        pod_size_hint=ARENA_POD_SIZE_HINT)
        update = Update(path=self.log_path, text="", draft=draft, new_draft=new_draft, stamp=stamp)
        self.current = update
        return update

    def rebuild(self) -> Update | None:
        """Re-render the current draft (card names may have arrived)."""
        with self._lock:
            cur = self._parser.current
            if self.current is None:
                return None
            draft = to_draft(cur, self.namer) if cur is not None else self.current.draft
            update = Update(path=self.log_path, text="", draft=draft, new_draft=False,
                            stamp=self.current.stamp)
            self.current = update
            return update

    @property
    def arena_draft(self) -> ArenaDraft | None:
        with self._lock:
            return self._parser.current

    # -- deck and game
    def deck(self) -> DeckList | None:
        """The deck submitted for the current draft's event, else the latest one."""
        with self._lock:
            cur = self._parser.current
            return self._games.deck_for(cur.event_name if cur is not None else None)

    def match(self) -> MatchState | None:
        with self._lock:
            return self._games.match

    def game(self) -> dict | None:
        """The live game view (see :func:`arena_game.game_view`), or None."""
        with self._lock:
            cur = self._parser.current
            deck = self._games.deck_for(cur.event_name if cur is not None else None)
            return game_view(self._games.match, deck)

    def last_activity(self) -> datetime | None:
        """When the draft or a game last changed, by the log's own clock."""
        with self._lock:
            times = []
            cur = self._parser.current
            if cur is not None and cur.last_event_time is not None:
                times.append(cur.last_event_time)
            gt = self._games.last_event_time
            if gt is not None:
                times.append(gt)
        return max(times) if times else None

    # -- loop
    def run(self, callback: Callable[[Update], None],
            stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            update = self.poll()
            if update is not None:
                callback(update)
            stop.wait(self.interval)
