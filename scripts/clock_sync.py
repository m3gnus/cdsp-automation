#!/usr/bin/env python3
"""Synchronize MOTU clock ownership with the active audio source."""

from __future__ import annotations

import binascii
import contextlib
import os
import time
from pathlib import Path

import websocket
from camilladsp import CamillaClient

from audio_eq import exclusive_file_lock
from motu_access import AccessDeferred, AccessUnavailable, MotuAccess, claim_or_log
from speaker_config import (
    SOURCE_IDS,
    env_managed_config_dirs,
    identify_managed_config,
)


MOTU_WS_URL = os.environ.get("MOTU_WS_URL", "ws://169.254.51.193:1280")
CAMILLA_IP = os.environ.get("CDSP_HOST", "127.0.0.1")
CAMILLA_PORT = int(os.environ.get("CDSP_PORT", "1234"))
CHECK_INTERVAL = float(os.environ.get("MOTU_CHECK_INTERVAL", "1"))

# MOTU UltraLite mk5 clock-source writes: parameter 11 (kClockSource), index
# 0, length 1, then the source value - CueMix 5's own encoding (see below).
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
# device is doing: the clock can also be changed from MOTU's CueMix 5 app,
# and a WebSocket send that did not raise is not evidence that the hardware
# adopted the value. Where the device can be read back, the read-back wins.
STATE_PATH = Path(
    os.environ.get("MOTU_CLOCK_STATE_PATH", "/var/lib/cdsp-automation/motu-clock-source")
)


# The UltraLite mk5 has no HTTP API: port 80 accepts a connection and closes
# it without answering any request. Its only control channel is the binary
# WebSocket on port 1280 that MOTU's CueMix 5 app uses. Every message is
#
#   parameter id (u16 BE) | index (u16 BE) | [length (u16 BE), sent only by
#   the client] | value
#
# and on every new connection the device pushes its whole parameter set,
# unsolicited, before the meter stream starts. The clock source is parameter
# 11 (CueMix 5 ``kClockSource``, a single byte), so reading it back means
# connecting, *sending nothing*, and waiting for that one frame. The ids and
# values below are the ones CueMix 5 itself defines (``dev.js``:
# ``kClockSource`` id 11; ``kClockSources`` Internal=3, S/PDIF=0, Optical=2),
# and CLOCK_PAYLOADS above are exactly its write encoding of them.
#
# The device serves one WebSocket client at a time: a new connection drops
# the previous one (the source switcher's meter reader, or a CueMix window),
# which simply reconnects. So each read-back is a short connection, and a
# confirmed clock is re-checked rarely.
MOTU_CLOCK_SOURCE_PARAM = 11
MOTU_CLOCK_SOURCE_VALUES = {3: "internal", 2: "optical"}
MOTU_READBACK_TIMEOUT = float(os.environ.get("MOTU_CLOCK_READBACK_TIMEOUT", "3"))
# How often a *confirmed* clock value is re-checked; this is what notices a
# clock changed behind our back from CueMix 5.
MOTU_VERIFY_INTERVAL = float(os.environ.get("MOTU_CLOCK_VERIFY_INTERVAL", "300"))
# How long to wait before asking again after a read-back failed.
MOTU_READBACK_RETRY_INTERVAL = float(
    os.environ.get("MOTU_CLOCK_READBACK_RETRY_INTERVAL", "300")
)
# The shortest gap between two writes of the same clock source. A read-back
# that contradicts a write we just made means the device did not take it;
# writing again at once, every pass, would re-lock (and click) every second.
MOTU_REWRITE_INTERVAL = float(os.environ.get("MOTU_CLOCK_REWRITE_INTERVAL", "30"))

_next_motu_error_log = 0.0


def switcher_manages_clock() -> bool:
    """True when the source switcher, not this daemon, writes the clock.

    Mirrors the switcher's own ``SOURCE_MOTU_CLOCK`` rule from this side: in
    ``auto`` it owns the clock whenever it is installed next to this unit.
    Then every write happens there, muted and after the output has gone
    silent -- including corrections -- and this daemon only verifies.
    """
    setting = os.environ.get("SOURCE_MOTU_CLOCK", "auto").strip().lower()
    if setting in {"0", "false", "no", "off"}:
        return False
    unit = Path(
        os.environ.get(
            "SOURCE_SWITCHER_UNIT_PATH",
            "/etc/systemd/system/cdsp-source-switcher.service",
        )
    )
    return unit.exists()


@contextlib.contextmanager
def transition_guard():
    """Hold the audio-control lock around one clock decision and write.

    Yields whether the lock is held.  A standalone daemon (no switcher) takes
    it so its write can never interleave with a volume or mute change.  A lock
    that cannot be taken yields False and the caller skips the write: an
    uncoordinated clock change is exactly what the lock exists to prevent.
    """
    path = Path(
        os.environ.get(
            "AUDIO_CONTROL_LOCK_PATH", "/var/lib/cdsp-automation/audio-control.lock"
        )
    )
    try:
        guard = exclusive_file_lock(path)
        guard.__enter__()
    except OSError as exc:
        _log_motu_error(f"MOTU: audio-control lock unavailable ({exc}); clock write skipped")
        yield False
        return
    try:
        yield True
    finally:
        guard.__exit__(None, None, None)


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

    # A clock write is the one MOTU access that never waits for the shared
    # window (a source change is audible until it lands); it only records
    # itself, so read-backs and UI volume accesses keep clear of it.
    claim_or_log("clock-write")
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


