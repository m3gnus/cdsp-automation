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

# MOTU UltraLite mk5 clock-source payloads captured from the web UI.
CLOCK_PAYLOADS = {
    "internal": "000b0000000103",
    "optical": "000b0000000102",
}
_next_motu_error_log = 0.0


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
    value = active_config.get("devices", {}).get("samplerate")
    try:
        rate = int(value)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def source_for_config_path(path: object) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    source = Path(path).stem.split("--", 1)[0]
    return source if source in {"toslink", "streamer", "gadget", "analog"} else None


def main() -> int:
    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    last_clock = None
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

        except Exception as exc:
            last_clock = None
            now = time.monotonic()
            if now >= next_error_log:
                print(f"CamillaDSP error: {exc}", flush=True)
                next_error_log = now + 30
            time.sleep(2)
            continue

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
