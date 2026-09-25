"""
Arena Player.log parsing and tailing, against lines extracted verbatim from a
real log (tests/fixtures/arena/). No network.
"""

from __future__ import annotations

import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.analysis import analyse, infer_pod_size, pod_size_with_source  # noqa: E402
from mtgo_draft_assistant.arena_log import (  # noqa: E402
    ArenaLogParser,
    ArenaWatcher,
    grp_ids,
    placeholder,
    to_draft,
)

ARENA = os.path.join(ROOT, "tests", "fixtures", "arena")
COMPLETE = os.path.join(ARENA, "arena_cube_complete.log")
DUPLICATE = os.path.join(ARENA, "arena_cube_duplicate_last_pick.log")
LIVE = os.path.join(ARENA, "arena_cube_in_progress.log")


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def parse_file(path: str) -> ArenaLogParser:
    p = ArenaLogParser()
    p.feed_text(read_bytes(path).decode("utf-8"))
    return p


# -- fixtures are what we think they are -------------------------------------


def test_fixtures_are_crlf_utf8():
    for path in (COMPLETE, DUPLICATE, LIVE):
        raw = read_bytes(path)
        raw.decode("utf-8")
        assert raw.count(b"\r\n") > 50
        assert raw.count(b"\n") == raw.count(b"\r\n"), "Arena logs are CRLF throughout"


# -- parser ----------------------------------------------------------------


def test_complete_draft():
    p = parse_file(COMPLETE)
    d = p.current
    assert d is not None and len(p.drafts) == 1
    assert d.draft_id == "6691070a-0afe-45b2-85c2-fbd4ae2ebf2b"
    assert d.event_name == "CubeDraft_Powered_20260908"
    assert d.started_at == "9/24/2026 9:52:22 AM"
    assert d.complete
    assert d.warnings == []
    picks = d.ordered_picks
    assert [(x.pack, x.pick) for x in picks] == [(pk, pi) for pk in (1, 2, 3) for pi in range(1, 16)]
    for x in picks:
        assert x.complete and x.picked in x.available
        assert len(x.available) == 16 - x.pick
    assert picks[0].picked == 90503
    assert picks[0].available[:3] == [100632, 95052, 60981]


def test_pick_numbers_agree_with_card_counts_everywhere():
    """Arena's SelfPick is trustworthy, unlike MTGO's header. Keep checking."""
    for path in (COMPLETE, DUPLICATE, LIVE):
        for x in parse_file(path).current.ordered_picks:
            assert len(x.available) == 16 - x.pick, path


def test_duplicate_pick_request_last_valid_wins():
    p = parse_file(DUPLICATE)
    d = p.current
    last = d.picks[(3, 15)]
    assert last.available == [102606]
    assert last.picked == 102606 and last.complete
    assert any("P3P15" in w and "not in the notified pack" in w for w in d.warnings)
    # the anomalous TableInfo response carried the pod's names
    assert d.players[0] == "Wumpwumpwump" and len(d.players) == 8
    assert d.complete


def test_repeated_notifications_collapse_to_one_pack():
    p = parse_file(LIVE)
    d = p.current
    assert not d.complete
    picks = d.ordered_picks
    assert [(x.pack, x.pick) for x in picks] == [(1, i) for i in range(1, len(picks) + 1)]
    done = [x for x in picks if x.complete]
    assert len(done) == len(picks) - 1, "the last notified pack is on screen, unpicked"
    assert picks[-1].picked is None and not picks[-1].complete
    assert picks[0].picked == 17047 and picks[0].available[0] == 7163


def test_version_only_bumps_on_change():
    p = ArenaLogParser()
    text = read_bytes(LIVE).decode("utf-8")
    p.feed_text(text)
    v = p.version
    p.feed_text("\r\n".join(l for l in text.split("\r\n") if "Draft.Notify" in l))   # replays
    assert p.version == v


def test_new_draft_replaces_current():
    p = ArenaLogParser()
    p.feed_text(read_bytes(COMPLETE).decode("utf-8"))
    first = p.current
    p.feed_text(read_bytes(LIVE).decode("utf-8"))
    assert p.current is not first and len(p.drafts) == 2
    assert not p.current.complete


