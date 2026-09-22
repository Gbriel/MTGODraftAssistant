"""
Watch the MTGO draft log directory and re-parse the newest log when it changes.

Design (see DESIGN.md §4):

  * Poll on a timer comparing (size, mtime). No inotify/watchdog: the log
    directory may live in OneDrive or another synced filesystem that drops
    change events. The file is ~10KB; re-reading it whole is free.
  * The newest ``.txt`` by mtime is the live draft. A different filename
    becoming newest means a new draft started; callers should reset state.
  * Never write to the directory. Read-only, always.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .draft_log import Draft, parse


@dataclass(frozen=True)
class FileStamp:
    path: Path
    size: int
    mtime_ns: int


@dataclass
class Update:
    """Emitted whenever the parsed draft changes."""

    path: Path
    text: str
    draft: Draft
    new_draft: bool          # True when this is a different file than last time
    stamp: FileStamp


def read_log(path: Path) -> str:
    """
    Read a draft log as UTF-8.

    MTGO may be mid-append when we read. A partial multi-byte sequence at the
    tail would make strict decoding raise, so decode with replacement; the
    next poll picks up the completed write anyway.
    """
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", errors="replace")


def newest_log(log_dir: Path, pattern: str = "*.txt") -> Path | None:
    """Most recently modified log in the directory, or None if there is none."""
    best: tuple[int, Path] | None = None
    try:
        candidates = list(log_dir.glob(pattern))
    except OSError:
        return None
    for p in candidates:
        try:
            st = p.stat()
        except OSError:
            continue
        if not p.is_file():
            continue
        key = (st.st_mtime_ns, p)
        if best is None or key > best:
            best = key
    return best[1] if best else None


class DraftWatcher:
    """
    Stateful poller. Call :meth:`poll` on a timer; it returns an :class:`Update`
    only when the parsed content actually changed.

    ``run`` is a convenience loop for threads.
    """

    def __init__(self, log_dir: str | os.PathLike[str], interval: float = 0.5,
                 pattern: str = "*.txt") -> None:
        self.log_dir = Path(log_dir)
        self.interval = interval
        self.pattern = pattern
        self._stamp: FileStamp | None = None
        self._text: str | None = None
        self.current: Update | None = None

    # -- single step -------------------------------------------------------

    def poll(self) -> Update | None:
        path = newest_log(self.log_dir, self.pattern)
        if path is None:
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        stamp = FileStamp(path=path, size=st.st_size, mtime_ns=st.st_mtime_ns)
        if stamp == self._stamp:
            return None

        try:
            text = read_log(path)
        except OSError:
            # Locked or vanished mid-read; try again next tick.
            return None

        new_draft = self._stamp is None or self._stamp.path != path
        self._stamp = stamp
        if not new_draft and text == self._text:
            # mtime bumped but content identical (e.g. a touch or a sync).
            return None
        self._text = text

        update = Update(
            path=path, text=text, draft=parse(text), new_draft=new_draft, stamp=stamp
        )
        self.current = update
        return update

    # -- loop --------------------------------------------------------------

    def run(self, callback: Callable[[Update], None],
            stop: threading.Event | None = None) -> None:
        """Poll until ``stop`` is set, invoking ``callback`` on each change."""
        stop = stop or threading.Event()
        while not stop.is_set():
            update = self.poll()
            if update is not None:
                callback(update)
            stop.wait(self.interval)
