"""Watcher behaviour, driven by replaying the snapshot fixtures into a temp dir."""

from __future__ import annotations

import glob
import os
import shutil
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.watcher import DraftWatcher, newest_log  # noqa: E402

SNAP_DIR = os.path.join(ROOT, "tests", "fixtures", "snapshots")
FINAL = os.path.join(ROOT, "tests", "fixtures", "final_draft_log.txt")
LOG_NAME = "Wumpwumpwump-2026.9.21-11053-35966161-C03C03C03.txt"


def snapshots() -> list[str]:
    return sorted(glob.glob(os.path.join(SNAP_DIR, "snap_*.txt")))


def _write(dst, src, mtime: float) -> None:
    shutil.copyfile(src, dst)
    os.utime(dst, (mtime, mtime))


def test_empty_dir_yields_nothing(tmp_path):
    w = DraftWatcher(tmp_path)
    assert newest_log(tmp_path) is None
    assert w.poll() is None
    assert w.current is None


def test_missing_dir_yields_nothing(tmp_path):
    w = DraftWatcher(tmp_path / "nope")
    assert w.poll() is None


def test_replay_emits_only_on_change(tmp_path):
    log = tmp_path / LOG_NAME
    w = DraftWatcher(tmp_path)
    t = 1_700_000_000.0
    prev_committed = 0
    snaps = snapshots()

    _write(log, snaps[0], t)
    u = w.poll()
    assert u is not None and u.new_draft
    assert u.path == log
    assert w.poll() is None, "no change -> no update"
    prev_committed = sum(p.complete for p in u.draft.picks)

    for i, snap in enumerate(snaps[1:], start=1):
        _write(log, snap, t + i)
        u = w.poll()
        assert u is not None, os.path.basename(snap)
        assert not u.new_draft
        committed = sum(p.complete for p in u.draft.picks)
        assert committed >= prev_committed
        prev_committed = committed
        assert w.poll() is None

    assert prev_committed == 45          # last snapshot is the finished draft


def test_touch_without_content_change_is_ignored(tmp_path):
    log = tmp_path / LOG_NAME
    w = DraftWatcher(tmp_path)
    _write(log, snapshots()[0], 1_700_000_000.0)
    assert w.poll() is not None
    os.utime(log, (1_700_000_100.0, 1_700_000_100.0))
    assert w.poll() is None


def test_new_filename_means_new_draft(tmp_path):
    w = DraftWatcher(tmp_path)
    _write(tmp_path / LOG_NAME, FINAL, 1_700_000_000.0)
    u = w.poll()
    assert u is not None and u.new_draft
    assert len(u.draft.picks) == 45

    other = tmp_path / "Wumpwumpwump-2026.9.22-99999-1-C03C03C03.txt"
    _write(other, snapshots()[0], 1_700_000_500.0)     # newer mtime
    u = w.poll()
    assert u is not None and u.new_draft
    assert u.path == other
    assert len(u.draft.picks) < 45


def test_older_file_appearing_does_not_switch(tmp_path):
    w = DraftWatcher(tmp_path)
    _write(tmp_path / LOG_NAME, FINAL, 1_700_000_500.0)
    assert w.poll() is not None
    _write(tmp_path / "old-draft.txt", snapshots()[0], 1_600_000_000.0)
    assert w.poll() is None


def test_non_txt_files_ignored(tmp_path):
    w = DraftWatcher(tmp_path)
    (tmp_path / "notes.md").write_text("Pack 1 pick 1:\n", encoding="utf-8")
    assert w.poll() is None


def test_no_log_dir_yields_nothing():
    w = DraftWatcher(None)
    assert w.poll() is None


def test_set_log_dir_forgets_old_file(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write(a / LOG_NAME, FINAL, 1_700_000_000.0)
    _write(b / LOG_NAME, snapshots()[0], 1_600_000_000.0)      # older, same name
    w = DraftWatcher(a)
    assert len(w.poll().draft.picks) == 45
    w.set_log_dir(b)
    u = w.poll()
    assert u is not None and u.new_draft and u.path == b / LOG_NAME
    assert len(u.draft.picks) < 45
    assert w.poll() is None


def test_run_loop_invokes_callback_and_stops(tmp_path):
    log = tmp_path / LOG_NAME
    _write(log, snapshots()[0], 1_700_000_000.0)
    w = DraftWatcher(tmp_path, interval=0.02)
    got = []
    stop = threading.Event()
    th = threading.Thread(target=w.run, args=(got.append, stop), daemon=True)
    th.start()
    deadline = time.time() + 2
    while not got and time.time() < deadline:
        time.sleep(0.01)
    _write(log, snapshots()[1], 1_700_000_001.0)
    deadline = time.time() + 2
    while len(got) < 2 and time.time() < deadline:
        time.sleep(0.01)
    stop.set()
    th.join(timeout=2)
    assert not th.is_alive()
    assert len(got) == 2
    assert got[0].new_draft and not got[1].new_draft
