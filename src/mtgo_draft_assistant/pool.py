"""
Pool tracker (DESIGN.md §3.3): for every card, can it still reach you?

States, from your seat:

  mine       you picked it
  on_screen  in the pack in front of you right now
  in_flight  you passed it and its pack is due back before the booster
             runs out; it might still come round
  gone       you saw it and it will not come to you again — the pod took it
             (``reason="taken"``), or the pack it was in will not return
             (``reason="no_wheel"``), or that booster is over
             (``reason="pack_over"``)
  unseen     in the cube list but never in front of you (only with a list)

The rule for a passed card: find the last pick where you saw it, ``(pack,
pick)``. If a block exists at ``(pack, pick + N)`` the pack came back; the
card is there (then its last sighting is later, so this branch is not taken)
or it is not: taken. If no such block exists yet: the booster is over,
``pick + N`` exceeds the pack size, or it is still in flight.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .analysis import Analysis
from .cube_list import CubeList
from .draft_log import Draft
from .ratings import normalise as _key

STATES = ("on_screen", "in_flight", "unseen", "gone", "mine")


@dataclass
class PoolCard:
    name: str
    state: str
    pack: int | None = None         # where last seen (None for unseen)
    pick: int | None = None
    due: int | None = None          # in_flight: the pick it is due back at
    reason: str | None = None       # gone: taken | no_wheel | pack_over | unknown_pod


@dataclass
class PoolReport:
    cards: list[PoolCard] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    cube: dict | None = None        # {name, source, size, unmatched: [...]} when a list is loaded
    note: str | None = None         # honest caveat for the UI

    def to_json(self) -> dict:
        return {
            "cards": [c.__dict__ for c in self.cards],
            "counts": self.counts,
            "cube": self.cube,
            "note": self.note,
        }


def pool_state(draft: Draft, analysis: Analysis, cube: CubeList | None = None) -> PoolReport:
    n = analysis.pod_size
    by = {(p.pack, p.pick): p for p in draft.picks}
    live = draft.current_pack
    cur_pack = analysis.current_pack
    mine = set(draft.pool)

    # last sighting of every card, in first-seen order
    first_seen: dict[str, None] = {}
    last_seen: dict[str, tuple[int, int]] = {}
    for p in draft.picks:
        for c in p.available:
            first_seen.setdefault(c, None)
            last_seen[c] = (p.pack, p.pick)

    cards: list[PoolCard] = []
    for name in first_seen:
        pack, pick = last_seen[name]
        if name in mine:
            cards.append(PoolCard(name, "mine", pack, pick))
            continue
        if live is not None and (pack, pick) == (live.pack, live.pick):
            cards.append(PoolCard(name, "on_screen", pack, pick))
            continue
        size = analysis.pack_sizes.get(pack, 0)
        if n <= 0:
            cards.append(PoolCard(name, "gone" if cur_pack is not None and pack < cur_pack else "in_flight",
                                  pack, pick, None, "unknown_pod" if not (cur_pack is not None and pack < cur_pack) else "pack_over"))
            continue
        due = pick + n
        if (pack, due) in by:
            cards.append(PoolCard(name, "gone", pack, pick, due, "taken"))
        elif cur_pack is not None and pack < cur_pack:
            cards.append(PoolCard(name, "gone", pack, pick, None, "pack_over"))
        elif due > size:
            cards.append(PoolCard(name, "gone", pack, pick, None, "no_wheel"))
        else:
            cards.append(PoolCard(name, "in_flight", pack, pick, due))

    report = PoolReport(cards=cards)
    if cube is not None:
        seen_names = list(first_seen)
        unmatched = [c for c in seen_names if c not in cube]
        seen_keys = {_key(c) for c in seen_names}
        unseen = [c for c in cube.cards if _key(c) not in seen_keys]
        for c in unseen:
            report.cards.append(PoolCard(c, "unseen"))
        report.cube = {
            "name": cube.name, "source": cube.source, "size": len(cube),
            "fetched_at": cube.fetched_at, "unmatched": unmatched,
        }
        if seen_names and len(unmatched) > max(3, len(seen_names) // 10):
            report.note = (f"{len(unmatched)} of {len(seen_names)} cards seen are not in the "
                           f"'{cube.name}' list; it is probably not this draft's cube, so "
                           "'unseen' is unreliable")
    else:
        report.note = "no cube list loaded: cards you have never seen cannot be enumerated"
    if n <= 0:
        report.note = ((report.note + "; ") if report.note else "") + \
            "pod size unknown until a pack wheels, so in-flight is a guess"

    counts = {s: 0 for s in STATES}
    for c in report.cards:
        counts[c.state] = counts.get(c.state, 0) + 1
    report.counts = counts
    return report
