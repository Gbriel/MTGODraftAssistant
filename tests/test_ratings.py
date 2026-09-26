"""17Lands ratings: name join, gating, cache, background refresh. No network."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.ratings import (  # noqa: E402
    Dataset,
    RatingsCache,
    RatingsProvider,
    normalise,
    rating_from_17lands,
)


def row(name, alsa=None, gih_wr=None, gih_games=0, **extra):
    """One 17Lands card_ratings row with the fields we read."""
    d = {
        "name": name, "mtga_id": 1, "color": "U", "rarity": "rare",
        "seen_count": 100, "avg_seen": alsa, "pick_count": 10, "avg_pick": None,
        "game_count": gih_games * 2, "play_rate": 0.5, "win_rate": None,
        "opening_hand_win_rate": None, "drawn_win_rate": None,
        "ever_drawn_game_count": gih_games, "ever_drawn_win_rate": gih_wr,
        "never_drawn_win_rate": None, "drawn_improvement_win_rate": None,
    }
    d.update(extra)
    return d


ROWS = [
    row("Black Lotus", alsa=1.2, gih_wr=0.66, gih_games=600, win_rate=0.6),
    row("Ancestral Recall", alsa=1.5, gih_wr=0.63, gih_games=530),
    row("Mother of Runes", alsa=4.0, gih_wr=None, gih_games=97),
    row("Life // Death", alsa=7.5),
    row("Palantír of Orthanc", alsa=3.3),
    row("Delver of Secrets", alsa=5.0),
    row("Wasteland", alsa=9.0, gih_wr=0.51, gih_games=499),   # below 500: null in practice
    row("No ALSA card", alsa=None),
]


class FakeClient:
    def __init__(self, rows=ROWS, fail=False):
        self.rows = rows
        self.fail = fail
        self.calls: list[tuple] = []

    def fetch(self, expansion, fmt, time_period="ALL_TIME", user_group=""):
        self.calls.append((expansion, fmt, time_period) + ((user_group,) if user_group else ()))
        if self.fail:
            raise OSError("network down")
        return list(self.rows)


def wait_until(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


# -- pure -------------------------------------------------------------------


def test_normalise_handles_split_cards_and_spacing():
    assert normalise("  Fire /// Ice ") == "fire // ice"
    assert normalise("Fire//Ice") == "fire//ice"      # no spaces added; front_face still works
    assert normalise("Black  Lotus") == "black lotus"


def test_rating_from_17lands_tolerates_nulls():
    r = rating_from_17lands(row("X", alsa=None))
    assert r.alsa is None and r.gih_wr is None and r.gih_games == 0
    r = rating_from_17lands({"name": "Y", "avg_seen": "3.5", "ever_drawn_game_count": "12"})
    assert r.alsa == 3.5 and r.gih_games == 12


def test_dataset_join_exact_front_face_and_accents():
    ds = Dataset(ROWS, "Cube - Powered", "PremierDraft", "2026-09-24T10:00:00")
    assert len(ds) == 8
    assert ds.get("Black Lotus").alsa == 1.2
    assert ds.get("Life /// Death").name == "Life // Death"          # MTGO spelling of a split card
    assert ds.get("Life // Death").name == "Life // Death"
    assert ds.get("Delver of Secrets // Insectile Aberration").name == "Delver of Secrets"
    assert ds.get("Palantir of Orthanc").name == "Palantír of Orthanc"
    assert ds.get("Palantír of Orthanc").name == "Palantír of Orthanc"
    assert ds.get("Sol Ring") is None
    found, missing = ds.lookup(["Black Lotus", "Sol Ring", "Mox Pearl"])
    assert list(found) == ["Black Lotus"] and missing == ["Sol Ring", "Mox Pearl"]


def test_win_rate_gated_by_min_games():
    ds = Dataset(ROWS, "e", "f", "2026-09-24T10:00:00", min_games=500)
    assert ds.with_win_rate == 2
    j = ds.get("Wasteland").to_json(500)
    assert j["gih_wr"] is None and j["gih_games"] == 499 and j["gih_pct"] is None
    assert ds.get("Black Lotus").to_json(500)["gih_wr"] == 0.66
    assert ds.get("Black Lotus").to_json(500)["gp_wr"] == 0.6
    assert ds.get("Ancestral Recall").to_json(500)["gp_wr"] is None   # game_count 1060 but win_rate null


def test_alsa_percentile_is_higher_for_earlier_picks():
    ds = Dataset(ROWS, "e", "f", "2026-09-24T10:00:00")
    lotus = ds.get("Black Lotus").alsa_pct
    waste = ds.get("Wasteland").alsa_pct
    mom = ds.get("Mother of Runes").alsa_pct
    assert lotus == 100.0 and waste == 0.0 and 0 < mom < 100
    assert ds.get("No ALSA card").alsa_pct is None
    assert ds.get("Black Lotus").gih_pct == 100.0 and ds.get("Ancestral Recall").gih_pct == 0.0


# -- cache -------------------------------------------------------------------


def test_cache_roundtrip(tmp_path):
    c = RatingsCache(tmp_path / "ratings.sqlite")
    assert c.get("Cube - Powered", "PremierDraft") is None
    c.put("Cube - Powered", "PremierDraft", ROWS, "2026-09-24T10:00:00")
    rows, when = c.get("Cube - Powered", "PremierDraft")
    assert rows == ROWS and when == "2026-09-24T10:00:00"
    assert c.get("Cube", "PremierDraft") is None
    c.close()


# -- provider ----------------------------------------------------------------


def test_provider_fetches_when_no_cache(tmp_path):
    client = FakeClient()
    changes = []
    p = RatingsProvider(tmp_path, client=client, on_change=lambda: changes.append(1),
                        now=lambda: datetime(2026, 9, 24, 12, 0, 0))
    assert p.status()["status"] == "empty"
    assert p.lookup(["Black Lotus"]) == ({}, ["Black Lotus"])
    p.start()
    try:
        assert wait_until(lambda: p.status()["status"] == "ok")
        st = p.status()
        assert st["cards"] == 8 and st["with_win_rate"] == 2 and st["error"] is None
        assert st["fetched_at"] == "2026-09-24T12:00:00" and st["age_hours"] == 0.0
        found, missing = p.lookup(["Black Lotus", "Sol Ring"])
        assert found["Black Lotus"].gih_wr == 0.66 and missing == ["Sol Ring"]
        assert changes
    finally:
        p.stop()
    assert client.calls == [("Cube - Powered", "PremierDraft", "ALL_TIME")]


def test_fresh_cache_is_used_without_network(tmp_path):
    now = datetime(2026, 9, 24, 12, 0, 0)
    RatingsCache(tmp_path / "ratings.sqlite").put(
        "Cube - Powered", "PremierDraft", ROWS, (now - timedelta(hours=3)).isoformat(timespec="seconds"))
    client = FakeClient()
    p = RatingsProvider(tmp_path, client=client, now=lambda: now)
    assert p.status()["status"] == "ok" and p.status()["age_hours"] == 3.0
    assert not p.is_stale()
    p.start()
    time.sleep(0.2)
    p.stop()
    assert client.calls == []


def test_stale_cache_is_served_then_refreshed(tmp_path):
    now = datetime(2026, 9, 24, 12, 0, 0)
    old = [row("Black Lotus", alsa=2.0)]
    RatingsCache(tmp_path / "ratings.sqlite").put(
        "Cube - Powered", "PremierDraft", old, (now - timedelta(hours=30)).isoformat(timespec="seconds"))
    client = FakeClient()
    p = RatingsProvider(tmp_path, client=client, now=lambda: now)
    assert p.is_stale()
    assert p.lookup(["Black Lotus"])[0]["Black Lotus"].alsa == 2.0     # old data, immediately
    p.start()
    try:
        assert wait_until(lambda: p.status()["cards"] == 8)
        assert p.lookup(["Black Lotus"])[0]["Black Lotus"].alsa == 1.2
        assert p.status()["age_hours"] == 0.0
    finally:
        p.stop()
    assert len(client.calls) == 1
    # and the refreshed pull is on disk for next time
    rows, _ = RatingsCache(tmp_path / "ratings.sqlite").get("Cube - Powered", "PremierDraft")
    assert len(rows) == 8


def test_failure_is_reported_and_not_retried_immediately(tmp_path):
    client = FakeClient(fail=True)
    p = RatingsProvider(tmp_path, client=client)
    p.POLL_SECONDS = 0.02                     # worker wakes often; the retry gap must still hold
    p.start()
    try:
        assert wait_until(lambda: p.status()["status"] == "error")
        assert "network down" in p.status()["error"]
        assert p.lookup(["Black Lotus"]) == ({}, ["Black Lotus"])
        time.sleep(0.2)
        assert len(client.calls) == 1         # no hammering inside RETRY_AFTER_FAILURE
        client.fail = False
        p.refresh_now()                       # an explicit ask may retry at once
        assert wait_until(lambda: p.status()["status"] == "ok")
    finally:
        p.stop()
    assert len(client.calls) == 2


def test_empty_response_is_an_error_not_a_dataset(tmp_path):
    p = RatingsProvider(tmp_path, client=FakeClient(rows=[]))
    p.start()
    try:
        assert wait_until(lambda: p.status()["status"] == "error")
        assert "no cards" in p.status()["error"]
    finally:
        p.stop()


def test_expansion_and_period_are_part_of_the_cache_key(tmp_path):
    now = datetime(2026, 9, 24, 12, 0, 0)
    RatingsCache(tmp_path / "ratings.sqlite").put("Cube", "PremierDraft", ROWS, now.isoformat())
    p = RatingsProvider(tmp_path, expansion="Cube - Powered", client=FakeClient(), now=lambda: now)
    assert p.status()["status"] == "empty"
    RatingsCache(tmp_path / "ratings.sqlite").put("Cube - Powered", "PremierDraft", ROWS, now.isoformat(),
                                                  time_period="LATEST_EVENT")
    p2 = RatingsProvider(tmp_path, client=FakeClient(), now=lambda: now)          # ALL_TIME: no hit
    assert p2.status()["status"] == "empty"
    p3 = RatingsProvider(tmp_path, client=FakeClient(), now=lambda: now, time_period="LATEST_EVENT")
    assert p3.status()["status"] == "ok" and p3.status()["time_period"] == "LATEST_EVENT"
    # a player group is its own dataset too
    p4 = RatingsProvider(tmp_path, client=FakeClient(), now=lambda: now, time_period="LATEST_EVENT",
                         user_group="top")
    assert p4.status()["status"] == "empty" and p4.status()["user_group"] == "top"


def test_user_group_is_sent_and_cached_separately(tmp_path):
    client = FakeClient()
    p = RatingsProvider(tmp_path, client=client, user_group="top")
    p.start()
    try:
        assert wait_until(lambda: p.status()["status"] == "ok")
    finally:
        p.stop()
    assert client.calls == [("Cube - Powered", "PremierDraft", "ALL_TIME", "top")]
    assert RatingsProvider(tmp_path, client=FakeClient()).status()["status"] == "empty"
    assert RatingsProvider(tmp_path, client=FakeClient(), user_group="top").status()["status"] == "ok"


def test_client_parses_the_wrapped_response(monkeypatch):
    import io
    import urllib.request
    from mtgo_draft_assistant.ratings import RatingsClient

    seen = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        seen.append(req.full_url)
        return Resp(b'{"copyright": "x", "notes": "", "data": [{"name": "Pyrogoyf", "ever_drawn_game_count": 77954}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rows = RatingsClient(min_interval=0).fetch("Cube - Powered", "PremierDraft", "ALL_TIME")
    assert rows == [{"name": "Pyrogoyf", "ever_drawn_game_count": 77954}]
    assert seen[0].startswith("https://www.17lands.com/api/card_data?")
    assert "event_type=PremierDraft" in seen[0] and "time_period=ALL_TIME" in seen[0]
    assert "expansion=Cube+-+Powered" in seen[0] or "expansion=Cube%20-%20Powered" in seen[0]
    assert "user_group" not in seen[0]
    RatingsClient(min_interval=0).fetch("Cube - Powered", "PremierDraft", "ALL_TIME", "top")
    assert "user_group=top" in seen[1]
