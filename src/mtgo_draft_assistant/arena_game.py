"""
Arena deck submissions and live game state from ``Player.log``.

Verified 2026-09-28 against four matches in a real log (DESIGN.md §2.6):

  * ``==> EventSetDeckV3 {"request": "<json>"}`` is the deck you submit after
    building: ``EventName``, and ``Deck.MainDeck`` / ``Deck.Sideboard`` as
    ``[{"cardId": grpId, "quantity": n}, ...]``. JSON inside JSON again.
  * A JSON line with ``matchGameRoomStateChangedEvent`` opens and closes a
    match: ``gameRoomInfo.stateType`` is ``MatchGameRoomStateType_Playing``
    or ``..._MatchCompleted``; ``gameRoomConfig.reservedPlayers`` lists
    ``playerName`` and ``systemSeatId``.
  * A JSON line with ``greToClientEvent`` holds ``greToClientMessages``; the
    ones of type ``GREMessageType_GameStateMessage`` carry
    ``gameStateMessage``: ``GameStateType_Full`` at game start (empty zones),
    then ``GameStateType_Diff``. A diff's ``zones`` entries carry the
    **complete** ``objectInstanceIds`` of every zone that changed, and its
    ``gameObjects`` the objects that changed, with ``grpId`` when visible to
    you. ``gameInfo.stage`` becomes ``GameStage_GameOver`` at the end.
  * Your seat is the one whose hand zone's objects have a ``grpId``; the
    opponent's hand lists ids with no objects behind them. The same seat is
    in the message's ``systemSeatIds``.
  * When a card changes zone it gets a new instance id; an
    ``AnnotationType_ObjectIdChanged`` annotation maps ``orig_id`` to
    ``new_id``. Zone membership is authoritative, so the tracker reads zones
    and looks objects up by id rather than trusting a stale object map.
  * Your library's objects are hidden: what is still in it is the submitted
    deck minus every card of yours currently in hand, on the battlefield, in
    the graveyard, in exile or on the stack.

Read-only, always.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field

SET_DECK = re.compile(r"==> EventSetDeckV3 (\{.*\})\s*$")
TIMESTAMP = re.compile(r"^\[UnityCrossThreadLogger\](\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} [AP]M)\s*$")

VISIBLE_ZONES = ("ZoneType_Hand", "ZoneType_Battlefield", "ZoneType_Graveyard",
                 "ZoneType_Exile", "ZoneType_Stack", "ZoneType_Command")


@dataclass
class DeckList:
    event_name: str | None
    main: list[tuple[int, int]]           # (grpId, quantity)
    side: list[tuple[int, int]]
    submitted_at: str | None = None
    deck_id: str | None = None

    @property
    def main_count(self) -> int:
        return sum(q for _, q in self.main)

    @property
    def side_count(self) -> int:
        return sum(q for _, q in self.side)


@dataclass
class GameState:
    game_number: int | None = None
    stage: str | None = None
    my_seat: int | None = None
    zones: dict[int, dict] = field(default_factory=dict)      # zoneId -> {type, owner, ids}
    objects: dict[int, dict] = field(default_factory=dict)    # instanceId -> {grpId, owner, kind}
    turn: int | None = None
    active_player: int | None = None
    life: dict[int, int] = field(default_factory=dict)

    @property
    def over(self) -> bool:
        return self.stage == "GameStage_GameOver"


@dataclass
class MatchState:
    match_id: str | None
    players: dict[int, str]               # seat -> name
    state: str                            # "playing" | "completed"
    game: GameState | None = None
    results: list[dict] = field(default_factory=list)


class ArenaGameParser:
    """Feed it lines; read ``latest_deck``, ``match`` and ``version``."""

    def __init__(self) -> None:
        self.decks: list[DeckList] = []
        self.match: MatchState | None = None
        self.version = 0
        self._last_ts: str | None = None
        self.last_event_at: str | None = None     # log timestamp preceding the latest change

    @property
    def last_event_time(self):
        from .arena_log import parse_timestamp
        return parse_timestamp(self.last_event_at)

    def _bump(self) -> None:
        self.version += 1
        self.last_event_at = self._last_ts

    @property
    def latest_deck(self) -> DeckList | None:
        return self.decks[-1] if self.decks else None

    def deck_for(self, event_name: str | None) -> DeckList | None:
        """The most recent submission for an event, else the most recent at all."""
        if event_name:
            for d in reversed(self.decks):
                if d.event_name == event_name:
                    return d
        return self.latest_deck

    # -- lines
    def feed_text(self, text: str) -> None:
        for line in text.split("\n"):
            self.feed_line(line.rstrip("\r"))

    def feed_line(self, line: str) -> None:
        m = TIMESTAMP.match(line)
        if m:
            self._last_ts = m.group(1)
            return
        if "EventSetDeckV3" in line:
            m = SET_DECK.search(line)
            if m:
                self._deck(m.group(1))
            return
        s = line.lstrip()
        if not s.startswith("{"):
            return
        if "matchGameRoomStateChangedEvent" in s:
            self._room(s)
        elif "greToClientEvent" in s:
            self._gre(s)

    # -- deck
    def _deck(self, body: str) -> None:
        try:
            req = json.loads(json.loads(body)["request"])
            deck = req.get("Deck") or {}
            if not isinstance(req, dict) or not isinstance(deck, dict):
                return
            main = [(int(c["cardId"]), int(c.get("quantity", 1))) for c in deck.get("MainDeck") or []]
            side = [(int(c["cardId"]), int(c.get("quantity", 1))) for c in deck.get("Sideboard") or []]
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        self.decks.append(DeckList(
            event_name=req.get("EventName"), main=main, side=side, submitted_at=self._last_ts,
            deck_id=(req.get("Summary") or {}).get("DeckId"),
        ))
        self._bump()

    # -- match room
    def _room(self, s: str) -> None:
        try:
            info = json.loads(s)["matchGameRoomStateChangedEvent"]["gameRoomInfo"]
        except (ValueError, KeyError, TypeError):
            return
        cfg = info.get("gameRoomConfig") or {}
        players = {}
        for p in cfg.get("reservedPlayers") or []:
            try:
                players[int(p["systemSeatId"])] = str(p.get("playerName") or "?")
            except (KeyError, TypeError, ValueError):
                continue
        state = "completed" if "Completed" in str(info.get("stateType")) else "playing"
        match_id = cfg.get("matchId")
        if self.match is None or self.match.match_id != match_id:
            self.match = MatchState(match_id=match_id, players=players, state=state)
        else:
            self.match.state = state
            if players:
                self.match.players = players
        if state == "completed":
            result = info.get("finalMatchResult") or {}
            self.match.results = list(result.get("resultList") or [])
        self._bump()

    # -- game state
    def _gre(self, s: str) -> None:
        try:
            msgs = json.loads(s)["greToClientEvent"]["greToClientMessages"]
        except (ValueError, KeyError, TypeError):
            return
        changed = False
        for m in msgs:
            gs = m.get("gameStateMessage")
            if not gs:
                continue
            changed = True
            if self.match is None:
                self.match = MatchState(match_id=None, players={}, state="playing")
            g = self.match.game
            if gs.get("type") == "GameStateType_Full" or g is None:
                g = GameState()
                self.match.game = g
            info = gs.get("gameInfo") or {}
            if info.get("gameNumber") is not None:
                g.game_number = info.get("gameNumber")
            if info.get("stage"):
                g.stage = info["stage"]
            for o in gs.get("gameObjects") or []:
                try:
                    g.objects[int(o["instanceId"])] = {
                        "grpId": o.get("grpId"), "owner": o.get("ownerSeatId"),
                        "kind": o.get("type"), "zoneId": o.get("zoneId"),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
            for z in gs.get("zones") or []:
                try:
                    g.zones[int(z["zoneId"])] = {
                        "type": z.get("type"), "owner": z.get("ownerSeatId"),
                        "ids": [int(i) for i in z.get("objectInstanceIds") or []],
                    }
                except (KeyError, TypeError, ValueError):
                    continue
            for a in gs.get("annotations") or []:
                if "AnnotationType_ObjectIdChanged" in (a.get("type") or []):
                    orig = new = None
                    for d in a.get("details") or []:
                        if d.get("key") == "orig_id":
                            orig = (d.get("valueInt32") or [None])[0]
                        elif d.get("key") == "new_id":
                            new = (d.get("valueInt32") or [None])[0]
                    if orig is not None and new is not None and orig in g.objects and new not in g.objects:
                        g.objects[new] = dict(g.objects[orig])
            ti = gs.get("turnInfo") or {}
            if ti.get("turnNumber") is not None:
                g.turn = ti.get("turnNumber")
            if ti.get("activePlayer") is not None:
                g.active_player = ti.get("activePlayer")
            for p in gs.get("players") or []:
                try:
                    g.life[int(p["systemSeatNumber"])] = int(p.get("lifeTotal"))
                except (KeyError, TypeError, ValueError):
                    continue
            if g.my_seat is None:
                g.my_seat = self._detect_seat(g, m.get("systemSeatIds"))
        if changed:
            self._bump()

    @staticmethod
    def _detect_seat(g: GameState, seats) -> int | None:
        for z in g.zones.values():
            if z["type"] == "ZoneType_Hand" and z["ids"]:
                if any(g.objects.get(i, {}).get("grpId") for i in z["ids"]):
                    return z["owner"]
        if isinstance(seats, list) and len(seats) == 1:
            return int(seats[0])
        return None


# -- the view the UI shows ----------------------------------------------------


def _cards_in(g: GameState, seat: int | None, zone_types: tuple[str, ...]) -> Counter:
    """grpId -> count of real cards owned by ``seat`` in those zone types."""
    out: Counter = Counter()
    for z in g.zones.values():
        if z["type"] not in zone_types:
            continue
        for i in z["ids"]:
            o = g.objects.get(i)
            if not o or o.get("kind") != "GameObjectType_Card" or not o.get("grpId"):
                continue
            owner = z["owner"] if z["owner"] is not None else o.get("owner")
            if seat is not None and owner != seat:
                continue
            out[int(o["grpId"])] += 1
    return out


def library_count(g: GameState, seat: int | None) -> int | None:
    for z in g.zones.values():
        if z["type"] == "ZoneType_Library" and z["owner"] == seat:
            return len(z["ids"])
    return None


def game_view(match: MatchState | None, deck: DeckList | None) -> dict | None:
    """
    What the Deck pane needs during a game: cards of yours seen outside the
    library, what must therefore still be in it, and what the opponent has
    shown. None when no match is on.
    """
    if match is None or match.state != "playing" or match.game is None:
        return None
    g = match.game
    me = g.my_seat
    opp = next((s for s in match.players if s != me), None)
    mine = _cards_in(g, me, VISIBLE_ZONES)
    theirs = _cards_in(g, opp, ("ZoneType_Battlefield", "ZoneType_Graveyard", "ZoneType_Exile", "ZoneType_Stack")) if opp is not None else Counter()
    remaining: dict[int, int] | None = None
    unexpected: dict[int, int] = {}
    if deck is not None:
        deck_counts = Counter()
        for grp, q in deck.main:
            deck_counts[grp] += q
        remaining = {}
        for grp, q in deck_counts.items():
            left = q - mine.get(grp, 0)
            if left > 0:
                remaining[grp] = left
        for grp, n in mine.items():
            extra = n - deck_counts.get(grp, 0)
            if extra > 0:
                unexpected[grp] = extra          # sideboarded in, or a deck we don't have
    return {
        "match_id": match.match_id,
        "game_number": g.game_number,
        "stage": g.stage,
        "over": g.over,
        "turn": g.turn,
        "my_seat": me,
        "opponent": match.players.get(opp) if opp is not None else None,
        "life": {str(k): v for k, v in g.life.items()},
        "library": library_count(g, me),
        "seen_mine": dict(mine),
        "remaining": remaining,
        "unexpected": unexpected,
        "seen_theirs": dict(theirs),
        "seat_known": me is not None,
    }
