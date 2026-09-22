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
