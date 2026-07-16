#!/usr/bin/env python3
"""Keep network-player volume controls aligned with CamillaDSP's master fader.

AirPlay can report its sender volume but cannot accept an exact volume value back.
Spotify Connect supports both directions when used with the patched librespot build.
"""

from __future__ import annotations

import grp
import json
import math
import os
import socket
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

try:
    from camilladsp import CamillaClient
except ImportError:  # Mapping/unit tests do not need the network client.
    CamillaClient = None


CDSP_HOST = os.environ.get("CDSP_HOST", "127.0.0.1")
CDSP_PORT = int(os.environ.get("CDSP_PORT", "1234"))
# CamillaGUI's master slider spans -50..0 dB. Keeping the AirPlay bridge on
# that same linear range makes the two slider positions track one-to-one.
VOLUME_MIN_DB = float(os.environ.get("AIRPLAY_VOLUME_MIN_DB", "-50"))
VOLUME_MAX_DB = float(os.environ.get("AIRPLAY_VOLUME_MAX_DB", "0"))
VOLUME_CURVE = float(os.environ.get("AIRPLAY_VOLUME_CURVE", "1.0"))
STATUS_PATH = Path(
    os.environ.get(
        "AIRPLAY_VOLUME_STATUS_PATH", "/run/airplay-volume-bridge/status.json"
    )
)
AUDIO_CONTROL_LOCK_PATH = Path(
    os.environ.get(
        "AUDIO_CONTROL_LOCK_PATH", "/var/lib/cdsp-automation/audio-control.lock"
    )
)
AUDIO_READY_PATH = Path(
    os.environ.get(
        "AUDIO_READY_PATH", "/run/cdsp-source-switcher/audio-ready.json"
    )
)
SOCKET_PATH = Path(
    os.environ.get(
        "AIRPLAY_VOLUME_SOCKET_PATH", "/run/airplay-volume-bridge/input.sock"
    )
)
SPOTIFY_COMMAND_SOCKET_PATH = Path(
    os.environ.get(
        "SPOTIFY_VOLUME_COMMAND_SOCKET_PATH",
        "/run/raspotify/uglan-volume.sock",
    )
)
POLL_INTERVAL = float(os.environ.get("VOLUME_SYNC_POLL_INTERVAL", "0.25"))
COMMAND_RETRY_SECONDS = float(
    os.environ.get("VOLUME_SYNC_COMMAND_RETRY_SECONDS", "1.0")
)
COMMAND_ACK_TIMEOUT = float(
    os.environ.get("VOLUME_SYNC_COMMAND_ACK_TIMEOUT", "3.0")
)
HEARTBEAT_INTERVAL = float(
    os.environ.get("VOLUME_SYNC_HEARTBEAT_SECONDS", "10.0")
)
VOLUME_SYNC_GROUP = os.environ.get("VOLUME_SYNC_GROUP", "audio")
AIRPLAY_ACTIVE_PATH = Path(
    os.environ.get(
        "AIRPLAY_ACTIVE_PATH", "/run/airplay-volume-bridge/playback-active"
    )
)
AIRPLAY_HANDOFF_TIMEOUT = float(os.environ.get("AIRPLAY_HANDOFF_TIMEOUT", "10.0"))
AIRPLAY_RELEASE_DELAY = float(os.environ.get("AIRPLAY_RELEASE_DELAY", "1.25"))
LMS_HOST = os.environ.get("LMS_HOST", "127.0.0.1")
LMS_PORT = int(os.environ.get("LMS_PORT", "9000"))
LMS_PLAYER_NAMES = tuple(
    name.strip()
    for name in os.environ.get(
        "AIRPLAY_INTERRUPTED_LMS_PLAYERS", "uglan,uglan-stereo"
    ).split(",")
    if name.strip()
)


