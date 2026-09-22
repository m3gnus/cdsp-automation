#!/usr/bin/env python3
"""Read and set the MOTU UltraLite mk5 main output volume.

This is the level MOTU's CueMix 5 app shows as the main volume knob. The
device is reachable only from the Pi (link-local on its USB network), so the
control UI is the one place it can be adjusted.

Protocol (all of it from CueMix 5's own app code, ``ulmk5/dev.js`` and
``ulmk5/datastore.js``; the MOTU calls its binary parameter store a
"datastore", which has nothing to do with the AVB HTTP ``/datastore`` API):

* ``kMainTrim`` is parameter id 5011, type byte, one value (index 0). Its
  range is "0-100 ... 0 to -100 dB": the byte is dB of *attenuation*, so 6
  means -6 dB, 0 is full level, and 100 is shown by CueMix as -inf
  (``iosetup.js`` ``kMainVolCtrlInfo``: min 100, max 0, unit dB, 0 decimals).
* ``kMainGroup`` is parameter id 5012, type int16: one enable bit per output
  DAC, numbered as ``LocalOutputs`` numbers them (Line 5-10 = 0x0-0x5,
  Main 1-2 = 0x6-0x7, Line 3-4 = 0x8-0x9). The main volume scales exactly the
  outputs whose bit is set.
* The device *pushes* ``id(2) index(2) value`` frames; a client *writes*
  ``id(2) index(2) length(2) value`` (``CreateDeviceMessage``). Everything is
  big-endian. A byte parameter therefore arrives as 5 bytes and is written as
  7, an int16 arrives as 6 bytes.

Safety model:

* Writes are bounded by a ceiling from the env file (MOTU_MAIN_VOLUME_MAX_DB),
  enforced here, server-side. The MOTU level sits after CamillaDSP, so the
  profile ``volume_limit`` cannot protect against it.
* Every write happens on a connection that first reads the device's pushed
  state, and is refused unless that state is complete, the main group still
  covers every analog line output, and the level is the one the caller last
  saw. A value that moved behind our back (the front-panel knob) is reported,
  never overwritten blindly.
* The MOTU serves one WebSocket client at a time and every new connection
  drops the source switcher's meter reader. Every connection made here is a
  deferrable access in the window shared with clock_sync (motu_access.py), so
  it never lands within that window of any other extra MOTU access.
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from motu_access import AccessDeferred, AccessUnavailable, MotuAccess

try:
    import websocket
except ImportError:  # the UI must still load without websocket-client
    websocket = None  # type: ignore[assignment]

# RFC 6455 binary frame opcode (websocket.ABNF.OPCODE_BINARY).
OPCODE_BINARY = 0x2


# CueMix 5 dev.js: kMainTrim {id:5011, t:kTByte, "range is 0-100 range is 0 to
# -100 dB"} and kMainGroup {id:5012, t:kTInt16, "Each bit is the enable for
# it's index"}.
MOTU_MAIN_TRIM_PARAM = 5011
MOTU_MAIN_GROUP_PARAM = 5012
# CueMix 5 dev_common.js: kMetersID = 6000. The meter stream starts once the
# full state push is done, so a meter frame before the two values above means
# the device did not report them.
MOTU_METERS_PARAM = 6000
# Attenuation byte CueMix shows as -inf (kMainVolCtrlInfo.min).
MAIN_TRIM_MAX_ATTENUATION = 100
# Every analog line output DAC: Main 1-2 and Line 3-10 (LocalOutputs 0x0-0x9).
# The crossover plays on Main 1-2 (high), Line 3-4 (mid) and Line 5-6 (low);
# requiring the whole set, not just those six, keeps the check independent of
# the routing: whatever feeds the analog outputs, the main volume moves all of
# them together and cannot tilt the crossover.
REQUIRED_MAIN_GROUP = 0x03FF

DEFAULT_MOTU_WS_URL = "ws://169.254.51.193:1280"
# The device's own maximum: 0 dB is the top of the MOTU's main attenuator, the
# same range the front-panel knob already covers, so this control can do
# nothing the knob cannot. MOTU_MAIN_VOLUME_MAX_DB can still impose a lower
# ceiling where one is wanted.
DEFAULT_MAX_DB = 0.0
# A GET reuses what was last read or written this long before reconnecting.
DEFAULT_CACHE_SECONDS = 30.0
READ_TIMEOUT = 3.0


class MotuVolumeError(Exception):
    """A refused or failed main-volume operation, with an HTTP-ish kind."""

    kind = "unavailable"


class MotuVolumeRateLimited(MotuVolumeError):
    kind = "rate_limited"

    def __init__(self, retry_after: float, detail: str = "") -> None:
        super().__init__(
            detail
            or f"the MOTU accepts one client at a time; next access in {retry_after:.0f} s"
        )
        self.retry_after = retry_after


class MotuVolumeConflict(MotuVolumeError):
    kind = "conflict"


class MotuVolumeRefused(MotuVolumeError):
    kind = "refused"


# ---------------------------------------------------------------- encoding


def decode_main_trim(frame: bytes) -> int | None:
    """Attenuation byte if ``frame`` is the device's pushed kMainTrim frame."""
    if len(frame) != 5:
        return None
    if int.from_bytes(frame[0:2], "big") != MOTU_MAIN_TRIM_PARAM:
        return None
    if int.from_bytes(frame[2:4], "big") != 0:
        return None
    return frame[4]


