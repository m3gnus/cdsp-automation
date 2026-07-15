#!/usr/bin/env python3
"""CamillaDSP USB/Bluetooth HID remote control."""

from __future__ import annotations

import asyncio
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import evdev
from camilladsp import CamillaClient
from audio_eq import read_audio_state, reset_tone_bands, update_tone_band
from speaker_profiles import (
    BUILTIN_SPEAKERS,
    audio_control_lock,
    read_speaker_selection,
    require_audio_unmute_allowed,
    resolve_profile_audio_path,
    speaker_selection_lock,
)


# ====================== CONFIGURATION ======================


REMOTE_NAME = os.environ.get("REMOTE_NAME", "HID Remote01 Keyboard")
CDSP_HOST = os.environ.get("CDSP_HOST", "127.0.0.1")
CDSP_PORT = int(os.environ.get("CDSP_PORT", "1234"))
DEVICE_RETRY_SECONDS = max(
    float(os.environ.get("REMOTE_DEVICE_RETRY_SECONDS", "2")), 0.1
)
STATUS_LOG_SECONDS = max(
    float(os.environ.get("REMOTE_STATUS_LOG_SECONDS", "300")), 1.0
)

TONE_MIN = float(os.environ.get("REMOTE_TONE_MIN", "-6"))
TONE_MAX = float(os.environ.get("REMOTE_TONE_MAX", "6"))
TONE_STEP = float(os.environ.get("REMOTE_TONE_STEP", "0.5"))
AUDIO_EQ_PATH = Path(
    os.environ.get("AUDIO_EQ_PATH", "/var/lib/cdsp-automation/audio-eq.json")
)
SPEAKER_SELECTION_PATH = Path(
    os.environ.get(
        "SPEAKER_SELECTION_PATH", "/var/lib/cdsp-automation/speaker-selection.json"
    )
)
SPEAKER_AUDIO_DIR = Path(
    os.environ.get("SPEAKER_AUDIO_DIR", "/var/lib/cdsp-automation/speaker-audio")
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

VOLUME_MIN = float(os.environ.get("REMOTE_VOLUME_MIN", "-80"))
VOLUME_MAX = float(os.environ.get("REMOTE_VOLUME_MAX", "0"))
VOLUME_STEP = float(os.environ.get("REMOTE_VOLUME_STEP", "1"))

ENTER_HOLD_SECONDS = float(os.environ.get("REMOTE_ENTER_HOLD_SECONDS", "1"))
RESTART_HOLD_SECONDS = float(os.environ.get("REMOTE_RESTART_HOLD_SECONDS", "1"))
SHUTDOWN_HOLD_SECONDS = float(os.environ.get("REMOTE_SHUTDOWN_HOLD_SECONDS", "10"))
# Fixed Raspberry Pi OS paths. Never derive NOPASSWD targets from PATH or the
# user-controlled EnvironmentFile.
SUDO_BIN = "/usr/bin/sudo"
SYSTEMCTL_BIN = "/usr/bin/systemctl"

KEY_BINDINGS = {
    "VOLUMEDOWN": "KEY_VOLUMEDOWN",
    "VOLUMEUP": "KEY_VOLUMEUP",
    "MUTE": "KEY_MUTE",
    "UP": "KEY_UP",
    "DOWN": "KEY_DOWN",
    "LEFT": "KEY_LEFT",
    "RIGHT": "KEY_RIGHT",
    "ENTER": "KEY_ENTER",
    "POWER": "KEY_POWER",
}


# ====================== GLOBAL STATE ======================

cdsp: CamillaClient | None = None
remote_device = None


# ====================== HELPER FUNCTIONS ======================


def key_matches(keycode: str | list[str], binding: str) -> bool:
    if isinstance(keycode, list):
        return binding in keycode
    return keycode == binding


def find_remote_device():
    """Search for the USB HID remote device by name."""
    print(f"Searching for remote '{REMOTE_NAME}'...", flush=True)
    last_status: tuple[tuple[str, ...], tuple[str, ...]] | None = None
    next_status_log = 0.0

    while True:
        seen: list[str] = []
        problems: list[str] = []
        try:
            paths = evdev.list_devices()
        except OSError as exc:
            now = time.monotonic()
            status = ((), (f"Cannot list input devices: {exc}",))
            if status != last_status or now >= next_status_log:
                print(
                    f"Cannot list input devices: {exc}. "
                    f"Retrying in {DEVICE_RETRY_SECONDS:g} seconds...",
                    flush=True,
                )
                last_status = status
                next_status_log = now + STATUS_LOG_SECONDS
            time.sleep(DEVICE_RETRY_SECONDS)
            continue

        for path in paths:
            try:
                device = evdev.InputDevice(path)
            except OSError as exc:
                problems.append(f"Cannot open {path}: {exc}")
                continue

            seen.append(device.name)
            if device.name == REMOTE_NAME:
                print(f"Found '{REMOTE_NAME}' at {path}", flush=True)
                return device

            try:
                device.close()
            except Exception:
                pass

        now = time.monotonic()
        status = (tuple(sorted(seen)), tuple(sorted(problems)))
        if status != last_status or now >= next_status_log:
            seen_suffix = f" Seen: {', '.join(seen)}" if seen else ""
            problem_suffix = f" Problems: {'; '.join(problems)}" if problems else ""
            print(
                f"Remote '{REMOTE_NAME}' not found. "
                f"Retrying in {DEVICE_RETRY_SECONDS:g} seconds."
                f"{seen_suffix}{problem_suffix}",
                flush=True,
            )
            last_status = status
            next_status_log = now + STATUS_LOG_SECONDS
        time.sleep(DEVICE_RETRY_SECONDS)


def connect_to_camilladsp() -> CamillaClient:
    """Try once to establish a CamillaDSP connection."""
    global cdsp

    print(f"Connecting to CamillaDSP at {CDSP_HOST}:{CDSP_PORT}...", flush=True)
    candidate = CamillaClient(CDSP_HOST, CDSP_PORT)
    try:
        candidate.connect()
    except Exception:
        try:
            candidate.disconnect()
        except Exception:
            pass
        cdsp = None
        raise
    cdsp = candidate
    print("Connected to CamillaDSP successfully.", flush=True)
    return candidate


def ensure_cdsp_connected() -> CamillaClient:
    """Return a connected CamillaDSP client, recreating it after failures."""
    global cdsp

    if cdsp is not None:
        try:
            if cdsp.is_connected():
                return cdsp
        except Exception:
            pass
        try:
            cdsp.disconnect()
        except Exception:
            pass
        cdsp = None
    return connect_to_camilladsp()


def format_db(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}dB"


def current_audio_eq_path() -> tuple[Path, str]:
    selection = read_speaker_selection(
        SPEAKER_SELECTION_PATH, allowed_ids=BUILTIN_SPEAKERS
    )
    return (
        resolve_profile_audio_path(
            SPEAKER_AUDIO_DIR,
            selection["selected"],
            legacy_path=AUDIO_EQ_PATH,
        ),
        selection["selected"],
    )


def adjust_volume(change: float) -> None:
    """Adjust the main volume by the specified amount."""
    try:
        client = ensure_cdsp_connected()
        with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
            current_volume = client.volume.main_volume()
            new_volume = max(VOLUME_MIN, min(VOLUME_MAX, current_volume + change))
            client.volume.set_main_volume(new_volume)
        print(f"Volume: {new_volume:.1f} dB", flush=True)
    except Exception as exc:
        print(f"Error adjusting volume: {exc}", flush=True)


def toggle_mute() -> None:
    """Toggle the mute state."""
    try:
        client = ensure_cdsp_connected()
        with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
            is_muted = client.volume.main_mute()
            if is_muted:
                require_audio_unmute_allowed(AUDIO_READY_PATH)
            client.volume.set_main_mute(not is_muted)
        print(f"Mute: {'ON' if not is_muted else 'OFF'}", flush=True)
    except Exception as exc:
        print(f"Error toggling mute: {exc}", flush=True)


def adjust_tone(parameter: str, change: float) -> None:
    """Adjust bass or treble in the persistent source-independent overlay."""
    try:
        band_id = "low" if parameter == "Bass" else "high"
        with speaker_selection_lock(SPEAKER_SELECTION_PATH):
            audio_path, speaker_id = current_audio_eq_path()
            state = update_tone_band(
                audio_path, band_id, change, TONE_MIN, TONE_MAX
            )
        band = next(item for item in state["bands"] if item["id"] == band_id)
        print(
            f"{parameter} [{speaker_id}]: {band['gain']:+.1f} dB "
            f"(EQ revision {state['revision']})",
            flush=True,
        )
    except Exception as exc:
        print(f"Error adjusting {parameter}: {exc}", flush=True)


def get_current_tone() -> tuple[float | None, float | None]:
    """Get current bass and treble from the persistent overlay."""
    try:
        with speaker_selection_lock(SPEAKER_SELECTION_PATH):
            audio_path, _speaker_id = current_audio_eq_path()
            state = read_audio_state(audio_path)
        bass = next((b["gain"] for b in state["bands"] if b["id"] == "low"), None)
        treble = next((b["gain"] for b in state["bands"] if b["id"] == "high"), None)
        return bass, treble
    except Exception as exc:
        print(f"Error getting tone: {exc}", flush=True)
    return None, None


def reset_tone() -> None:
    """Reset both persistent shelf controls to 0."""
    try:
        with speaker_selection_lock(SPEAKER_SELECTION_PATH):
            audio_path, speaker_id = current_audio_eq_path()
            reset_tone_bands(audio_path)
        print(f"Tone reset [{speaker_id}]: Bass=0 dB, Treble=0 dB", flush=True)
    except Exception as exc:
        print(f"Error resetting tone: {exc}", flush=True)


def show_status() -> None:
    try:
        client = ensure_cdsp_connected()
        volume = client.volume.main_volume()
        muted = client.volume.main_mute()
        bass, treble = get_current_tone()
        print(
            f"Status: Volume={volume:.1f}dB, "
            f"Mute={'ON' if muted else 'OFF'}, "
            f"Bass={format_db(bass)}, Treble={format_db(treble)}",
            flush=True,
        )
    except Exception as exc:
        print(f"Error getting status: {exc}", flush=True)


def validate_trusted_executable(path: str) -> None:
    if os.path.realpath(path) != path:
        raise RuntimeError(f"privileged executable path is not canonical: {path}")
    try:
        metadata = os.stat(path)
    except OSError as exc:
        raise RuntimeError(f"privileged executable is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not metadata.st_mode & stat.S_IXUSR
    ):
        raise RuntimeError(f"privileged executable is not trusted: {path}")


def run_sudo(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    if not command:
        raise ValueError("privileged command cannot be empty")
    validate_trusted_executable(SUDO_BIN)
    validate_trusted_executable(command[0])
    return subprocess.run(
        [SUDO_BIN, "-n", *command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def restart_services() -> None:
    """
    Restart CamillaDSP and related services.

    The trigger remains running and rebuilds its client in place, keeping the
    amplifier relay latched throughout this recovery sequence.
    """
    services = [
        "camilladsp.service",
        "camillagui.service",
        "cdsp-motu-sync.service",
        "cdsp-source-switcher.service",
    ]

    print("Restarting services...", flush=True)
    for service in services:
        try:
            result = run_sudo([SYSTEMCTL_BIN, "restart", service], timeout=10)
            if result.returncode == 0:
                print(f"  Restarted: {service}", flush=True)
            else:
                print(
                    f"  Skipped: {service} ({result.stderr.strip() or result.stdout.strip()})",
                    flush=True,
                )
        except subprocess.TimeoutExpired:
            print(f"  Timeout: {service}", flush=True)
        except Exception as exc:
            print(f"  Error restarting {service}: {exc}", flush=True)

    try:
        result = run_sudo(
            [SYSTEMCTL_BIN, "--no-block", "restart", "cdsp-remote.service"],
            timeout=5,
        )
        if result.returncode == 0:
            print("  Restart requested: cdsp-remote.service", flush=True)
        else:
            print(
                f"  Skipped: cdsp-remote.service ({result.stderr.strip()})", flush=True
            )
    except subprocess.TimeoutExpired:
        print("  Timeout: cdsp-remote.service", flush=True)
    except Exception as exc:
        print(f"  Error restarting cdsp-remote.service: {exc}", flush=True)


def shutdown_system() -> None:
    print("Power button held 10s - shutting down...", flush=True)
    try:
        result = run_sudo([SYSTEMCTL_BIN, "poweroff"], timeout=5)
        if result.returncode != 0:
            print(
                f"Shutdown failed: {result.stderr.strip() or result.stdout.strip()}",
                flush=True,
            )
    except Exception as exc:
        print(f"Shutdown failed: {exc}", flush=True)


# ====================== EVENT HANDLING ======================


async def handle_remote_events(device) -> None:
    """Process events from the remote control device."""
    counter_volume = 0
    hold_started_at: dict[str, float] = {}
    hold_handled: set[str] = set()

    while True:
        try:
            async for event in device.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue

                attrib = evdev.categorize(event)
                key = attrib.keycode
                now = time.monotonic()

                if attrib.keystate == 1:
                    if key_matches(key, KEY_BINDINGS["VOLUMEDOWN"]) or key_matches(
                        key, KEY_BINDINGS["VOLUMEUP"]
                    ):
                        change = (
                            -VOLUME_STEP
                            if key_matches(key, KEY_BINDINGS["VOLUMEDOWN"])
                            else VOLUME_STEP
                        )
                        adjust_volume(change)
                    elif key_matches(key, KEY_BINDINGS["MUTE"]):
                        toggle_mute()
                    elif key_matches(key, KEY_BINDINGS["UP"]) or key_matches(
                        key, KEY_BINDINGS["DOWN"]
                    ):
                        change = (
                            TONE_STEP
                            if key_matches(key, KEY_BINDINGS["UP"])
                            else -TONE_STEP
                        )
                        adjust_tone("Treble", change)
                    elif key_matches(key, KEY_BINDINGS["LEFT"]) or key_matches(
                        key, KEY_BINDINGS["RIGHT"]
                    ):
                        change = (
                            TONE_STEP
                            if key_matches(key, KEY_BINDINGS["RIGHT"])
                            else -TONE_STEP
                        )
                        adjust_tone("Bass", change)
                    elif key_matches(key, KEY_BINDINGS["ENTER"]):
                        hold_started_at["ENTER"] = now
                        hold_handled.discard("ENTER")
                    elif key_matches(key, KEY_BINDINGS["POWER"]):
                        hold_started_at["POWER"] = now
                        hold_handled.discard("POWER")

                elif attrib.keystate == 2:
                    if key_matches(key, KEY_BINDINGS["VOLUMEDOWN"]) or key_matches(
                        key, KEY_BINDINGS["VOLUMEUP"]
                    ):
                        counter_volume += 1
                        if counter_volume >= 2:
                            change = (
                                -VOLUME_STEP
                                if key_matches(key, KEY_BINDINGS["VOLUMEDOWN"])
                                else VOLUME_STEP
                            )
                            adjust_volume(change)
                            counter_volume = 0
                    elif key_matches(key, KEY_BINDINGS["ENTER"]):
                        started = hold_started_at.setdefault("ENTER", now)
                        if (
                            now - started >= ENTER_HOLD_SECONDS
                            and "ENTER" not in hold_handled
                        ):
                            reset_tone()
                            hold_handled.add("ENTER")
                    elif key_matches(key, KEY_BINDINGS["POWER"]):
                        started = hold_started_at.setdefault("POWER", now)
                        if (
                            now - started >= SHUTDOWN_HOLD_SECONDS
                            and "POWER" not in hold_handled
                        ):
                            hold_handled.add("POWER")
                            shutdown_system()

                elif attrib.keystate == 0:
                    if key_matches(key, KEY_BINDINGS["ENTER"]):
                        started = hold_started_at.pop("ENTER", now)
                        held = now - started
                        if "ENTER" not in hold_handled:
                            if held >= ENTER_HOLD_SECONDS:
                                reset_tone()
                            else:
                                show_status()
                        hold_handled.discard("ENTER")

                    elif key_matches(key, KEY_BINDINGS["POWER"]):
                        started = hold_started_at.pop("POWER", now)
                        held = now - started
                        if "POWER" not in hold_handled:
                            if held >= SHUTDOWN_HOLD_SECONDS:
                                shutdown_system()
                            elif held >= RESTART_HOLD_SECONDS:
                                print(
                                    "Power button held 1s - restarting services...",
                                    flush=True,
                                )
                                restart_services()
                        hold_handled.discard("POWER")

                    counter_volume = 0

        except OSError as exc:
            print(f"Device error: {exc}. Attempting to reconnect...", flush=True)
            await asyncio.sleep(2)
            device = find_remote_device()
            grab_device(device)


# ====================== MAIN ======================


def grab_device(device) -> None:
    try:
        device.grab()
        print("Remote input grabbed", flush=True)
    except OSError as exc:
        print(f"Remote input grab skipped: {exc}", flush=True)


def cleanup(signum=None, frame=None) -> None:
    """Clean up resources on exit."""
    print("\nShutting down...", flush=True)
    if remote_device is not None:
        try:
            remote_device.ungrab()
        except Exception:
            pass
        try:
            remote_device.close()
        except Exception:
            pass
    if cdsp:
        try:
            cdsp.disconnect()
        except Exception:
            pass
    sys.exit(0)


def main() -> int:
    global remote_device

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("=" * 50, flush=True)
    print("CamillaDSP USB HID Remote Control", flush=True)
    print("=" * 50, flush=True)

    remote_device = find_remote_device()
    grab_device(remote_device)
    connect_to_camilladsp()

    try:
        client = ensure_cdsp_connected()
        volume = client.volume.main_volume()
        muted = client.volume.main_mute()
        config = os.path.basename(client.config.file_path())
        print(
            f"Current: Volume={volume:.1f}dB, Mute={'ON' if muted else 'OFF'}, Config={config}",
            flush=True,
        )
    except Exception as exc:
        print(f"Could not get initial status: {exc}", flush=True)

    print("=" * 50, flush=True)
    print("Ready. Listening for remote events...", flush=True)
    print("=" * 50, flush=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(handle_remote_events(remote_device))
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
