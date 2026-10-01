"""Wheel diff and in-flight analysis against the real fixtures."""

from __future__ import annotations

import glob
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.analysis import (  # noqa: E402
    analyse,
    current_position,
    in_flight,
    pack_sizes,
    pod_size,
    wheel_diffs,
)
from mtgo_draft_assistant.draft_log import Draft, Pick, parse  # noqa: E402

SNAP_DIR = os.path.join(ROOT, "tests", "fixtures", "snapshots")
FINAL = os.path.join(ROOT, "tests", "fixtures", "final_draft_log.txt")


def snapshots() -> list[str]:
    return sorted(glob.glob(os.path.join(SNAP_DIR, "snap_*.txt")))


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def final() -> Draft:
    return parse(read(FINAL))


# -- derived sizes -----------------------------------------------------------


def test_pod_and_pack_sizes_are_derived_not_hardcoded(final):
    assert pod_size(final) == 8
    assert pack_sizes(final) == {1: 15, 2: 15, 3: 15}


def test_pack_size_derivable_from_a_mid_pack_block():
    d = Draft(players=["a", "b", "c", "d"])
    d.picks.append(Pick(pack=1, pick=4, picked="x", available=list("xabcdefgh"), complete=True))
    # 9 cards at pick 4 -> 12-card booster
    assert pack_sizes(d) == {1: 12}


# -- wheel diffs -------------------------------------------------------------


def test_final_draft_has_21_clean_wheels(final):
    wheels, warnings = wheel_diffs(final)
    assert warnings == []
    assert len(wheels) == 21
    for w in wheels:
        assert w.return_pick == w.first_pick + 8
        assert set(w.returned) <= set(w.passed)
        assert len(w.taken) == 7
        assert set(w.taken) | set(w.returned) == set(w.passed)
        assert w.your_pick not in w.passed
        assert w.warning is None


def test_first_wheel_of_the_draft_is_correct(final):
    wheels, _ = wheel_diffs(final)
    w = wheels[0]
    assert (w.pack, w.first_pick, w.return_pick) == (1, 1, 9)
    assert w.your_pick == "Broadside Bombardiers"
    # From the fixture: P1P9 showed these 7 cards.
    assert w.returned == [
        "Loran of the Third Path", "Faerie Mastermind", "Talisman of Curiosity",
        "Damn", "Arena of Glory", "Loot, the Pathfinder", "Tamiyo, Collector of Tales",
    ]
    assert w.taken == [
        "Mother of Runes", "Dark Ritual", "Sheoldred's Edict", "Elvish Mystic",
        "Dack Fayden", "Wastewood Verge", "Bolas's Citadel",
    ]


def test_wheel_is_visible_as_soon_as_pack_is_on_screen():
    """The returning block need not be picked yet for the diff to exist."""
    seen = 0
    for path in snapshots():
        d = parse(read(path))
        live = d.current_pack
        if live is None or live.pick <= pod_size(d):
            continue
        wheels, _ = wheel_diffs(d)
        match = [w for w in wheels if (w.pack, w.return_pick) == (live.pack, live.pick)]
        assert len(match) == 1, os.path.basename(path)
        assert match[0].returned == live.available
        seen += 1
    assert seen > 0, "no snapshot had a wheeled pack on screen; fixtures changed?"


def _synthetic_pod4() -> Draft:
    """4-player pod, 6-card packs: pick p wheels at p + 4."""
    d = Draft(players=["me", "a", "b", "c"], hero="me")
    p1 = ["A", "B", "C", "D", "E", "F"]
    d.picks.append(Pick(1, 1, "A", p1, True))
    d.picks.append(Pick(1, 2, "G", ["G", "H", "I", "J", "K"], True))
    d.picks.append(Pick(1, 3, "L", ["L", "M", "N", "O"], True))
    d.picks.append(Pick(1, 4, "P", ["P", "Q", "R"], True))
    return d


def test_wheel_with_non_default_pod_size():
    d = _synthetic_pod4()
    d.picks.append(Pick(1, 5, None, ["C", "F"], False))   # P1P1 came back
    wheels, warnings = wheel_diffs(d)
    assert warnings == []
    assert len(wheels) == 1
    w = wheels[0]
    assert (w.first_pick, w.return_pick) == (1, 5)
    assert w.returned == ["C", "F"]
    assert w.taken == ["B", "D", "E"]


def test_wheel_mismatch_falls_back_and_warns():
    d = _synthetic_pod4()
    # Returned cards belong to the P1P2 pack, not P1P1 — a mispaired lap.
    d.picks.append(Pick(1, 5, None, ["H", "K"], False))
    wheels, warnings = wheel_diffs(d)
    assert len(wheels) == 1
    w = wheels[0]
    assert w.first_pick == 2
    assert w.warning and "matched P1P2" in w.warning
    assert warnings == [w.warning]