def decode_main_group(frame: bytes) -> int | None:
    """Output bitmask if ``frame`` is the device's pushed kMainGroup frame."""
    if len(frame) != 6:
        return None
    if int.from_bytes(frame[0:2], "big") != MOTU_MAIN_GROUP_PARAM:
        return None
    if int.from_bytes(frame[2:4], "big") != 0:
        return None
    return int.from_bytes(frame[4:6], "big")


def encode_main_trim_write(attenuation: int) -> bytes:
    """The client write frame for kMainTrim, exactly as CueMix 5 builds it.

    datastore.js ``CreateDeviceMessage``, case kTByte: id (2 bytes), index
    (2), length 1 (2), value (1).
    """
    if (
        isinstance(attenuation, bool)
        or not isinstance(attenuation, int)
        or not 0 <= attenuation <= MAIN_TRIM_MAX_ATTENUATION
    ):
        raise ValueError(f"attenuation must be an integer 0-100, not {attenuation!r}")
    return (
        MOTU_MAIN_TRIM_PARAM.to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + bytes([attenuation])
    )


def decode_main_trim_write(frame: bytes) -> int | None:
    """Inverse of :func:`encode_main_trim_write` (write layout, with length)."""
    if len(frame) != 7:
        return None
    if int.from_bytes(frame[0:2], "big") != MOTU_MAIN_TRIM_PARAM:
        return None
    if int.from_bytes(frame[2:4], "big") != 0:
        return None
    if int.from_bytes(frame[4:6], "big") != 1:
        return None
    return frame[6]


def attenuation_to_db(attenuation: int) -> float:
    """dB for an attenuation byte; 100 (-inf in CueMix) is reported as -100."""
    return -float(attenuation)


def db_to_attenuation(volume_db: float) -> int:
    """Nearest attenuation byte, rounding as CueMix does (Math.round)."""
    return int(math.floor(-volume_db + 0.5))


