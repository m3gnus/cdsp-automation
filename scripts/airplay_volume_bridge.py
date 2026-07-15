#!/usr/bin/env python3
"""Map Shairport Sync's AirPlay slider onto the CamillaDSP master fader."""

from __future__ import annotations

import json
import math
import os
import socket
import sys
import tempfile
import time
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


def set_client_volume(client, airplay_db: float) -> dict:
    # Keep the Shairport callback path stdlib-only.  The callback executes the
    # copy in /usr/local/libexec merely to send a datagram; deployment helpers
    # used by the daemon live beside the daemon script and may not be installed
    # beside that callback copy.
    from speaker_profiles import audio_control_lock, require_audio_unmute_allowed

    mapped_db, muted = map_airplay_volume(
        airplay_db, VOLUME_MIN_DB, VOLUME_MAX_DB, VOLUME_CURVE
    )
    with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
        if not muted:
            require_audio_unmute_allowed(AUDIO_READY_PATH)
        client.volume.set_main_volume(mapped_db)
        client.volume.set_main_mute(muted)
    result = {
        "ok": True,
        "airplay_db": airplay_db,
        "camilla_db": mapped_db,
        "muted": muted,
        "updated_at": time.time(),
    }
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
        client.sendto(airplay_db.encode("ascii"), str(SOCKET_PATH))
    finally:
        client.close()


def run_daemon() -> int:
    """Coalesce slider events and keep slow DSP RPC work outside Shairport."""
    map_airplay_volume(-30, VOLUME_MIN_DB, VOLUME_MAX_DB, VOLUME_CURVE)
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(SOCKET_PATH))
    SOCKET_PATH.chmod(0o666)
    camilla = None
    try:
        while True:
            payload = server.recv(128)
            server.setblocking(False)
            try:
                while True:
                    payload = server.recv(128)
            except BlockingIOError:
                pass
            finally:
                server.setblocking(True)
            try:
                if camilla is None:
                    if CamillaClient is None:
                        raise RuntimeError("pycamilladsp is not installed")
                    camilla = CamillaClient(CDSP_HOST, CDSP_PORT)
                    camilla.connect()
                set_client_volume(
                    camilla, float(payload.decode("ascii").split(",", 1)[0])
                )
            except Exception as exc:
                try:
                    if camilla is not None:
                        camilla.disconnect()
                except Exception:
                    pass
                camilla = None
                write_status(
                    {"ok": False, "error": str(exc), "updated_at": time.time()}
                )
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
    if len(argv) == 2 and argv[0] == "--notify":
        try:
            notify(argv[1])
            return 0
        except Exception as exc:
            print(f"AirPlay volume notification failed: {exc}", file=sys.stderr)
            return 1
    if len(argv) != 1:
        print(
            "usage: airplay_volume_bridge.py [--daemon | --notify] AIRPLAY_DB",
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
