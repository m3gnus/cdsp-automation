#!/usr/bin/env python3
"""CamillaDSP USB/Bluetooth HID remote control.

What each button does comes from the key map (remote_keymap.py); without a
site file the remote keeps its long-standing layout.  ``--learn`` prints the
key names a remote sends, ``--print-keymap`` the map in force.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import evdev
from camilladsp import CamillaClient
import motu_volume
import remote_keymap
from audio_eq import read_audio_state, reset_tone_bands, update_tone_band
from settings import (
    AUDIO_CONTROL_LOCK_PATH,
    AUDIO_EQ_PATH,
    CDSP_HOST,
    CDSP_PORT,
    ENV_FILE,
    REMOTE_KEYMAP_PATH,
    SPEAKER_AUDIO_DIR,
    SPEAKER_SELECTION_PATH,
    SPEAKER_STATUS_PATH,
)
from speaker_profiles import (
    BUILTIN_SPEAKERS,
    audio_control_lock,
    read_speaker_selection,
    resolve_profile_audio_path,
    speaker_selection_lock,
    volume_ceiling,
)


# ====================== CONFIGURATION ======================


REMOTE_NAME = os.environ.get("REMOTE_NAME", "HID Remote01 Keyboard")
DEVICE_RETRY_SECONDS = 2.0
STATUS_LOG_SECONDS = 300.0

TONE_MIN = -6.0
TONE_MAX = 6.0

VOLUME_MIN = float(os.environ.get("REMOTE_VOLUME_MIN", "-80"))
# The ceiling comes from the profile the switcher verified into service, not
# from this module. REMOTE_VOLUME_MAX stays available as a deployment's own
# preference, but it can only tighten that ceiling -- never lift it.
VOLUME_MAX_OVERRIDE = (
    float(os.environ["REMOTE_VOLUME_MAX"])
    if os.environ.get("REMOTE_VOLUME_MAX")
    else None
)
# How long a MOTU volume press waits for the source switcher to apply it.
MOTU_REPLY_SECONDS = 2.0
# Fixed Raspberry Pi OS paths. Never derive NOPASSWD targets from PATH or the
# user-controlled EnvironmentFile.
SUDO_BIN = "/usr/bin/sudo"
SYSTEMCTL_BIN = "/usr/bin/systemctl"



# ====================== GLOBAL STATE ======================

cdsp: CamillaClient | None = None
remote_device = None
keymap: remote_keymap.Keymap = remote_keymap.default_keymap()


# ====================== HELPER FUNCTIONS ======================


def load_site_keymap() -> remote_keymap.Keymap:
    loaded, problem = remote_keymap.load_keymap(REMOTE_KEYMAP_PATH)
    # stderr, so --print-keymap's stdout stays a clean JSON document.
    if problem:
        print(f"Remote key map: {problem}", file=sys.stderr, flush=True)
    else:
        print(f"Remote key map: {loaded.source}", file=sys.stderr, flush=True)
    return loaded


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


def current_volume_max() -> float:
    """Ceiling of the applied speaker profile, tightened by any override."""
    return volume_ceiling(SPEAKER_STATUS_PATH, override=VOLUME_MAX_OVERRIDE)


def adjust_volume(change: float) -> None:
    """Adjust the main volume by the specified amount."""
    try:
        client = ensure_cdsp_connected()
        with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
            maximum = current_volume_max()
            # A ceiling below the usual floor still wins; clamping up to
            # VOLUME_MIN afterwards would hand back the volume just refused.
            minimum = min(VOLUME_MIN, maximum)
            current_volume = client.volume.main_volume()
            new_volume = max(minimum, min(maximum, current_volume + change))
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


def available_sources() -> list[str]:
    """Sources the selected speaker can play, in the switcher's order."""
    # The control UI owns this rule (speaker profiles, operator configs); one
    # copy of it keeps the remote and the page offering the same sources.
    import web_ui

    availability = web_ui.source_availability()
    return [name for name in remote_keymap.SOURCES if availability.get(name, {}).get("exists")]


def select_source(source: str) -> None:
    """Pin ``source`` (or "auto") as the switcher's manual override."""
    try:
        import web_ui

        web_ui.write_source_override(source)
        print(f"Source: {source}", flush=True)
    except Exception as exc:
        print(f"Error selecting source {source}: {exc}", flush=True)


