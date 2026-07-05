#!/usr/bin/env python3
"""GPIO trigger output controlled by CamillaDSP capture RMS levels."""

from __future__ import annotations

import asyncio
import os
import signal

import RPi.GPIO as GPIO
from camilladsp import CamillaClient


POWER_GPIO = int(os.environ.get("POWER_GPIO", "4"))
CAMILLA_IP = os.environ.get("CDSP_HOST", "127.0.0.1")
CAMILLA_PORT = int(os.environ.get("CDSP_PORT", "1234"))
DELAY_TIME = float(os.environ.get("TRIGGER_DELAY_SECONDS", "320"))
CHECK_INTERVAL = float(os.environ.get("TRIGGER_CHECK_INTERVAL", "0.2"))
AUDIO_THRESHOLD_DB = float(os.environ.get("TRIGGER_AUDIO_THRESHOLD_DB", "-80"))


def music_is_playing(rms_levels: object) -> bool:
    if not isinstance(rms_levels, (list, tuple)):
        return False
    return any(
        isinstance(level, (int, float)) and level > AUDIO_THRESHOLD_DB
        for level in rms_levels
    )


async def relay_control(stop: asyncio.Event) -> None:
    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    silence_seconds = 0.0
    relay_on = False

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(POWER_GPIO, GPIO.OUT, initial=GPIO.LOW)

    try:
        while not stop.is_set():
            try:
                if not cdsp.is_connected():
                    cdsp.connect()
                    print("Connected to CamillaDSP", flush=True)

                if music_is_playing(cdsp.levels.capture_rms()):
                    silence_seconds = 0.0
                    if not relay_on:
                        GPIO.output(POWER_GPIO, GPIO.HIGH)
                        relay_on = True
                        print("Music playing - relay ON", flush=True)
                elif relay_on:
                    silence_seconds += CHECK_INTERVAL
                    if silence_seconds >= DELAY_TIME:
                        GPIO.output(POWER_GPIO, GPIO.LOW)
                        relay_on = False
                        silence_seconds = 0.0
                        print("No music - relay OFF", flush=True)
            except Exception as exc:
                print(f"Error: {exc}", flush=True)
                await asyncio.sleep(2)
                continue

            try:
                await asyncio.wait_for(stop.wait(), timeout=CHECK_INTERVAL)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            GPIO.output(POWER_GPIO, GPIO.LOW)
        finally:
            GPIO.cleanup()


async def main() -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            signal.signal(signum, lambda _sig, _frame: stop.set())

    await relay_control(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
