#!/usr/bin/env python3
"""Synchronize MOTU clock ownership with the active audio source."""

from __future__ import annotations

import binascii
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websocket
from camilladsp import CamillaClient

from speaker_config import (
    SOURCE_IDS,
    env_managed_config_dirs,
    identify_managed_config,
)


MOTU_WS_URL = os.environ.get("MOTU_WS_URL", "ws://169.254.51.193:1280")
CAMILLA_IP = os.environ.get("CDSP_HOST", "127.0.0.1")
CAMILLA_PORT = int(os.environ.get("CDSP_PORT", "1234"))
CHECK_INTERVAL = float(os.environ.get("MOTU_CHECK_INTERVAL", "1"))

# MOTU UltraLite mk5 clock-source payloads captured from the web UI.
CLOCK_PAYLOADS = {
    "internal": "000b0000000103",
    "optical": "000b0000000102",
}
# The MOTU re-locks its clock on every clock-source write, even a redundant
# one, which mutes the outputs briefly and clicks audibly. The device keeps
# its clock source across power cycles, so remembering the last value we set
# lets restarts of this service (or CamillaDSP reconnects) stay silent.
#
# This file is a *cache of what we last asked for*, not proof of what the
# device is doing: the clock can also be changed from the MOTU's own web UI,
# and a WebSocket send that did not raise is not evidence that the hardware
# adopted the value. Where the device can be read back, the read-back wins.
STATE_PATH = Path(
    os.environ.get("MOTU_CLOCK_STATE_PATH", "/var/lib/cdsp-automation/motu-clock-source")
)


def _datastore_url(ws_url: str) -> str:
    """Derive the MOTU AVB datastore URL from the WebSocket URL."""
    try:
        parsed = urllib.parse.urlsplit(ws_url)
        host = parsed.hostname
    except ValueError:
        return ""
    if not host:
        return ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
    return f"{scheme}://{host}/datastore"


# The web UI's binary WebSocket is write-only for our purposes; the device
# publishes its current settings over the plain HTTP datastore document. A
# deployment whose interface does not serve one simply never gets a read-back,
# and the daemon falls back to the cached value exactly as it always did.
# Unset derives the URL from the WebSocket host; set-but-empty is an explicit
# "this interface has no datastore, stop asking". Without that distinction an
# empty value fell back to the derived URL, so a USB-only interface such as an
# UltraLite mk5 -- which answers port 80 but serves no datastore -- could not
# be told to stop, and logged a failed read-back every retry interval forever.
_DATASTORE_URL_ENV = os.environ.get("MOTU_DATASTORE_URL")
MOTU_DATASTORE_URL = (
    _datastore_url(MOTU_WS_URL)
    if _DATASTORE_URL_ENV is None
    else _DATASTORE_URL_ENV.strip()
)
MOTU_READBACK_TIMEOUT = float(os.environ.get("MOTU_CLOCK_READBACK_TIMEOUT", "3"))
# How often a *confirmed* clock value is re-checked. It is one small HTTP GET,
# and it is what notices a clock changed behind our back from the MOTU web UI.
MOTU_VERIFY_INTERVAL = float(os.environ.get("MOTU_CLOCK_VERIFY_INTERVAL", "60"))
# How long to wait before asking again after a read-back failed: an interface
# without the datastore API must not be polled every second forever.
MOTU_READBACK_RETRY_INTERVAL = float(
    os.environ.get("MOTU_CLOCK_READBACK_RETRY_INTERVAL", "300")
)
MOTU_READBACK_MAX_BYTES = 1 << 20

_next_motu_error_log = 0.0


def _log_motu_error(message: str) -> None:
    """Log MOTU trouble at most every 30 s: it can fail once per poll."""
    global _next_motu_error_log
    now = time.monotonic()
    if now >= _next_motu_error_log:
        print(message, flush=True)
        _next_motu_error_log = now + 30


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
    """Ask the MOTU to change clock source.

    A ``True`` return means the command left this host, not that the device
    applied it - only :func:`read_motu_clock` can say that.
    """
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
        _log_motu_error(f"MOTU error: {exc}")
        return False
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


# Our two clock names against the words a MOTU uses for its own sources.
# Anything the device reports that matches neither is "unknown", so the
# daemon keeps its hands off a clock it does not understand.
_CLOCK_NAME_HINTS = (
    ("internal", "internal"),
    ("optical", "optical"),
    ("toslink", "optical"),
    ("spdif", "optical"),
    ("s/pdif", "optical"),
    ("adat", "optical"),
)
_CLOCK_CHOICE_KEYS = {
    "clocksourcestrings",
    "clocksourcenames",
    "clocksourcelist",
    "clocksources",
}


def _leaf(key: str) -> str:
    return key.rsplit("/", 1)[-1].lower()


def _interpret_clock_name(name: object) -> str | None:
    if not isinstance(name, str):
        return None
    text = name.strip().lower()
    if not text:
        return None
    for hint, clock in _CLOCK_NAME_HINTS:
        if hint in text:
            return clock
    return None


def _clock_choices(payload: dict, prefix: str) -> list[str]:
    """The device's own list of clock-source names, for numeric selections."""
    for key, value in payload.items():
        if not isinstance(key, str) or _leaf(key) not in _CLOCK_CHOICE_KEYS:
            continue
        key_prefix = key.rsplit("/", 1)[0] if "/" in key else ""
        if key_prefix != prefix:
            continue
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        if isinstance(value, str):
            separator = ":" if ":" in value else ","
            return [item for item in value.split(separator) if item]
    return []