def map_airplay_volume(
    airplay_db: float,
    minimum_db: float = -50.0,
    maximum_db: float = 0.0,
    curve: float = 1.0,
) -> tuple[float, bool]:
    """Map AirPlay's -30..0 dB range onto CamillaGUI's -50..0 dB slider."""
    if not all(
        math.isfinite(value) for value in (airplay_db, minimum_db, maximum_db, curve)
    ):
        raise ValueError("volume values must be finite")
    if not -120.0 <= minimum_db <= -20.0:
        raise ValueError("minimum volume must be between -120 and -20 dB")
    if not minimum_db < maximum_db <= 0.0:
        raise ValueError(
            "maximum volume must be above minimum and no greater than 0 dB"
        )
    if not 0.2 <= curve <= 4.0:
        raise ValueError("volume curve must be between 0.2 and 4")
    if airplay_db <= -144.0:
        return minimum_db, True
    position = (max(-30.0, min(0.0, airplay_db)) + 30.0) / 30.0
    mapped = minimum_db + (maximum_db - minimum_db) * position**curve
    return round(mapped, 4), False


def map_spotify_volume(
    spotify_volume: int,
    minimum_db: float = -50.0,
    maximum_db: float = 0.0,
) -> tuple[float, bool]:
    """Map Spotify Connect's unsigned 16-bit volume onto the master fader."""
    if not isinstance(spotify_volume, int) or not 0 <= spotify_volume <= 65535:
        raise ValueError("Spotify volume must be an integer between 0 and 65535")
    if not math.isfinite(minimum_db) or not math.isfinite(maximum_db):
        raise ValueError("volume values must be finite")
    if not -120.0 <= minimum_db <= -20.0 or not minimum_db < maximum_db <= 0:
        raise ValueError("invalid CamillaDSP volume range")
    position = spotify_volume / 65535.0
    mapped = minimum_db + (maximum_db - minimum_db) * position
    return round(mapped, 4), spotify_volume == 0


def map_camilla_to_spotify(
    camilla_db: float,
    muted: bool,
    minimum_db: float = -50.0,
    maximum_db: float = 0.0,
) -> int:
    """Convert the master fader back to Spotify's exact Connect volume value."""
    if muted:
        return 0
    if not all(math.isfinite(value) for value in (camilla_db, minimum_db, maximum_db)):
        raise ValueError("volume values must be finite")
    if not minimum_db < maximum_db:
        raise ValueError("invalid CamillaDSP volume range")
    position = (max(minimum_db, min(maximum_db, camilla_db)) - minimum_db) / (
        maximum_db - minimum_db
    )
    # Keep a non-muted master distinct from Spotify's special zero/mute value.
    return max(1, min(65535, round(position * 65535)))


def write_status(payload: dict) -> None:
    """Best-effort atomic status for the control UI."""
    temporary: Path | None = None
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=STATUS_PATH.parent, delete=False
        ) as handle:
            json.dump(payload, handle)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.chmod(0o644)
        temporary.replace(STATUS_PATH)
        temporary = None
    except OSError:
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def set_mapped_volume(
    client, mapped_db: float, muted: bool, *, source: str, source_volume: float | int
) -> dict:
    # Keep the Shairport callback path stdlib-only.  The callback executes the
    # copy in /usr/local/libexec merely to send a datagram; deployment helpers
    # used by the daemon live beside the daemon script and may not be installed
    # beside that callback copy.
    from speaker_profiles import audio_control_lock, require_audio_unmute_allowed

    with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
        if not muted:
            require_audio_unmute_allowed(AUDIO_READY_PATH)
        client.volume.set_main_volume(mapped_db)
        client.volume.set_main_mute(muted)
    result = {
        "ok": True,
        "source": source,
        "source_volume": source_volume,
        "camilla_db": mapped_db,
        "muted": muted,
        "updated_at": time.time(),
    }
    return result