def test_wheel_unmatchable_still_reported_with_warning():
    d = _synthetic_pod4()
    d.picks.append(Pick(1, 5, None, ["ZZZ"], False))
    wheels, warnings = wheel_diffs(d)
    assert len(wheels) == 1
    assert wheels[0].warning and "not a subset" in wheels[0].warning
    assert warnings


def test_no_players_block_yields_warning_not_crash():
    d = Draft()
    d.picks.append(Pick(1, 1, "A", ["A", "B"], True))
    wheels, warnings = wheel_diffs(d)
    assert wheels == []
    assert warnings and "pod size" in warnings[0]
    assert in_flight(d) == []


def _synthetic_pick_two() -> Draft:
    """4-player pod, 8-card packs, two cards per pick: pick p wheels at p + 4 with 6 gone."""
    d = Draft(pod_size_hint=4, source="arena", cards_per_pick=2)
    p1 = list("ABCDEFGH")
    d.picks.append(Pick(1, 1, "A", p1, True, also_picked=["B"]))
    d.picks.append(Pick(1, 2, "I", list("IJKLMN"), True, also_picked=["J"]))
    d.picks.append(Pick(1, 3, "O", list("OPQR"), True, also_picked=["P"]))
    d.picks.append(Pick(1, 4, "S", list("ST"), True, also_picked=["T"]))
    return d


def test_pick_two_pack_sizes_and_pool():
    d = _synthetic_pick_two()
    assert pack_sizes(d) == {1: 8}
    assert d.pool == list("ABIJOPST")
    assert d.picks[0].passed == list("CDEFGH")


def test_pick_two_wheel_expects_two_cards_per_seat():
    d = _synthetic_pick_two()
    # 6 cards passed at P1P1; the other 3 seats take 2 each: nothing comes back
    assert in_flight(d) == []                       # 8 cards / 2 = 4 picks; pick 1 + 4 > 4
    # a 6-player pod with 14-card packs: pick 1 returns at pick 7 with 10 gone
    e = Draft(pod_size_hint=6, source="arena", cards_per_pick=2)
    cards = [f"c{i}" for i in range(14)]
    e.picks.append(Pick(1, 1, "c0", cards, True, also_picked=["c1"]))
    for k in range(2, 7):
        e.picks.append(Pick(1, k, f"x{k}", [f"x{k}", f"y{k}"] + [f"z{k}{i}" for i in range(14 - 2 * (k - 1) - 2)], True, also_picked=[f"y{k}"]))
    e.picks.append(Pick(1, 7, None, ["c5", "c9"], False))      # P1P1 came back with 2 of the 12 passed
    wheels, warnings = wheel_diffs(e)
    assert warnings == [] and len(wheels) == 1
    assert wheels[0].returned == ["c5", "c9"] and len(wheels[0].taken) == 10
    assert pack_sizes(e) == {1: 14}


# -- in flight ---------------------------------------------------------------


def test_in_flight_across_all_snapshots():
    for path in snapshots():
        d = parse(read(path))
        n = pod_size(d)
        cur_pack, cur_pick = current_position(d)
        sizes = pack_sizes(d)
        flights = in_flight(d)
        by = {(p.pack, p.pick) for p in d.picks}
        for f in flights:
            assert f.pack == cur_pack
            assert f.due_pick == f.first_pick + n
            # due now (waiting for MTGO to write it) counts as still in flight
            assert f.due_pick >= cur_pick
            assert f.due_pick <= sizes[cur_pack]
            assert f.picks_until_return == f.due_pick - cur_pick
            assert f.your_pick not in f.passed
            assert (cur_pack, f.due_pick) not in by
        expected = [
            p for p in d.picks
            if p.pack == cur_pack and p.complete
            and p.pick + n <= sizes[cur_pack] and (cur_pack, p.pick + n) not in by
        ]
        assert [f.first_pick for f in flights] == [p.pick for p in expected], path


def test_in_flight_pack_1_pick_1_on_screen_in_pack_2():
    """snap_003 ends with P2P1 on screen: nothing from pack 2 has been passed."""
    d = parse(read(os.path.join(SNAP_DIR, "snap_003_4676b.txt")))
    assert current_position(d) == (2, 1)
    assert in_flight(d) == []


def test_in_flight_mid_pack():
    d = _synthetic_pod4()                      # 4 complete picks, 6-card packs
    d.picks.append(Pick(1, 5, None, ["C", "F"], False))
    flights = in_flight(d)
    # P1P1 is back (pick 5). P1P2 is due at 6. P1P3+ never return (7 > 6).
    assert [(f.first_pick, f.due_pick, f.picks_until_return) for f in flights] == [(2, 6, 1)]
    assert flights[0].passed == ["H", "I", "J", "K"]


def test_completed_draft_has_nothing_in_flight(final):
    assert in_flight(final) == []
    assert current_position(final) == (3, 16)


def test_analyse_bundle(final):
    a = analyse(final)
    assert a.pod_size == 8
    assert len(a.wheels) == 21
    assert a.in_flight == []
    assert a.warnings == []
    assert (a.current_pack, a.current_pick) == (3, 16)
