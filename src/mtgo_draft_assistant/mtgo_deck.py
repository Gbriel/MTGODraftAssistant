"""
MTGO deck files (``.dek``), exported by hand from the client's deck editor.

MTGO never writes the registered deck anywhere (DESIGN.md §2.3), so the only
way to know the 40 is for the user to export it: in the deck editor, Export,
save as ``.dek``. The file is XML:

    <?xml version="1.0" encoding="utf-8"?>
    <Deck xmlns:xsi=... xmlns:xsd=...>
      <NetDeckID>0</NetDeckID>
      <PreconstructedDeckID>0</PreconstructedDeckID>
      <Cards CatID="12345" Quantity="1" Sideboard="false" Name="Mox Diamond" />
      ...
    </Deck>

The parser only needs the ``Cards`` elements and tolerates namespaces, extra
elements and odd casing. A deck is matched to a draft by time: the newest
``.dek`` in the folder written after the draft started.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class DekDeck:
    path: Path
    main: list[tuple[str, int]] = field(default_factory=list)     # (name, quantity)
    side: list[tuple[str, int]] = field(default_factory=list)
    modified_at: str | None = None

    @property
    def main_count(self) -> int:
        return sum(q for _, q in self.main)

    @property
    def side_count(self) -> int:
        return sum(q for _, q in self.side)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _attr(el: ET.Element, name: str) -> str | None:
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return None


def parse_dek(text: str, path: Path | None = None) -> DekDeck:
    root = ET.fromstring(text)
    deck = DekDeck(path=path or Path("deck.dek"))
    for el in root.iter():
        if _local(el.tag) != "cards":
            continue
        name = (_attr(el, "name") or "").strip()
        if not name:
            continue
        try:
            qty = int(_attr(el, "quantity") or 1)
        except ValueError:
            qty = 1
        side = (_attr(el, "sideboard") or "false").strip().lower() in ("true", "1", "yes")
        (deck.side if side else deck.main).append((name, qty))
    return deck


def load_dek(path: str | Path) -> DekDeck:
    p = Path(path)
    deck = parse_dek(p.read_text(encoding="utf-8-sig"), p)
    deck.modified_at = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
    return deck


def find_dek(folder: str | Path | None, after: datetime | None = None) -> Path | None:
    """
    The newest ``.dek`` in ``folder`` modified after ``after`` (the draft's
    start), or None. Without ``after``, simply the newest.
    """
    if folder is None:
        return None
    best: tuple[float, Path] | None = None
    try:
        entries = list(Path(folder).glob("*.dek"))
    except OSError:
        return None
    for p in entries:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if after is not None and mtime < after.timestamp():
            continue
        if best is None or mtime > best[0]:
            best = (mtime, p)
    return best[1] if best else None


def draft_started_at(timestamp: str | None) -> datetime | None:
    """MTGO's draft-log ``Time:`` value, e.g. ``9/21/2026 4:07:52 PM``."""
    if not timestamp:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(timestamp.strip(), fmt)
        except ValueError:
            continue
    return None


class DekWatcher:
    """Re-reads the matching ``.dek`` only when it changes. Read-only."""

    def __init__(self, folder: str | Path | None) -> None:
        self.folder = Path(folder) if folder is not None else None
        self._stamp: tuple[Path, int, int] | None = None
        self._deck: DekDeck | None = None
        self.error: str | None = None

    def set_folder(self, folder: str | Path | None) -> None:
        self.folder = Path(folder) if folder is not None else None
        self._stamp = None
        self._deck = None

    def deck(self, after: datetime | None = None) -> DekDeck | None:
        path = find_dek(self.folder, after)
        if path is None:
            self._stamp, self._deck = None, None
            return None
        try:
            st = path.stat()
        except OSError:
            return self._deck
        stamp = (path, st.st_size, st.st_mtime_ns)
        if stamp != self._stamp:
            try:
                self._deck = load_dek(path)
                self.error = None
            except (OSError, ET.ParseError) as e:
                self.error = f"{path.name}: {e}"
                self._deck = None
            self._stamp = stamp
        return self._deck
