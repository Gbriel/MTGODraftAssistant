"""Arena deck submissions, live game state, and the Arena card database. No network."""

from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.arena_cards import ArenaCardDb, clean, find_database  # noqa: E402
from mtgo_draft_assistant.arena_game import ArenaGameParser, game_view  # noqa: E402
from mtgo_draft_assistant.arena_log import ArenaWatcher  # noqa: E402

MATCH = os.path.join(ROOT, "tests", "fixtures", "arena", "arena_match_opening.log")
LIVE = os.path.join(ROOT, "tests", "fixtures", "arena", "arena_cube_in_progress.log")

# The shape of a real EventSetDeckV3 line (structure verbatim from a 2026-09-24 log).
# The deck used in tests is built from the cards this seat is actually seen holding
# in the match fixture (found by parsing it) plus a few ids never seen.
UNSEEN_IDS = [90503, 100632, 60981, 28269, 95538, 60315, 96671]


def seen_in_fixture() -> list[int]:
    """grpIds of this seat's cards visible at the end of the match fixture."""
    lines = read(MATCH).split("\r\n")
    p = ArenaGameParser()
    last_gre = max(i for i, l in enumerate(lines) if "greToClientEvent" in l)
    for l in lines[:last_gre + 1]:
        p.feed_line(l)
    return sorted(game_view(p.match, None)["seen_mine"])


def deck_line(main, side, event="CubeDraft_Powered_20260908"):
    req = {"EventName": event, "Summary": {"DeckId": "0d42d1d3-87bd-41c3-9ce7-8fdcb800a2ca", "Name": "Draft Deck",
                                          "Attributes": [{"name": "Version", "value": "11"}]},
           "Deck": {"MainDeck": [{"cardId": g, "quantity": q} for g, q in main],
                    "Sideboard": [{"cardId": g, "quantity": q} for g, q in side],
                    "CommandZone": [], "Companions": [], "CardSkins": []}}
    return "[UnityCrossThreadLogger]==> EventSetDeckV3 " + json.dumps({"id": "b0cd7f46", "request": json.dumps(req)})


