"""Pool tracker states across every snapshot, and cube list loading."""

from __future__ import annotations

import glob
import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.analysis import analyse  # noqa: E402
from mtgo_draft_assistant.cube_list import (  # noqa: E402
    CubeList,
    from_names,
    load,
    load_file,
    load_url,
    parse_html_table,
    parse_text,
)
from mtgo_draft_assistant.draft_log import Draft, Pick, parse  # noqa: E402
from mtgo_draft_assistant.pool import pool_state  # noqa: E402

SNAP_DIR = os.path.join(ROOT, "tests", "fixtures", "snapshots")
FINAL = os.path.join(ROOT, "tests", "fixtures", "final_draft_log.txt")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def snapshots():
    return sorted(glob.glob(os.path.join(SNAP_DIR, "snap_*.txt")))


# -- states ------------------------------------------------------------------


@pytest.mark.parametrize("path", snapshots() + [FINAL], ids=os.path.basename)
def test_every_seen_card_has_exactly_one_consistent_state(path):
    d = parse(read(path))
    a = analyse(d)
    r = pool_state(d, a)
    seen = {c for p in d.picks for c in p.available}
    by = {c.name: c for c in r.cards}
    assert set(by) == seen and len(r.cards) == len(seen)
    assert r.cube is None and "no cube list" in r.note
    assert sum(r.counts.values()) == len(seen) and r.counts["unseen"] == 0

    mine = {c.name for c in r.cards if c.state == "mine"}
    assert mine == set(d.pool)
    live = d.current_pack
    on_screen = {c.name for c in r.cards if c.state == "on_screen"}
    assert on_screen == (set(live.available) - mine if live else set())

    # in flight must agree with the analysis exactly
    flight = {c.name: c for c in r.cards if c.state == "in_flight"}
    expected = {}
    for f in a.in_flight:
        for name in f.passed:
            expected[name] = f.due_pick
    # a card passed twice (it wheeled and you passed it again) is keyed by its later sighting
    assert set(flight) == set(expected)
    for name, c in flight.items():
        assert c.due == expected[name] and c.due > c.pick

    # every card the pod took in a wheel is gone with reason "taken"
    for w in a.wheels:
        for name in w.taken:
            assert by[name].state == "gone" and by[name].reason == "taken", name
    # cards in earlier boosters are all resolved
    for c in r.cards:
        if a.current_pack and c.pack and c.pack < a.current_pack:
            assert c.state in ("mine", "gone")
            if c.state == "gone":
                assert c.reason in ("taken", "pack_over", "no_wheel")


def test_completed_draft_has_no_open_states():
    d = parse(read(FINAL))
    r = pool_state(d, analyse(d))
    assert r.counts["in_flight"] == 0 and r.counts["on_screen"] == 0
    assert r.counts["mine"] == 45
    assert r.counts["gone"] == len(r.cards) - 45


def test_no_wheel_reason_for_late_picks():
    """P1P9+ packs never come back in an 8-pod with 15-card packs."""
    d = parse(read(os.path.join(SNAP_DIR, "snap_002_4226b.txt")))
    a = analyse(d)
    r = pool_state(d, a)
    late = [c for c in r.cards if c.state == "gone" and c.reason == "no_wheel"]
    assert late and all(c.pick + a.pod_size > a.pack_sizes[c.pack] for c in late)


def test_unknown_pod_size_is_flagged_not_guessed():
    d = Draft()
    d.picks.append(Pick(1, 1, "A", ["A", "B", "C"], True))
    d.picks.append(Pick(1, 2, None, ["D", "E"], False))
    r = pool_state(d, analyse(d))
    assert "pod size unknown" in r.note
    assert {c.name: c.state for c in r.cards} == {"A": "mine", "B": "in_flight", "C": "in_flight",
                                                   "D": "on_screen", "E": "on_screen"}
    assert all(c.reason == "unknown_pod" for c in r.cards if c.state == "in_flight")


# -- with a cube list ----------------------------------------------------------


def test_unseen_is_cube_minus_seen_and_unmatched_is_reported():
    d = parse(read(os.path.join(SNAP_DIR, "snap_003_4676b.txt")))
    seen = list({c for p in d.picks for c in p.available})
    extra = ["Never Printed Alpha", "Never Printed Beta", "Never Printed Gamma"]
    assert not set(extra) & set(seen)
    cube = CubeList("test cube", "file", seen[:50] + extra)
    r = pool_state(d, analyse(d), cube)
    unseen = {c.name for c in r.cards if c.state == "unseen"}
    assert unseen == set(extra)
    assert r.counts["unseen"] == 3
    assert set(r.cube["unmatched"]) == set(seen[50:])
    assert r.cube["size"] == len(cube) and r.cube["name"] == "test cube"
    assert "probably not this draft's cube" in r.note

    exact = CubeList("exact", "file", seen + extra[:1])
    r2 = pool_state(d, analyse(d), exact)
    assert r2.cube["unmatched"] == [] and r2.note is None
    assert [c.name for c in r2.cards if c.state == "unseen"] == extra[:1]


