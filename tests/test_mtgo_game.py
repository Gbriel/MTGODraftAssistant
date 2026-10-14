"""MTGO deck files and match logs. The match fixture is a real log, verbatim."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.matches import (  # noqa: E402
    MatchWatcher,
    guess_hero,
    match_id_of,
    match_view,
    newest_match_after,
    parse_match,
    parse_records,
    seen_counts,
)
from mtgo_draft_assistant.mtgo_deck import (  # noqa: E402
    DekWatcher,
    draft_started_at,
    find_dek,
    load_dek,
    parse_dek,
)

MATCH = os.path.join(ROOT, "tests", "fixtures", "mtgo", "Match_GameLog_6fe35af5-8510-4cb0-a11d-0328e3823809.dat")

DEK = """<?xml version="1.0" encoding="utf-8"?>
<Deck xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <NetDeckID>0</NetDeckID>
  <PreconstructedDeckID>0</PreconstructedDeckID>
  <Cards CatID="20650" Quantity="1" Sideboard="false" Name="Mox Diamond" />
  <Cards CatID="13786" Quantity="1" Sideboard="false" Name="Dark Ritual" />
  <Cards CatID="90364" Quantity="6" Sideboard="false" Name="Swamp" />
  <Cards CatID="12345" Quantity="1" Sideboard="true" Name="Duress" />
  <Cards CatID="99999" Quantity="1" Sideboard="True" Name="Chain of Smog" />
