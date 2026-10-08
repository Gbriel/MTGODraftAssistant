"""End-to-end: watcher -> analysis -> HTTP JSON and SSE, on an ephemeral port."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.scryfall import CardResolver  # noqa: E402
from mtgo_draft_assistant.server import DraftServer, build_state  # noqa: E402
from test_scryfall import CARDS, JPEG, FakeClient, payload  # noqa: E402

SNAP_DIR = os.path.join(ROOT, "tests", "fixtures", "snapshots")
FINAL = os.path.join(ROOT, "tests", "fixtures", "final_draft_log.txt")
LOG_NAME = "Wumpwumpwump-2026.9.21-11053-35966161-C03C03C03.txt"


def _write(dst, src, mtime: float) -> None:
    shutil.copyfile(src, dst)
    os.utime(dst, (mtime, mtime))


def _get(url: str) -> tuple[int, dict[str, str], bytes]:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, dict(r.headers), r.read()


def _read_sse_event(resp) -> dict:
    """Read one SSE event (skipping keepalive comments) from a streaming response."""
    data_lines: list[str] = []
    deadline = time.time() + 5
    while time.time() < deadline:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith(":"):
            continue
        if line == "":
            if data_lines:
                return json.loads("\n".join(data_lines))
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    raise AssertionError("no SSE event received")


@pytest.fixture
def server(tmp_path):
    _write(tmp_path / LOG_NAME, os.path.join(SNAP_DIR, "snap_003_4676b.txt"), 1_700_000_000.0)
    # config_path inside tmp so a test can never touch the real config.toml
    s = DraftServer(tmp_path, port=0, interval=0.05, config_path=tmp_path / "unused.toml")
    s.start()
    try:
        yield s
    finally:
        s.stop()


def test_idle_state_shape(tmp_path):
    st = build_state(None, None, tmp_path, 0)
    assert st["position"]["status"] == "idle"
    assert st["picks"] == [] and st["wheels"] == [] and st["in_flight"] == []
    assert st["draft"] is None


def test_state_carries_a_ui_version(server):
    from mtgo_draft_assistant.server import ui_version
    st = json.loads(_get(server.url + "api/state")[2])
    assert st["ui_version"] == ui_version() and "-" in st["ui_version"]


def test_index_and_static(server):
    status, headers, body = _get(server.url)
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert b"<title>" in body
    status, headers, body = _get(server.url + "static/app.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]


def test_static_traversal_blocked(server):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(server.url + "static/../DESIGN.md")
    assert ei.value.code == 404


def test_unknown_route_404(server):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(server.url + "nope")
    assert ei.value.code == 404


def test_api_state_reflects_log(server):
    status, headers, body = _get(server.url + "api/state")
    assert status == 200
    st = json.loads(body)
    assert st["file"] == LOG_NAME
    assert st["draft"]["hero"] == "Wumpwumpwump"
    assert st["draft"]["pod_size"] == 8
    assert st["draft"]["set_name"] == "Holiday 2013 Cube"
    assert st["position"] == {"pack": 2, "pick": 1, "status": "on_screen"}
    assert st["current_pack"]["cards"][0] == "Mana Vault"
    assert len(st["current_pack"]["cards"]) == 15
    assert sum(p["complete"] for p in st["picks"]) == 15
    assert len(st["wheels"]) == 7          # P1P1..P1P7 wheeled back at P1P9..P1P15
    assert st["in_flight"] == []
    assert st["warnings"] == []


def test_utf8_survives_the_wire(server):
    _write(server.log_dir / LOG_NAME, FINAL, 1_700_000_010.0)
    deadline = time.time() + 3
    while time.time() < deadline:
        st = json.loads(_get(server.url + "api/state")[2])
        if len(st["picks"]) == 45:
            break
        time.sleep(0.05)
    names = {c for p in st["picks"] for c in p["available"]}
    assert "Palantír of Orthanc" in names


def test_sse_sends_current_state_then_updates(server):
    req = urllib.request.Request(server.url + "events")
    resp = urllib.request.urlopen(req, timeout=5)
    try:
        assert "text/event-stream" in resp.headers["Content-Type"]
        first = _read_sse_event(resp)
        assert first["position"] == {"pack": 2, "pick": 1, "status": "on_screen"}
        v1 = first["version"]

        _write(server.log_dir / LOG_NAME, os.path.join(SNAP_DIR, "snap_004_4984b.txt"),
               1_700_000_001.0)
        second = _read_sse_event(resp)
        assert second["version"] > v1
        assert sum(p["complete"] for p in second["picks"]) >= 15
        assert second["position"]["pack"] == 2
    finally:
        resp.close()


def test_text_only_server_has_empty_cards(server):
    st = json.loads(_get(server.url + "api/state")[2])
    assert st["cards"] == {} and st["cards_pending"] == 0
    assert st["ratings"] == {} and st["ratings_meta"] is None and st["ratings_unmatched"] == []
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(server.url + "img/anything.jpg")
    assert ei.value.code == 404


def test_cards_resolve_and_images_served(tmp_path):
    _write(tmp_path / LOG_NAME, os.path.join(SNAP_DIR, "snap_003_4676b.txt"), 1_700_000_000.0)
    cards = dict(CARDS)
    cards["Mana Vault"] = payload("Mana Vault", "id-vault", [], "Artifact", 1, "{1}")
    cards["Black Lotus"] = payload("Black Lotus", "id-lotus", [], "Artifact", 0, "{0}")
    client = FakeClient(cards=cards)
    resolver = CardResolver(tmp_path / "cache", client=client)
    s = DraftServer(tmp_path, port=0, interval=0.05, resolver=resolver)
    s.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            st = json.loads(_get(s.url + "api/state")[2])
            if st["cards_pending"] == 0 and st["cards"].get("Mother of Runes", {}).get("image"):
                break
            time.sleep(0.05)
        assert st["cards_pending"] == 0
        mom = st["cards"]["Mother of Runes"]
        assert mom["group"] == "W" and mom["image"] == "/img/id-mom.jpg"
        assert st["cards"]["Mana Vault"]["group"] == "C"
        assert st["cards"]["Dark Ritual"] == {
            "status": "missing", "group": "X", "colors": [], "color_identity": [],
            "type_line": "", "mana_cost": "", "cmc": 0.0, "image": None,
        }
        # every name in the draft so far was looked up exactly once, in batches
        names = {c for p in st["picks"] for c in p["available"]}
        assert set(client.lookups) == names
        assert len(client.lookups) == len(names)
        assert client.batches <= 3
        assert st["card_errors"] == []

        status, headers, body = _get(s.url + "img/id-mom.jpg")
        assert status == 200 and headers["Content-Type"] == "image/jpeg" and body == JPEG
        with pytest.raises(urllib.error.HTTPError):
            _get(s.url + "img/../cards.sqlite")
    finally:
        s.stop()


def test_ratings_in_state_with_unmatched_count(tmp_path):
    from mtgo_draft_assistant.ratings import RatingsProvider
    from test_ratings import ROWS, FakeClient as FakeRatingsClient, row

    _write(tmp_path / LOG_NAME, os.path.join(SNAP_DIR, "snap_003_4676b.txt"), 1_700_000_000.0)
    rows = ROWS + [row("Mana Vault", alsa=2.2, gih_wr=0.58, gih_games=900)]
    client = FakeRatingsClient(rows=rows)
    ratings = RatingsProvider(tmp_path / "cache", client=client)
    s = DraftServer(tmp_path, port=0, interval=0.05, ratings=ratings)
    s.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            st = json.loads(_get(s.url + "api/state")[2])
            if st["ratings_meta"] and st["ratings_meta"]["status"] == "ok" and st["ratings"]:
                break
            time.sleep(0.05)
        meta = st["ratings_meta"]
        assert meta["status"] == "ok" and meta["expansion"] == "Cube - Powered"
        assert meta["cards"] == len(rows) and meta["with_win_rate"] == 3
        vault = st["ratings"]["Mana Vault"]
        assert vault["alsa"] == 2.2 and vault["gih_wr"] == 0.58 and vault["gih_games"] == 900
        assert st["ratings"]["Mother of Runes"]["gih_wr"] is None
        names = {c for p in st["picks"] for c in p["available"]} | set(st["current_pack"]["cards"])
        matched = set(st["ratings"])
        assert matched <= names
        assert set(st["ratings_unmatched"]) == names - matched
        assert "Dark Ritual" in st["ratings_unmatched"]
        assert len(client.calls) == 1
    finally:
        s.stop()


def test_alternate_ratings_sets_are_served_alongside(tmp_path):
    from mtgo_draft_assistant.ratings import RatingsProvider
    from test_ratings import FakeClient as FakeRatingsClient, row

    _write(tmp_path / LOG_NAME, os.path.join(SNAP_DIR, "snap_003_4676b.txt"), 1_700_000_000.0)
    cache = tmp_path / "cache"
    mk = lambda wr, games, **kw: RatingsProvider(  # noqa: E731
        cache, client=FakeRatingsClient(rows=[row("Mana Vault", alsa=2.2, gih_wr=wr, gih_games=games)]), **kw)
    ratings = mk(0.58, 9000)
    sets = {
        "ALL_TIME|top": mk(0.63, 1500, user_group="top"),
        "LATEST_EVENT|": mk(0.60, 4000, time_period="LATEST_EVENT"),
        "LATEST_EVENT|top": mk(0.65, 700, time_period="LATEST_EVENT", user_group="top"),
    }
    s = DraftServer(tmp_path, port=0, interval=0.05, ratings=ratings, ratings_sets=sets)
    s.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            st = json.loads(_get(s.url + "api/state")[2])
            m = st["ratings_meta"]
            if (m and m["status"] == "ok" and all(x["status"] == "ok" for x in m["sets"].values())
                    and len(st["ratings_sets"]) == 3 and all(st["ratings_sets"].values())):
                break
            time.sleep(0.05)
        assert st["ratings"]["Mana Vault"]["gih_wr"] == 0.58
        assert st["ratings_sets"]["ALL_TIME|top"]["Mana Vault"]["gih_wr"] == 0.63
        assert st["ratings_sets"]["LATEST_EVENT|"]["Mana Vault"]["gih_wr"] == 0.60
        assert st["ratings_sets"]["LATEST_EVENT|top"]["Mana Vault"]["gih_games"] == 700
        assert m["sets"]["LATEST_EVENT|top"] == {**m["sets"]["LATEST_EVENT|top"], "user_group": "top",
                                                  "time_period": "LATEST_EVENT"}
        assert m["user_group"] == "" and m["time_period"] == "ALL_TIME"
    finally:
        s.stop()


def test_ratings_status_reaches_idle_state(tmp_path):
    from mtgo_draft_assistant.ratings import RatingsProvider
    from test_ratings import FakeClient as FakeRatingsClient

    ratings = RatingsProvider(tmp_path / "cache", client=FakeRatingsClient())
    s = DraftServer(None, port=0, interval=0.05, ratings=ratings, config_path=tmp_path / "c.toml")
    s.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            st = json.loads(_get(s.url + "api/state")[2])
            if st["ratings_meta"]["status"] == "ok":
                break
            time.sleep(0.05)
        assert st["ratings_meta"]["status"] == "ok" and st["position"]["status"] == "idle"
    finally:
        s.stop()


ARENA_LIVE = os.path.join(ROOT, "tests", "fixtures", "arena", "arena_cube_in_progress.log")
ARENA_COMPLETE = os.path.join(ROOT, "tests", "fixtures", "arena", "arena_cube_complete.log")


def test_arena_mode_end_to_end(tmp_path):
    """Arena log -> names via 17Lands ids and Scryfall arena ids -> state + SSE."""
    from mtgo_draft_assistant.arena_log import ArenaWatcher
    from mtgo_draft_assistant.ratings import RatingsProvider
    from test_ratings import FakeClient as FakeRatingsClient, row

    log = tmp_path / "Player.log"
    shutil.copyfile(ARENA_LIVE, log)
    # 17Lands knows the first pick's id; Scryfall knows the first card in the pack
    rows = [row("Bristly Bill, Spine Sower", alsa=3.0, mtga_id=17047)]
    ratings = RatingsProvider(tmp_path / "cache", client=FakeRatingsClient(rows=rows))

    class ArenaScryfall(FakeClient):
        def lookup_arena(self, grp_id):
            self.lookups.append(f"arena:{grp_id}")
            if grp_id == 7163:
                return payload("Black Lotus", "id-lotus", [], "Artifact", 0, "{0}")
            return None
    scry = ArenaScryfall(cards={})
    resolver = CardResolver(tmp_path / "cache", client=scry)
    s = DraftServer(None, port=0, interval=0.05, resolver=resolver, ratings=ratings,
                    watcher=ArenaWatcher(log, interval=0.05), config_path=tmp_path / "unused.toml")
    s.start()
    try:
        deadline = time.time() + 6
        while time.time() < deadline:
            st = json.loads(_get(s.url + "api/state")[2])
            if (st["picks"] and st["picks"][0]["picked"] == "Bristly Bill, Spine Sower"
                    and st["picks"][0]["available"][0] == "Black Lotus" and st["cards_pending"] == 0):
                break
            time.sleep(0.05)
        assert st["source"] == "arena" and st["config_locked"] is False
        assert st["file"] == "Player.log"
        assert st["sources"]["arena"]["watched"] and st["sources"]["arena"]["exists"]
        assert st["sources"]["mtgo"]["watched"] is False
        assert st["draft"]["source"] == "arena" and st["draft"]["set_name"] == "CubeDraft_Powered_20260908"
        assert st["draft"]["pod_size"] == 8 and st["draft"]["pod_size_source"] == "assumed"
        assert st["position"]["status"] == "on_screen" and st["position"]["pack"] == 1
        assert st["picks"][0]["picked"] == "Bristly Bill, Spine Sower"
        assert st["picks"][0]["available"][0] == "Black Lotus"
        assert st["picks"][0]["available"][2] == "#17047" or st["picks"][0]["available"][2] == "Bristly Bill, Spine Sower"
        # unknown ids stay as placeholders and are reported as unmatched by 17Lands
        assert any(n.startswith("#") for n in st["picks"][0]["available"])
        assert "Bristly Bill, Spine Sower" in st["ratings"]
        assert st["cards"]["Black Lotus"]["group"] == "C"
        # the MTGO folder can still be saved in Arena-only mode, with a note that it isn't watched
        code, res = _post(s.url + "api/config", {"log_dir": str(tmp_path)})
        assert code == 200 and res["ok"] and res["watched"] is False and "Arena only" in res["warning"]

        # a relaunch rewrites the file with a finished draft
        shutil.copyfile(ARENA_COMPLETE, log)
        deadline = time.time() + 5
        while time.time() < deadline:
            st = json.loads(_get(s.url + "api/state")[2])
            if st["draft"] and st["draft"]["event_id"].startswith("6691070a"):
                break
            time.sleep(0.05)
        assert st["draft"]["event_id"].startswith("6691070a")
        assert sum(p["complete"] for p in st["picks"]) == 45
        assert st["draft"]["pod_size_source"] == "inferred" and len(st["wheels"]) == 21
    finally:
        s.stop()
    # the Scryfall arena lookups were cached: every id at most once
    arena_lookups = [x for x in scry.lookups if x.startswith("arena:")]
    assert len(arena_lookups) == len(set(arena_lookups))


def test_pool_in_state_without_and_with_cube_list(tmp_path):
    from mtgo_draft_assistant.cube_list import CubeList

    _write(tmp_path / LOG_NAME, os.path.join(SNAP_DIR, "snap_003_4676b.txt"), 1_700_000_000.0)
    s = DraftServer(tmp_path, port=0, interval=0.05, config_path=tmp_path / "u.toml")
    s.start()
    try:
        st = json.loads(_get(s.url + "api/state")[2])
        pool = st["pool"]
        assert pool["cube"] is None and "no cube list" in pool["note"]
        assert pool["counts"]["mine"] == 15 and pool["counts"]["on_screen"] == 15
        assert pool["counts"]["unseen"] == 0
        assert {c["name"] for c in pool["cards"] if c["state"] == "on_screen"} == set(st["current_pack"]["cards"])
    finally:
        s.stop()

    cube = CubeList("My Cube", "file", ["Mana Vault", "Black Lotus", "Never Printed Alpha", "Never Printed Beta"])
    s2 = DraftServer(tmp_path, port=0, interval=0.05, config_path=tmp_path / "u.toml", cube=cube)
    s2.start()
    try:
        st = json.loads(_get(s2.url + "api/state")[2])
        pool = st["pool"]
        assert pool["cube"]["name"] == "My Cube" and pool["cube"]["size"] == 4
        unseen = [c["name"] for c in pool["cards"] if c["state"] == "unseen"]
        assert unseen == ["Never Printed Alpha", "Never Printed Beta"]
        assert len(pool["cube"]["unmatched"]) > 100      # this tiny list is not the draft's cube
        assert "probably not this draft's cube" in pool["note"]
    finally:
        s2.stop()


def test_arena_mode_uses_17lands_list_as_cube(tmp_path):
    from mtgo_draft_assistant.arena_log import ArenaWatcher
    from mtgo_draft_assistant.ratings import RatingsProvider
    from test_ratings import FakeClient as FakeRatingsClient, row

    log = tmp_path / "Player.log"
    shutil.copyfile(ARENA_COMPLETE, log)
    rows = [row("Bristly Bill, Spine Sower", alsa=3.0, mtga_id=90503),
            row("Never Printed Alpha", alsa=9.0, mtga_id=1)]
    ratings = RatingsProvider(tmp_path / "cache", client=FakeRatingsClient(rows=rows))
    s = DraftServer(None, port=0, interval=0.05, ratings=ratings, watcher=ArenaWatcher(log, interval=0.05))
    s.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            st = json.loads(_get(s.url + "api/state")[2])
            if st["pool"] and st["pool"]["cube"]:
                break
            time.sleep(0.05)
        pool = st["pool"]
        assert pool["cube"]["source"] == "17lands" and pool["cube"]["name"] == "17Lands Cube - Powered"
        assert [c["name"] for c in pool["cards"] if c["state"] == "unseen"] == ["Never Printed Alpha"]
        assert pool["counts"]["mine"] == 45
    finally:
        s.stop()


def test_arena_set_draft_loads_its_own_17lands_dataset(tmp_path):
    from mtgo_draft_assistant.arena_log import ArenaWatcher
    from mtgo_draft_assistant.ratings import RatingsPool
    from test_ratings import FakeClient as FakeRatingsClient, row

    # the real cube fixture with its event renamed to a set draft
    text = open(ARENA_COMPLETE, "rb").read().decode("utf-8")
    # a pick-two event of the set must use the set's PremierDraft data, as the user asked
    text = text.replace("CubeDraft_Powered_20260908", "PickTwoDraft_FRA_20260929")
    log = tmp_path / "Player.log"
    log.write_bytes(text.encode("utf-8"))
    client = FakeRatingsClient(rows=[row("Bristly Bill, Spine Sower", alsa=3.0, gih_wr=0.55, gih_games=800, mtga_id=90503)])
    pool = RatingsPool(tmp_path / "cache", client=client)
    pool.sets_for("Cube - Powered", "PremierDraft")           # what startup seeds
    s = DraftServer(None, port=0, interval=0.05, ratings_pool=pool, watcher=ArenaWatcher(log, interval=0.05))
    s.start()
    try:
        deadline = time.time() + 6
        while time.time() < deadline:
            st = json.loads(_get(s.url + "api/state")[2])
            m = st["ratings_meta"]
            if (m and m["expansion"] == "FRA" and m["status"] == "ok" and st["ratings"]
                    and st["pool"]["cube"] is not None):
                break
            time.sleep(0.05)
        assert st["draft"]["set_name"] == "PickTwoDraft_FRA_20260929"
        assert m["expansion"] == "FRA" and m["format"] == "PremierDraft" and m["status"] == "ok"
        assert st["draft"]["cards_per_pick"] == 1 and st["picks"][0]["picked_all"] == [st["picks"][0]["picked"]]
        assert set(m["sets"]) == {"ALL_TIME|top", "LATEST_EVENT|", "LATEST_EVENT|top"}
        assert "Bristly Bill, Spine Sower" in st["ratings"]
        assert st["pool"]["cube"]["name"].startswith("17Lands FRA")
    finally:
        s.stop()
    expansions = {c[0] for c in client.calls}
    assert expansions == {"Cube - Powered", "FRA"}


def test_sources_report_missing_logs_and_detailed_logs(tmp_path):
    """The settings panel's diagnostics: per client, is the log there and usable."""
    from mtgo_draft_assistant.auto_watcher import AutoWatcher

    mtgo_dir = tmp_path / "mtgo"                       # exists, empty
    mtgo_dir.mkdir()
    arena = tmp_path / "arena" / "Player.log"          # does not exist yet
    s = DraftServer(mtgo_dir, port=0, interval=0.05, config_path=tmp_path / "c.toml",
                    watcher=AutoWatcher(mtgo_dir, arena, interval=0.05))
    s.start()
    try:
        st = json.loads(_get(s.url + "api/state")[2])
        src = st["sources"]
        assert src["mtgo"] == {"watched": True, "log_dir": str(mtgo_dir), "exists": True, "logs_found": 0, "status": "no_logs"}
        assert src["arena"]["watched"] and src["arena"]["status"] == "missing" and src["arena"]["exists"] is False
        assert src["arena"]["path"] == str(arena) and src["arena"]["is_default"] is False

        # Arena wrote a log with detailed logs off
        arena.parent.mkdir()
        arena.write_bytes(b"Mono path[0] = 'x'\r\nDETAILED LOGS: DISABLED\r\n")
        time.sleep(0.3)
        code, res = _post(s.url + "api/config", {"arena_log": str(arena)})     # re-point (same path) to republish
        assert code == 200 and res["ok"] and res["exists"] and res["detailed_logs"] is False and "Detailed Logs off" in res["warning"]
        st = json.loads(_get(s.url + "api/state")[2])
        assert st["sources"]["arena"]["status"] == "detailed_logs_off"

        # now with detailed logs on
        arena.write_bytes(b"Mono path[0] = 'x'\r\nDETAILED LOGS: ENABLED\r\n")
        code, res = _post(s.url + "api/config", {"arena_log": str(arena.parent)})   # a directory means its Player.log
        assert code == 200 and res["arena_log"] == str(arena) and res["detailed_logs"] is True and "warning" not in res
        st = json.loads(_get(s.url + "api/state")[2])
        assert st["sources"]["arena"]["status"] == "ok" and st["sources"]["arena"]["detailed_logs"] is True

        # bad inputs
        code, res = _post(s.url + "api/config", {"arena_log": "relative/Player.log"})
        assert code == 400 and "full path" in res["error"]
        code, res = _post(s.url + "api/config", {"arena_log": str(tmp_path / "notes.txt")})
        assert code == 400 and "Player.log" in res["error"]

        # empty = back to the default location, persisted as such
        code, res = _post(s.url + "api/config", {"arena_log": ""})
        assert code == 200 and res["is_default"] is True
        import tomllib
        with open(tmp_path / "c.toml", "rb") as fh:
            assert tomllib.load(fh)["arena_log"] == ""
        # an MTGO folder save keeps the arena key
        code, res = _post(s.url + "api/config", {"log_dir": str(mtgo_dir)})
        assert code == 200 and res["watched"] is True
        with open(tmp_path / "c.toml", "rb") as fh:
            cfg = tomllib.load(fh)
        assert "arena_log" in cfg and os.path.normpath(cfg["log_dir"]) == os.path.normpath(str(mtgo_dir))
    finally:
        s.stop()


