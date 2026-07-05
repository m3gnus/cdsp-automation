#!/usr/bin/env python3
"""CamillaDSP USB/Bluetooth HID remote control."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time

import evdev
from camilladsp import CamillaClient


# ====================== CONFIGURATION ======================

REMOTE_NAME = os.environ.get("REMOTE_NAME", "HID Remote01 Keyboard")
CDSP_HOST = os.environ.get("CDSP_HOST", "127.0.0.1")
CDSP_PORT = int(os.environ.get("CDSP_PORT", "1234"))

TONE_MIN = float(os.environ.get("REMOTE_TONE_MIN", "-6"))
TONE_MAX = float(os.environ.get("REMOTE_TONE_MAX", "6"))
TONE_STEP = float(os.environ.get("REMOTE_TONE_STEP", "0.5"))

VOLUME_MIN = float(os.environ.get("REMOTE_VOLUME_MIN", "-80"))
VOLUME_MAX = float(os.environ.get("REMOTE_VOLUME_MAX", "0"))
VOLUME_STEP = float(os.environ.get("REMOTE_VOLUME_STEP", "1"))

ENTER_HOLD_SECONDS = float(os.environ.get("REMOTE_ENTER_HOLD_SECONDS", "1"))
RESTART_HOLD_SECONDS = float(os.environ.get("REMOTE_RESTART_HOLD_SECONDS", "1"))
SHUTDOWN_HOLD_SECONDS = float(os.environ.get("REMOTE_SHUTDOWN_HOLD_SECONDS", "10"))
TRIGGER_RESTART_DELAY_SECONDS = float(os.environ.get("REMOTE_TRIGGER_RESTART_DELAY_SECONDS", "3"))

SYSTEMCTL_BIN = shutil.which("systemctl") or "systemctl"
SYSTEMD_RUN_BIN = shutil.which("systemd-run") or "systemd-run"
SHUTDOWN_BIN = shutil.which("shutdown") or "shutdown"

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

    while True:
        seen: list[str] = []
        try:
            paths = evdev.list_devices()
        except OSError as exc:
            print(f"Cannot list input devices: {exc}. Retrying in 2 seconds...", flush=True)
            time.sleep(2)
            continue

        for path in paths:
            try:
                device = evdev.InputDevice(path)
            except OSError as exc:
                print(f"Cannot open input device {path}: {exc}", flush=True)
                continue

            seen.append(device.name)
            if device.name == REMOTE_NAME:
                print(f"Found '{REMOTE_NAME}' at {path}", flush=True)
                return device

            try:
                device.close()
            except Exception:
                pass

        suffix = f" Seen: {', '.join(seen)}" if seen else ""
        print(f"Remote '{REMOTE_NAME}' not found. Retrying in 2 seconds.{suffix}", flush=True)
        time.sleep(2)


def connect_to_camilladsp() -> CamillaClient:
    """Establish connection to CamillaDSP."""
    global cdsp

    print(f"Connecting to CamillaDSP at {CDSP_HOST}:{CDSP_PORT}...", flush=True)
    cdsp = CamillaClient(CDSP_HOST, CDSP_PORT)

    while True:
        try:
            cdsp.connect()
            print("Connected to CamillaDSP successfully.", flush=True)
            return cdsp
        except Exception as exc:
            print(f"Failed to connect: {exc}. Retrying in 2 seconds...", flush=True)
            time.sleep(2)


def ensure_cdsp_connected() -> CamillaClient:
    """Return a connected CamillaDSP client, recreating it after failures."""
    global cdsp

    if cdsp is None:
        return connect_to_camilladsp()

    try:
        if not cdsp.is_connected():
            cdsp.connect()
        return cdsp
    except Exception:
        try:
            cdsp.disconnect()
        except Exception:
            pass
        cdsp = CamillaClient(CDSP_HOST, CDSP_PORT)
        cdsp.connect()
        return cdsp


def format_db(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}dB"


def adjust_volume(change: float) -> None:
    """Adjust the main volume by the specified amount."""
    try:
        client = ensure_cdsp_connected()
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
        is_muted = client.volume.main_mute()
        client.volume.set_main_mute(not is_muted)
        print(f"Mute: {'ON' if not is_muted else 'OFF'}", flush=True)
    except Exception as exc:
        print(f"Error toggling mute: {exc}", flush=True)


def adjust_tone(parameter: str, change: float) -> None:
    """Adjust bass or treble gain in the active in-memory config."""
    try:
        client = ensure_cdsp_connected()
        cdspconf = client.config.active()
        if not cdspconf:
            print("No active configuration", flush=True)
            return

        filters = cdspconf.get("filters", {})
        filt = filters.get(parameter)
        if not filt or "parameters" not in filt or "gain" not in filt["parameters"]:
            print(f"Filter '{parameter}' with gain parameter not found in config", flush=True)
            return

        current_gain = float(filt["parameters"]["gain"])
        new_gain = max(TONE_MIN, min(TONE_MAX, current_gain + change))
        filt["parameters"]["gain"] = new_gain
        client.config.set_active(cdspconf)
        print(f"{parameter}: {new_gain:+.1f} dB", flush=True)

    except Exception as exc:
        print(f"Error adjusting {parameter}: {exc}", flush=True)


def get_current_tone() -> tuple[float | None, float | None]:
    """Get current bass and treble values."""
    try:
        client = ensure_cdsp_connected()
        cdspconf = client.config.active()
        if cdspconf:
            filters = cdspconf.get("filters", {})
            bass = filters.get("Bass", {}).get("parameters", {}).get("gain")
            treble = filters.get("Treble", {}).get("parameters", {}).get("gain")
            return (
                None if bass is None else float(bass),
                None if treble is None else float(treble),
            )
    except Exception as exc:
        print(f"Error getting tone: {exc}", flush=True)
    return None, None


def reset_tone() -> None:
    """Reset both bass and treble to 0."""
    try:
        client = ensure_cdsp_connected()
        cdspconf = client.config.active()
        if not cdspconf:
            return

        filters = cdspconf.get("filters", {})
        missing = [name for name in ("Bass", "Treble") if name not in filters]
        if missing:
            print(f"Tone reset skipped; missing filters: {', '.join(missing)}", flush=True)
            return

        filters["Bass"]["parameters"]["gain"] = 0
        filters["Treble"]["parameters"]["gain"] = 0
        client.config.set_active(cdspconf)
        print("Tone reset: Bass=0 dB, Treble=0 dB", flush=True)

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


def run_sudo(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "-n", *command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def restart_services() -> None:
    """
    Restart CamillaDSP and related services.

    cdsp-trigger is restarted through a delayed transient unit so this process
    can restart cdsp-remote last without cancelling the trigger restart.
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
                print(f"  Skipped: {service} ({result.stderr.strip() or result.stdout.strip()})", flush=True)
        except subprocess.TimeoutExpired:
            print(f"  Timeout: {service}", flush=True)
        except Exception as exc:
            print(f"  Error restarting {service}: {exc}", flush=True)

    unit_name = f"cdsp-trigger-restart-{int(time.time())}"
    try:
        result = run_sudo(
            [
                SYSTEMD_RUN_BIN,
                "--no-block",
                f"--on-active={TRIGGER_RESTART_DELAY_SECONDS:g}",
                f"--unit={unit_name}",
                SYSTEMCTL_BIN,
                "restart",
                "cdsp-trigger.service",
            ],
            timeout=5,
        )
        if result.returncode == 0:
            print(
                f"  Scheduled: cdsp-trigger.service (in {TRIGGER_RESTART_DELAY_SECONDS:g}s)",
                flush=True,
            )
        else:
            print(f"  Warning: systemd-run failed: {result.stderr.strip()}", flush=True)
    except Exception as exc:
        print(f"  Warning: Could not schedule trigger restart: {exc}", flush=True)

    try:
        result = run_sudo([SYSTEMCTL_BIN, "restart", "cdsp-remote.service"], timeout=10)
        if result.returncode == 0:
            print("  Restarted: cdsp-remote.service", flush=True)
        else:
            print(f"  Skipped: cdsp-remote.service ({result.stderr.strip()})", flush=True)
    except subprocess.TimeoutExpired:
        print("  Timeout: cdsp-remote.service", flush=True)
    except Exception as exc:
        print(f"  Error restarting cdsp-remote.service: {exc}", flush=True)