def next_source() -> None:
    """Step the override: auto -> each available source -> auto."""
    try:
        import web_ui

        order = ["auto", *available_sources()]
        current = web_ui.read_source_override() or "auto"
        position = order.index(current) if current in order else 0
        select_source(order[(position + 1) % len(order)])
    except Exception as exc:
        print(f"Error changing source: {exc}", flush=True)


def amps_off() -> None:
    """Drop the trigger relay, as the control UI's button does.

    The trigger runs as this same user, so its SIGUSR1 needs no privilege.
    """
    try:
        validate_trusted_executable(SYSTEMCTL_BIN)
        result = subprocess.run(
            [SYSTEMCTL_BIN, "show", "-p", "MainPID", "--value", "cdsp-trigger.service"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        pid = int(result.stdout.strip() or "0")
        if pid <= 0:
            print("Amps off: cdsp-trigger.service is not running", flush=True)
            return
        os.kill(pid, signal.SIGUSR1)
        print("Amps off (automatic trigger stays enabled)", flush=True)
    except Exception as exc:
        print(f"Error turning amps off: {exc}", flush=True)


def adjust_motu_volume(change: float) -> None:
    """Step the MOTU main output through the switcher, its only client."""
    try:
        status = motu_volume.read_status()
        if not status.get("known") or not status.get("writable"):
            print(f"MOTU volume unavailable: {status.get('reason')}", flush=True)
            return
        current = float(status["volume_db"])
        _request_id, result = motu_volume.submit_request(
            {"volume_db": current + change, "expected_db": current},
            MOTU_REPLY_SECONDS,
        )
        if result is None:
            print("MOTU volume: request pending (switcher busy)", flush=True)
        elif result.get("ok"):
            print(f"MOTU volume: {motu_volume.read_status().get('volume_db')} dB", flush=True)
        else:
            print(f"MOTU volume refused: {result.get('error')}", flush=True)
    except Exception as exc:
        print(f"Error adjusting MOTU volume: {exc}", flush=True)


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


def run_action(action: str) -> None:
    """Carry out one key-map action."""
    volume_step = keymap.volume_step_db
    tone_step = keymap.tone_step_db
    handlers = {
        "volume_up": lambda: adjust_volume(volume_step),
        "volume_down": lambda: adjust_volume(-volume_step),
        "mute": toggle_mute,
        "bass_up": lambda: adjust_tone("Bass", tone_step),
        "bass_down": lambda: adjust_tone("Bass", -tone_step),
        "treble_up": lambda: adjust_tone("Treble", tone_step),
        "treble_down": lambda: adjust_tone("Treble", -tone_step),
        "tone_reset": reset_tone,
        "status": show_status,
        "restart_services": restart_services,
        "shutdown": shutdown_system,
        "next_source": next_source,
        "source_auto": lambda: select_source("auto"),
        "amps_off": amps_off,
        "motu_volume_up": lambda: adjust_motu_volume(keymap.motu_step_db),
        "motu_volume_down": lambda: adjust_motu_volume(-keymap.motu_step_db),
    }
    for name in remote_keymap.SOURCES:
        handlers[f"source_{name}"] = lambda name=name: select_source(name)
    handler = handlers.get(action)
    if handler is None:
        print(f"Unknown remote action: {action}", flush=True)
        return
    if action == "restart_services":
        print("Power button held - restarting services...", flush=True)
    handler()


async def handle_remote_events(device) -> None:
    """Process events from the remote control device."""
    global remote_device

    dispatcher = remote_keymap.KeyDispatcher(keymap)
    while True:
        try:
            async for event in device.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue

                attrib = evdev.categorize(event)
                for action in dispatcher.event(
                    attrib.keycode, attrib.keystate, time.monotonic()
                ):
                    run_action(action)

        except OSError as exc:
            print(f"Device error: {exc}. Attempting to reconnect...", flush=True)
            close_remote_device(device)
            await asyncio.sleep(2)
            device = find_remote_device()
            remote_device = device
            grab_device(device)
            dispatcher = remote_keymap.KeyDispatcher(keymap)


def env_file_value(key: str) -> str | None:
    """``key`` from the env file, for a run from a shell rather than the unit.

    The unit loads the file with EnvironmentFile=; a person running --learn
    by hand has not, and the file's values may contain spaces, so sourcing
    it in a shell is not an option.
    """
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        name, separator, value = line.partition("=")
        if separator and name.strip() == key:
            return value.strip()
    return None


def learn_keys() -> int:
    """Print every key a remote sends, and what the key map does with it.

    Listens on every input device whose name shares the remote's base name
    (a Bluetooth remote often registers as "... Keyboard", "... Mouse" and
    "... Consumer Control"), without grabbing them.  The running service
    grabs the keyboard device, so stop it first.
    """
    global REMOTE_NAME
    if "REMOTE_NAME" not in os.environ:
        REMOTE_NAME = env_file_value("REMOTE_NAME") or REMOTE_NAME
    base = REMOTE_NAME
    for suffix in (" Keyboard", " Mouse", " Consumer Control"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    devices = []
    print("Input devices:", flush=True)
    for path in evdev.list_devices():
        try:
            device = evdev.InputDevice(path)
        except OSError as exc:
            print(f"  {path}: cannot open ({exc})", flush=True)
            continue
        listening = base in device.name
        print(f"  {path}: {device.name}{'  <- listening' if listening else ''}", flush=True)
        if listening:
            devices.append(device)
        else:
            device.close()
    if not devices:
        print(
            f"No input device matches '{base}'. Set REMOTE_NAME in the env file "
            "to one of the names above.",
            flush=True,
        )
        return 1
    print(
        "\nPress each button on the remote (Ctrl+C to stop). If nothing appears,"
        "\nstop the service first: sudo systemctl stop cdsp-remote\n",
        flush=True,
    )
    states = {0: "released", 1: "pressed", 2: "held"}

    async def read(device) -> None:
        async for event in device.async_read_loop():
            if event.type != evdev.ecodes.EV_KEY:
                continue
            attrib = evdev.categorize(event)
            if attrib.keystate == 2:
                continue  # auto-repeat floods the output
            names = attrib.keycode if isinstance(attrib.keycode, list) else [attrib.keycode]
            binding = next((keymap.keys[n] for n in names if n in keymap.keys), None)
            mapped = (
                ", ".join(
                    f"{slot}={getattr(binding, slot)}"
                    for slot in remote_keymap.SLOTS
                    if getattr(binding, slot)
                )
                if binding
                else "not mapped"
            )
            note = "" if device.name == REMOTE_NAME else "  (not the REMOTE_NAME device: unusable)"
            print(
                f"{'/'.join(names):<22} {states.get(attrib.keystate, attrib.keystate):<9}"
                f" [{device.name}] -> {mapped}{note}",
                flush=True,
            )

    async def read_all() -> None:
        await asyncio.gather(*(read(device) for device in devices))

    try:
        asyncio.run(read_all())
    except KeyboardInterrupt:
        pass
    finally:
        for device in devices:
            close_remote_device(device)
    return 0


# ====================== MAIN ======================


def grab_device(device) -> None:
    try:
        device.grab()
        print("Remote input grabbed", flush=True)
    except OSError as exc:
        print(f"Remote input grab skipped: {exc}", flush=True)


def close_remote_device(device) -> None:
    """Best-effort release of an input device during reconnect or shutdown."""
    if device is None:
        return
    try:
        device.ungrab()
    except Exception:
        pass
    try:
        device.close()
    except Exception:
        pass


def cleanup(signum=None, frame=None) -> None:
    """Clean up resources on exit."""
    print("\nShutting down...", flush=True)
    close_remote_device(remote_device)
    if cdsp:
        try:
            cdsp.disconnect()
        except Exception:
            pass
    sys.exit(0)


def main(argv: list[str] | None = None) -> int:
    global remote_device, keymap

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--learn", action="store_true", help="print the key names the remote sends"
    )
    parser.add_argument(
        "--print-keymap",
        action="store_true",
        help=f"print the key map in force (a starting point for {REMOTE_KEYMAP_PATH})",
    )
    args = parser.parse_args(argv)
    keymap = load_site_keymap()
    if args.print_keymap:
        print(json.dumps(remote_keymap.keymap_document(keymap), indent=2))
        return 0
    if args.learn:
        return learn_keys()

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("=" * 50, flush=True)
    print("CamillaDSP USB HID Remote Control", flush=True)
    print("=" * 50, flush=True)

    remote_device = find_remote_device()
    grab_device(remote_device)

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
