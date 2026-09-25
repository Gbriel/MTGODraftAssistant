"""
Draft analysis: wheel diffs and in-flight packs (DESIGN.md §3.1, §3.2).

Nothing here is hardcoded to an 8-player pod or 15-card packs. Pod size comes
from the ``Players:`` block, or is inferred from the wheels when there is no
player list (Arena); pack size is derived from the card count of the first
block seen in each pack.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .draft_log import Draft, Pick


@dataclass
class WheelDiff:
    """A pack you saw at ``first_pick`` that came back at ``return_pick``."""

    pack: int
    first_pick: int
    return_pick: int
    your_pick: str
    passed: list[str]           # what you handed on, in original order
    returned: list[str]         # what came back
    taken: list[str]            # passed - returned, in original order
    warning: str | None = None  # set when the clean pairing failed


@dataclass
class InFlight:
    """A pack you passed that hasn't come back to you yet."""

    pack: int
    first_pick: int
    due_pick: int
    your_pick: str
    passed: list[str]
    picks_until_return: int


@dataclass
class Analysis:
    pod_size: int
    pod_size_source: str        # "players" | "inferred" | "assumed" | "unknown"
    pack_sizes: dict[int, int]
    current_pack: int | None
    current_pick: int | None
    wheels: list[WheelDiff] = field(default_factory=list)
    in_flight: list[InFlight] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------


def infer_pod_size(draft: Draft, lo: int = 2, hi: int = 12) -> int | None:
    """
    Work out the pod size from the packs alone: the N for which every pack
    seen at pick p comes back at p + N as a subset of what was passed, with
    exactly N - 1 cards missing. Needs at least one completed lap; None until
    then. Verified against every MTGO log in the corpus (always 8).
    """
    by = {(p.pack, p.pick): p for p in draft.picks}
    best: int | None = None
    for n in range(lo, hi + 1):
        pairs = 0
        for (pack, pick), origin in by.items():
            if not origin.complete or origin.picked is None:
                continue
            ret = by.get((pack, pick + n))
            if ret is None:
                continue
            passed = set(origin.passed)
            if not set(ret.available) <= passed or len(passed - set(ret.available)) != n - 1:
                pairs = -1
                break
            pairs += 1
        if pairs > 0:
            if best is not None:
                return None          # ambiguous: two pod sizes both fit
            best = n
    return best


def pod_size_with_source(draft: Draft) -> tuple[int, str]:
    """
    Number of drafters and where it came from. From the Players block when
    there is one (MTGO); otherwise inferred from the wheels, else the log's
    hint (Arena human drafts are 8). 0 / "unknown" if nothing is known.
    """
    if draft.players:
        return len(draft.players), "players"
    inferred = infer_pod_size(draft)
    if inferred is not None:
        return inferred, "inferred"
    if draft.pod_size_hint:
        return draft.pod_size_hint, "assumed"
    return 0, "unknown"


def pod_size(draft: Draft) -> int:
    return pod_size_with_source(draft)[0]


def pack_sizes(draft: Draft) -> dict[int, int]:
    """
    Cards per booster, per pack number. Derived from the first block seen in
    each pack: a block at pick ``k`` with ``n`` cards implies ``n + k - 1``.
    """
    sizes: dict[int, int] = {}
    for p in draft.picks:
        if p.pack not in sizes and p.available:
            sizes[p.pack] = len(p.available) + p.pick - 1
    return sizes


def current_position(draft: Draft) -> tuple[int | None, int | None]:
    """(pack, pick) of the decision on screen, or the next one due."""
    if not draft.picks:
        return None, None
    last = draft.picks[-1]
    if not last.complete:
        return last.pack, last.pick
    # Between picks: the next block hasn't been written yet.
    return last.pack, last.pick + 1


def _by_position(draft: Draft) -> dict[tuple[int, int], Pick]:
    return {(p.pack, p.pick): p for p in draft.picks}