def test_cube_matching_is_case_split_and_accent_insensitive():
    cube = CubeList("c", "file", ["Fire // Ice", "Palantír of Orthanc", "sol ring"])
    assert "Fire /// Ice" in cube and "Sol Ring" in cube and "Palantír of Orthanc" in cube
    assert "Palantir of Orthanc" in cube
    assert "Mox Pearl" not in cube
    assert len(CubeList("d", "file", ["A", "a", " A "])) == 1


def test_double_encoded_names_are_repaired():
    from mtgo_draft_assistant.cube_list import repair_mojibake
    assert repair_mojibake("PalantÃ\xadr of Orthanc") == "Palantír of Orthanc"
    assert repair_mojibake("LÃ³rien Revealed") == "Lórien Revealed"
    assert repair_mojibake("Sol Ring") == "Sol Ring"
    assert repair_mojibake("Ã") == "Ã"                        # not decodable: untouched
    cube = CubeList("c", "url", ["PalantÃ\xadr of Orthanc", "Palantír of Orthanc"])
    assert cube.cards == ["Palantír of Orthanc"] and "Palantír of Orthanc" in cube


# -- loading -----------------------------------------------------------------


def test_parse_text_strips_counts_and_comments():
    assert parse_text("# my cube\n1 Sol Ring\n2x Mox Pearl\n\nBlack Lotus  # power\n") == \
        ["Sol Ring", "Mox Pearl", "Black Lotus"]


HTML = """<html><body><p>intro</p>
<table><tbody>
<tr><td><strong>Color</strong></td><td><strong>Card</strong></td></tr>
<tr><td>White</td><td>Balance</td></tr>
<tr><td style="x">Blue</td><td>Ancestral&nbsp;Recall</td></tr>
<tr><td>Multicolor</td><td>Life // Death</td></tr>
<tr><td>Colorless</td><td>Sol Ring</td></tr>
<tr><td>Land</td><td>Badlands</td></tr>
</tbody></table>
<table><tr><td>White</td><td>Balance</td><td>x</td><td>y</td></tr></table>
<table><tr><td>Not a colour</td><td>Nope</td></tr><tr><td>Red</td><td>Lightning Bolt</td></tr></table>
</body></html>"""


def test_parse_html_table_takes_colour_rows_only():
    names = parse_html_table(HTML)
    assert names == ["Balance", "Ancestral Recall", "Life // Death", "Sol Ring", "Badlands",
                     "Balance", "Lightning Bolt"]
    assert len(CubeList("x", "url", names)) == 6          # the repeat collapses


def test_load_file_and_dispatch(tmp_path):
    f = tmp_path / "mycube.txt"
    f.write_text("Sol Ring\nBlack Lotus\n", encoding="utf-8")
    cl = load(str(f), tmp_path)
    assert cl.name == "mycube" and cl.source == "file" and cl.cards == ["Sol Ring", "Black Lotus"]
    h = tmp_path / "page.html"
    h.write_text(HTML, encoding="utf-8")
    assert len(load_file(h)) == 6


def test_load_url_caches_and_survives_a_failed_refresh(tmp_path):
    calls = []
    now = datetime(2026, 9, 24, 12, 0, 0)

    def fetch_ok(url):
        calls.append(url)
        return HTML

    cl = load_url("https://example.test/vintage-cube-cardlist", tmp_path, fetcher=fetch_ok, now=lambda: now)
    assert cl.source == "url" and cl.name == "vintage cube cardlist" and len(cl) == 6
    assert len(calls) == 1
    # the cache's age is its mtime; pin it so the test does not depend on the real clock
    import os
    cached = next((tmp_path / "cubes").glob("*.txt"))
    os.utime(cached, (now.timestamp(), now.timestamp()))
    # fresh cache: no fetch
    cl2 = load_url("https://example.test/vintage-cube-cardlist", tmp_path, fetcher=fetch_ok, now=lambda: now)
    assert len(calls) == 1 and cl2.cards == cl.cards
    # stale cache, network down: cached copy with a note
    later = now + timedelta(days=8)

    def fetch_fail(url):
        calls.append(url)
        raise OSError("offline")

    cl3 = load_url("https://example.test/vintage-cube-cardlist", tmp_path, fetcher=fetch_fail, now=lambda: later)
    assert len(calls) == 2 and cl3.cards == cl.cards and "refresh failed" in cl3.fetched_at
    # no cache at all and network down: error surfaces
    with pytest.raises(OSError):
        load_url("https://example.test/other", tmp_path, fetcher=fetch_fail, now=lambda: later)


def test_from_names():
    cl = from_names(["A", "B"], "Cube - Powered")
    assert cl.source == "17lands" and len(cl) == 2 and "b" in cl