def read(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8")


# -- deck ----------------------------------------------------------------------


def test_deck_submission_is_parsed_with_quantities():
    p = ArenaGameParser()
    p.feed_line("[UnityCrossThreadLogger]9/24/2026 10:12:00 AM")
    p.feed_line(deck_line([(12623, 1), (99488, 2)], [(90503, 1)]))
    d = p.latest_deck
    assert d.event_name == "CubeDraft_Powered_20260908"
    assert d.main == [(12623, 1), (99488, 2)] and d.side == [(90503, 1)]
    assert d.main_count == 3 and d.side_count == 1
    assert d.submitted_at == "9/24/2026 10:12:00 AM" and d.deck_id.startswith("0d42d1d3")
    assert p.version == 1
    p.feed_line(deck_line([(1, 40)], [], event="OtherEvent"))
    assert p.deck_for("CubeDraft_Powered_20260908").main[0] == (12623, 1)
    assert p.deck_for("Unknown").main == [(1, 40)]            # falls back to the latest


def test_garbage_deck_line_is_ignored():
    p = ArenaGameParser()
    p.feed_line("[UnityCrossThreadLogger]==> EventSetDeckV3 {not json")
    p.feed_line('[UnityCrossThreadLogger]==> EventSetDeckV3 {"request": "{\\"Deck\\": 5}"}')
    assert p.decks == [] and p.version == 0


# -- game ----------------------------------------------------------------------


def test_match_fixture_tracks_library_and_seen_cards():
    text = read(MATCH)
    lines = text.split("\r\n")
    p = ArenaGameParser()
    last_gre = max(i for i, l in enumerate(lines) if "greToClientEvent" in l)
    for l in lines[:last_gre + 1]:
        p.feed_line(l)
    m = p.match
    assert m.match_id.startswith("058c15c0") and m.state == "playing"
    assert m.players == {1: "AidsSully", 2: "Wumpwumpwump"}
    v = game_view(m, None)
    assert v["my_seat"] == 2 and v["opponent"] == "AidsSully" and v["seat_known"]
    assert v["game_number"] == 1 and v["turn"] == 4 and not v["over"]
    assert v["library"] == 29
    assert sum(v["seen_mine"].values()) == 11 and v["library"] + sum(v["seen_mine"].values()) == 40
    assert len(v["seen_mine"]) == 11 and all(n == 1 for n in v["seen_mine"].values())
    assert v["remaining"] is None and v["unexpected"] == {}
    assert v["seen_theirs"] and all(n == 1 for n in v["seen_theirs"].values())
    assert v["life"] == {"1": 15, "2": 20}

    # the closing room event ends the match: no live view any more
    for l in lines[last_gre + 1:]:
        p.feed_line(l)
    assert p.match.state == "completed" and len(p.match.results) == 2
    assert game_view(p.match, None) is None


def test_remaining_is_deck_minus_seen():
    seen = seen_in_fixture()
    in_deck, left_out = seen[:8], seen[8:]
    assert left_out, "fixture should show more than 8 of this seat's cards"
    unseen = [g for g in UNSEEN_IDS if g not in seen]
    lines = read(MATCH).split("\r\n")
    p = ArenaGameParser()
    p.feed_line(deck_line([(g, 1) for g in in_deck + unseen] + [(12345, 5)], [(99999, 1)]))
    last_gre = max(i for i, l in enumerate(lines) if "greToClientEvent" in l)
    for l in lines[:last_gre + 1]:
        p.feed_line(l)
    v = game_view(p.match, p.latest_deck)
    assert v["remaining"] is not None
    for g in in_deck:
        assert g not in v["remaining"]                    # seen once, in the deck once
    for g in unseen:
        assert v["remaining"][g] == 1
    assert v["remaining"][12345] == 5
    assert sum(v["remaining"].values()) == len(unseen) + 5
    # cards seen that the submitted main deck does not hold are flagged, never hidden
    assert set(v["unexpected"]) == set(left_out)


def test_full_state_resets_the_game():
    lines = read(MATCH).split("\r\n")
    p = ArenaGameParser()
    for l in lines:
        p.feed_line(l)
    n_objects = len(p.match.game.objects)
    assert n_objects > 0
    full = next(l for l in lines if "GameStateType_Full" in l)
    p.feed_line(full)
    assert len(p.match.game.objects) < n_objects and p.match.game.stage == "GameStage_Start"


# -- watcher integration ---------------------------------------------------------


def test_watcher_exposes_deck_and_game_and_counts_them_as_activity(tmp_path):
    log = tmp_path / "Player.log"
    log.write_bytes(read(LIVE).encode("utf-8"))
    w = ArenaWatcher(log, namer=lambda g: None)
    u = w.poll()
    assert u is not None and w.deck() is None and w.game() is None
    t0 = w.last_activity()
    with open(log, "ab") as fh:
        fh.write(b"[UnityCrossThreadLogger]9/30/2026 9:00:00 PM\r\n")
        fh.write((deck_line([(g, 1) for g in UNSEEN_IDS], []) + "\r\n").encode("utf-8"))
    u = w.poll()
    assert u is not None and not u.new_draft, "a deck submission is an update to the same draft"
    assert w.deck().main_count == len(UNSEEN_IDS)
    assert w.last_activity() > t0, "deck submission counts as Arena activity"

    lines = read(MATCH).split("\r\n")
    last_gre = max(i for i, l in enumerate(lines) if "greToClientEvent" in l)
    with open(log, "ab") as fh:
        fh.write(("\r\n".join(lines[:last_gre + 1]) + "\r\n").encode("utf-8"))
    u = w.poll()
    assert u is not None
    g = w.game()
    assert g is not None and g["library"] == 29 and g["remaining"] is not None


def test_watcher_with_games_but_no_draft_still_reports(tmp_path):
    log = tmp_path / "Player.log"
    lines = read(MATCH).split("\r\n")
    last_gre = max(i for i, l in enumerate(lines) if "greToClientEvent" in l)
    log.write_bytes(("\r\n".join(lines[:last_gre + 1]) + "\r\n").encode("utf-8"))
    w = ArenaWatcher(log)
    u = w.poll()
    assert u is not None and u.draft.picks == [] and u.draft.source == "arena"
    assert w.game()["library"] == 29


# -- Arena card database -------------------------------------------------------------


def test_clean_strips_arena_markup():
    assert clean("Fable of the <nobr>Mirror-Breaker</nobr>") == "Fable of the Mirror-Breaker"
    assert clean("  Black   Lotus ") == "Black Lotus" and clean("") is None and clean(None) is None


def _fake_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE Cards (GrpId INTEGER, TitleId INTEGER, ExpansionCode TEXT, CollectorNumber TEXT, IsToken INTEGER, Rarity INTEGER);
        CREATE TABLE Localizations_enUS (LocId INTEGER, Formatted INTEGER, Loc TEXT);
        CREATE TABLE Versions (k TEXT, v TEXT);
        INSERT INTO Cards VALUES (101033, 24805, 'ANA', '0', 0, 4);
        INSERT INTO Cards VALUES (90503, 756582, 'OTJ', '157', 0, 4);
        INSERT INTO Localizations_enUS VALUES (24805, 1, 'Dismember');
        INSERT INTO Localizations_enUS VALUES (24805, 0, 'dismember-unformatted');
        INSERT INTO Localizations_enUS VALUES (756582, 1, 'Bristly Bill, <nobr>Spine Sower</nobr>');
        INSERT INTO Versions VALUES ('Data', '2026.63.0.270');
    """)
    db.commit()
    db.close()


def test_card_db_names_ids_scryfall_lacks(tmp_path):
    path = tmp_path / "Raw_CardDatabase_abc.mtga"
    _fake_db(path)
    db = ArenaCardDb(path)
    assert db.name(101033) == "Dismember"
    assert db.name(90503) == "Bristly Bill, Spine Sower"
    assert db.name(1) is None
    assert db.card(101033) == {"grp_id": 101033, "name": "Dismember", "set": "ANA", "number": "0",
                               "token": False, "rarity": 4}
    assert db.version() == "Data=2026.63.0.270"
    db.close()


def test_find_database_picks_newest(tmp_path):
    import time
    old = tmp_path / "Raw_CardDatabase_old.mtga"
    new = tmp_path / "Raw_CardDatabase_new.mtga"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    os.utime(old, (time.time() - 100, time.time() - 100))
    assert find_database((str(tmp_path / "Raw_CardDatabase_*.mtga"),)) == new
    assert find_database((str(tmp_path / "nothing_*.mtga"),)) is None
