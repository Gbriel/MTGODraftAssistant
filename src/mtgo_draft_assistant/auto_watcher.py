"""
Watch MTGO and Arena at the same time and follow whichever drafted last.

Each source has a clock of its own that says when its draft last moved:
MTGO's is the newest log file's mtime (a pick appends to the file), Arena's
is the log timestamp preceding the latest draft message. Both are local
wall-clock time, so they compare directly. The source with the newer time
is the active one; a change of source is reported as a new draft so the UI
resets.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .arena_log import ArenaWatcher
from .watcher import DraftWatcher, Update


class AutoWatcher:
    """Same contract as :class:`DraftWatcher` / :class:`ArenaWatcher`."""

    def __init__(self, log_dir: str | os.PathLike[str] | None,
                 arena_log: str | os.PathLike[str] | None = None,
                 interval: float = 0.5) -> None:
        self.mtgo = DraftWatcher(log_dir, interval=interval)
        self.arena = ArenaWatcher(arena_log, interval=interval)
        self.interval = interval
        self.source: str | None = None       # "mtgo" | "arena" | None
        self._lock = threading.Lock()
        self.current: Update | None = None

    # -- interface parity with DraftWatcher
    @property
    def log_dir(self) -> Path | None:
        return self.mtgo.log_dir

    @property
    def pattern(self) -> str:
        return self.mtgo.pattern

    @property
    def arena_log(self) -> Path:
        return self.arena.log_path

    def set_log_dir(self, log_dir: str | os.PathLike[str] | None) -> None:
        """The MTGO directory is the configurable one; Arena's file is fixed."""
        with self._lock:
            self.mtgo.set_log_dir(log_dir)
            if self.source == "mtgo":
                self.source = None
                self.current = None

    # -- clocks
    def _mtgo_time(self) -> datetime | None:
        cur = self.mtgo.current
        if cur is None:
            return None
        return datetime.fromtimestamp(cur.stamp.mtime_ns / 1e9)

    def _arena_time(self) -> datetime | None:
        return self.arena.last_activity()

    def _pick_source(self) -> str | None:
        tm, ta = self._mtgo_time(), self._arena_time()
        if tm is None and ta is None:
            return None
        if ta is None:
            return "mtgo"
        if tm is None:
            return "arena"
        return "arena" if ta > tm else "mtgo"

    # -- polling
    def poll(self) -> Update | None:
        with self._lock:
            um = self.mtgo.poll()
            ua = self.arena.poll()
            source = self._pick_source()
            if source != self.source:
                self.source = source
                cur = self.mtgo.current if source == "mtgo" else self.arena.current if source == "arena" else None
                if cur is None:
                    self.current = None
                    return None
                self.current = Update(path=cur.path, text=cur.text, draft=cur.draft,
                                      new_draft=True, stamp=cur.stamp)
                return self.current
            fresh = um if source == "mtgo" else ua if source == "arena" else None
            if fresh is not None:
                self.current = fresh
            return fresh

    def deck(self):
        return self.arena.deck() if self.source == "arena" else None

    def game(self):
        return self.arena.game() if self.source == "arena" else None

    def rebuild(self) -> Update | None:
        """Re-render an Arena draft after card names arrived; MTGO needs nothing."""
        with self._lock:
            if self.source != "arena":
                return None
            u = self.arena.rebuild()
            if u is not None:
                self.current = u
            return u

    def run(self, callback: Callable[[Update], None],
            stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            update = self.poll()
            if update is not None:
                callback(update)
            stop.wait(self.interval)