def _finite_db(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be a finite number")
    return numeric


# ------------------------------------------------------------- configuration


def configured_max_db() -> float:
    """The ceiling from MOTU_MAIN_VOLUME_MAX_DB, in dB (never above 0).

    An unparseable value raises instead of falling back to a default: a typo
    in a safety limit must stop writes, not silently pick a level.
    """
    raw = os.environ.get("MOTU_MAIN_VOLUME_MAX_DB", "").strip()
    if not raw:
        return DEFAULT_MAX_DB
    try:
        value = float(raw)
    except ValueError:
        raise MotuVolumeRefused(
            f"MOTU_MAIN_VOLUME_MAX_DB={raw!r} is not a number; writes disabled"
        ) from None
    if not math.isfinite(value):
        raise MotuVolumeRefused(
            f"MOTU_MAIN_VOLUME_MAX_DB={raw!r} is not finite; writes disabled"
        )
    return min(0.0, value)


def min_attenuation(max_db: float) -> int:
    """Smallest attenuation byte the ceiling allows, rounded toward quieter."""
    return max(0, min(MAIN_TRIM_MAX_ATTENUATION, math.ceil(-max_db - 1e-9)))


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def motu_ws_url() -> str:
    return os.environ.get("MOTU_WS_URL", "").strip() or DEFAULT_MOTU_WS_URL


# ------------------------------------------------------------------- device


@dataclass
class DeviceState:
    attenuation: int
    group: int


def _default_connect(url: str, timeout: float) -> Any:
    if websocket is None:
        raise MotuVolumeError("websocket-client is not installed")
    ws = websocket.WebSocket()
    ws.connect(url, timeout=timeout)
    return ws


def read_device_state(ws: Any, timeout: float = READ_TIMEOUT) -> DeviceState:
    """Take kMainTrim and kMainGroup from the state the device pushes.

    Sends nothing. Raises when either value is missing, so a partial read is
    never mistaken for a level.
    """
    attenuation: int | None = None
    group: int | None = None
    deadline = time.monotonic() + timeout
    while attenuation is None or group is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MotuVolumeError("the MOTU did not report its main volume in time")
        ws.settimeout(remaining)
        opcode, data = ws.recv_data()
        if opcode != OPCODE_BINARY:
            continue
        frame = bytes(data)
        if len(frame) >= 2 and int.from_bytes(frame[0:2], "big") == MOTU_METERS_PARAM:
            raise MotuVolumeError("the MOTU state push did not include the main volume")
        trim = decode_main_trim(frame)
        if trim is not None:
            attenuation = trim
            continue
        mask = decode_main_group(frame)
        if mask is not None:
            group = mask
    if attenuation > MAIN_TRIM_MAX_ATTENUATION:
        raise MotuVolumeError(f"the MOTU reported an impossible main trim {attenuation}")
    return DeviceState(attenuation=attenuation, group=group)


def _close(ws: Any) -> None:
    try:
        ws.close()
    except Exception:
        pass


class MotuMainVolume:
    """Rate-limited, ceiling-bounded access to the MOTU main volume."""

    def __init__(
        self,
        *,
        connect: Callable[[str, float], Any] = _default_connect,
        clock: Callable[[], float] = time.monotonic,
        access: MotuAccess | None = None,
    ) -> None:
        self._connect = connect
        self._clock = clock
        # Shared with clock_sync across processes; the thread lock only keeps
        # this process's requests from racing each other.
        self._access = access or MotuAccess(clock=clock)
        self._lock = threading.Lock()
        self._claimed: float | None = None
        self._state: DeviceState | None = None
        self._state_at = 0.0
        self._confirmed = False
        self._error: str | None = None

    # -- helpers
    def _retry_after(self) -> float:
        try:
            return self._access.retry_after()
        except AccessUnavailable:
            return 0.0

    def _open(self) -> Any:
        """Claim the shared access window and connect. Every attempt counts."""
        try:
            # Connect, then read the state dump: two timeouts at most.
            self._claimed = self._access.claim(
                "ui-volume", deferrable=True, hold=2 * READ_TIMEOUT + 1.0
            )
        except AccessDeferred as deferred:
            raise MotuVolumeRateLimited(deferred.retry_after, str(deferred)) from None
        except AccessUnavailable as exc:
            # Uncoordinated, a connection here could land next to a clock
            # write and keep the switcher's meters dark; stay off the device.
            self._error = f"MOTU access cannot be coordinated: {exc}"
            raise MotuVolumeError(self._error) from None
        return self._connect(motu_ws_url(), READ_TIMEOUT)

    def _release(self) -> None:
        """Hand the device back to the switcher's meters as soon as we are done."""
        claimed, self._claimed = self._claimed, None
        release = getattr(self._access, "release", None)
        if release is not None:
            release(claimed)

    def _read(self, ws: Any, now: float) -> DeviceState:
        try:
            state = read_device_state(ws)
        except Exception as exc:
            self._forget(f"MOTU main volume unreadable: {exc}")
            raise MotuVolumeError(str(exc)) from None
        self._state = state
        self._state_at = now
        self._confirmed = True
        self._error = None
        return state

    def _forget(self, error: str) -> None:
        self._state = None
        self._confirmed = False
        self._error = error

    def _payload(self, now: float) -> dict[str, Any]:
        try:
            max_db: float | None = attenuation_to_db(min_attenuation(configured_max_db()))
            config_error = None
        except MotuVolumeRefused as exc:
            max_db = None
            config_error = str(exc)
        state = self._state
        group_ok = state is not None and (state.group & REQUIRED_MAIN_GROUP) == REQUIRED_MAIN_GROUP
        reason = config_error or self._error
        if state is not None and not group_ok:
            reason = (
                f"main group 0x{state.group:04x} does not cover every analog "
                "output; a change would unbalance the crossover"
            )
        return {
            "known": state is not None,
            "volume_db": attenuation_to_db(state.attenuation) if state else None,
            "silent": bool(state and state.attenuation >= MAIN_TRIM_MAX_ATTENUATION),
            "confirmed": self._confirmed if state else False,
            "age_seconds": round(now - self._state_at, 1) if state else None,
            "group": f"0x{state.group:04x}" if state else None,
            "max_db": max_db,
            "min_db": attenuation_to_db(MAIN_TRIM_MAX_ATTENUATION),
            "writable": state is not None and group_ok and config_error is None,
            "reason": reason,
            "retry_after": round(self._retry_after(), 1),
        }

    # -- public API
    def status(self) -> dict[str, Any]:
        """Current level, reading the device only when the cache is stale.

        A read that cannot happen (rate limit) returns what is cached, or
        ``known: False`` - never a default level.
        """
        with self._lock:
            now = self._clock()
            cache = _env_seconds("MOTU_VOLUME_CACHE_SECONDS", DEFAULT_CACHE_SECONDS)
            fresh_enough = (
                self._state is not None and self._confirmed and now - self._state_at <= cache
            )
            if not fresh_enough:
                ws = None
                try:
                    ws = self._open()
                    self._read(ws, now)
                except MotuVolumeError:
                    pass
                except Exception as exc:
                    self._forget(f"MOTU unreachable: {exc}")
                finally:
                    if ws is not None:
                        _close(ws)
                    self._release()
            return self._payload(now)

    def set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set the main volume to ``volume_db``, clamped to the ceiling.

        ``expected_db`` must be the level the caller believes is current (what
        it last read from here). The write is refused unless the device still
        reports exactly that, so a control can never jump from a stale or
        default value.
        """
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        requested = _finite_db(payload.get("volume_db"), "volume_db")
        expected = _finite_db(payload.get("expected_db"), "expected_db")
        floor = min_attenuation(configured_max_db())  # raises on a bad ceiling
        target = min(MAIN_TRIM_MAX_ATTENUATION, max(floor, db_to_attenuation(requested)))
        expected_att = db_to_attenuation(expected)

        with self._lock:
            now = self._clock()
            ws = None
            try:
                try:
                    ws = self._open()
                except MotuVolumeError:
                    raise
                except Exception as exc:
                    self._forget(f"MOTU unreachable: {exc}")
                    raise MotuVolumeError(f"MOTU unreachable: {exc}") from None
                state = self._read(ws, now)
                if (state.group & REQUIRED_MAIN_GROUP) != REQUIRED_MAIN_GROUP:
                    raise MotuVolumeRefused(
                        f"main group 0x{state.group:04x} does not cover every "
                        "analog output; refusing a change that would unbalance "
                        "the crossover"
                    )
                if state.attenuation != expected_att:
                    raise MotuVolumeConflict(
                        f"the MOTU is at {attenuation_to_db(state.attenuation):.0f} dB, "
                        f"not {attenuation_to_db(expected_att):.0f} dB; nothing written"
                    )
                if target != state.attenuation:
                    try:
                        ws.send(encode_main_trim_write(target), opcode=OPCODE_BINARY)
                    except Exception as exc:
                        # The frame may or may not have reached the device, so
                        # neither the old level nor the new one is known. Never
                        # resend on a guess; a later read settles it.
                        self._forget(f"MOTU main volume write outcome unknown: {exc}")
                        raise MotuVolumeError(self._error) from None
                    print(
                        f"MOTU main volume {attenuation_to_db(state.attenuation):.0f} dB"
                        f" -> {attenuation_to_db(target):.0f} dB",
                        flush=True,
                    )
                    # A send that did not raise is not proof the device took
                    # it; the next read (or conflicting write) settles that.
                    self._state = DeviceState(attenuation=target, group=state.group)
                    self._state_at = now
                    self._confirmed = False
            finally:
                if ws is not None:
                    _close(ws)
                self._release()
            result = self._payload(now)
            result["requested_db"] = requested
            result["written"] = target != expected_att
            return result


MAIN_VOLUME = MotuMainVolume()