</Deck>
"""


# -- .dek --------------------------------------------------------------------


def test_parse_dek_main_and_sideboard():
    d = parse_dek(DEK)
    assert d.main == [("Mox Diamond", 1), ("Dark Ritual", 1), ("Swamp", 6)]
    assert d.side == [("Duress", 1), ("Chain of Smog", 1)]
    assert d.main_count == 8 and d.side_count == 2


def test_parse_dek_tolerates_namespaces_and_odd_casing():
    text = ('<d:Deck xmlns:d="urn:x"><d:cards name="Sol Ring" QUANTITY="2" sideboard="FALSE"/>'
            '<d:cards name="" quantity="1"/><d:Other/></d:Deck>')
    d = parse_dek(text)
    assert d.main == [("Sol Ring", 2)] and d.side == []


def test_find_dek_picks_newest_after_the_draft_started(tmp_path):
    a, b, c = (tmp_path / n for n in ("old.dek", "mid.dek", "new.dek"))
    for p in (a, b, c):
        p.write_text(DEK, encoding="utf-8")
    now = time.time()
    os.utime(a, (now - 7200, now - 7200))
    os.utime(b, (now - 1800, now - 1800))
    os.utime(c, (now - 60, now - 60))
    assert find_dek(tmp_path) == c
    assert find_dek(tmp_path, datetime.fromtimestamp(now - 3600)) == c
    assert find_dek(tmp_path, datetime.fromtimestamp(now)) is None          # nothing since the draft
    assert find_dek(tmp_path / "missing") is None and find_dek(None) is None
    (tmp_path / "notes.txt").write_text("x")
    assert find_dek(tmp_path) == c


def test_dek_watcher_rereads_only_on_change(tmp_path):
    p = tmp_path / "deck.dek"
    p.write_text(DEK, encoding="utf-8")
    w = DekWatcher(tmp_path)
    d1 = w.deck()
    assert d1 is not None and d1.main_count == 8 and d1.path == p and d1.modified_at
    assert w.deck() is d1                                   # unchanged file: same object
    p.write_text(DEK.replace('Quantity="6"', 'Quantity="7"'), encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))
    d2 = w.deck()
    assert d2 is not d1 and d2.main_count == 9
    p.write_text("<Deck><Cards", encoding="utf-8")
    os.utime(p, (time.time() + 10, time.time() + 10))
    assert w.deck() is None and "deck.dek" in w.error


def test_draft_started_at_parses_the_log_header():
    assert draft_started_at("9/21/2026 4:07:52 PM") == datetime(2026, 9, 21, 16, 7, 52)
    assert draft_started_at(None) is None and draft_started_at("nope") is None


# -- match log ---------------------------------------------------------------


def test_records_decode_with_timestamps():
    b = open(MATCH, "rb").read()
    recs = parse_records(b)
    assert len(recs) == 349
    assert recs[0].text == "jojo_lefou rolled a 4." and recs[0].at.strftime("%Y-%m-%d %H:%M") == "2026-09-22 10:07"
    assert recs[-1].text == "Wumpwumpwump wins the match 2-1"
    assert all(r.at >= recs[0].at for r in recs)


def test_match_structure():
    m = parse_match(open(MATCH, "rb").read(), match_id_of(__import__("pathlib").Path(MATCH)), None)
    assert m.match_id == "6fe35af5-8510-4cb0-a11d-0328e3823809"
    assert m.players == ["Wumpwumpwump", "jojo_lefou"]
    assert [g.number for g in m.games] == [1, 2, 3]
    assert [g.winner for g in m.games] == ["Wumpwumpwump", "jojo_lefou", "Wumpwumpwump"]
    assert m.winner == "Wumpwumpwump" and m.score == "2-1" and m.over
    g1 = m.games[0]
    assert g1.first == "Wumpwumpwump" and g1.hand_sizes == {"Wumpwumpwump": 7, "jojo_lefou": 7}
    assert m.games[1].hand_sizes["jojo_lefou"] == 6                   # mulligan line parsed
    assert g1.turn == 5 and g1.players == m.players


def test_seen_cards_count_once_per_name_except_basics_and_no_tokens():
    m = parse_match(open(MATCH, "rb").read())
    g3 = m.games[2]
    mine = seen_counts(g3, "Wumpwumpwump")
    assert "Blood Token" not in mine
    assert mine["Xander's Lounge"] == 1 and mine["Swamp"] == 2
    assert all(n == 1 for name, n in mine.items() if name not in ("Swamp",))
    assert seen_counts(g3, None) == {}


def test_match_view_with_and_without_a_deck():
    m = parse_match(open(MATCH, "rb").read(), "6fe35af5")
    v = match_view(m, "Wumpwumpwump", None)
    assert v["opponent"] == "jojo_lefou" and v["game_number"] == 3 and v["over"] and v["score"] == "2-1"
    assert v["remaining"] is None and v["library"] is None and v["life"] == {}
    assert v["seen_mine"]["Mox Diamond"] == 1 and v["seen_theirs"]
    assert v["games"] == [{"number": 1, "winner": "Wumpwumpwump"}, {"number": 2, "winner": "jojo_lefou"}, {"number": 3, "winner": "Wumpwumpwump"}]
    deck = [("Mox Diamond", 1), ("Swamp", 6), ("Never Played", 1)]
    v2 = match_view(m, "Wumpwumpwump", deck)
    assert v2["remaining"] == {"Swamp": 4, "Never Played": 1}
    assert "Mox Diamond" not in v2["remaining"]
    assert v2["unexpected"] and "Mox Diamond" not in v2["unexpected"]
    # hero unknown: still a view, but nothing attributed
    v3 = match_view(m, None, None)
    assert v3["seat_known"] is False and v3["seen_mine"] == {}


def test_watcher_follows_the_newest_log_after_the_draft(tmp_path):
    import shutil
    pattern = str(tmp_path / "Match_GameLog_*.dat")
    assert newest_match_after(None, pattern) is None
    old = tmp_path / "Match_GameLog_aaaaaaaa-0000-0000-0000-000000000000.dat"
    new = tmp_path / "Match_GameLog_6fe35af5-8510-4cb0-a11d-0328e3823809.dat"
    shutil.copyfile(MATCH, old)
    shutil.copyfile(MATCH, new)
    now = time.time()
    os.utime(old, (now - 7200, now - 7200))
    os.utime(new, (now - 60, now - 60))
    w = MatchWatcher(pattern)
    assert w.match(datetime.fromtimestamp(now - 3600)).match_id.startswith("6fe35af5")
    assert w.match(datetime.fromtimestamp(now - 10000)).match_id.startswith("6fe35af5")
    assert w.match(datetime.fromtimestamp(now)) is None
    # two identical logs: both names are in every log, so the account is ambiguous
    assert guess_hero(pattern) is None
    # a second opponent (same-length name keeps the length prefixes valid): the account is the common name
    old.write_bytes(MATCH_BYTES.replace(b"jojo_lefou", b"opponent_b"))
    assert guess_hero(pattern) == "Wumpwumpwump"


MATCH_BYTES = open(MATCH, "rb").read()


# -- results without a "wins the match" line ------------------------------------------


def _records(*lines: str) -> bytes:
    """Encode text lines the way MTGO does: ticks, kind byte, 7-bit length, '@P' + text."""
    import struct
    out = b""
    for i, t in enumerate(lines):
        body = ("@P" + t).encode("utf-8")
        n, length = len(body), b""
        while True:
            c, n = n & 0x7F, n >> 7
            length += bytes([c | (0x80 if n else 0)])
            if not n:
                break
        out += struct.pack("<q", 638_000_000_000_000_000 + i * 10_000_000) + b"\0" + length + body
    return out


def test_result_from_the_match_line():
    m = parse_match(_records("A joined the game.", "B joined the game.", "A wins the game.", "A wins the game.", "A wins the match 2-0"))
    assert (m.winner, m.score, m.result_source) == ("A", "2-0", "match line")


def test_result_from_two_game_wins_when_the_loser_walked_out():
    join = ("A joined the game.", "B joined the game.")          # every game starts with both joining
    m = parse_match(_records(*join, "B wins the game.", *join, "A wins the game.",
                             "A leads the match 1-1", *join, "A wins the game.", "B has left the game."))
    assert (m.winner, m.score, m.result_source) == ("A", "2-1", "game wins")


def test_result_from_the_lead_when_the_opponent_left():
    m = parse_match(_records("A joined the game.", "B joined the game.", "B has conceded from the game.", "A wins the game.",
                             "B has lost connection to the game.", "A leads the match 1-0", "B has left the game."))
    assert (m.winner, m.score, m.result_source) == ("A", "1-0", "opponent left")
    # the leader leaving is not a win for anyone
    m2 = parse_match(_records("A joined the game.", "B joined the game.", "A wins the game.", "A leads the match 1-0", "A has left the game."))
    assert m2.winner is None and m2.result_source is None
    # 1-1 with nobody leaving stays undecided
    m3 = parse_match(_records("A joined the game.", "B joined the game.", "A wins the game.", "B wins the game.", "B leads the match 1-1"))
    assert m3.winner is None and not m3.over


# -- attribution rules (DESIGN.md §2.3; measured over 2,521 real logs) --------------

from mtgo_draft_assistant import matches as M  # noqa: E402


def _ref(name: str, inst: int) -> str:
    return f"@[{name}@:{1000 + inst},{inst}:@]"


def _game(*lines: str) -> M.GameLog:
    g = M.GameLog(number=1, players=["A", "B"])
    for line in lines:
        M._attribute(g, line)
    return g


def _names(g: M.GameLog, who: str) -> set[str]:
    return set(g.seen.get(who, {}).values())


def test_targets_instruments_and_both_player_draws_are_not_attributed():
    g = _game(
        f"A casts {_ref('Lightning Bolt', 1)} targeting {_ref('Grizzly Bears', 2)}.",
        f"A draws seven cards with {_ref('Timetwister', 3)}.",
        f"A puts a +1/+1 counter on {_ref('Grizzly Bears', 2)}.",
        f"A chooses to use {_ref('Overlord', 4)}'s ability.",
        f"A reveals 3 cards with {_ref('Thoughtseize', 5)}: {_ref('P', 6)}, {_ref('Q', 7)}, and {_ref('R', 8)}.",
        f"A reveals their hand to {_ref('Duress', 9)}'s ability, containing {_ref('S', 10)} and {_ref('T', 11)}.",
        f"A puts {_ref('Titania', 12)} on top of their library with {_ref('Subtlety', 13)}'s ability.",
    )
    assert _names(g, "A") == {"Lightning Bolt", "P", "Q", "R", "S", "T", "Titania"}
    assert _names(g, "B") == set()
    assert g.last["A"]["P"] == M.EV_REVEALED and g.last["A"]["Titania"] == M.EV_TO_LIBRARY


def test_attackers_belong_to_the_other_player_including_two_line_records():
    g = _game(
        f"B is being attacked by {_ref('Grizzly Bears', 1)} and {_ref('Goblin Guide', 2)}",
        f"B is being attacked by {_ref('Boo', 3)}\nB's {_ref('Liliana of the Veil', 4)} is being attacked by {_ref('Headliner Scarlett', 5)}",
    )
    assert _names(g, "A") == {"Grizzly Bears", "Goblin Guide", "Boo", "Headliner Scarlett"}
    assert _names(g, "B") == {"Liliana of the Veil"}
    assert g.last["A"]["Boo"] == M.EV_ATTACKING and g.last["B"]["Liliana of the Veil"] == M.EV_BATTLEFIELD


def test_library_to_graveyard_moves_and_abilities():
    g = _game(
        f"A casts {_ref('Wheel of Fortune', 1)}.",
        f"A puts triggered ability from {_ref('Wheel of Fortune', 2)} onto the stack (Whenever you draw with X, do Y.).",
        f"A activates an ability of {_ref('Wishclaw Talisman', 3)} ( Search your library for a card with mana value 2.).",
        f"A puts {_ref('Brainstorm', 4)} into their graveyard.",
        f"A mills {_ref('X', 5)}, {_ref('Y', 6)}, and {_ref('Z', 7)}.",
        f"A puts {_ref('Hasty Thing', 8)} onto the battlefield attacking B",
    )
    assert _names(g, "A") == {"Wheel of Fortune", "Wishclaw Talisman", "Brainstorm", "X", "Y", "Z", "Hasty Thing"}
    last = g.last["A"]
    assert last["Wheel of Fortune"] == M.EV_CAST                 # an ability never overrides a real event
    assert last["Wishclaw Talisman"] == M.EV_ABILITY
    assert last["Brainstorm"] == M.EV_TO_GRAVEYARD and last["Y"] == M.EV_MILLED and last["Hasty Thing"] == M.EV_BATTLEFIELD


def test_exile_bounce_and_counter_only_update_known_cards():
    g = _game(
        f"A casts {_ref('Grizzly Bears', 1)}.",
        f"B casts {_ref('Big Spell', 2)}.",
        f"B exiles {_ref('Grizzly Bears', 3)} with with {_ref('Swords to Plowshares', 4)}'s ability.",
        f"A counters {_ref('Big Spell', 5)} with {_ref('Force of Will', 6)}.",
        f"A returns {_ref('Unknown Creature', 7)} to its owner's hand with {_ref('Reprieve', 8)}.",
        f"B's {_ref('Skyclave Apparition', 9)} creates an Illusion Token under B's control.",
        f"A's {_ref('Thopter Foundry', 10)} creates a Thopter Token.",
    )
    assert g.last["A"]["Grizzly Bears"] == M.EV_EXILED and g.last["B"]["Big Spell"] == M.EV_COUNTERED
    assert "Swords to Plowshares" not in _names(g, "A") | _names(g, "B")
    assert "Force of Will" not in _names(g, "A")                 # cast earlier in a real log; the "with" is not proof
    assert "Unknown Creature" not in _names(g, "A") | _names(g, "B")
    assert g.last["A"]["Reprieve"] == M.EV_ABILITY               # bouncing card: measured safe
    assert "Skyclave Apparition" not in _names(g, "B")           # the possessive names the token's recipient
    assert g.last["A"]["Thopter Foundry"] == M.EV_BATTLEFIELD


def test_shown_in_match_spans_games_with_last_event():
    m = parse_match(MATCH_BYTES, "6fe35af5")
    v = match_view(m, "Wumpwumpwump", None)
    shown = {e["name"]: e for e in v["shown_theirs"]}
    assert len(shown) == 25 and sum(e["current"] for e in shown.values()) == 11
    assert "Duress" not in shown                                 # the hero's: "opp reveals 3 cards with Duress: ..."
    assert shown["Fractured Identity"] == {"name": "Fractured Identity", "n": 1, "last": "milled", "game": 1, "current": False}
    assert shown["Plains"]["n"] == 5 and shown["Plains"]["current"]
    assert shown["Badgermole Cub"]["last"] == M.EV_ATTACKING
    assert v["shown_theirs"][0]["current"] and not v["shown_theirs"][-1]["current"]
    assert v["last_mine"]["Xander's Lounge"] == M.EV_DISCARDED and v["last_mine"]["Mox Diamond"] == M.EV_CAST
    assert "Blood Token" not in shown
