"""
Replay 16 real snapshots of a draft log as it was being written, in order.

This is the regression net for live tailing. Parsing a finished log is easy;
the hard part is reading a file while MTGO is still appending to it. These are
the properties a live tracker depends on — if one breaks, the parser is wrong.
"""

from __future__ import annotations

import glob
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.draft_log import Draft, parse  # noqa: E402

SNAP_DIR = os.path.join(ROOT, "tests", "fixtures", "snapshots")
FINAL = os.path.join(ROOT, "tests", "fixtures", "final_draft_log.txt")

POD_SIZE = 8
PACK_SIZE = 15


def snapshots() -> list[str]:
    return sorted(glob.glob(os.path.join(SNAP_DIR, "snap_*.txt")))


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def committed(d: Draft) -> list[tuple[int, int, str]]:
    return [(p.pack, p.pick, p.picked) for p in d.picks if p.complete]


def test_fixtures_present():
    assert len(snapshots()) == 16
    assert os.path.exists(FINAL)


def test_logs_are_utf8():
    """MTGO writes UTF-8. Reading as latin-1 mojibakes names like Palantír."""
    for path in snapshots() + [FINAL]:
        with open(path, "rb") as fh:
            fh.read().decode("utf-8")  # raises if not valid UTF-8


@pytest.mark.parametrize("path", snapshots(), ids=os.path.basename)
def test_position_recoverable_from_card_count(path):
    """The header pick number is garbage; the card count is not."""
    for p in parse(read(path)).picks:
        if p.complete:
            assert len(p.available) == PACK_SIZE + 1 - p.pick, (
                f"P{p.pack}P{p.pick} had {len(p.available)} cards"
            )


@pytest.mark.parametrize("path", snapshots(), ids=os.path.basename)
def test_in_progress_block_has_no_pick(path):
    """The trailing block is the pack on screen, not a committed pick."""
    live = parse(read(path)).current_pack
    if live is not None:
        assert live.picked is None
        assert not live.complete


def test_committed_history_is_append_only():
    """A pick must never change once it has been committed."""
    prev: list[tuple[int, int, str]] = []
    prev_name = None
    for path in snapshots():
        done = committed(parse(read(path)))
        assert done[: len(prev)] == prev, (
            f"{os.path.basename(path)} rewrote history from {prev_name}"
        )
        assert len(done) >= len(prev), "committed pick count regressed"
        prev, prev_name = done, os.path.basename(path)


def test_final_draft_is_complete():
    d = parse(read(FINAL))
    assert len(d.picks) == 45
    assert all(p.complete for p in d.picks)
    assert sorted({p.pack for p in d.picks}) == [1, 2, 3]
    assert len(d.players) == POD_SIZE
    assert d.hero == "Wumpwumpwump"
    assert d.set_name == "Holiday 2013 Cube"
    assert len(set(d.pool)) == 45, "cube is singleton — no duplicate picks"


def test_wheel_pairs_are_consistent():
    """
    A pack seen at pick p returns at pick p + pod_size, and what comes back
    must be a subset of what you passed. Verified 21/21 on this draft.
    """
    d = parse(read(FINAL))
    by = {(p.pack, p.pick): p for p in d.picks}
    pairs = 0
    for (pack, pick), p in by.items():
        nxt = by.get((pack, pick + POD_SIZE))
        if nxt is None:
            continue
        passed = set(p.available) - {p.picked}
        returned = set(nxt.available)
        assert returned <= passed, f"P{pack}P{pick} wheel mismatch"
        assert len(passed - returned) == POD_SIZE - 1, (
            f"P{pack}P{pick}: expected {POD_SIZE - 1} cards taken by the pod"
        )
        pairs += 1
    assert pairs == 21