def _fallback_origin(draft: Draft, returned: Pick, n: int) -> Pick | None:
    """
    If the expected origin doesn't contain the returned cards, look for any
    earlier completed pick in the same pack whose passed set does.
    """
    ret = set(returned.available)
    candidates = [
        p for p in draft.picks
        if p.pack == returned.pack and p.complete and p.pick < returned.pick
        and ret <= set(p.passed)
    ]
    if not candidates:
        return None
    # Prefer the one whose distance is closest to a full lap.
    candidates.sort(key=lambda p: abs((returned.pick - p.pick) - n))
    return candidates[0]


def wheel_diffs(draft: Draft, n: int | None = None) -> tuple[list[WheelDiff], list[str]]:
    """
    Pair each completed pick ``p`` with the block at ``p + N`` in the same pack.
    The returning block need not be complete: as soon as it is on screen we
    know what came back.
    """
    n = pod_size(draft) if n is None else n
    warnings: list[str] = []
    if n <= 0:
        return [], ["pod size unknown: no Players block parsed and no lap completed yet"]

    by = _by_position(draft)
    out: list[WheelDiff] = []
    for (pack, pick), origin in sorted(by.items()):
        if not origin.complete or origin.picked is None:
            continue
        returned = by.get((pack, pick + n))
        if returned is None:
            continue

        passed = origin.passed
        ret_set = set(returned.available)
        warning = None
        if not ret_set <= set(passed):
            alt = _fallback_origin(draft, returned, n)
            if alt is not None and alt is not origin:
                warning = (
                    f"P{pack}P{returned.pick}: returned cards did not match "
                    f"P{pack}P{pick}; matched P{pack}P{alt.pick} instead"
                )
                origin, passed = alt, alt.passed
            else:
                warning = (
                    f"P{pack}P{returned.pick}: returned cards are not a subset of "
                    f"what you passed at P{pack}P{pick}"
                )
        taken = [c for c in passed if c not in ret_set]
        if warning is None and len(taken) != n - 1:
            warning = (
                f"P{pack}P{pick}: expected {n - 1} cards taken by the pod, "
                f"found {len(taken)}"
            )
        if warning:
            warnings.append(warning)
        out.append(WheelDiff(
            pack=pack,
            first_pick=origin.pick,
            return_pick=returned.pick,
            your_pick=origin.picked or "",
            passed=list(passed),
            returned=list(returned.available),
            taken=taken,
            warning=warning,
        ))
    return out, warnings


def in_flight(draft: Draft, n: int | None = None) -> list[InFlight]:
    """
    Packs you passed in the current pack number that are due back but haven't
    arrived. A pack seen at ``q`` returns at ``q + N`` if that is still within
    the booster; otherwise it never comes back and isn't listed.
    """
    n = pod_size(draft) if n is None else n
    if n <= 0:
        return []
    cur_pack, cur_pick = current_position(draft)
    if cur_pack is None or cur_pick is None:
        return []
    sizes = pack_sizes(draft)
    size = sizes.get(cur_pack)
    if size is None:
        return []

    out: list[InFlight] = []
    for p in draft.picks:
        if p.pack != cur_pack or not p.complete or p.picked is None:
            continue
        due = p.pick + n
        if due > size:
            continue                # never wheels
        if due <= cur_pick:
            continue                # already back (it's a wheel diff now)
        out.append(InFlight(
            pack=p.pack,
            first_pick=p.pick,
            due_pick=due,
            your_pick=p.picked,
            passed=p.passed,
            picks_until_return=due - cur_pick,
        ))
    return out


def analyse(draft: Draft) -> Analysis:
    n, source = pod_size_with_source(draft)
    wheels, warnings = wheel_diffs(draft, n)
    cur_pack, cur_pick = current_position(draft)
    return Analysis(
        pod_size=n,
        pod_size_source=source,
        pack_sizes=pack_sizes(draft),
        current_pack=cur_pack,
        current_pick=cur_pick,
        wheels=wheels,
        in_flight=in_flight(draft, n),
        warnings=warnings,
    )