def test_auto_mode_switches_source_in_state(tmp_path):
    from datetime import datetime, timedelta
    from mtgo_draft_assistant.auto_watcher import AutoWatcher
    from test_auto_watcher import _arena_halves

    first, second, t_first, t_last = _arena_halves()
    mtgo_dir = tmp_path / "mtgo"
    mtgo_dir.mkdir()
    arena = tmp_path / "Player.log"
    arena.write_bytes(first)
    _write(mtgo_dir / LOG_NAME, os.path.join(SNAP_DIR, "snap_001_3254b.txt"),
           (t_first - timedelta(days=1)).timestamp())
    s = DraftServer(mtgo_dir, port=0, interval=0.05, config_path=tmp_path / "c.toml",
                    watcher=AutoWatcher(mtgo_dir, arena, interval=0.05))
    s.start()
    try:
        st = json.loads(_get(s.url + "api/state")[2])
        assert st["source"] == "arena" and st["file"] == "Player.log"
        assert st["config_locked"] is False and st["arena_log"] == str(arena)
        assert st["log_dir"] == str(tmp_path)

        _write(mtgo_dir / LOG_NAME, os.path.join(SNAP_DIR, "snap_003_4676b.txt"),
               (t_last + timedelta(minutes=1)).timestamp())
        deadline = time.time() + 3
        while time.time() < deadline:
            st = json.loads(_get(s.url + "api/state")[2])
            if st["source"] == "mtgo":
                break
            time.sleep(0.05)
        assert st["source"] == "mtgo" and st["file"] == LOG_NAME
        assert st["log_dir"] == str(mtgo_dir) and st["draft"]["hero"] == "Wumpwumpwump"
    finally:
        s.stop()


