"""Scryfall resolver and cache, with a fake client (no network in tests)."""

from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mtgo_draft_assistant.scryfall import (  # noqa: E402
    CardCache,
    CardInfo,
    CardResolver,
    card_from_scryfall,
)

JPEG = b"\xff\xd8\xff\xe0fakejpeg"


def payload(name, cid, colors, type_line, cmc=1.0, mana="{1}", faces=False):
    d = {"id": cid, "name": name, "type_line": type_line, "cmc": cmc, "mana_cost": mana,
         "color_identity": colors, "layout": "normal"}
    img = {"small": f"https://cards.scryfall.io/small/front/x/{cid}.jpg"}
    if faces:
        d["layout"] = "transform"
        d["card_faces"] = [{"colors": colors, "image_uris": img, "type_line": type_line.split(" // ")[0]}]
    else:
        d["colors"] = colors
        d["image_uris"] = img
    return d


CARDS = {
    "Mother of Runes": payload("Mother of Runes", "id-mom", ["W"], "Creature — Human Cleric"),
    "Badlands": payload("Badlands", "id-bad", [], "Land — Swamp Mountain", cmc=0, mana=""),
    "Tidehollow Sculler": payload("Tidehollow Sculler", "id-tide", ["W", "B"], "Artifact Creature — Zombie", 2),
    "Sol Ring": payload("Sol Ring", "id-sol", [], "Artifact"),
    "Delver of Secrets": payload("Delver of Secrets // Insectile Aberration", "id-delver", ["U"],
                                 "Creature — Human Wizard // Creature — Human Insect", faces=True),
}


class FakeClient:
    """
    Mimics ScryfallClient. ``lookups`` records every name that went through
    either the batch or the single endpoint; ``batches`` counts batch calls.
    Names in ``fuzzy_only`` are rejected by the batch endpoint (exact-name
    miss) and resolved by the single fuzzy lookup, as a split card might be.
    """

    def __init__(self, cards=CARDS, fail=(), fuzzy_only=()):
        self.cards = cards
        self.fail = set(fail)
        self.fuzzy_only = set(fuzzy_only)
        self.lookups: list[str] = []
        self.single_lookups: list[str] = []
        self.batches = 0
        self.downloads: list[str] = []

    def lookup_many(self, names):
        self.batches += 1
        found, not_found = {}, []
        for n in names:
            self.lookups.append(n)
            if n in self.fail:
                raise OSError("network down")
            if n in self.cards and n not in self.fuzzy_only:
                found[n] = self.cards[n]
            else:
                not_found.append(n)
        return found, not_found

    def lookup(self, name):
        self.single_lookups.append(name)
        if name in self.fail:
            raise OSError("network down")
        return self.cards.get(name)

    def download(self, url):
        self.downloads.append(url)
        return JPEG


