#!/usr/bin/env python3
"""Guest jukebox: a phone page where visitors search and queue music on LMS.

Visitors can only search and add single tracks to the end of the queue of
one Lyrion Music Server (LMS) player.  They cannot skip, remove, reorder,
pause or change the volume; the operator keeps those in LMS's own interface
(Material Skin) or the control UI.  Search goes through the streaming
plugins installed in LMS (Qobuz, TIDAL, ...), so the music plays in full
quality from LMS to Squeezelite on this Pi.

Standard library only, and it runs as the normal install user: it never
touches CamillaDSP, the MOTU or system services.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


# ------------------------------------------------------------ settings


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(_env(name, str(default))))
    except ValueError:
        return default


LMS_HOST = _env("LMS_HOST", "127.0.0.1")
LMS_PORT = _env_int("LMS_PORT", 9000, 1)
JUKEBOX_HOST = _env("JUKEBOX_HOST", "0.0.0.0")
JUKEBOX_PORT = _env_int("JUKEBOX_PORT", 8090, 1)
# The LMS player guests queue on: its name as LMS shows it, or its MAC.
# Empty picks the only player LMS knows; with several it must be set.
JUKEBOX_PLAYER = _env("JUKEBOX_PLAYER", "")
# LMS app commands searched, in order.  The first one LMS has installed wins.
JUKEBOX_SERVICES = tuple(
    s.strip().lower()
    for s in _env("JUKEBOX_SERVICES", "qobuz,tidal").split(",")
    if s.strip()
)
JUKEBOX_TITLE = _env("JUKEBOX_TITLE", _env("SITE_NAME", "Jukebox"))
# How many of one guest's tracks may wait in the queue at once.
JUKEBOX_MAX_PER_GUEST = _env_int("JUKEBOX_MAX_PER_GUEST", 3, 1)
# Seconds a guest waits between two additions.
JUKEBOX_COOLDOWN_SECONDS = _env_int("JUKEBOX_COOLDOWN_SECONDS", 20)
# Longest track a guest may add, in minutes (0 = no limit).
JUKEBOX_MAX_TRACK_MINUTES = _env_int("JUKEBOX_MAX_TRACK_MINUTES", 12)
# Start playing a guest's track when the player is stopped (not when paused).
JUKEBOX_AUTOPLAY = _env("JUKEBOX_AUTOPLAY", "1") not in {"0", "false", "no", "off"}

LMS_TIMEOUT_SECONDS = 8.0
SEARCH_RESULTS = 25
SEARCH_MIN_INTERVAL_SECONDS = 1.0
MAX_QUERY_LENGTH = 100
MAX_REQUEST_BODY_BYTES = 4096
REQUEST_TIMEOUT_SECONDS = 20.0
UPCOMING_SHOWN = 15
# Search results a guest can add, kept so the add request only has to name
# an id: guests can never hand LMS a URL or a command of their own.
OFFER_TTL_SECONDS = 30 * 60
MAX_OFFERS = 5000
# Folder names under a service's search results that hold single tracks.
TRACK_FOLDER_WORDS = (
    "track", "song", "title", "titel", "titre", "nummer", "spår", "låt",
    "brani", "canciones", "canções", "kappale", "sang",
)
# Relative artwork paths the art proxy may fetch from LMS.
ART_PATH_PREFIXES = ("/imageproxy/", "/music/", "/plugins/", "/html/images/")


class JukeboxError(Exception):
    """A refusal or failure the guest sees as a short message."""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


# ----------------------------------------------------------------- LMS


def lms_request(player: str, terms: list[Any]) -> dict[str, Any]:
    """One JSON-RPC call to LMS; its "result" object."""
    body = json.dumps(
        {"id": 1, "method": "slim.request", "params": [player, [str(t) for t in terms]]}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://{LMS_HOST}:{LMS_PORT}/jsonrpc.js",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=LMS_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise JukeboxError(
            "The music server is not answering right now.", HTTPStatus.BAD_GATEWAY
        ) from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    return result if isinstance(result, dict) else {}


def _loop(result: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    for name in names:
        items = result.get(name)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _flag(value: Any) -> bool:
    return str(value).strip() in {"1", "true", "True"}


def find_player(players: list[dict[str, Any]], wanted: str) -> dict[str, Any]:
    """The configured player, or the only one when none is configured."""
    wanted = wanted.strip().lower()
    if wanted:
        for player in players:
            if wanted in {
                str(player.get("name", "")).strip().lower(),
                str(player.get("playerid", "")).strip().lower(),
            }:
                return player
        raise JukeboxError(
            f"The player '{JUKEBOX_PLAYER}' is not connected to the music server.",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if len(players) == 1:
        return players[0]
    if not players:
        raise JukeboxError(
            "No player is connected to the music server.", HTTPStatus.SERVICE_UNAVAILABLE
        )
    raise JukeboxError(
        "Several players are connected: set JUKEBOX_PLAYER.",
        HTTPStatus.SERVICE_UNAVAILABLE,
    )


def player_id() -> str:
    players = _loop(lms_request("", ["players", 0, 100]), "players_loop")
    player = find_player(players, JUKEBOX_PLAYER)
    pid = str(player.get("playerid", "")).strip()
    if not pid:
        raise JukeboxError("The music server sent a player without an id.", HTTPStatus.BAD_GATEWAY)
    return pid


def installed_service(pid: str) -> str:
    """The first configured streaming service LMS has installed."""
    apps = _loop(lms_request(pid, ["apps", 0, 200]), "appss_loop", "apps_loop", "loop_loop")
    commands = {str(app.get("cmd", "")).strip().lower() for app in apps}
    for service in JUKEBOX_SERVICES:
        if service in commands:
            return service
    # Older servers or plugins may not list themselves under "apps": try the
    # first configured service and let its own answer decide.
    if JUKEBOX_SERVICES:
        return JUKEBOX_SERVICES[0]
    raise JukeboxError("No streaming service is configured.", HTTPStatus.SERVICE_UNAVAILABLE)


def art_url(raw: Any) -> str:
    """Artwork the guest's browser can load: absolute, or through our proxy."""
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith(("https://", "http://")):
        return value
    if not value.startswith("/"):
        value = "/" + value
    if value.startswith(ART_PATH_PREFIXES) and ".." not in value:
        return "art?p=" + urllib.parse.quote(value, safe="")
    return ""