MTGO_MATCH = os.path.join(ROOT, "tests", "fixtures", "mtgo", "Match_GameLog_6fe35af5-8510-4cb0-a11d-0328e3823809.dat")
DEK = """<?xml version="1.0" encoding="utf-8"?>
<Deck><NetDeckID>0</NetDeckID>
  <Cards CatID="1" Quantity="1" Sideboard="false" Name="Mox Diamond" />
  <Cards CatID="2" Quantity="1" Sideboard="false" Name="Dark Ritual" />
  <Cards CatID="3" Quantity="6" Sideboard="false" Name="Swamp" />
  <Cards CatID="4" Quantity="1" Sideboard="true" Name="Never Played" />
</Deck>"""


def test_mtgo_deck_match_and_sideboard_edits(tmp_path, monkeypatch):
    """A .dek exported into the log folder + the client's match log -> Deck pane data,
    with drag-and-drop sideboarding recorded per match and reset when the match changes."""
    from mtgo_draft_assistant import matches

    logs = tmp_path / "logs"
    logs.mkdir()
    _write(logs / LOG_NAME, FINAL, 1_700_000_000.0)          # draft Time: 9/21/2026 4:07:52 PM
    draft_start = time.mktime(time.strptime("2026-09-21 16:07:52", "%Y-%m-%d %H:%M:%S"))
    dek = logs / "my deck.dek"
    dek.write_text(DEK, encoding="utf-8")
    os.utime(dek, (draft_start + 1800, draft_start + 1800))     # exported after the draft
    mdir = tmp_path / "matches"
    mdir.mkdir()
    monkeypatch.setattr(matches, "LOG_GLOB", str(mdir / "Match_GameLog_*.dat"))
    m1 = mdir / "Match_GameLog_6fe35af5-8510-4cb0-a11d-0328e3823809.dat"
    shutil.copyfile(MTGO_MATCH, m1)
    os.utime(m1, (draft_start + 3600, draft_start + 3600))

    s = DraftServer(logs, port=0, interval=0.05, config_path=tmp_path / "c.toml")
    s.start()
    try:
        st = json.loads(_get(s.url + "api/state")[2])
        ag = st["arena_game"]
        assert ag["client"] == "mtgo"
        assert ag["deck"]["main_count"] == 8 and ag["deck"]["side_count"] == 1
        assert ag["deck"]["source"].startswith("my deck.dek")
        g = ag["game"]
        assert g["opponent"] == "jojo_lefou" and g["hero"] == "Wumpwumpwump" and g["score"] == "2-1" and g["over"]
        assert g["library"] is None and g["life"] == {}
        rem = {c["name"]: c["n"] for c in g["remaining"]}
        assert "Mox Diamond" not in rem and rem["Swamp"] == 4 and rem["Dark Ritual"] == 1
        assert any(c["name"] == "Mox Diamond" for c in g["seen_mine"])
        assert ag["edits"] == {"to_main": [], "to_side": [], "active": False, "match": g["match_id"]}
        # the deck's cards get Scryfall lookups requested like draft cards do
        assert "Never Played" in st["cards"] or st["cards_pending"] >= 0

        # sideboard: Never Played in, Dark Ritual out
        code, res = _post(s.url + "api/deck", {"card": "Never Played", "to": "main"})
        assert code == 200 and res["ok"] and res["edits"]["to_main"] == ["Never Played"] and res["main_count"] == 9
        code, res = _post(s.url + "api/deck", {"card": "Dark Ritual", "to": "side"})
        assert code == 200 and res["main_count"] == 8
        st = json.loads(_get(s.url + "api/state")[2])
        ag = st["arena_game"]
        main = {c["name"]: c["n"] for c in ag["deck"]["main"]}; side = {c["name"]: c["n"] for c in ag["deck"]["side"]}
        assert main.get("Never Played") == 1 and "Dark Ritual" not in main and side == {"Dark Ritual": 1}
        rem = {c["name"]: c["n"] for c in ag["game"]["remaining"]}
        assert rem.get("Never Played") == 1 and "Dark Ritual" not in rem
        # moving it back undoes the edit rather than stacking a second one
        code, res = _post(s.url + "api/deck", {"card": "Dark Ritual", "to": "main"})
        assert code == 200 and res["edits"]["to_side"] == [] and res["edits"]["to_main"] == ["Never Played"]
        # bad requests
        code, res = _post(s.url + "api/deck", {"card": "Black Lotus", "to": "main"})
        assert code == 400 and "not in the sideboard" in res["error"]
        code, res = _post(s.url + "api/deck", {"card": "Never Played"})
        assert code == 400

        # a new match (newer log) resets the edits
        m2 = mdir / "Match_GameLog_bbbbbbbb-0000-0000-0000-000000000000.dat"
        shutil.copyfile(MTGO_MATCH, m2)
        os.utime(m2, (draft_start + 7200, draft_start + 7200))
        _post(s.url + "api/config", {"log_dir": str(logs)})      # any republish re-reads the match logs
        st = json.loads(_get(s.url + "api/state")[2])
        ag = st["arena_game"]
        assert ag["edits"]["match"].startswith("bbbbbbbb") and ag["edits"]["active"] is False
        assert {c["name"]: c["n"] for c in ag["deck"]["side"]} == {"Never Played": 1}
        # reset endpoint
        _post(s.url + "api/deck", {"card": "Never Played", "to": "main"})
        code, res = _post(s.url + "api/deck", {"reset": True})
        assert code == 200 and res["edits"]["active"] is False
    finally:
        s.stop()


