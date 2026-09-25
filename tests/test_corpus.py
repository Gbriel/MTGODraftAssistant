"""
Every complete MTGO draft log we have, run through the same invariants.

DESIGN.md §6 warned that everything was verified on one draft. This widens
the net: ``tests/fixtures/drafts/`` holds further real logs copied verbatim
from the MTGO log directory. Drop a new one in and it is tested automatically.
"""

from __future__ import annotations

import collections
import glob
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.analysis import analyse, infer_pod_size, pack_sizes  # noqa: E402
from mtgo_draft_assistant.draft_log import parse  # noqa: E402

DRAFT_DIR = os.path.join(ROOT, "tests", "fixtures", "drafts")
FINAL = os.path.join(ROOT, "tests", "fixtures", "final_draft_log.txt")


def complete_logs() -> list[str]:
    return [FINAL] + sorted(glob.glob(os.path.join(DRAFT_DIR, "*.txt")))


def read(path: str) -> str:
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8")


def test_corpus_has_grown_beyond_one_draft():
    assert len(complete_logs()) >= 3


@pytest.mark.parametrize("path", complete_logs(), ids=os.path.basename)
def test_complete_draft_invariants(path):
    d = parse(read(path))
    n = len(d.players)
    sizes = pack_sizes(d)
    assert n >= 2
    assert d.hero in d.players
    assert d.set_name
    assert sorted({p.pack for p in d.picks}) == sorted(sizes)
    assert all(p.complete and p.picked for p in d.picks)
    assert len(d.picks) == sum(sizes.values())
    for p in d.picks:
        assert len(p.available) == sizes[p.pack] + 1 - p.pick, f"P{p.pack}P{p.pick}"
        assert p.picked in p.available
    assert len(set(d.pool)) == len(d.pool), "cube is singleton"


@pytest.mark.parametrize("path", complete_logs(), ids=os.path.basename)
def test_every_wheel_pairs_cleanly(path):
    d = parse(read(path))
    n = len(d.players)
    a = analyse(d)
    assert a.warnings == []
    expected = sum(max(0, size - n) for size in a.pack_sizes.values())
    assert len(a.wheels) == expected
    for w in a.wheels:
        assert w.return_pick == w.first_pick + n
        assert set(w.returned) <= set(w.passed)
        assert len(w.taken) == n - 1
        assert w.warning is None


@pytest.mark.parametrize("path", complete_logs(), ids=os.path.basename)
def test_pod_size_is_inferable_from_the_wheels_alone(path):
    """For logs with no player list (Arena) the pod size has to come from the packs."""
    d = parse(read(path))
    assert infer_pod_size(d) == len(d.players)


@pytest.mark.parametrize("path", complete_logs(), ids=os.path.basename)
def test_header_pick_number_is_still_garbage(path):
    """If MTGO ever fixes this, we want to know — but the parser must keep ignoring it."""
    text = read(path)
    headers = re.findall(r"^Pack (\d+) pick (\d+):", text, re.M)
    by_pack = collections.defaultdict(list)
    for pack, pick in headers:
        by_pack[int(pack)].append(int(pick))
    for pack, nums in by_pack.items():
        assert nums != list(range(1, len(nums) + 1)), (
            f"{os.path.basename(path)}: pack {pack} header pick numbers are sequential now; "
            "re-check DESIGN.md §2.2 before trusting them"
        )


@pytest.mark.parametrize("path", complete_logs(), ids=os.path.basename)
def test_separator_only_in_pack_one(path):
    text = read(path)
    seps = re.findall(r"^-{4,}\s*Pack (\d+):", text, re.M)
    assert seps and set(seps) == {"1"}
