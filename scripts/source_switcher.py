#!/usr/bin/env python3
"""Automatic CamillaDSP config switching by active source."""

from __future__ import annotations

import glob
import os
import subprocess
import time

from camilladsp import CamillaClient


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


CAMILLA_IP = os.environ.get("CDSP_HOST", "127.0.0.1")
CAMILLA_PORT = int(os.environ.get("CDSP_PORT", "1234"))
CHECK_INTERVAL = float(os.environ.get("SOURCE_CHECK_INTERVAL", "1.0"))
IDLE_TIMEOUT = float(os.environ.get("SOURCE_IDLE_TIMEOUT", "60"))
SETTLE_TIME = float(os.environ.get("SOURCE_SETTLE_TIME", "2.0"))
AUDIO_THRESHOLD_DB = float(os.environ.get("SOURCE_AUDIO_THRESHOLD_DB", "-80"))
DEBUG_MODE = env_bool("SOURCE_DEBUG", False)

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("CDSP_CONFIG_DIR", os.path.join(HOME, "camilladsp/configs"))

TOSLINK_CFG = os.path.join(CONFIG_DIR, "toslink.yml")
STREAMER_CFG = os.path.join(CONFIG_DIR, "streamer.yml")
GADGET_CFG = os.path.join(CONFIG_DIR, "gadget.yml")


def is_alsa_active(card_name: str) -> bool:
    """Check if any ALSA PCM for the card is in RUNNING state."""
    for path in glob.glob(f"/proc/asound/{card_name}/pcm*/sub*/status"):
        try:
            with open(path, "r", encoding="utf-8") as status_file:
                if "state: RUNNING" in status_file.read():
                    return True
        except OSError:
            continue
    return False


def is_gadget_available() -> bool:
    """Check if USB Gadget capture rate is non-zero."""
    try:
        res = subprocess.check_output(
            ["amixer", "-c", "UAC2Gadget", "contents"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        in_capture_rate = False
        for line in res.splitlines():
            if "name='Capture Rate'" in line:
                in_capture_rate = True
                continue
            if in_capture_rate and ": values=" in line:
                rate_text = line.split("values=", 1)[1].strip().split(",", 1)[0]
                return int(rate_text) > 0
    except Exception:
        pass
    return False


def same_config(current: str | None, target: str) -> bool:
    if not current:
        return False
    return os.path.abspath(current) == os.path.abspath(target)


def audio_active(levels: object) -> bool:
    if not isinstance(levels, (list, tuple)):
        return False
    return any(
        isinstance(level, (int, float)) and level > AUDIO_THRESHOLD_DB
        for level in levels
    )


def validate_configs() -> None:
    missing = [path for path in (TOSLINK_CFG, STREAMER_CFG, GADGET_CFG) if not os.path.exists(path)]
    if missing:
        raise FileNotFoundError("Missing CamillaDSP config(s): " + ", ".join(missing))


def apply_config(cdsp: CamillaClient, file_path: str, settle_time: float = SETTLE_TIME) -> None:
    """Apply a CamillaDSP config file and wait for hardware to settle."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)
    config_name = os.path.basename(file_path)
    print(f">>> Switching to: {config_name}", flush=True)
    cdsp.config.set_file_path(file_path)
    cdsp.general.reload()
    time.sleep(settle_time)


def log_idle(source: str, seconds: float) -> None:
    if DEBUG_MODE and int(seconds) > 0 and int(seconds) % 5 == 0:
        print(f"-> {source}: idle {seconds:g}/{IDLE_TIMEOUT:g}s", flush=True)


def main() -> int:
    print(">>> CamillaDSP Source Switcher Started <<<", flush=True)
    print("Priority: 1) Streamer (AirPlay) -> 2) USB Gadget -> 3) TOSLINK", flush=True)
    validate_configs()

    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    streamer_silence_timer = 0.0
    gadget_silence_timer = 0.0
    last_active_source = None

    while True:
        try:
            if not cdsp.is_connected():
                cdsp.connect()
                print("Connected to CamillaDSP", flush=True)

            current_config = cdsp.config.file_path()
            streamer_hw_active = is_alsa_active("Loopback")
            gadget_hw_available = is_gadget_available()

            if DEBUG_MODE:
                print(
                    "DEBUG: "
                    f"Streamer HW={streamer_hw_active}, "
                    f"Gadget HW={gadget_hw_available}, "
                    f"Last={last_active_source}, "
                    f"ST={streamer_silence_timer:g}, "
                    f"GT={gadget_silence_timer:g}, "
                    f"Config={os.path.basename(current_config or '')}",
                    flush=True,
                )

            # Priority 1: Streamer (AirPlay via ALSA Loopback).
            if streamer_hw_active:
                last_active_source = "streamer"
                if not same_config(current_config, STREAMER_CFG):
                    apply_config(cdsp, STREAMER_CFG)
                    streamer_silence_timer = 0.0
                    gadget_silence_timer = 0.0
                    time.sleep(CHECK_INTERVAL)
                    continue

                if audio_active(cdsp.levels.capture_rms()):
                    streamer_silence_timer = 0.0
                    if DEBUG_MODE:
                        print("-> Streamer: audio active", flush=True)
                else:
                    streamer_silence_timer += CHECK_INTERVAL
                    log_idle("Streamer", streamer_silence_timer)

                if streamer_silence_timer < IDLE_TIMEOUT:
                    time.sleep(CHECK_INTERVAL)
                    continue

                if DEBUG_MODE:
                    print("Streamer idle timeout - checking other sources", flush=True)
                last_active_source = None

            elif last_active_source == "streamer" and same_config(current_config, STREAMER_CFG):
                streamer_silence_timer += CHECK_INTERVAL
                log_idle("Streamer grace", streamer_silence_timer)
                if streamer_silence_timer < IDLE_TIMEOUT:
                    time.sleep(CHECK_INTERVAL)
                    continue
                last_active_source = None

            # Priority 2: USB Gadget.
            if gadget_hw_available:
                last_active_source = "gadget"
                if not same_config(current_config, GADGET_CFG):
                    apply_config(cdsp, GADGET_CFG, settle_time=1.5)
                    gadget_silence_timer = 0.0
                    streamer_silence_timer = 0.0
                    time.sleep(CHECK_INTERVAL)
                    continue

                if audio_active(cdsp.levels.capture_rms()):
                    gadget_silence_timer = 0.0
                    if DEBUG_MODE:
                        print("-> Gadget: audio active", flush=True)
                else:
                    gadget_silence_timer += CHECK_INTERVAL
                    log_idle("Gadget", gadget_silence_timer)

                if gadget_silence_timer < IDLE_TIMEOUT:
                    time.sleep(CHECK_INTERVAL)
                    continue

                if DEBUG_MODE:
                    print("Gadget idle timeout - switching to TOSLINK", flush=True)
                last_active_source = None

            elif last_active_source == "gadget" and same_config(current_config, GADGET_CFG):
                gadget_silence_timer += CHECK_INTERVAL
                log_idle("Gadget grace", gadget_silence_timer)
                if gadget_silence_timer < IDLE_TIMEOUT:
                    time.sleep(CHECK_INTERVAL)
                    continue
                last_active_source = None

            # Priority 3: TOSLINK fallback.
            if not same_config(current_config, TOSLINK_CFG):
                apply_config(cdsp, TOSLINK_CFG)
                streamer_silence_timer = 0.0
                gadget_silence_timer = 0.0
                last_active_source = None
            elif DEBUG_MODE:
                print("-> TOSLINK (default)", flush=True)

        except Exception as exc:
            print(f"Error: {exc}", flush=True)
            time.sleep(2)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
