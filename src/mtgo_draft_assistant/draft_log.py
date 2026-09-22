"""
Parser for Magic Online draft logs (client 3.4.158.x, verified 2026-09-21).

Format notes learned empirically from a live capture — these are the traps:

  * The pick number in the "Pack N pick M:" header is WRONG. MTGO emits
    "pick 1" for the first pick of each pack and "pick 2" for every pick
    after that. The PACK number is correct. Real pick position must be
    derived by counting blocks within a pack.

  * The "------ Pack 1: <name> ------" separator is emitted before every
    pick in pack 1 and NOT AT ALL in later packs. Do not use it to detect
    pack boundaries. It is still the only place the set/cube name appears.

  * The picked card is marked with a leading "--> ". A completed pick is
    additionally followed by a "Picked: <card>" confirmation line.

  * The file is appended DURING the draft. The final block may have no
    "-->" and no "Picked:" line: that is the pack currently on screen,
    written before the human has chosen. This is what makes a live
    overlay possible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PICK_HEADER = re.compile(r"^Pack (\d+) pick (\d+):\s*$")
SEPARATOR = re.compile(r"^-{4,}\s*Pack \d+:\s*(?P<name>.*?)\s*-{4,}\s*$")
PICKED_CONFIRM = re.compile(r"^Picked: (?P<card>.+?)\s*$")
CHOSEN = "--> "


@dataclass
class Pick:
    pack: int
    pick: int                      # derived, not taken from the header
    picked: str | None             # None while the pack is still on screen
    available: list[str]           # every card in the pack, including the pick
    complete: bool = False         # True once "Picked:" confirms it

    @property
    def passed(self) -> list[str]:
        """Cards left in the pack after this pick — i.e. what got passed on."""
        return [c for c in self.available if c != self.picked]


@dataclass
class Draft:
    event_id: str | None = None
    timestamp: str | None = None
    hero: str | None = None
    players: list[str] = field(default_factory=list)
    set_name: str | None = None
    picks: list[Pick] = field(default_factory=list)

    @property
    def pool(self) -> list[str]:
        return [p.picked for p in self.picks if p.picked]

    @property
    def current_pack(self) -> Pick | None:
        """The pack awaiting a decision right now, if the draft is in progress."""
        if self.picks and not self.picks[-1].complete:
            return self.picks[-1]
        return None


def parse(text: str) -> Draft:
    draft = Draft()
    lines = text.splitlines()
    i = 0
    n = len(lines)

    # ---- header ------------------------------------------------------
    while i < n:
        line = lines[i]
        if line.startswith("Event #:"):
            draft.event_id = line.split("Event #:", 1)[1].strip()
        elif line.startswith("Time:"):
            draft.timestamp = line.split("Time:", 1)[1].strip()
        elif line.strip() == "Players:":
            i += 1
            while i < n and lines[i].strip():
                name = lines[i].strip()
                if name.startswith(CHOSEN.strip()):
                    name = name[len(CHOSEN.strip()):].strip()
                    draft.hero = name
                draft.players.append(name)
                i += 1
            break
        i += 1

    # ---- pick blocks -------------------------------------------------
    picks_seen_in_pack: dict[int, int] = {}
    current: Pick | None = None

    while i < n:
        line = lines[i]

        sep = SEPARATOR.match(line)
        if sep and not draft.set_name:
            draft.set_name = sep.group("name")

        header = PICK_HEADER.match(line)
        if header:
            pack = int(header.group(1))          # trustworthy
            # header.group(2) is deliberately ignored — see module docstring
            picks_seen_in_pack[pack] = picks_seen_in_pack.get(pack, 0) + 1
            current = Pick(
                pack=pack,
                pick=picks_seen_in_pack[pack],
                picked=None,
                available=[],
            )
            draft.picks.append(current)
            i += 1
            # consume the card list until a blank line or the file ends
            while i < n and lines[i].strip():
                card = lines[i]
                if card.startswith(CHOSEN):
                    name = card[len(CHOSEN):].strip()
                    current.picked = name
                    current.available.append(name)
                else:
                    current.available.append(card.strip())
                i += 1
            continue

        confirm = PICKED_CONFIRM.match(line)
        if confirm and current is not None:
            # Trust the confirmation line over the "-->" marker if they disagree.
            current.picked = confirm.group("card")
            current.complete = True

        i += 1

    return draft


def summarise(draft: Draft) -> str:
    out = [
        f"Event {draft.event_id} — {draft.set_name or 'unknown set'}",
        f"Drafter: {draft.hero}  ({len(draft.players)} players)",
        f"Started: {draft.timestamp}",
        "",
    ]
    for p in draft.picks:
        if p.complete:
            out.append(f"  P{p.pack}P{p.pick:<2} {p.picked}   ({len(p.available)} cards seen)")
        else:
            out.append(
                f"  P{p.pack}P{p.pick:<2} >>> ON SCREEN NOW, {len(p.available)} cards: "
                + ", ".join(p.available)
            )
    done = sum(1 for p in draft.picks if p.complete)
    out += ["", f"{done} picks committed, {len(draft.picks)} blocks in file."]
    return "\n".join(out)


if __name__ == "__main__":
    import sys

    for path in sys.argv[1:]:
        with open(path, encoding="utf-8") as fh:
            print(summarise(parse(fh.read())))
            print()