def wait_until(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


# -- pure functions ----------------------------------------------------------


def test_card_from_scryfall_groups():
    assert card_from_scryfall("Mother of Runes", CARDS["Mother of Runes"]).group == "W"
    assert card_from_scryfall("Badlands", CARDS["Badlands"]).group == "L"
    assert card_from_scryfall("Tidehollow Sculler", CARDS["Tidehollow Sculler"]).group == "M"
    assert card_from_scryfall("Sol Ring", CARDS["Sol Ring"]).group == "C"


def test_card_from_scryfall_uses_front_face_for_dfc():
    info = card_from_scryfall("Delver of Secrets", CARDS["Delver of Secrets"])
    assert info.colors == ["U"]
    assert info.group == "U"
    assert info.image_file == "id-delver.jpg"
    assert info.type_line.startswith("Creature")


def test_colors_sorted_wubrg():
    info = card_from_scryfall("x", payload("x", "i", ["B", "W"], "Creature"))
    assert info.colors == ["W", "B"]


def test_to_json_missing_card_is_group_x():
    j = CardInfo(name="Nope", status="missing").to_json()
    assert j["group"] == "X" and j["image"] is None


# -- cache -------------------------------------------------------------------


def test_cache_roundtrip(tmp_path):
    c = CardCache(tmp_path / "cards.sqlite")
    info = card_from_scryfall("Tidehollow Sculler", CARDS["Tidehollow Sculler"])
    c.put(info)
    got = c.get("Tidehollow Sculler")
    assert got == info
    assert c.get("absent") is None
    c.close()


# -- resolver ----------------------------------------------------------------


def test_resolver_fetches_and_caches(tmp_path):
    client = FakeClient()
    changes = []
    r = CardResolver(tmp_path, client=client, on_change=lambda: changes.append(1))
    assert r.lookup(["Mother of Runes"]) == {}
    # Queue before starting the worker so all three names land in one batch.
    queued = r.request(["Mother of Runes", "Badlands", "Mother of Runes", "Not A Card"])
    assert queued == 3
    r.start()
    try:
        assert wait_until(lambda: r.pending == 0)
        found = r.lookup(["Mother of Runes", "Badlands", "Not A Card"])
        assert found["Mother of Runes"].group == "W"
        assert found["Mother of Runes"].image_file == "id-mom.jpg"
        assert (tmp_path / "images" / "id-mom.jpg").read_bytes() == JPEG
        assert found["Not A Card"].status == "missing"
        assert changes, "on_change never fired"
        # Nothing more to do: a second request queues nothing.
        assert r.request(["Mother of Runes", "Badlands", "Not A Card"]) == 0
    finally:
        r.stop()
    assert sorted(client.lookups) == ["Badlands", "Mother of Runes", "Not A Card"]
    assert client.batches == 1, "three names should go out as one batch"
    assert client.single_lookups == ["Not A Card"], "only the batch miss gets a fuzzy lookup"
    assert len(client.downloads) == 2


def test_batch_miss_falls_back_to_fuzzy_lookup(tmp_path):
    client = FakeClient(fuzzy_only={"Delver of Secrets"})
    r = CardResolver(tmp_path, client=client)
    r.start()
    try:
        r.request(["Delver of Secrets", "Sol Ring"])
        assert wait_until(lambda: r.pending == 0)
        found = r.lookup(["Delver of Secrets", "Sol Ring"])
        assert found["Delver of Secrets"].group == "U"
        assert found["Sol Ring"].group == "C"
    finally:
        r.stop()
    assert client.single_lookups == ["Delver of Secrets"]


def test_large_request_is_chunked_into_batches(tmp_path):
    cards = {f"Card {i}": payload(f"Card {i}", f"id-{i}", ["R"], "Instant") for i in range(160)}
    client = FakeClient(cards=cards)
    r = CardResolver(tmp_path, client=client)
    r.request(list(cards))              # queue everything before the worker wakes
    r.start()
    try:
        assert wait_until(lambda: r.pending == 0, timeout=10)
        assert len(r.lookup(list(cards))) == 160
    finally:
        r.stop()
    assert client.batches == 3          # 160 names: 75 + 75 + 10
    assert len(client.downloads) == 160


def test_client_backs_off_on_429(monkeypatch):
    import io
    import urllib.error
    import urllib.request
    from mtgo_draft_assistant.scryfall import ScryfallClient

    calls = []
    sleeps = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                         {"Retry-After": "0.01"}, io.BytesIO(b""))
        return Resp(b'{"id": "x", "name": "X", "type_line": "Instant", "colors": ["R"]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("mtgo_draft_assistant.scryfall.time.sleep", lambda s: sleeps.append(s))
    c = ScryfallClient(min_interval=0)
    assert c.lookup("X")["name"] == "X"
    assert len(calls) == 3
    assert [s for s in sleeps if s >= 0.01], "should have slept per Retry-After"


def test_client_gives_up_after_max_retries(monkeypatch):
    import io
    import urllib.error
    import urllib.request
    import pytest
    from mtgo_draft_assistant.scryfall import ScryfallClient

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("mtgo_draft_assistant.scryfall.time.sleep", lambda s: None)
    c = ScryfallClient(min_interval=0, max_retries=2)
    with pytest.raises(urllib.error.HTTPError):
        c.download("https://cards.scryfall.io/x.jpg")


def test_client_404_is_none_not_error(monkeypatch):
    import io
    import urllib.error
    import urllib.request
    from mtgo_draft_assistant.scryfall import ScryfallClient

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    c = ScryfallClient(min_interval=0)
    assert c.lookup("Nothing") is None


def test_second_resolver_reuses_disk_cache(tmp_path):
    r1 = CardResolver(tmp_path, client=FakeClient())
    r1.start()
    r1.request(["Sol Ring"])
    assert wait_until(lambda: r1.pending == 0)
    r1.stop()

    client = FakeClient()
    r2 = CardResolver(tmp_path, client=client)
    assert r2.lookup(["Sol Ring"])["Sol Ring"].group == "C"
    assert r2.request(["Sol Ring"]) == 0
    assert client.lookups == [] and client.downloads == []
    r2.stop()


def test_missing_image_is_refetched_without_relookup(tmp_path):
    r1 = CardResolver(tmp_path, client=FakeClient())
    r1.start()
    r1.request(["Sol Ring"])
    assert wait_until(lambda: r1.pending == 0)
    r1.stop()
    (tmp_path / "images" / "id-sol.jpg").unlink()

    client = FakeClient()
    r2 = CardResolver(tmp_path, client=client)
    # Metadata is known but the image isn't advertised until it's on disk.
    assert r2.lookup(["Sol Ring"])["Sol Ring"].image_file is None
    r2.start()
    assert r2.request(["Sol Ring"]) == 1
    assert wait_until(lambda: r2.pending == 0)
    assert client.lookups == [] and client.single_lookups == [] and len(client.downloads) == 1
    assert r2.lookup(["Sol Ring"])["Sol Ring"].image_file == "id-sol.jpg"
    r2.stop()


def test_network_error_is_recorded_and_not_hammered(tmp_path):
    client = FakeClient(fail={"Badlands"})
    r = CardResolver(tmp_path, client=client)
    r.start()
    try:
        r.request(["Badlands"])
        assert wait_until(lambda: r.pending == 0)
        assert r.errors and "Badlands" in r.errors[0]
        assert r.lookup(["Badlands"]) == {}
        assert r.request(["Badlands"]) == 0          # inside the retry window
    finally:
        r.stop()
    assert client.lookups == ["Badlands"]


def test_request_is_thread_safe(tmp_path):
    r = CardResolver(tmp_path, client=FakeClient())
    r.start()
    names = list(CARDS)
    threads = [threading.Thread(target=r.request, args=(names,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wait_until(lambda: r.pending == 0)
    assert len(r.lookup(names)) == len(names)
    r.stop()
