#!/usr/bin/env python3
"""Synchronize MOTU clock ownership with the active audio source."""

from __future__ import annotations

import binascii
import os
import time
from pathlib import Path

import websocket
from camilladsp import CamillaClient


MOTU_WS_URL = os.environ.get("MOTU_WS_URL", "ws://169.254.51.193:1280")
CAMILLA_IP = os.environ.get("CDSP_HOST", "127.0.0.1")
CAMILLA_PORT = int(os.environ.get("CDSP_PORT", "1234"))
CHECK_INTERVAL = float(os.environ.get("MOTU_CHECK_INTERVAL", "1"))
SOURCE_IDS = ("toslink", "streamer", "gadget", "analog")

# MOTU UltraLite mk5 clock-source payloads captured from the web UI.
CLOCK_PAYLOADS = {
    "internal": "000b0000000103",
    "optical": "000b0000000102",
}
# The MOTU re-locks its clock on every clock-source write, even a redundant
# one, which mutes the outputs briefly and clicks audibly. The device keeps
# its clock source across power cycles, so remembering the last value we set
# lets restarts of this service (or CamillaDSP reconnects) stay silent.
STATE_PATH = Path(
    os.environ.get("MOTU_CLOCK_STATE_PATH", "/var/lib/cdsp-automation/motu-clock-source")
)
_next_motu_error_log = 0.0


def read_persisted_clock() -> str | None:
    try:
        value = STATE_PATH.read_text().strip()
    except OSError:
        return None
    return value if value in CLOCK_PAYLOADS else None


def persist_clock(source: str) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        scratch = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        scratch.write_text(source + "\n")
        scratch.replace(STATE_PATH)
    except OSError as exc:
        # Persistence only suppresses redundant writes; keep running without it.
        print(f"MOTU: cannot persist clock state: {exc}", flush=True)


def set_motu_clock(source: str) -> bool:
    global _next_motu_error_log
    payload_hex = CLOCK_PAYLOADS.get(source)
    if payload_hex is None:
        print(f"MOTU: unknown clock source {source}", flush=True)
        return False

    ws = None
    try:
        payload = binascii.unhexlify(payload_hex)
        ws = websocket.WebSocket()
        ws.connect(MOTU_WS_URL, timeout=3)
        ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
        print(f"MOTU: clock source set to {source}", flush=True)
        return True
    except Exception as exc:
        now = time.monotonic()
        if now >= _next_motu_error_log:
            print(f"MOTU error: {exc}", flush=True)
            _next_motu_error_log = now + 30
        return False
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def current_sample_rate(active_config: object) -> int | None:
    if not isinstance(active_config, dict):
        return None
    devices = active_config.get("devices")
    if not isinstance(devices, dict):
        return None
    value = devices.get("samplerate")
    try:
        rate = int(value)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def source_for_config_path(path: object) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    stem = Path(path).stem

    # Legacy configs are named <source>.yml and generated configs are named
    # <source>--<speaker>.yml. Operator-owned speaker configs use the inverse
    # <speaker>-<source>.yml form, for example partymeh-streamer.yml.
    generated_source = stem.split("--", 1)[0]
    if generated_source in SOURCE_IDS:
        return generated_source
    for source in SOURCE_IDS:
        if stem.endswith(f"-{source}"):
            return source
    return None


def main() -> int:
    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    last_clock = read_persisted_clock()
    next_error_log = 0.0

    print("MOTU Clock Sync (source identity mode) started", flush=True)
    print(f"MOTU WebSocket: {MOTU_WS_URL}", flush=True)

    while True:
        try:
            if not cdsp.is_connected():
                cdsp.connect()
                print("Connected to CamillaDSP", flush=True)

            rate = current_sample_rate(cdsp.config.active())
            source = source_for_config_path(cdsp.config.file_path())
            if rate is None or source is None:
                time.sleep(CHECK_INTERVAL)
                continue

            desired_clock = "optical" if source == "toslink" else "internal"
            if desired_clock != last_clock:
                if set_motu_clock(desired_clock):
                    print(
                        f"CamillaDSP source={source}, sample rate={rate} Hz",
                        flush=True,
                    )
                    last_clock = desired_clock
                    persist_clock(desired_clock)

        except Exception as exc:
            # A lost CamillaDSP connection does not change the MOTU clock, so
            # keep last_clock: forgetting it caused an audible re-lock on
            # every CamillaDSP restart.
            now = time.monotonic()
            if now >= next_error_log:
                print(f"CamillaDSP error: {exc}", flush=True)
                next_error_log = now + 30
            time.sleep(2)
            continue

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