def clock_from_datastore(payload: object) -> str | None:
    """Map a MOTU datastore document to ``internal``/``optical``, or None.

    None means "the document does not tell us", never "the clock is wrong".
    """
    if not isinstance(payload, dict):
        return None
    for key, value in payload.items():
        if not isinstance(key, str) or _leaf(key) != "clocksource":
            continue
        prefix = key.rsplit("/", 1)[0] if "/" in key else ""
        clock = _interpret_clock_name(value)
        if clock:
            return clock
        # Some firmware reports the selection as an index into the device's
        # own list of source names.
        if isinstance(value, bool):
            continue
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        choices = _clock_choices(payload, prefix)
        if 0 <= index < len(choices):
            clock = _interpret_clock_name(choices[index])
            if clock:
                return clock
    return None


def read_motu_clock() -> str | None:
    """Read the clock source the MOTU is *actually* using.

    Returns None when the device cannot be read or reports something we do
    not recognize. None is "unknown", never "wrong": callers fall back to the
    cached value rather than forcing an audible re-lock on a guess.
    """
    if not MOTU_DATASTORE_URL:
        return None
    try:
        request = urllib.request.Request(
            MOTU_DATASTORE_URL, headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=MOTU_READBACK_TIMEOUT) as response:
            raw = response.read(MOTU_READBACK_MAX_BYTES)
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:
        _log_motu_error(f"MOTU: clock read-back unavailable: {exc}")
        return None
    return clock_from_datastore(payload)


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


def conventional_source(path: object) -> str | None:
    """Last-resort identity for configs the speaker catalog does not describe.

    Only the two names this project generates itself are accepted: the default
    speaker's plain ``<source>.yml`` and a generated ``<source>--<speaker>.yml``.
    An operator config is named by the catalog and must be identified through
    it, so no ``<speaker>-<source>.yml`` guess is made here - guessing is
    exactly what let audio switching and clock switching disagree.
    """
    if not isinstance(path, str) or not path:
        return None
    source = Path(path).stem.split("--", 1)[0]
    return source if source in SOURCE_IDS else None


def source_for_config_path(path: object) -> str | None:
    """Name the source owning a CamillaDSP config path.

    The speaker catalog is the authority, through the same
    :func:`identify_managed_config` the source switcher and the control UI
    use, so an operator config mapped to an arbitrary filename still selects
    the right clock.
    """
    config_dir, generated_dir, default_speaker_id = env_managed_config_dirs()
    identity = identify_managed_config(
        path,
        config_dir=config_dir,
        generated_dir=generated_dir,
        default_speaker_id=default_speaker_id,
    )
    if identity is not None:
        return identity[0]
    return conventional_source(path)


def main() -> int:
    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    # The persisted value is a cache of our last request, not proof of the
    # device's state, so start out unverified: the first read-back settles
    # what the MOTU is really clocked to before anything is written.
    last_clock = read_persisted_clock()
    verified = False
    next_readback = 0.0
    next_error_log = 0.0
    known_path: str | None = None
    known_source: str | None = None

    print("MOTU Clock Sync (source identity mode) started", flush=True)
    print(f"MOTU WebSocket: {MOTU_WS_URL}", flush=True)
    print(f"MOTU read-back: {MOTU_DATASTORE_URL or 'disabled'}", flush=True)

    while True:
        try:
            if not cdsp.is_connected():
                cdsp.connect()
                print("Connected to CamillaDSP", flush=True)

            rate = current_sample_rate(cdsp.config.active())
            config_path = cdsp.config.file_path()
            # Identifying a generated config reads and digests it; the path is
            # content-addressed and immutable, so remember the answer.
            if config_path != known_path or known_source is None:
                known_path = config_path
                known_source = source_for_config_path(config_path)
            source = known_source
            if rate is None or source is None:
                time.sleep(CHECK_INTERVAL)
                continue

            desired_clock = "optical" if source == "toslink" else "internal"
            now = time.monotonic()
            # Only verify when the cache claims there is nothing to do: that
            # is the case where trusting it wrongly leaves the clock wrong,
            # and a clock change that is already due must never wait behind
            # an HTTP read that a silent device can stall until it times out.
            if desired_clock == last_clock and now >= next_readback:
                actual = read_motu_clock()
                if actual is None:
                    verified = False
                    next_readback = now + MOTU_READBACK_RETRY_INTERVAL
                else:
                    verified = True
                    next_readback = now + MOTU_VERIFY_INTERVAL
                    if actual != last_clock:
                        print(
                            f"MOTU: clock source is {actual}, not the "
                            f"remembered {last_clock}",
                            flush=True,
                        )
                        last_clock = actual
                        persist_clock(actual)

            if desired_clock != last_clock:
                if set_motu_clock(desired_clock):
                    print(
                        f"CamillaDSP source={source}, sample rate={rate} Hz"
                        + ("" if verified else " (clock unconfirmed)"),
                        flush=True,
                    )
                    # A send that did not raise is not proof. Record it as the
                    # cached belief, then confirm it on the next pass: a device
                    # that ignored the write is written again instead of being
                    # latched as done.
                    last_clock = desired_clock
                    verified = False
                    next_readback = 0.0
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