def _track_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    if not _flag(item.get("isaudio")) or _flag(item.get("hasitems")):
        return None
    name = str(item.get("name") or item.get("title") or "").strip()
    if not name:
        return None
    subtitle = " · ".join(
        part
        for part in (
            str(item.get("artist") or item.get("line2") or "").strip(),
            str(item.get("album") or "").strip(),
        )
        if part
    )
    duration = item.get("duration")
    try:
        duration = float(duration) if duration not in (None, "") else None
    except (TypeError, ValueError):
        duration = None
    return {
        "name": name,
        "subtitle": subtitle,
        "image": art_url(item.get("image") or item.get("icon")),
        "url": str(item.get("url") or "").strip(),
        "item_id": str(item.get("id") or "").strip(),
        "duration": duration,
    }


def _is_track_folder(item: dict[str, Any]) -> bool:
    name = str(item.get("name") or "").lower()
    return _flag(item.get("hasitems")) and any(word in name for word in TRACK_FOLDER_WORDS)


def search_service(pid: str, service: str, query: str) -> list[dict[str, Any]]:
    """Single tracks matching query, from the service's own search menu."""
    top = _loop(lms_request(pid, [service, "items", 0, 50]), "loop_loop", "item_loop")
    search_item = next(
        (i for i in top if str(i.get("type", "")).lower() == "search"), None
    ) or next((i for i in top if "search" in str(i.get("name", "")).lower()), None)
    if search_item is None or not search_item.get("id"):
        raise JukeboxError(
            f"{service.capitalize()} has no search in the music server. Is it signed in?",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    base = [f"item_id:{search_item['id']}", f"search:{query}", "want_url:1"]
    found = _loop(
        lms_request(pid, [service, "items", 0, 50, *base]), "loop_loop", "item_loop"
    )
    tracks = [t for t in map(_track_from_item, found) if t]
    if not tracks:
        # Most services answer with folders (Artists, Albums, Tracks, ...).
        folders = [i for i in found if _is_track_folder(i)][:2]
        for folder in folders:
            if not folder.get("id"):
                continue
            items = _loop(
                lms_request(
                    pid,
                    [service, "items", 0, SEARCH_RESULTS, f"item_id:{folder['id']}", "want_url:1"],
                ),
                "loop_loop",
                "item_loop",
            )
            tracks.extend(t for t in map(_track_from_item, items) if t)
            if tracks:
                break
    return tracks[:SEARCH_RESULTS]


def queue_status(pid: str) -> dict[str, Any]:
    """The playing track and the ones after it."""
    result = lms_request(pid, ["status", "-", UPCOMING_SHOWN + 1, "tags:alKNdu"])
    loop = _loop(result, "playlist_loop")
    try:
        current_index = int(result.get("playlist_cur_index", 0))
    except (TypeError, ValueError):
        current_index = 0
    tracks = []
    for item in loop:
        try:
            index = int(item.get("playlist index", 0))
        except (TypeError, ValueError):
            continue
        tracks.append(
            {
                "index": index,
                "title": str(item.get("title") or item.get("remote_title") or "").strip()
                or "Unknown track",
                "artist": str(item.get("artist") or "").strip(),
                "album": str(item.get("album") or "").strip(),
                "image": art_url(item.get("artwork_url") or ""),
                "url": str(item.get("url") or "").strip(),
            }
        )
    current = next((t for t in tracks if t["index"] == current_index), None)
    upcoming = [t for t in tracks if t["index"] > current_index]

    def number(key: str) -> float | None:
        try:
            return float(result[key])
        except (KeyError, TypeError, ValueError):
            return None

    try:
        total = int(result.get("playlist_tracks", 0))
    except (TypeError, ValueError):
        total = 0
    return {
        "mode": str(result.get("mode") or "stop"),
        "current": current,
        "upcoming": upcoming,
        "elapsed": number("time"),
        "duration": number("duration"),
        "total": total,
    }


# -------------------------------------------------------------- jukebox


@dataclass
class Offer:
    service: str
    name: str
    subtitle: str
    image: str
    url: str
    item_id: str
    duration: float | None
    created: float


@dataclass
class Guest:
    last_add: float = 0.0
    last_search: float = 0.0
    added_urls: list[str] = field(default_factory=list)


class Jukebox:
    """Search results, guests and their queued tracks, shared by all requests."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.offers: dict[str, Offer] = {}
        self.guests: dict[str, Guest] = {}
        self.next_offer = 0

    def _guest(self, guest_id: str) -> Guest:
        return self.guests.setdefault(guest_id, Guest())

    def _prune_offers(self, now: float) -> None:
        stale = [k for k, o in self.offers.items() if now - o.created > OFFER_TTL_SECONDS]
        for key in stale:
            del self.offers[key]
        while len(self.offers) > MAX_OFFERS:
            del self.offers[next(iter(self.offers))]

    def search(self, guest_id: str, query: str) -> dict[str, Any]:
        query = " ".join(str(query or "").split())[:MAX_QUERY_LENGTH]
        if len(query) < 2:
            raise JukeboxError("Type at least two letters.")
        now = self.clock()
        with self.lock:
            guest = self._guest(guest_id)
            if now - guest.last_search < SEARCH_MIN_INTERVAL_SECONDS:
                raise JukeboxError("Slow down a little.", HTTPStatus.TOO_MANY_REQUESTS)
            guest.last_search = now
        pid = player_id()
        service = installed_service(pid)
        tracks = search_service(pid, service, query)
        results = []
        with self.lock:
            self._prune_offers(now)
            for track in tracks:
                if not track["url"] and not track["item_id"]:
                    continue
                self.next_offer += 1
                key = f"o{self.next_offer}"
                self.offers[key] = Offer(
                    service=service,
                    name=track["name"],
                    subtitle=track["subtitle"],
                    image=track["image"],
                    url=track["url"],
                    item_id=track["item_id"],
                    duration=track["duration"],
                    created=now,
                )
                results.append(
                    {
                        "id": key,
                        "name": track["name"],
                        "subtitle": track["subtitle"],
                        "image": track["image"],
                        "duration": track["duration"],
                    }
                )
        return {"service": service, "results": results}

    def _mine(self, guest: Guest, upcoming_urls: list[str]) -> int:
        # Only tracks still waiting count against the guest's limit.
        waiting = [u for u in guest.added_urls if u in upcoming_urls]
        guest.added_urls = waiting
        return len(waiting)

    def add(self, guest_id: str, offer_id: str) -> dict[str, Any]:
        now = self.clock()
        with self.lock:
            offer = self.offers.get(str(offer_id or ""))
            if offer is None or now - offer.created > OFFER_TTL_SECONDS:
                raise JukeboxError("That search result has expired. Search again.")
            guest = self._guest(guest_id)
            wait = JUKEBOX_COOLDOWN_SECONDS - (now - guest.last_add)
            if guest.last_add and wait > 0:
                raise JukeboxError(
                    f"Wait {int(wait) + 1} seconds before adding another track.",
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
        if (
            JUKEBOX_MAX_TRACK_MINUTES
            and offer.duration
            and offer.duration > JUKEBOX_MAX_TRACK_MINUTES * 60
        ):
            raise JukeboxError(
                f"Tracks longer than {JUKEBOX_MAX_TRACK_MINUTES} minutes can't be queued."
            )
        pid = player_id()
        before = queue_status(pid)
        upcoming_urls = [t["url"] for t in before["upcoming"] if t["url"]]
        if before["current"] and before["current"]["url"]:
            upcoming_urls.append(before["current"]["url"])
        with self.lock:
            guest = self._guest(guest_id)
            if self._mine(guest, upcoming_urls) >= JUKEBOX_MAX_PER_GUEST:
                raise JukeboxError(
                    f"You already have {JUKEBOX_MAX_PER_GUEST} tracks waiting. "
                    "Add more when one has played.",
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
            if offer.url and offer.url in upcoming_urls:
                raise JukeboxError("That track is already in the queue.", HTTPStatus.CONFLICT)
            # Claim the slot before the slow LMS call, so two quick taps
            # cannot both pass the checks above.
            guest.last_add = now
        try:
            if offer.url:
                lms_request(pid, ["playlist", "add", offer.url, offer.name])
            else:
                lms_request(pid, [offer.service, "playlist", "add", f"item_id:{offer.item_id}"])
        except JukeboxError:
            with self.lock:
                guest.last_add = 0.0
            raise
        with self.lock:
            if offer.url:
                guest.added_urls.append(offer.url)
        if JUKEBOX_AUTOPLAY and before["mode"] == "stop":
            # The new track is the last one; start it rather than replaying
            # whatever the queue stopped on.
            lms_request(pid, ["playlist", "index", before["total"]])
        return {"ok": True, "message": f"Added “{offer.name}”."}

    def state(self, guest_id: str) -> dict[str, Any]:
        pid = player_id()
        status = queue_status(pid)
        with self.lock:
            guest = self._guest(guest_id)
            mine = set(guest.added_urls)

        def public(track: dict[str, Any] | None) -> dict[str, Any] | None:
            if track is None:
                return None
            out = {k: track[k] for k in ("title", "artist", "album", "image")}
            out["mine"] = bool(track["url"]) and track["url"] in mine
            return out

        return {
            "title": JUKEBOX_TITLE,
            "mode": status["mode"],
            "current": public(status["current"]),
            "upcoming": [public(t) for t in status["upcoming"]],
            "elapsed": status["elapsed"],
            "duration": status["duration"],
            "limits": {
                "per_guest": JUKEBOX_MAX_PER_GUEST,
                "cooldown": JUKEBOX_COOLDOWN_SECONDS,
            },
        }


JUKEBOX = Jukebox()


# ----------------------------------------------------------------- web


def fetch_art(path: str) -> tuple[bytes, str]:
    if not path.startswith(ART_PATH_PREFIXES) or ".." in path:
        raise JukeboxError("Not artwork.", HTTPStatus.NOT_FOUND)
    try:
        with urllib.request.urlopen(
            f"http://{LMS_HOST}:{LMS_PORT}{path}", timeout=LMS_TIMEOUT_SECONDS
        ) as response:
            kind = response.headers.get("Content-Type", "image/jpeg")
            if not kind.startswith("image/"):
                raise JukeboxError("Not artwork.", HTTPStatus.NOT_FOUND)
            return response.read(4 * 1024 * 1024), kind
    except (urllib.error.URLError, OSError) as exc:
        raise JukeboxError("Artwork unavailable.", HTTPStatus.NOT_FOUND) from exc


def host_names_this_pi(host_header: str) -> bool:
    """An IP literal, localhost or this machine's name (DNS rebinding guard)."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    own = socket.gethostname().lower()
    return host in {"localhost", own, f"{own}.local"}


def same_origin(origin: str | None, host_header: str | None) -> bool:
    """A browser POST must come from this page itself."""
    origin = (origin or "").strip()
    if not origin:
        return True
    parsed = urllib.parse.urlsplit(origin)
    host = (host_header or "").strip().lower()
    return (
        parsed.scheme in {"http", "https"}
        and bool(host)
        and host_names_this_pi(host)
        and parsed.netloc.lower() == host
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "Jukebox/1.0"
    timeout = REQUEST_TIMEOUT_SECONDS

    def guest_id(self) -> str:
        return str(self.client_address[0])

    def send_bytes(self, body: bytes, kind: str, status: HTTPStatus = HTTPStatus.OK,
                   cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_bytes(json.dumps(payload).encode("utf-8"), "application/json", status)

    def send_error_json(self, error: JukeboxError) -> None:
        self.send_json({"ok": False, "error": str(error)}, error.status)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self.send_bytes(page().encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/state":
                self.send_json(JUKEBOX.state(self.guest_id()))
            elif parsed.path == "/api/search":
                self.send_json(JUKEBOX.search(self.guest_id(), query.get("q", [""])[0]))
            elif parsed.path == "/art":
                body, kind = fetch_art(query.get("p", [""])[0])
                self.send_bytes(body, kind, cache="max-age=86400")
            else:
                raise JukeboxError("Not found.", HTTPStatus.NOT_FOUND)
        except JukeboxError as error:
            self.send_error_json(error)

    def do_POST(self) -> None:
        try:
            if self.path != "/api/add":
                raise JukeboxError("Not found.", HTTPStatus.NOT_FOUND)
            if not same_origin(self.headers.get("Origin"), self.headers.get("Host")):
                raise JukeboxError("Cross-site request refused.", HTTPStatus.FORBIDDEN)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_REQUEST_BODY_BYTES:
                raise JukeboxError("Bad request.")
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise JukeboxError("Bad request.") from None
            if not isinstance(payload, dict):
                raise JukeboxError("Bad request.")
            self.send_json(JUKEBOX.add(self.guest_id(), payload.get("id", "")))
        except JukeboxError as error:
            self.send_error_json(error)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Searches carry what guests typed; keep only errors in the journal.
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            print(f"{self.address_string()} {fmt % args}", flush=True)


def page() -> str:
    title = (
        JUKEBOX_TITLE.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return PAGE.replace("{{TITLE}}", title)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#111114">
<title>{{TITLE}}</title>
<style>
:root {
  --bg: #f6f5f2; --card: #ffffff; --text: #18181b; --muted: #6b6b73;
  --line: #e4e2dc; --accent: #b4532a; --accent-text: #ffffff; --mine: #2f6f4f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111114; --card: #1b1b20; --text: #f1f0ec; --muted: #9c9ba3;
    --line: #2b2b31; --accent: #e0814f; --accent-text: #111114; --mine: #6fc39a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  -webkit-text-size-adjust: 100%;
}
main { max-width: 640px; margin: 0 auto; padding: 16px 16px 96px; }
h1 { font-size: 1.35rem; margin: 8px 0 16px; letter-spacing: -0.01em; }
h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em;
     color: var(--muted); margin: 24px 0 8px; font-weight: 600; }
.now { display: flex; gap: 14px; align-items: center; background: var(--card);
       border: 1px solid var(--line); border-radius: 16px; padding: 12px; }
.now img, .now .ph { width: 84px; height: 84px; border-radius: 10px; flex: none; }
.ph { background: var(--line); }
.now .t { font-weight: 650; font-size: 1.05rem; }
.sub { color: var(--muted); font-size: 0.9rem; }
.bar { height: 4px; background: var(--line); border-radius: 2px; margin-top: 10px; overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--accent); width: 0; }
form { display: flex; gap: 8px; position: sticky; top: 0; padding: 8px 0;
       background: var(--bg); z-index: 2; }
input[type=search] { flex: 1; min-width: 0; font: inherit; padding: 12px 14px;
  border-radius: 12px; border: 1px solid var(--line); background: var(--card); color: var(--text); }
button { font: inherit; font-weight: 600; border: 0; border-radius: 12px;
         padding: 12px 16px; background: var(--accent); color: var(--accent-text); cursor: pointer; }
button:disabled { opacity: 0.5; }
ul { list-style: none; margin: 0; padding: 0; }
li { display: flex; gap: 12px; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--line); }
li img, li .ph { width: 48px; height: 48px; border-radius: 8px; flex: none; object-fit: cover; }
li .txt { flex: 1; min-width: 0; }
li .txt div { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
li button { padding: 8px 14px; flex: none; }
.mine { color: var(--mine); font-size: 0.75rem; font-weight: 700; margin-left: 6px; }
.empty { color: var(--muted); padding: 12px 0; }
.note { color: var(--muted); font-size: 0.85rem; margin-top: 8px; }
#toast { position: fixed; left: 16px; right: 16px; bottom: calc(16px + env(safe-area-inset-bottom));
  max-width: 608px; margin: 0 auto; background: var(--text); color: var(--bg);
  padding: 12px 16px; border-radius: 12px; opacity: 0; transform: translateY(8px);
  transition: opacity .2s, transform .2s; pointer-events: none; z-index: 3; }
#toast.show { opacity: 1; transform: none; }
</style>
</head>
<body>
<main>
  <h1>{{TITLE}}</h1>
  <div class="now" id="now"><div class="ph"></div><div><div class="t">Loading…</div></div></div>

  <h2>Add a track</h2>
  <form id="f" autocomplete="off">
    <input type="search" id="q" placeholder="Artist, album or song" enterkeyhint="search" maxlength="100">
    <button id="go">Search</button>
  </form>
  <ul id="results"></ul>
  <p class="note" id="rules"></p>

  <h2>Up next</h2>
  <ul id="upcoming"><li class="empty">Nothing queued yet.</li></ul>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script>
const $ = (id) => document.getElementById(id);
let toastTimer;
function toast(text) {
  const t = $("toast"); t.textContent = text; t.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 3500);
}
function el(tag, props, children) {
  const n = document.createElement(tag);
  Object.assign(n, props || {});
  (children || []).forEach((c) => n.append(c));
  return n;
}
function art(src, size) {
  if (!src) return el("div", {className: "ph"});
  const i = el("img", {src, alt: "", loading: "lazy", width: size, height: size});
  i.onerror = () => i.replaceWith(el("div", {className: "ph"}));
  return i;
}
async function api(path, options) {
  const r = await fetch(path, options);
  let body = {};
  try { body = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(body.error || "Something went wrong.");
  return body;
}
function line(text, cls) { return el("div", {className: cls || "", textContent: text}); }

function renderState(s) {
  const now = $("now"); now.replaceChildren();
  if (s.current) {
    const pct = s.duration ? Math.min(100, 100 * (s.elapsed || 0) / s.duration) : 0;
    const bar = el("div", {className: "bar"}, [el("i")]);
    bar.firstChild.style.width = pct + "%";
    const sub = [s.current.artist, s.current.album].filter(Boolean).join(" · ");
    const state = s.mode === "play" ? "" : (s.mode === "pause" ? "Paused · " : "Stopped · ");
    now.append(art(s.current.image, 84), el("div", {style: "flex:1;min-width:0"}, [
      line(s.current.title, "t"), line(state + sub, "sub"), bar]));
  } else {
    now.append(el("div", {className: "ph"}), el("div", {}, [
      line("Nothing playing", "t"), line("Search below and add the first track.", "sub")]));
  }
  const up = $("upcoming"); up.replaceChildren();
  if (!s.upcoming.length) up.append(el("li", {className: "empty", textContent: "Nothing queued yet."}));
  s.upcoming.forEach((t) => {
    const name = line(t.title);
    if (t.mine) name.append(el("span", {className: "mine", textContent: "YOURS"}));
    up.append(el("li", {}, [art(t.image, 48), el("div", {className: "txt"}, [
      name, line([t.artist, t.album].filter(Boolean).join(" · "), "sub")])]));
  });
  $("rules").textContent = `You can have ${s.limits.per_guest} tracks waiting at a time.`;
}
async function refresh() {
  try { renderState(await api("api/state")); }
  catch (e) { $("now").replaceChildren(el("div", {className: "ph"}),
                el("div", {}, [line("Music server unavailable", "t"), line(e.message, "sub")])); }
}
function fmt(sec) {
  if (!sec) return "";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return ` · ${m}:${String(s).padStart(2, "0")}`;
}
async function add(id, btn) {
  btn.disabled = true;
  try {
    const r = await api("api/add", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({id})});
    toast(r.message); btn.textContent = "Added"; refresh();
  } catch (e) { toast(e.message); btn.disabled = false; }
}
$("f").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("q").value.trim();
  if (q.length < 2) return;
  $("q").blur();
  const list = $("results");
  list.replaceChildren(el("li", {className: "empty", textContent: "Searching…"}));
  $("go").disabled = true;
  try {
    const r = await api("api/search?q=" + encodeURIComponent(q));
    list.replaceChildren();
    if (!r.results.length) list.append(el("li", {className: "empty", textContent: "No tracks found."}));
    r.results.forEach((t) => {
      const b = el("button", {textContent: "Add", type: "button"});
      b.addEventListener("click", () => add(t.id, b));
      list.append(el("li", {}, [art(t.image, 48), el("div", {className: "txt"}, [
        line(t.name), line((t.subtitle || "") + fmt(t.duration), "sub")]), b]));
    });
  } catch (e) {
    list.replaceChildren(el("li", {className: "empty", textContent: e.message}));
  } finally { $("go").disabled = false; }
});
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
</script>
</body>
</html>
"""


def main() -> int:
    server = ThreadingHTTPServer((JUKEBOX_HOST, JUKEBOX_PORT), Handler)
    server.daemon_threads = True
    print(
        f"Jukebox on http://{JUKEBOX_HOST}:{JUKEBOX_PORT} "
        f"(LMS {LMS_HOST}:{LMS_PORT}, player {JUKEBOX_PLAYER or 'auto'}, "
        f"services {','.join(JUKEBOX_SERVICES)})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
