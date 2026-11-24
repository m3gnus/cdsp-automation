from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer

import pytest

import jukebox


class FakeLms:
    """Answers the JSON-RPC calls the jukebox makes, and records them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.players = [{"name": "Hifi", "playerid": "aa:bb:cc:dd:ee:ff", "connected": 1}]
        self.apps = [{"cmd": "qobuz", "name": "Qobuz"}]
        self.playlist: list[dict] = [
            {"title": "Now", "artist": "A", "url": "qobuz://1.flac", "artwork_url": "/imageproxy/x/image.jpg"},
        ]
        self.cur = 0
        self.mode = "play"
        self.tracks = [
            {"id": "s.0.2.0", "name": "Blue in Green", "isaudio": 1, "hasitems": 0,
             "url": "qobuz://42.flac", "image": "https://img.example/42.jpg"},
            {"id": "s.0.2.1", "name": "So What", "isaudio": 1, "hasitems": 0,
             "url": "qobuz://43.flac"},
        ]

    def __call__(self, player: str, terms: list) -> dict:
        terms = [str(t) for t in terms]
        self.calls.append((player, terms))
        if terms[0] == "players":
            return {"players_loop": self.players}
        if terms[0] == "apps":
            return {"appss_loop": self.apps}
        if terms[:2] == ["qobuz", "items"]:
            args = terms[4:]
            if not args:
                return {"loop_loop": [{"id": "s", "name": "Search", "type": "search"}]}
            if args[0] == "item_id:s":
                return {"loop_loop": [
                    {"id": "s.0.0", "name": "Artists", "hasitems": 1},
                    {"id": "s.0.1", "name": "Albums", "hasitems": 1, "isaudio": 1},
                    {"id": "s.0.2", "name": "Tracks", "hasitems": 1},
                ]}
            if args[0] == "item_id:s.0.2":
                return {"loop_loop": self.tracks}
            return {"loop_loop": []}
        if terms[0] == "status":
            loop = [
                {**t, "playlist index": i}
                for i, t in enumerate(self.playlist)
                if i >= self.cur
            ]
            return {"mode": self.mode, "playlist_cur_index": self.cur,
                    "playlist_tracks": len(self.playlist), "playlist_loop": loop,
                    "time": 30, "duration": 300}
        if terms[:2] == ["playlist", "add"]:
            self.playlist.append({"title": terms[3], "url": terms[2]})
            return {}
        if terms[:2] == ["playlist", "index"]:
            self.cur = int(terms[2])
            self.mode = "play"
            return {}
        raise AssertionError(f"unexpected LMS call {terms}")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def lms(monkeypatch):
    fake = FakeLms()
    monkeypatch.setattr(jukebox, "lms_request", fake)
    monkeypatch.setattr(jukebox, "JUKEBOX_PLAYER", "")
    monkeypatch.setattr(jukebox, "JUKEBOX_SERVICES", ("qobuz", "tidal"))
    monkeypatch.setattr(jukebox, "JUKEBOX_MAX_PER_GUEST", 2)
    monkeypatch.setattr(jukebox, "JUKEBOX_COOLDOWN_SECONDS", 20)
    monkeypatch.setattr(jukebox, "JUKEBOX_AUTOPLAY", True)
    return fake


@pytest.fixture
def box():
    return jukebox.Jukebox(clock=Clock())


def search_ids(box, guest="1.1.1.1", query="miles davis"):
    return [r["id"] for r in box.search(guest, query)["results"]]


def test_search_descends_into_the_tracks_folder_only(lms, box):
    result = box.search("1.1.1.1", "kind of blue")
    assert result["service"] == "qobuz"
    assert [r["name"] for r in result["results"]] == ["Blue in Green", "So What"]
    assert result["results"][0]["image"] == "https://img.example/42.jpg"
    browsed = [t for _, t in lms.calls if t[:2] == ["qobuz", "items"]]
    assert ["item_id:s", "search:kind of blue", "want_url:1"] == browsed[1][4:]
    assert not any("item_id:s.0.0" in t or "item_id:s.0.1" in t for t in browsed)
    # Guests get opaque ids, never LMS urls.
    assert "url" not in result["results"][0]


def test_search_rejects_short_queries_and_rapid_repeats(lms, box):
    with pytest.raises(jukebox.JukeboxError):
        box.search("1.1.1.1", " a ")
    box.search("1.1.1.1", "miles")
    with pytest.raises(jukebox.JukeboxError) as err:
        box.search("1.1.1.1", "miles")
    assert err.value.status == HTTPStatus.TOO_MANY_REQUESTS


def test_add_appends_the_url_to_the_end_of_the_queue(lms, box):
    offer = search_ids(box)[0]
    assert box.add("1.1.1.1", offer)["ok"]
    assert lms.calls[-1][1] == ["playlist", "add", "qobuz://42.flac", "Blue in Green"]
    state = box.state("1.1.1.1")
    assert [t["title"] for t in state["upcoming"]] == ["Blue in Green"]
    assert state["upcoming"][0]["mine"] is True
    assert box.state("2.2.2.2")["upcoming"][0]["mine"] is False


def test_add_refuses_unknown_ids_duplicates_and_cooldown(lms, box):
    with pytest.raises(jukebox.JukeboxError):
        box.add("1.1.1.1", "qobuz://evil")
    first, _ = search_ids(box)
    box.add("1.1.1.1", first)
    box.clock.now += 5
    with pytest.raises(jukebox.JukeboxError) as err:
        box.add("1.1.1.1", first)
    assert err.value.status == HTTPStatus.TOO_MANY_REQUESTS
    box.clock.now += 30
    with pytest.raises(jukebox.JukeboxError) as err:
        box.add("2.2.2.2", first)
    assert err.value.status == HTTPStatus.CONFLICT


def test_each_guest_has_a_limit_of_waiting_tracks(lms, box):
    lms.tracks.append({"id": "s.0.2.2", "name": "Freddie", "isaudio": 1, "hasitems": 0,
                       "url": "qobuz://44.flac"})
    ids = search_ids(box)
    box.add("1.1.1.1", ids[0])
    box.clock.now += 30
    box.add("1.1.1.1", ids[1])
    box.clock.now += 30
    with pytest.raises(jukebox.JukeboxError) as err:
        box.add("1.1.1.1", ids[2])
    assert "2 tracks waiting" in str(err.value)
    # Once the guest's tracks have played, the slots free up again.
    lms.cur = len(lms.playlist)
    lms.playlist.append({"title": "Other", "url": "qobuz://99.flac"})
    box.add("1.1.1.1", ids[2])


def test_add_starts_a_stopped_player_on_the_new_track(lms, box):
    lms.mode = "stop"
    box.add("1.1.1.1", search_ids(box)[0])
    assert lms.calls[-1][1] == ["playlist", "index", "1"]
    assert lms.cur == 1


def test_add_leaves_a_paused_player_alone(lms, box):
    lms.mode = "pause"
    box.add("1.1.1.1", search_ids(box)[0])
    assert lms.calls[-1][1][:2] == ["playlist", "add"]


def test_long_tracks_are_refused(lms, box, monkeypatch):
    monkeypatch.setattr(jukebox, "JUKEBOX_MAX_TRACK_MINUTES", 10)
    lms.tracks[0]["duration"] = 3600
    with pytest.raises(jukebox.JukeboxError):
        box.add("1.1.1.1", search_ids(box)[0])


def test_player_choice(lms, monkeypatch):
    lms.players.append({"name": "Office", "playerid": "11:22:33:44:55:66"})
    with pytest.raises(jukebox.JukeboxError):
        jukebox.player_id()
    monkeypatch.setattr(jukebox, "JUKEBOX_PLAYER", "office")
    assert jukebox.player_id() == "11:22:33:44:55:66"
    monkeypatch.setattr(jukebox, "JUKEBOX_PLAYER", "AA:BB:CC:DD:EE:FF")
    assert jukebox.player_id() == "aa:bb:cc:dd:ee:ff"


def test_art_urls_only_proxy_lms_image_paths():
    assert jukebox.art_url("https://x/y.jpg") == "https://x/y.jpg"
    assert jukebox.art_url("/imageproxy/a/image.jpg").startswith("art?p=%2Fimageproxy")
    assert jukebox.art_url("/settings/server") == ""
    assert jukebox.art_url("/music/../x") == ""
    with pytest.raises(jukebox.JukeboxError):
        jukebox.fetch_art("/jsonrpc.js")


def test_same_origin_guard():
    assert jukebox.same_origin(None, "192.168.1.5:8090")
    assert jukebox.same_origin("http://192.168.1.5:8090", "192.168.1.5:8090")
    assert not jukebox.same_origin("http://evil.example", "192.168.1.5:8090")
    assert not jukebox.same_origin("http://evil.example:8090", "evil.example:8090")


def test_http_endpoints(lms, monkeypatch):
    monkeypatch.setattr(jukebox, "JUKEBOX", jukebox.Jukebox(clock=Clock()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), jukebox.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/") as r:
            assert b"Up next" in r.read()
        with urllib.request.urlopen(base + "/api/search?q=miles") as r:
            offer = json.loads(r.read())["results"][0]["id"]
        request = urllib.request.Request(
            base + "/api/add", data=json.dumps({"id": offer}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as r:
            assert json.loads(r.read())["ok"]
        with urllib.request.urlopen(base + "/api/state") as r:
            assert json.loads(r.read())["upcoming"][0]["mine"]
        foreign = urllib.request.Request(
            base + "/api/add", data=b"{}",
            headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(foreign)
        assert err.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