def test_garbage_lines_are_ignored():
    p = ArenaLogParser()
    p.feed_text("[UnityCrossThreadLogger]Draft.Notify {not json\r\n"
                "[UnityCrossThreadLogger]==> EventPlayerDraftMakePick {\"request\": 5}\r\n"
                "random text\r\n<== Something(abc)\r\nnot json either\r\n")
    assert p.current is None and p.version == 0


# -- to_draft + analysis -----------------------------------------------------


def test_to_draft_uses_names_when_known_and_placeholders_otherwise():
    p = parse_file(COMPLETE)
    names = {90503: "Bristly Bill, Spine Sower", 100632: "Some Card"}
    d = to_draft(p.current, names.get)
    assert d.source == "arena" and d.pod_size_hint == 8 and d.players == []
    assert d.set_name == "CubeDraft_Powered_20260908"
    assert d.picks[0].picked == "Bristly Bill, Spine Sower"
    assert d.picks[0].available[0] == "Some Card"
    assert d.picks[0].available[1] == placeholder(95052)
    assert len(d.picks) == 45 and all(x.complete for x in d.picks)
    assert len(grp_ids(p.current)) == len(set(grp_ids(p.current)))


def test_pod_size_is_inferred_from_the_packs():
    d = to_draft(parse_file(COMPLETE).current)
    assert infer_pod_size(d) == 8
    assert pod_size_with_source(d) == (8, "inferred")
    a = analyse(d)
    assert a.pod_size == 8 and a.pod_size_source == "inferred"
    assert len(a.wheels) == 21 and a.warnings == []
    for w in a.wheels:
        assert len(w.taken) == 7 and set(w.returned) <= set(w.passed)


def test_pod_size_assumed_before_the_first_lap():
    d = to_draft(parse_file(LIVE).current)
    n, src = pod_size_with_source(d)
    assert (n, src) == (8, "assumed")
    a = analyse(d)
    assert a.pod_size == 8 and a.current_pack == 1
    assert a.current_pick == len(d.picks)


# -- watcher -----------------------------------------------------------------


def test_watcher_tails_incrementally_and_resets_on_rewrite(tmp_path):
    log = tmp_path / "Player.log"
    raw = read_bytes(LIVE)
    lines = raw.split(b"\r\n")
    # write the first half, then the rest in two appends, one cut mid-line
    half = len(lines) // 2
    log.write_bytes(b"\r\n".join(lines[:half]) + b"\r\n")
    w = ArenaWatcher(log, namer=lambda g: None)
    u1 = w.poll()
    assert u1 is not None and u1.new_draft and u1.draft.source == "arena"
    n1 = len(u1.draft.picks)
    assert w.poll() is None                                  # nothing new

    rest = b"\r\n".join(lines[half:])
    cut = len(rest) // 2
    with open(log, "ab") as fh:
        fh.write(rest[:cut])
    u2 = w.poll()                                            # partial line held back
    with open(log, "ab") as fh:
        fh.write(rest[cut:])
    u3 = w.poll()
    final = u3 or u2
    assert final is not None and not final.new_draft
    assert len(final.draft.picks) > n1
    assert len(final.draft.picks) == len(parse_file(LIVE).current.picks)

    # Arena relaunch: the file is rewritten from scratch with a different draft
    log.write_bytes(read_bytes(COMPLETE))
    u4 = w.poll()
    assert u4 is not None and u4.new_draft
    assert u4.draft.event_id == "6691070a-0afe-45b2-85c2-fbd4ae2ebf2b"
    assert len(u4.draft.picks) == 45


def test_watcher_rebuild_applies_new_names(tmp_path):
    log = tmp_path / "Player.log"
    log.write_bytes(read_bytes(COMPLETE))
    known: dict[int, str] = {}
    w = ArenaWatcher(log, namer=known.get)
    u = w.poll()
    assert u.draft.picks[0].picked == placeholder(90503)
    known[90503] = "Bristly Bill, Spine Sower"
    r = w.rebuild()
    assert r is not None and not r.new_draft
    assert r.draft.picks[0].picked == "Bristly Bill, Spine Sower"


def test_watcher_missing_file_is_quiet(tmp_path):
    w = ArenaWatcher(tmp_path / "nope" / "Player.log")
    assert w.poll() is None
    stop = threading.Event()
    stop.set()
    w.run(lambda u: None, stop)          # returns immediately
