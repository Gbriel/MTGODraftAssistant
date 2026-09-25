"""
Cube lists: the set of cards a draft could contain. Needed to enumerate what
you have *not* seen (DESIGN.md §3.3). Sources:

  * a local text file, one card per line (blank lines and ``#`` comments
    ignored; a leading count like ``1 Sol Ring`` is stripped);
  * a URL to an HTML page with a two-column Color | Card table, such as
    https://www.mtgo.com/vintage-cube-cardlist (fetched once, cached to
    disk, refreshed weekly);
  * the 17Lands dataset for the Arena cube (exact for Arena drafts).

A list is only as good as its match to the draft: the pool tracker reports
how many cards seen in the draft are missing from the list, and that number
is the honest signal that the list is the wrong one.
"""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from .ratings import normalise, strip_accents

USER_AGENT = "MTGODraftAssistant/0.1 (local read-only draft tracker)"
REFRESH_DAYS = 7
COLOUR_WORDS = {
    "white", "blue", "black", "red", "green", "colorless", "colourless", "multicolor",
    "multicolour", "land", "lands", "artifact", "gold", "hybrid",
    # the mtgo.com list labels two-colour sections by guild
    "azorius", "dimir", "rakdos", "gruul", "selesnya", "orzhov", "izzet", "golgari",
    "boros", "simic",
}
COUNT_PREFIX = re.compile(r"^\s*\d+\s*[xX]?\s+")


@dataclass
class CubeList:
    name: str                       # how the UI labels it
    source: str                     # "file" | "url" | "17lands"
    cards: list[str] = field(default_factory=list)
    fetched_at: str | None = None
    _keys: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        # dedupe, keep first spelling and order
        seen: dict[str, str] = {}
        for c in self.cards:
            c = repair_mojibake(c)
            k = normalise(c)
            if k and k not in seen:
                seen[k] = c.strip()
        self.cards = list(seen.values())
        self._keys = set(seen)
        self._ascii = {strip_accents(k) for k in seen}

    def __len__(self) -> int:
        return len(self.cards)

    def __contains__(self, name: str) -> bool:
        k = normalise(name)
        return k in self._keys or strip_accents(k) in self._ascii


def repair_mojibake(s: str) -> str:
    """
    The mtgo.com list is double-encoded at source ("PalantÃ­r"). If a name
    decodes cleanly as latin-1 -> UTF-8 and contains the tell-tale Ã/Â, use
    the repaired form; otherwise leave it alone.
    """
    if "Ã" not in s and "Â" not in s:
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


# -- sources -----------------------------------------------------------------


def parse_text(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        out.append(COUNT_PREFIX.sub("", line).strip())
    return out


class _TableRows(HTMLParser):
    """Collect the text of every <td>/<th> per <tr>, grouped by <table>."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    @property
    def rows(self) -> list[list[str]]:
        return [r for t in self.tables for r in t]

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                if not self.tables:
                    self.tables.append([])
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _is_header(row: list[str]) -> bool:
    return len(row) >= 2 and row[0].strip().lower() in ("color", "colour") \
        and row[1].strip().lower() == "card"


def parse_html_table(html_text: str) -> list[str]:
    """
    Card names from Color | Card tables. A table with that header row
    contributes every other row's second cell; a headerless table only rows
    whose first cell is a colour or guild word.
    """
    p = _TableRows()
    p.feed(html_text)
    names: list[str] = []
    for table in p.tables:
        headed = any(_is_header(r) for r in table)
        for row in table:
            if len(row) < 2 or _is_header(row):
                continue
            label, card = row[0].strip(), row[1].strip()
            if not card:
                continue
            if headed or label.lower() in COLOUR_WORDS:
                names.append(card)
    return names


def load_file(path: str | Path) -> CubeList:
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    cards = parse_html_table(text) if p.suffix.lower() in (".html", ".htm") else parse_text(text)
    return CubeList(name=p.stem, source="file", cards=cards,
                    fetched_at=datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"))


def fetch_url(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def load_url(url: str, cache_dir: str | Path, fetcher=fetch_url,
             refresh_days: float = REFRESH_DAYS, now=datetime.now) -> CubeList:
    """
    A URL's card list, cached as a text file so drafts don't depend on the
    page being up. Refreshed when the cache is older than ``refresh_days``;
    a failed refresh falls back to the cached copy.
    """
    cache_dir = Path(cache_dir) / "cubes"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".txt")
    stale = True
    if cached.is_file():
        age = now() - datetime.fromtimestamp(cached.stat().st_mtime)
        stale = age.total_seconds() >= refresh_days * 86400
    if stale:
        try:
            body = fetcher(url)
            cards = parse_html_table(body) if "<table" in body.lower() else parse_text(body)
            if not cards:
                raise ValueError("no card names found on the page")
            cached.write_text(f"# {url}\n# fetched {now().isoformat(timespec='seconds')}\n"
                              + "\n".join(cards) + "\n", encoding="utf-8")
        except (urllib.error.URLError, OSError, ValueError) as e:
            if not cached.is_file():
                raise
            cl = load_file(cached)
            cl.name = _url_name(url)
            cl.source = "url"
            cl.fetched_at = (cl.fetched_at or "") + f" (refresh failed: {e})"
            return cl
    cl = load_file(cached)
    cl.name = _url_name(url)
    cl.source = "url"
    return cl


def _url_name(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail.replace("-", " ").replace("_", " ") or url


def from_names(names: list[str], label: str) -> CubeList:
    return CubeList(name=label, source="17lands", cards=list(names),
                    fetched_at=datetime.now().isoformat(timespec="seconds"))


def load(spec: str, cache_dir: str | Path) -> CubeList:
    """``spec`` is a URL or a path."""
    if spec.lower().startswith(("http://", "https://")):
        return load_url(spec, cache_dir)
    return load_file(Path(spec).expanduser())