def shutdown_system() -> None:
    print("Power button held 10s - shutting down...", flush=True)
    try:
        result = run_sudo([SHUTDOWN_BIN, "-h", "now"], timeout=5)
        if result.returncode != 0:
            print(f"Shutdown failed: {result.stderr.strip() or result.stdout.strip()}", flush=True)
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
                    if key_matches(key, KEY_BINDINGS["VOLUMEDOWN"]) or key_matches(key, KEY_BINDINGS["VOLUMEUP"]):
                        change = -VOLUME_STEP if key_matches(key, KEY_BINDINGS["VOLUMEDOWN"]) else VOLUME_STEP
                        adjust_volume(change)
                    elif key_matches(key, KEY_BINDINGS["MUTE"]):
                        toggle_mute()
                    elif key_matches(key, KEY_BINDINGS["UP"]) or key_matches(key, KEY_BINDINGS["DOWN"]):
                        change = TONE_STEP if key_matches(key, KEY_BINDINGS["UP"]) else -TONE_STEP
                        adjust_tone("Treble", change)
                    elif key_matches(key, KEY_BINDINGS["LEFT"]) or key_matches(key, KEY_BINDINGS["RIGHT"]):
                        change = TONE_STEP if key_matches(key, KEY_BINDINGS["RIGHT"]) else -TONE_STEP
                        adjust_tone("Bass", change)
                    elif key_matches(key, KEY_BINDINGS["ENTER"]):
                        hold_started_at["ENTER"] = now
                        hold_handled.discard("ENTER")
                    elif key_matches(key, KEY_BINDINGS["POWER"]):
                        hold_started_at["POWER"] = now
                        hold_handled.discard("POWER")

                elif attrib.keystate == 2:
                    if key_matches(key, KEY_BINDINGS["VOLUMEDOWN"]) or key_matches(key, KEY_BINDINGS["VOLUMEUP"]):
                        counter_volume += 1
                        if counter_volume >= 2:
                            change = -VOLUME_STEP if key_matches(key, KEY_BINDINGS["VOLUMEDOWN"]) else VOLUME_STEP
                            adjust_volume(change)
                            counter_volume = 0
                    elif key_matches(key, KEY_BINDINGS["ENTER"]):
                        started = hold_started_at.setdefault("ENTER", now)
                        if now - started >= ENTER_HOLD_SECONDS and "ENTER" not in hold_handled:
                            reset_tone()
                            hold_handled.add("ENTER")
                    elif key_matches(key, KEY_BINDINGS["POWER"]):
                        started = hold_started_at.setdefault("POWER", now)
                        if now - started >= SHUTDOWN_HOLD_SECONDS and "POWER" not in hold_handled:
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
                                print("Power button held 1s - restarting services...", flush=True)
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
        print(f"Current: Volume={volume:.1f}dB, Mute={'ON' if muted else 'OFF'}, Config={config}", flush=True)
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