def set_client_volume(client, airplay_db: float, *, persist: bool = True) -> dict:
    mapped_db, muted = map_airplay_volume(
        airplay_db, VOLUME_MIN_DB, VOLUME_MAX_DB, VOLUME_CURVE
    )
    result = set_mapped_volume(
        client,
        mapped_db,
        muted,
        source="airplay",
        source_volume=airplay_db,
    )
    result["airplay_db"] = airplay_db
    if persist:
        write_status(result)
    return result


def set_spotify_volume(client, spotify_volume: int, *, persist: bool = True) -> dict:
    mapped_db, muted = map_spotify_volume(
        spotify_volume, VOLUME_MIN_DB, VOLUME_MAX_DB
    )
    result = set_mapped_volume(
        client,
        mapped_db,
        muted,
        source="spotify",
        source_volume=spotify_volume,
    )
    result["spotify_volume"] = spotify_volume
    if persist:
        write_status(result)
    return result


def apply_volume(airplay_db: float) -> dict:
    if CamillaClient is None:
        raise RuntimeError("pycamilladsp is not installed")
    client = CamillaClient(CDSP_HOST, CDSP_PORT)
    try:
        client.connect()
        return set_client_volume(client, airplay_db)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def notify(airplay_db: str) -> None:
    """Send a non-blocking local datagram; safe for Shairport's callback path."""
    float(airplay_db.split(",", 1)[0])
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.settimeout(0.1)
        client.sendto(f"airplay:{airplay_db}".encode("ascii"), str(SOCKET_PATH))
    finally:
        client.close()


def notify_airplay_session(active: bool) -> None:
    """Ask the daemon to hand scheduled playback to or from AirPlay."""
    target = "active" if active else ""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.settimeout(0.1)
        event = "start" if active else "stop"
        client.sendto(f"airplay_session:{event}".encode("ascii"), str(SOCKET_PATH))
    finally:
        client.close()
    deadline = time.monotonic() + AIRPLAY_HANDOFF_TIMEOUT
    while time.monotonic() < deadline:
        try:
            state = AIRPLAY_ACTIVE_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            state = ""
        if state == target:
            return
        time.sleep(0.05)
    action = "start" if active else "stop"
    raise TimeoutError(f"AirPlay {action} handoff was not acknowledged")