def clock_source_value(frame: bytes) -> int | None:
    """The raw clock-source byte if ``frame`` is the device's clock frame.

    The device reports a byte parameter as ``id, index, value`` - five bytes,
    with no length field. Any other frame (other parameters, meters) is None.
    """
    if len(frame) != 5:
        return None
    param = int.from_bytes(frame[0:2], "big")
    index = int.from_bytes(frame[2:4], "big")
    if param != MOTU_CLOCK_SOURCE_PARAM or index != 0:
        return None
    return frame[4]


def read_motu_clock() -> str | None:
    """Read the clock source the MOTU is *actually* using.

    Connects to the control WebSocket, sends nothing, and takes the clock
    source from the state the device pushes to every new client. Returns None
    when the device cannot be read or reports a source we do not drive
    (S/PDIF). None is "unknown", never "wrong": callers fall back to the
    cached value rather than forcing an audible re-lock on a guess.

    A read-back is deferrable: inside the shared MOTU access window it raises
    AccessDeferred (with ``retry_after``) without connecting, and it raises
    AccessUnavailable when the shared record cannot be used, since connecting
    uncoordinated could keep the switcher's meters dark.
    """
    MotuAccess().claim("clock-readback", deferrable=True)
    ws = None
    value = None
    try:
        ws = websocket.WebSocket()
        ws.connect(MOTU_WS_URL, timeout=MOTU_READBACK_TIMEOUT)
        deadline = time.monotonic() + MOTU_READBACK_TIMEOUT
        while value is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no clock-source frame from the device")
            ws.settimeout(remaining)
            opcode, data = ws.recv_data()
            if opcode == websocket.ABNF.OPCODE_BINARY:
                value = clock_source_value(bytes(data))
    except Exception as exc:
        _log_motu_error(f"MOTU: clock read-back unavailable: {exc}")
        return None
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    clock = MOTU_CLOCK_SOURCE_VALUES.get(value)
    if clock is None:
        _log_motu_error(f"MOTU: clock source {value} is neither internal nor optical")
    return clock


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
    last_write: str | None = None
    rewrite_not_before = 0.0
    known_path: str | None = None
    known_source: str | None = None

    print("MOTU Clock Sync (source identity mode) started", flush=True)
    print(f"MOTU WebSocket: {MOTU_WS_URL}", flush=True)

    while True:
        try:
            if not cdsp.is_connected():
                cdsp.connect()
                print("Connected to CamillaDSP", flush=True)

            def observe() -> tuple[int | None, str | None]:
                nonlocal known_path, known_source
                rate = current_sample_rate(cdsp.config.active())
                config_path = cdsp.config.file_path()
                # Identifying a generated config reads and digests it; the
                # path is content-addressed and immutable, so remember it.
                if config_path != known_path or known_source is None:
                    known_path = config_path
                    known_source = source_for_config_path(config_path)
                return rate, known_source

            def adopt_shared_clock() -> None:
                """Take a clock the switcher wrote as our own last request."""
                nonlocal last_clock, verified, next_readback
                shared = read_persisted_clock()
                if shared is not None and shared != last_clock:
                    last_clock = shared
                    verified = False
                    next_readback = 0.0

            adopt_shared_clock()
            rate, source = observe()
            if rate is None or source is None:
                time.sleep(CHECK_INTERVAL)
                continue

            desired_clock = "optical" if source == "toslink" else "internal"
            now = time.monotonic()
            # Only verify when the cache claims there is nothing to do: that
            # is the case where trusting it wrongly leaves the clock wrong,
            # and a clock change that is already due must never wait behind
            # a read-back that a silent device can stall until it times out.
            if desired_clock == last_clock and now >= next_readback:
                try:
                    actual = read_motu_clock()
                except AccessDeferred as deferred:
                    # Another MOTU access (often our own clock write just
                    # now) is too recent: a second one would keep the
                    # switcher's meters down. Verify once the window opens.
                    next_readback = now + deferred.retry_after
                    time.sleep(CHECK_INTERVAL)
                    continue
                except AccessUnavailable as exc:
                    _log_motu_error(f"MOTU: clock read-back skipped: {exc}")
                    actual = None
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

            if desired_clock != last_clock and switcher_manages_clock():
                # The switcher corrects this itself, muted; a write from here
                # could land on live audio.  Keep verifying only.
                pass
            elif desired_clock != last_clock:
                with transition_guard() as locked:
                    # Re-decide under the lock: a transition that finished
                    # while we waited has already moved both the config and
                    # the clock, and the answer may now be "nothing to do".
                    adopt_shared_clock()
                    rate, source = observe()
                    if source is not None:
                        desired_clock = (
                            "optical" if source == "toslink" else "internal"
                        )
                    if (
                        not locked
                        or rate is None
                        or source is None
                        or desired_clock == last_clock
                    ):
                        pass
                    elif desired_clock == last_write and now < rewrite_not_before:
                        # The device just contradicted this very write. Asking
                        # again every pass would re-lock it every second.
                        pass
                    elif set_motu_clock(desired_clock):
                        print(
                            f"CamillaDSP source={source}, sample rate={rate} Hz"
                            + ("" if verified else " (clock unconfirmed)"),
                            flush=True,
                        )
                        # A send that did not raise is not proof. Record it as
                        # the cached belief, then confirm it on the next pass:
                        # a device that ignored the write is written again
                        # instead of being latched as done.
                        last_clock = desired_clock
                        last_write = desired_clock
                        rewrite_not_before = now + MOTU_REWRITE_INTERVAL
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
