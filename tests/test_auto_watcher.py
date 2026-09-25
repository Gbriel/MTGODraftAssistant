"""AutoWatcher: follow whichever of MTGO / Arena drafted most recently."""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.arena_log import parse_timestamp  # noqa: E402
from mtgo_draft_assistant.auto_watcher import AutoWatcher  # noqa: E402

SNAP_DIR = os.path.join(ROOT, "tests", "fixtures", "snapshots")
ARENA_LIVE = os.path.join(ROOT, "tests", "fixtures", "arena", "arena_cube_in_progress.log")
LOG_NAME = "Wumpwumpwump-2026.9.21-11053-35966161-C03C03C03.txt"
TS = re.compile(rb"\[UnityCrossThreadLogger\](\d+/\d+/\d+ \d+:\d+:\d+ [AP]M)\r\n")


def _mtgo(dst, snap, when: datetime) -> None:
    shutil.copyfile(os.path.join(SNAP_DIR, snap), dst)
    t = when.timestamp()
    os.utime(dst, (t, t))


def _arena_halves():
    """The live fixture split in two, with the draft's own clock after each half."""
    from mtgo_draft_assistant.arena_log import ArenaLogParser

    raw = open(ARENA_LIVE, "rb").read()
    lines = raw.split(b"\r\n")
    half = len(lines) // 2
    first, second = b"\r\n".join(lines[:half]) + b"\r\n", b"\r\n".join(lines[half:])

    def clock(data: bytes):
        p = ArenaLogParser()
        p.feed_text(data.decode("utf-8"))
        return p.current.last_event_time

    t_first, t_last = clock(first), clock(raw)
    assert t_first is not None and t_last is not None and t_first < t_last
    return first, second, t_first, t_last


def test_parse_timestamp():
    assert parse_timestamp("9/24/2026 7:55:43 PM") == datetime(2026, 9, 24, 19, 55, 43)
    assert parse_timestamp("nonsense") is None and parse_timestamp(None) is None


def test_follows_the_most_recent_source_and_switches_both_ways(tmp_path):
    first, second, t_first, t_last = _arena_halves()
    mtgo_dir = tmp_path / "mtgo"
    mtgo_dir.mkdir()
    arena = tmp_path / "Player.log"
    arena.write_bytes(first)
    # MTGO drafted a day earlier than Arena's first half
    _mtgo(mtgo_dir / LOG_NAME, "snap_001_3254b.txt", t_first - timedelta(days=1))

    w = AutoWatcher(mtgo_dir, arena, interval=0.01)
    u = w.poll()
    assert u is not None and u.new_draft and u.draft.source == "arena"
    assert w.source == "arena"
    assert w.poll() is None

    # MTGO makes a pick after Arena's last event so far: switch, reported as a new draft
    assert t_last > t_first + timedelta(seconds=2), "fixture too short for this test"
    _mtgo(mtgo_dir / LOG_NAME, "snap_002_4226b.txt", t_first + timedelta(seconds=1))
    u = w.poll()
    assert u is not None and u.new_draft and u.draft.source == "mtgo"
    assert w.source == "mtgo" and len(u.draft.picks) > 8

    # another MTGO pick while MTGO is active: an ordinary update
    _mtgo(mtgo_dir / LOG_NAME, "snap_003_4676b.txt", t_first + timedelta(seconds=2))
    u = w.poll()
    assert u is not None and not u.new_draft and u.draft.source == "mtgo"

    # Arena resumes with events later than MTGO's last write: switch back
    with open(arena, "ab") as fh:
        fh.write(second)
    u = w.poll()
    assert u is not None and u.new_draft and u.draft.source == "arena"
    assert w.source == "arena"
    assert w.rebuild() is not None                    # arena rebuild is available
    w.set_log_dir(mtgo_dir)                           # re-pointing MTGO doesn't disturb Arena
    assert w.source == "arena"


def test_only_one_source_present(tmp_path):
    mtgo_dir = tmp_path / "mtgo"
    mtgo_dir.mkdir()
    _mtgo(mtgo_dir / LOG_NAME, "snap_001_3254b.txt", datetime(2026, 9, 1, 12, 0, 0))
    w = AutoWatcher(mtgo_dir, tmp_path / "missing" / "Player.log")
    u = w.poll()
    assert u is not None and u.draft.source == "mtgo" and w.source == "mtgo"
    assert w.rebuild() is None

    w2 = AutoWatcher(None, tmp_path / "missing" / "Player.log")
    assert w2.poll() is None and w2.source is None
