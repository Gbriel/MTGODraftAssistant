"""
Names for Arena card ids from Arena's own card database, read-only.

Arena ships a SQLite file, ``Raw_CardDatabase_<hash>.mtga``, under its install
directory (the hash changes with every client update, so it is found by glob
and the newest one wins). ``Cards.GrpId`` is the id the draft log uses;
``Cards.TitleId`` joins to ``Localizations_enUS.LocId`` with ``Formatted = 1``
for the English name. Verified 2026-09-28: 27,071 cards, and it names ids
Scryfall's arena lookup does not know, such as Arena-only printings with
expansion code ``ANA`` (id 101033 is Dismember).

This is the first place to ask for a name, before 17Lands and Scryfall,
because it is complete, local and instant. Nothing here writes anything.
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3
import threading
from pathlib import Path

TAGS = re.compile(r"<[^>]+>")           # Arena wraps parts of names in <nobr>…</nobr>


def clean(name: str | None) -> str | None:
    if not name:
        return None
    return " ".join(TAGS.sub("", name).split()) or None

DEFAULT_GLOBS = (
    r"C:\Program Files\Wizards of the Coast\MTGA\MTGA_Data\Downloads\Raw\Raw_CardDatabase_*.mtga",
    r"C:\Program Files (x86)\Wizards of the Coast\MTGA\MTGA_Data\Downloads\Raw\Raw_CardDatabase_*.mtga",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\MTGA\MTGA_Data\Downloads\Raw\Raw_CardDatabase_*.mtga"),
)


def find_database(patterns: tuple[str, ...] = DEFAULT_GLOBS) -> Path | None:
    """The newest card database on this machine, or None if Arena isn't installed."""
    best: tuple[float, Path] | None = None
    for pat in patterns:
        for hit in glob.glob(pat):
            try:
                mtime = os.path.getmtime(hit)
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, Path(hit))
    return best[1] if best else None


class ArenaCardDb:
    """Read-only lookups against Arena's card database. Safe to share across threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        uri = "file:" + self.path.resolve().as_posix() + "?mode=ro"
        self._db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._lock = threading.Lock()
        self._names: dict[int, str | None] = {}

    def name(self, grp_id: int) -> str | None:
        if grp_id in self._names:
            return self._names[grp_id]
        with self._lock:
            row = self._db.execute(
                "SELECT l.Loc FROM Cards c JOIN Localizations_enUS l ON l.LocId = c.TitleId "
                "WHERE c.GrpId = ? AND l.Formatted = 1",
                (int(grp_id),),
            ).fetchone()
        name = clean(row[0]) if row else None
        self._names[grp_id] = name
        return name

    def card(self, grp_id: int) -> dict | None:
        """A little more than the name: set code, collector number, token flag."""
        with self._lock:
            row = self._db.execute(
                "SELECT c.GrpId, l.Loc, c.ExpansionCode, c.CollectorNumber, c.IsToken, c.Rarity "
                "FROM Cards c JOIN Localizations_enUS l ON l.LocId = c.TitleId "
                "WHERE c.GrpId = ? AND l.Formatted = 1",
                (int(grp_id),),
            ).fetchone()
        if row is None:
            return None
        return {"grp_id": row[0], "name": clean(row[1]), "set": row[2], "number": row[3],
                "token": bool(row[4]), "rarity": row[5]}

    def version(self) -> str | None:
        try:
            with self._lock:
                rows = self._db.execute("SELECT * FROM Versions").fetchall()
        except sqlite3.Error:
            return None
        return ", ".join(f"{k}={v}" for k, v in rows) if rows else None

    def close(self) -> None:
        with self._lock:
            self._db.close()


def open_default() -> ArenaCardDb | None:
    path = find_database()
    if path is None:
        return None
    try:
        return ArenaCardDb(path)
    except sqlite3.Error:
        return None
