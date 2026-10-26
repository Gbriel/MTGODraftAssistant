"""Personal history: match logs summarised and cached, tied to drafts by deck overlap, counted."""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant import matches as M  # noqa: E402
from mtgo_draft_assistant.history import (  # noqa: E402
    History, MatchIndex, MatchSummary, build_decks, compute, link_matches, scan_drafts, shown_by, summarise,
)

FIX = Path(ROOT) / "tests" / "fixtures"
MATCH = FIX / "mtgo" / "Match_GameLog_6fe35af5-8510-4cb0-a11d-0328e3823809.dat"   # 2026-09-22 10:07 vs jojo_lefou, 2-1
DRAFT_922 = FIX / "drafts" / "Wumpwumpwump-2026.9.22-11053-35969982-C03C03C03.txt"  # 9/22 09:15: the deck that match was played with
DRAFT_921 = FIX / "drafts" / "Wumpwumpwump-2026.9.21-11053-35967551-C03C03C03.txt"  # 9/21 20:33
FINAL = FIX / "final_draft_log.txt"                                                # 9/21 16:07
HERO = "Wumpwumpwump"


def _logs(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    for src in (DRAFT_922, DRAFT_921):
        shutil.copyfile(src, logs / src.name)
    shutil.copyfile(FINAL, logs / "Wumpwumpwump-2026.9.21-11053-35966161-C03C03C03.txt")
    return logs


def test_match_index_summarises_caches_and_dedupes(tmp_path, monkeypatch):
    # the same match in two deployment folders (old and new MTGO install) counts once
    for folder in ("old", "new"):
        (tmp_path / folder).mkdir()
        shutil.copyfile(MATCH, tmp_path / folder / MATCH.name)
    pattern = str(tmp_path / "*" / "Match_GameLog_*.dat")
    idx = MatchIndex(tmp_path / "cache", pattern)
    ms = idx.scan()
    assert len(ms) == 1
    m = ms[0]
    assert m.match_id.startswith("6fe35af5") and m.players == [HERO, "jojo_lefou"]
    assert m.winner == HERO and m.score == "2-1" and len(m.games) == 3
    assert m.started_at.startswith("2026-09-22T10:07")
    assert "Mox Diamond" in m.games[2]["seen"][HERO] and "Blood Token" not in m.games[2]["seen"][HERO]
    idx.close()
    # second index on the same cache: nothing is parsed again
    monkeypatch.setattr(M, "parse_match", lambda *a, **k: (_ for _ in ()).throw(AssertionError("parsed again")))
    idx2 = MatchIndex(tmp_path / "cache", pattern)
    assert [s.match_id for s in idx2.scan()] == [m.match_id]
    idx2.close()


def test_scan_drafts_reads_pod_and_picks(tmp_path):
    drafts = scan_drafts(_logs(tmp_path))
    assert [d.started_at for d in drafts] == [datetime(2026, 9, 21, 16, 7, 52), datetime(2026, 9, 21, 20, 33, 53),
                                              datetime(2026, 9, 22, 9, 15, 11)]
    d = drafts[-1]
    assert d.hero == HERO and len(d.players) == 8 and 40 <= len(d.picks) <= 46 and d.event_id == "11053"


def test_matches_link_to_the_draft_whose_picks_they_show(tmp_path):
    drafts = scan_drafts(_logs(tmp_path))
    m = summarise(M.parse_match(MATCH.read_bytes(), M.match_id_of(MATCH), MATCH))
    shown = shown_by(m, HERO)
    assert "Mox Diamond" in shown and "Swamp" not in shown
    linked = link_matches(drafts, [m], HERO)
    assert set(linked) == {drafts[-1].path}                   # the 9/22 draft, not the two from 9/21
    assert len(shown & set(drafts[-1].picks)) / len(shown) >= 0.9
    # a match played with cards from no draft (constructed) stays unlinked
    other = dataclasses.replace(m, games=[{"winner": g["winner"], "seen": {HERO: ["Not A Cube Card", "Nor This"]}} for g in m.games])
    assert link_matches(drafts, [other], HERO) == {}
    # a match showing none of your cards goes to the draft just before it
    blank = dataclasses.replace(m, games=[{"winner": g["winner"], "seen": {}} for g in m.games])
    assert set(link_matches(drafts, [blank], HERO)) == {drafts[-1].path}
    # no match before the draft started
    early = dataclasses.replace(m, started_at="2026-09-20T10:00:00")
    assert link_matches(drafts, [early], HERO) == {}


def test_compute_card_and_opponent_stats(tmp_path):
    drafts = scan_drafts(_logs(tmp_path))
    m = summarise(M.parse_match(MATCH.read_bytes(), M.match_id_of(MATCH), MATCH))
    snap = compute(drafts, [m], None)
    assert snap["hero"] == HERO                                # from the draft logs' arrow
    assert snap["opponents"] == {"jojo_lefou": [1, 0]} and snap["matches_with_result"] == 1
    assert snap["drafts"] == 3 and snap["decks"] == 1 and snap["decks_from_drafts"] == 1 and snap["decks_inferred"] == 0
    cards = {c["name"]: c for c in snap["cards"]}
    d922 = drafts[-1]
    name = next(n for n in shown_by(m, HERO) if n in d922.picks)
    c = cards[name]
    seen_games = [g for g in m.games if name in g["seen"][HERO]]
    assert c["played"] == 1 and (c["play_w"], c["play_l"]) == (1, 0) and c["play_trophies"] == 0
    assert c["picked"] >= 1 and c["picked_with_matches"] == 1 and (c["pick_w"], c["pick_l"]) == (1, 0)
    assert c["cube_g"] == len(seen_games) and c["cube_w"] == sum(g["winner"] == HERO for g in seen_games)
    assert (c["all_g"], c["all_w"]) == (c["cube_g"], c["cube_w"])
    # a card picked only in a draft without matches has no record
    only_921 = next(n for n in drafts[0].picks if n not in d922.picks and n not in drafts[1].picks)
    assert cards[only_921]["picked"] == 1 and cards[only_921]["played"] == 0 and cards[only_921]["cube_g"] == 0
    # three wins and no losses in the deck is a trophy
    more = [dataclasses.replace(m, match_id=f"m{i}", started_at=f"2026-09-22T1{i}:00:00") for i in range(3)]
    snap3 = compute(drafts, more, HERO)
    assert snap3["deck_rows"][-1]["trophy"] and snap3["deck_rows"][-1]["w"] == 3 and snap3["deck_rows"][-1]["source"] == "draft"
    assert {c["name"]: c for c in snap3["cards"]}[name]["play_trophies"] == 1
    assert snap3["opponents"] == {"jojo_lefou": [3, 0]}
    # your averages: the match fixture is 2-1 in games
    assert snap["totals"]["cube"] == {"decks": 1, "trophies": 0, "m_w": 1, "m_l": 0, "g_w": 2, "g": 3}
    assert snap["totals"]["all"]["m_w"] == 1 and snap["totals"]["all"]["g"] == 3
    assert snap3["totals"]["cube"]["trophies"] == 1 and snap3["totals"]["cube"]["g_w"] == 6


def _match(mid: str, when: str, cards: list[str], won: bool = True, opp: str = "opp") -> MatchSummary:
    return MatchSummary(match_id=mid, path="", started_at=when, players=[HERO, opp],
                        winner=HERO if won else opp, score="2-0",
                        games=[{"winner": HERO if won else opp, "seen": {HERO: cards, opp: ["Their Card"]}}])


def test_decks_are_inferred_from_the_matches_when_no_draft_log_exists():
    A = [f"a{i}" for i in range(20)]                   # one deck: each match shows a different ~half of it
    B = [f"b{i}" for i in range(20)]
    ms = [
        _match("a1", "2025-03-01T10:00:00", A[:12]),
        _match("a2", "2025-03-01T10:40:00", A[6:18], won=False),
        _match("a3", "2025-03-01T11:20:00", A[10:20] + A[:2]),
        _match("b1", "2025-03-01T13:00:00", B[:12]),             # same day, different cards: a new deck
        _match("b2", "2025-03-01T13:45:00", B[5:17]),
        _match("b3", "2025-03-01T14:30:00", B[8:20]),
        _match("a4", "2025-03-01T16:00:00", A[:12]),             # deck A again after three finished matches: a fourth entry
        _match("c1", "2025-03-09T10:00:00", A[:12]),             # a week later: too far to join either
    ]
    decks = build_decks([], ms, HERO)
    groups = [[m.match_id for m in d.matches] for d in decks]
    assert groups == [["a1", "a2", "a3"], ["b1", "b2", "b3"], ["a4"], ["c1"]]
    assert all(d.source == "inferred" for d in decks)
    snap = compute([], ms, HERO)
    assert snap["decks"] == 4 and snap["decks_inferred"] == 4 and snap["decks_from_drafts"] == 0
    rows = snap["deck_rows"]
    assert (rows[0]["w"], rows[0]["l"], rows[0]["trophy"]) == (2, 1, False) and rows[1]["trophy"]
    cards = {c["name"]: c for c in snap["cards"]}
    assert cards["a0"]["played"] == 3 and (cards["a0"]["play_w"], cards["a0"]["play_l"]) == (4, 1)   # a1-3, a4, c1
    assert cards["b0"]["played"] == 1 and cards["b0"]["play_trophies"] == 1 and cards["b0"]["cube_g"] == 1
    assert cards["a11"]["cube_g"] == 5 and cards["a11"]["cube_w"] == 4                             # a1 a2(L) a3 a4 c1
    assert "Their Card" not in cards


def test_constructed_chains_are_set_aside():
    deck = [f"legacy{i}" for i in range(20)]
    # twelve matches over three days, each showing 12 of the deck's 16 non-basics
    ms = [_match(f"l{i}", f"2025-05-{1 + i // 4:02d}T{10 + 2 * (i % 4):02d}:00:00", deck[(i % 3) * 2:(i % 3) * 2 + 12], won=i % 2 == 0)
          for i in range(12)]
    ms.append(_match("cube1", "2025-05-04T10:00:00", ["x1", "x2", "x3", "x4", "x5"]))
    decks = build_decks([], ms, HERO)
    srcs = sorted((d.source, len(d.matches)) for d in decks)
    assert srcs == [("constructed", 12), ("inferred", 1)]
    snap = compute([], ms, HERO)
    assert snap["constructed_matches"] == 12 and snap["decks"] == 1
    cards = {c["name"]: c for c in snap["cards"]}
    assert cards["legacy0"]["played"] == 0 and cards["legacy0"]["cube_g"] == 0 and cards["legacy0"]["all_g"] == 4   # the four deck[0:12] matches
    assert cards["x1"]["played"] == 1 and cards["x1"]["cube_g"] == 1
    assert snap["opponents"] == {"opp": [7, 6]}
    # constructed matches count in the all-formats averages only
    assert (snap["totals"]["cube"]["m_w"], snap["totals"]["cube"]["m_l"]) == (1, 0)
    assert (snap["totals"]["all"]["m_w"], snap["totals"]["all"]["m_l"]) == (7, 6)


def test_history_object_refreshes_and_answers_records(tmp_path):
    logs = _logs(tmp_path)
    mdir = tmp_path / "m"
    mdir.mkdir()
    shutil.copyfile(MATCH, mdir / MATCH.name)
    h = History(logs, tmp_path / "cache", pattern=str(mdir / "Match_GameLog_*.dat"))
    assert h.snapshot()["status"] == "scanning" and h.version == 0
    snap = h.refresh()
    assert snap["status"] == "ready" and snap["error"] is None and h.version == 1
    assert h.records_for(["jojo_lefou", "nobody", HERO]) == {"jojo_lefou": [1, 0]}
    assert h.snapshot()["cards"] and h.snapshot()["version"] == 1
    # head-to-head: the record plus the meetings, newest first, from your side
    hh = h.head_to_head("jojo_lefou")
    assert (hh["w"], hh["l"], hh["unfinished"]) == (1, 0, 0)
    assert hh["matches"] == [{"started_at": "2026-09-22T10:07:17", "result": "W", "score": "2-1"}]
    # someone never met is an answer too (an empty list), unlike "not scanned yet"
    assert h.head_to_head("nobody") == {"name": "nobody", "w": 0, "l": 0, "unfinished": 0, "matches": []}
    # the match on screen is already in the index (MTGO writes the log live): leave it out
    mid = M.match_id_of(MATCH)
    assert h.head_to_head("jojo_lefou", exclude=mid)["matches"] == []
    assert h.head_to_head(HERO)["matches"] == []
    assert History(logs, tmp_path / "cache2").head_to_head("jojo_lefou") is None    # not scanned yet
    # background refresh coalesces and bumps the version
    h.request_refresh()
    h.wait(10)
    assert h.version == 2
    h.close()