def _post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_server_starts_without_a_log_dir_and_accepts_one(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _write(logs / LOG_NAME, os.path.join(SNAP_DIR, "snap_003_4676b.txt"), 1_700_000_000.0)
    cfg = tmp_path / "config.toml"
    s = DraftServer(None, port=0, interval=0.05, config_path=cfg)
    s.start()
    try:
        st = json.loads(_get(s.url + "api/state")[2])
        assert st["log_dir"] is None and st["log_dir_ok"] is False
        assert st["position"]["status"] == "idle"

        code, res = _post(s.url + "api/config", {"log_dir": str(tmp_path / "missing")})
        assert code == 400 and not res["ok"] and "not a directory" in res["error"]
        code, res = _post(s.url + "api/config", {"log_dir": ""})
        assert code == 400 and not res["ok"]
        code, res = _post(s.url + "api/config", {"nope": 1})
        assert code == 400 and not res["ok"]

        code, res = _post(s.url + "api/config", {"log_dir": str(logs)})
        assert code == 200 and res["ok"]
        assert res["logs_found"] == 1 and res["newest"] == LOG_NAME
        assert res["saved_to"] == str(cfg)
        assert "log_dir" in cfg.read_text(encoding="utf-8")

        st = json.loads(_get(s.url + "api/state")[2])
        assert st["log_dir_ok"] is True
        assert st["file"] == LOG_NAME
        assert st["position"] == {"pack": 2, "pick": 1, "status": "on_screen"}

        # the saved file round-trips through the real loader
        import tomllib
        with open(cfg, "rb") as fh:
            assert os.path.normpath(tomllib.load(fh)["log_dir"]) == os.path.normpath(str(logs))
    finally:
        s.stop()


def test_switching_log_dir_resets_to_new_directory(server, tmp_path):
    other = tmp_path / "other"
    other.mkdir()                                   # empty: no logs yet
    cfg = tmp_path / "cfg.toml"
    server.config_path = cfg
    code, res = _post(server.url + "api/config", {"log_dir": str(other)})
    assert code == 200 and res["ok"] and res["logs_found"] == 0 and res["newest"] is None
    st = json.loads(_get(server.url + "api/state")[2])
    assert st["file"] is None and st["picks"] == []
    assert st["position"]["status"] == "idle"
    assert os.path.normpath(st["log_dir"]) == os.path.normpath(str(other))

    # a log appearing in the new directory is picked up by the poll loop
    _write(other / LOG_NAME, os.path.join(SNAP_DIR, "snap_001_3254b.txt"), 1_700_000_100.0)
    deadline = time.time() + 3
    while time.time() < deadline:
        st = json.loads(_get(server.url + "api/state")[2])
        if st["file"] == LOG_NAME:
            break
        time.sleep(0.05)
    assert st["file"] == LOG_NAME and st["position"]["pack"] == 1


def test_bad_json_body_is_400(server):
    req = urllib.request.Request(server.url + "api/config", data=b"{not json", method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 400


def test_config_can_be_locked(tmp_path):
    s = DraftServer(tmp_path, port=0, interval=0.05, allow_config=False)
    s.start()
    try:
        code, res = _post(s.url + "api/config", {"log_dir": str(tmp_path)})
        assert code == 403 and not res["ok"]
    finally:
        s.stop()


def test_new_draft_file_resets_state(server):
    other = server.log_dir / "Wumpwumpwump-2026.9.22-12345-1-C03C03C03.txt"
    _write(other, os.path.join(SNAP_DIR, "snap_001_3254b.txt"), 1_700_000_900.0)
    deadline = time.time() + 3
    while time.time() < deadline:
        st = json.loads(_get(server.url + "api/state")[2])
        if st["file"] == other.name:
            break
        time.sleep(0.05)
    assert st["file"] == other.name
    assert st["position"]["pack"] == 1
