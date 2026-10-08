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
