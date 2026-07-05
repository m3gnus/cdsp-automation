#!/usr/bin/env python3
"""Synchronize MOTU clock source with CamillaDSP sample rate."""

from __future__ import annotations

import binascii
import os
import time

import websocket
from camilladsp import CamillaClient


MOTU_WS_URL = os.environ.get("MOTU_WS_URL", "ws://169.254.51.193:1280")
CAMILLA_IP = os.environ.get("CDSP_HOST", "127.0.0.1")
CAMILLA_PORT = int(os.environ.get("CDSP_PORT", "1234"))
OPTICAL_RATE = int(os.environ.get("MOTU_OPTICAL_RATE", "48000"))
CHECK_INTERVAL = float(os.environ.get("MOTU_CHECK_INTERVAL", "1"))

# MOTU UltraLite mk5 clock-source payloads captured from the web UI.
CLOCK_PAYLOADS = {
    "internal": "000b0000000103",
    "optical": "000b0000000102",
}


def set_motu_clock(source: str) -> None:
    payload_hex = CLOCK_PAYLOADS.get(source)
    if payload_hex is None:
        print(f"MOTU: unknown clock source {source}", flush=True)
        return

    ws = None
    try:
        payload = binascii.unhexlify(payload_hex)
        ws = websocket.WebSocket()
        ws.connect(MOTU_WS_URL, timeout=3)
        ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
        print(f"MOTU: clock source set to {source}", flush=True)
    except Exception as exc:
        print(f"MOTU error: {exc}", flush=True)
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


def main() -> int:
    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    last_rate = None
    next_error_log = 0.0

    print("MOTU Clock Sync (sample rate mode) started", flush=True)
    print(f"MOTU WebSocket: {MOTU_WS_URL}", flush=True)

    while True:
        try:
            if not cdsp.is_connected():
                cdsp.connect()
                print("Connected to CamillaDSP", flush=True)

            rate = current_sample_rate(cdsp.config.active())
            if rate is None:
                time.sleep(CHECK_INTERVAL)
                continue

            if rate != last_rate:
                print(f"CamillaDSP sample rate: {rate} Hz", flush=True)
                set_motu_clock("optical" if rate == OPTICAL_RATE else "internal")
                last_rate = rate

        except Exception as exc:
            last_rate = None
            now = time.monotonic()
            if now >= next_error_log:
                print(f"CamillaDSP error: {exc}", flush=True)
                next_error_log = now + 30
            time.sleep(2)
            continue

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
