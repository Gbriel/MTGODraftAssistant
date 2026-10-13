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
    History, MatchIndex, MatchSummary, compute, link_matches, scan_drafts, shown_by, summarise,
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
    assert snap["drafts"] == 3 and snap["drafts_linked"] == 1
    cards = {c["name"]: c for c in snap["cards"]}
    d922 = drafts[-1]
    name = next(n for n in shown_by(m, HERO) if n in d922.picks)
    c = cards[name]
    seen_games = [g for g in m.games if name in g["seen"][HERO]]
    assert c["picked"] >= 1 and c["drafts_with_matches"] == 1 and (c["pick_w"], c["pick_l"]) == (1, 0) and c["trophies"] == 0
    assert c["gih_g"] == len(seen_games) and c["gih_w"] == sum(g["winner"] == HERO for g in seen_games)
    assert (c["gih_m"], c["gih_mw"]) == (1, 1) and c["all_g"] == c["gih_g"]
    # a card picked only in a draft without matches has no record
    only_921 = next(n for n in drafts[0].picks if n not in d922.picks and n not in drafts[1].picks)
    assert cards[only_921]["picked"] == 1 and cards[only_921]["drafts_with_matches"] == 0 and cards[only_921]["gih_g"] == 0
    # three wins and no losses in the draft is a trophy
    more = [dataclasses.replace(m, match_id=f"m{i}", started_at=f"2026-09-22T1{i}:00:00") for i in range(3)]
    snap3 = compute(drafts, more, HERO)
    assert snap3["draft_rows"][-1]["trophy"] and snap3["draft_rows"][-1]["w"] == 3
    assert {c["name"]: c for c in snap3["cards"]}[name]["trophies"] == 1
    assert snap3["opponents"] == {"jojo_lefou": [3, 0]}


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
    # background refresh coalesces and bumps the version
    h.request_refresh()
    h.wait(10)
    assert h.version == 2
    h.close()