def _write_airplay_state(value: str) -> None:
    AIRPLAY_ACTIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=AIRPLAY_ACTIVE_PATH.parent,
        prefix=f".{AIRPLAY_ACTIVE_PATH.name}.",
        delete=False,
    ) as handle:
        handle.write(value + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(0o644)
    temporary.replace(AIRPLAY_ACTIVE_PATH)


def _lms_request(player: str, terms: list[object]) -> dict:
    body = json.dumps(
        {"id": 1, "method": "slim.request", "params": [player, terms]}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://{LMS_HOST}:{LMS_PORT}/jsonrpc.js",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("result", {}) if isinstance(payload, dict) else {}


def interrupt_scheduled_playback() -> None:
    """Inhibit the scheduler, stop its players, then release ALSA to AirPlay."""
    _write_airplay_state("starting")
    try:
        players = _lms_request("", ["players", "0", "100"]).get(
            "players_loop", []
        )
        for player in players or []:
            if str(player.get("name", "")).strip() in LMS_PLAYER_NAMES:
                player_id = str(player.get("playerid", "")).strip()
                if player_id:
                    _lms_request(player_id, ["stop"])
        # Squeezelite closes uglan_main after its one-second idle timeout.
        time.sleep(max(0.0, AIRPLAY_RELEASE_DELAY))
        _write_airplay_state("active")
    except Exception:
        AIRPLAY_ACTIVE_PATH.unlink(missing_ok=True)
        raise


def finish_airplay_playback() -> None:
    """Let the scheduler resume the currently active wall-clock event."""
    AIRPLAY_ACTIVE_PATH.unlink(missing_ok=True)


def notify_spotify(environ: dict[str, str] | None = None) -> None:
    """Forward librespot's volume_changed event to the persistent daemon."""
    environ = os.environ if environ is None else environ
    if environ.get("PLAYER_EVENT") != "volume_changed":
        return
    volume = int(environ["VOLUME"])
    map_spotify_volume(volume, VOLUME_MIN_DB, VOLUME_MAX_DB)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.settimeout(0.1)
        client.sendto(f"spotify:{volume}".encode("ascii"), str(SOCKET_PATH))
    finally:
        client.close()


def send_spotify_volume(command_id: int, volume: int) -> None:
    """Request a logical Connect volume and identify the eventual ack."""
    if command_id < 1:
        raise ValueError("Spotify command id must be positive")
    if not 0 <= volume <= 65535:
        raise ValueError("Spotify volume must be between 0 and 65535")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.settimeout(0.1)
        client.sendto(
            f"{command_id}:{volume}".encode("ascii"),
            str(SPOTIFY_COMMAND_SOCKET_PATH),
        )
    finally:
        client.close()


def read_camilla_volume(client) -> tuple[float, bool]:
    return round(float(client.volume.main_volume()), 4), bool(
        client.volume.main_mute()
    )


def read_mirrorable_camilla_volume(client) -> tuple[float, bool] | None:
    """Read a stable master state, or pause while a config transition owns it."""
    from speaker_profiles import audio_control_lock, require_audio_unmute_allowed

    with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
        try:
            require_audio_unmute_allowed(AUDIO_READY_PATH)
        except RuntimeError:
            return None
        return read_camilla_volume(client)


def secure_socket(path: Path) -> None:
    """Restrict local volume control to the installation's audio group."""
    gid = grp.getgrnam(VOLUME_SYNC_GROUP).gr_gid
    os.chown(path, -1, gid)
    path.chmod(0o660)


class SpotifyCommandTracker:
    """Keep the newest desired Spotify state pending until librespot acks it."""

    def __init__(self) -> None:
        self.next_id = max(1, time.time_ns())
        self.pending: dict[str, int | float | tuple[float, bool]] | None = None
        self.last_ack_at = 0.0
        self.last_ack_volume: int | None = None

    def queue(
        self, volume: int, camilla_state: tuple[float, bool], *, now: float
    ) -> int:
        self.next_id += 1
        self.pending = {
            "id": self.next_id,
            "volume": volume,
            "camilla_state": camilla_state,
            "queued_at": now,
            "last_sent_at": 0.0,
            "attempts": 0,
        }
        return self.next_id

    def should_send(self, now: float) -> bool:
        return bool(
            self.pending
            and now - float(self.pending["last_sent_at"])
            >= COMMAND_RETRY_SECONDS
        )

    def mark_sent(self, now: float) -> None:
        if self.pending:
            self.pending["last_sent_at"] = now
            self.pending["attempts"] = int(self.pending["attempts"]) + 1

    def acknowledge(self, command_id: int, volume: int, *, now: float) -> bool:
        if not self.pending:
            return False
        if (
            int(self.pending["id"]) != command_id
            or int(self.pending["volume"]) != volume
        ):
            return False
        self.pending = None
        self.last_ack_at = now
        self.last_ack_volume = volume
        return True

    def needs_heartbeat(self, now: float) -> bool:
        return self.pending is None and now - self.last_ack_at >= HEARTBEAT_INTERVAL

    def healthy(self, now: float) -> bool:
        if self.pending and now - float(self.pending["queued_at"]) > COMMAND_ACK_TIMEOUT:
            return False
        return bool(
            self.last_ack_at
            and now - self.last_ack_at
            <= HEARTBEAT_INTERVAL + COMMAND_ACK_TIMEOUT
        )


def parse_bridge_message(payload: bytes) -> tuple[str, tuple[int, ...] | str]:
    message = payload.decode("ascii")
    if message.startswith("spotify_ack:"):
        fields = message.split(":")
        if len(fields) != 3:
            raise ValueError("invalid Spotify acknowledgement")
        return "spotify_ack", (int(fields[1]), int(fields[2]))
    if ":" in message:
        source, value = message.split(":", 1)
        return source, value
    return "airplay", message


def run_daemon() -> int:
    """Apply source events and mirror external master changes into Spotify."""
    map_airplay_volume(-30, VOLUME_MIN_DB, VOLUME_MAX_DB, VOLUME_CURVE)
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(SOCKET_PATH))
    secure_socket(SOCKET_PATH)
    server.settimeout(POLL_INTERVAL)
    camilla = None
    last_camilla: tuple[float, bool] | None = None
    spotify_sync = SpotifyCommandTracker()
    status: dict = {
        "ok": True,
        "updated_at": time.time(),
        "airplay": {
            "source_to_cdsp": True,
            "cdsp_to_source": False,
            "reason": "AirPlay does not expose exact receiver-to-sender volume setting",
        },
        "spotify": {"source_to_cdsp": True, "cdsp_to_source": True},
    }
    try:
        while True:
            try:
                try:
                    payload = server.recv(128)
                except socket.timeout:
                    payload = b""
                payloads = [payload] if payload else []
                if payload:
                    server.setblocking(False)
                    try:
                        while len(payloads) < 64:
                            payloads.append(server.recv(128))
                    except BlockingIOError:
                        pass
                    finally:
                        server.settimeout(POLL_INTERVAL)
                volume_payloads = []
                for payload in payloads:
                    source, value = parse_bridge_message(payload)
                    if source != "airplay_session":
                        volume_payloads.append(payload)
                        continue
                    if value == "start":
                        interrupt_scheduled_playback()
                        status["airplay"]["playback"] = "active"
                    elif value == "stop":
                        finish_airplay_playback()
                        status["airplay"]["playback"] = "idle"
                    else:
                        raise ValueError(f"unsupported AirPlay session event: {value}")
                payloads = volume_payloads
                if camilla is None:
                    if CamillaClient is None:
                        raise RuntimeError("pycamilladsp is not installed")
                    camilla = CamillaClient(CDSP_HOST, CDSP_PORT)
                    camilla.connect()
                for payload in payloads:
                    source, value = parse_bridge_message(payload)
                    now = time.time()
                    if source == "spotify_ack":
                        command_id, ack_volume = value
                        if spotify_sync.acknowledge(command_id, ack_volume, now=now):
                            status["spotify"].update(
                                {
                                    "command_socket": True,
                                    "last_ack_at": now,
                                    "last_cdsp_volume": ack_volume,
                                }
                            )
                            status["spotify"].pop("error", None)
                        continue
                    if source == "airplay":
                        result = set_client_volume(
                            camilla,
                            float(str(value).split(",", 1)[0]),
                            persist=False,
                        )
                        status["airplay"]["last_source_volume_db"] = result[
                            "airplay_db"
                        ]
                    elif source == "spotify":
                        result = set_spotify_volume(
                            camilla, int(str(value)), persist=False
                        )
                        status["spotify"]["last_source_volume"] = result[
                            "spotify_volume"
                        ]
                    else:
                        raise ValueError(f"unsupported volume source: {source}")
                    status.update(result)
                    last_camilla = (result["camilla_db"], result["muted"])
                    spotify_volume = (
                        int(result["spotify_volume"])
                        if source == "spotify"
                        else map_camilla_to_spotify(
                            last_camilla[0],
                            last_camilla[1],
                            VOLUME_MIN_DB,
                            VOLUME_MAX_DB,
                        )
                    )
                    # This also supersedes any older command still queued while
                    # librespot was disconnected. Silent application cannot echo.
                    spotify_sync.queue(spotify_volume, last_camilla, now=now)

                current = read_mirrorable_camilla_volume(camilla)
                if current is None:
                    status["spotify"]["paused_for_transition"] = True
                else:
                    status["spotify"].pop("paused_for_transition", None)
                now = time.time()
                if current is not None and (
                    last_camilla is None or current != last_camilla
                ):
                    spotify_volume = map_camilla_to_spotify(
                        current[0], current[1], VOLUME_MIN_DB, VOLUME_MAX_DB
                    )
                    spotify_sync.queue(spotify_volume, current, now=now)
                    last_camilla = current
                    status.update(
                        {
                            "ok": True,
                            "source": "camilladsp",
                            "camilla_db": current[0],
                            "muted": current[1],
                        }
                    )
                elif current is not None and spotify_sync.needs_heartbeat(now):
                    spotify_sync.queue(
                        map_camilla_to_spotify(
                            current[0], current[1], VOLUME_MIN_DB, VOLUME_MAX_DB
                        ),
                        current,
                        now=now,
                    )

                if spotify_sync.should_send(now):
                    pending = spotify_sync.pending
                    assert pending is not None
                    try:
                        send_spotify_volume(
                            int(pending["id"]), int(pending["volume"])
                        )
                        spotify_sync.mark_sent(now)
                    except OSError as exc:
                        status["spotify"].update(
                            {
                                "receiver_socket": False,
                                "command_socket": False,
                                "state": "unavailable",
                                "error": str(exc),
                            }
                        )
                receiver_socket = SPOTIFY_COMMAND_SOCKET_PATH.is_socket()
                command_ready = spotify_sync.healthy(now)
                status["spotify"].update(
                    {
                        "receiver_socket": receiver_socket,
                        "command_socket": command_ready,
                        "state": (
                            "live"
                            if command_ready
                            else "idle"
                            if receiver_socket
                            else "unavailable"
                        ),
                    }
                )
                if command_ready:
                    status["spotify"].pop("reason", None)
                    status["spotify"].pop("error", None)
                elif receiver_socket:
                    status["spotify"]["reason"] = (
                        "waiting for an active Spotify Connect session"
                    )
                    status["spotify"].pop("error", None)
                else:
                    status["spotify"].pop("reason", None)
                    status["spotify"].setdefault(
                        "error", "Spotify receiver command socket is unavailable"
                    )
                status.pop("error", None)
                status["updated_at"] = time.time()
                write_status(status)
            except Exception as exc:
                try:
                    if camilla is not None:
                        camilla.disconnect()
                except Exception:
                    pass
                camilla = None
                status.update(
                    {"ok": False, "error": str(exc), "updated_at": time.time()}
                )
                write_status(status)
                time.sleep(min(1.0, max(0.05, POLL_INTERVAL)))
    finally:
        try:
            if camilla is not None:
                camilla.disconnect()
        except Exception:
            pass
        server.close()
        SOCKET_PATH.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--daemon"]:
        return run_daemon()
    if argv == ["--notify-spotify"]:
        try:
            notify_spotify()
            return 0
        except Exception as exc:
            print(f"Spotify volume notification failed: {exc}", file=sys.stderr)
            return 1
    if argv in (["--airplay-start"], ["--airplay-stop"]):
        try:
            notify_airplay_session(argv == ["--airplay-start"])
            return 0
        except Exception as exc:
            print(f"AirPlay playback handoff failed: {exc}", file=sys.stderr)
            return 1
    if len(argv) == 2 and argv[0] == "--notify":
        try:
            notify(argv[1])
            return 0
        except Exception as exc:
            print(f"AirPlay volume notification failed: {exc}", file=sys.stderr)
            return 1
    if len(argv) != 1:
        print(
            "usage: airplay_volume_bridge.py [--daemon | --notify AIRPLAY_DB | --notify-spotify | --airplay-start | --airplay-stop]",
            file=sys.stderr,
        )
        return 2
    try:
        airplay_db = float(argv[0].split(",", 1)[0])
        result = apply_volume(airplay_db)
        print(
            f"AirPlay {result['airplay_db']:.2f} -> CamillaDSP "
            f"{result['camilla_db']:.2f} dB, mute={result['muted']}",
            flush=True,
        )
        return 0
    except Exception as exc:
        write_status({"ok": False, "error": str(exc), "updated_at": time.time()})
        print(f"AirPlay volume bridge failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
