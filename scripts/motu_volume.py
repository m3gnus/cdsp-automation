#!/usr/bin/env python3
"""The MOTU UltraLite mk5 main output volume: protocol and request files.

This is the level MOTU's CueMix 5 app shows as the main volume knob. The
device is reachable only from the Pi (link-local on its USB network), so the
control UI is the one place it can be adjusted.

The MOTU serves one WebSocket client at a time, and that client is the source
switcher (source_switcher.MotuConnection). The control UI never connects: it
writes the level it wants to REQUEST_PATH, the switcher applies it on its own
connection and publishes the device's level, and the outcome of the last
request, in STATUS_PATH.

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
  enforced by the switcher when it applies a request. The MOTU level sits
  after CamillaDSP, so the profile ``volume_limit`` cannot protect against it.
* A request is refused unless the device's pushed state is complete, the main
  group still covers every analog line output, and the level is the one the
  caller last saw. A value that moved behind our back (the front-panel knob)
  is reported, never overwritten blindly.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import time
from typing import Any

import settings
from audio_eq import atomic_write_json


# RFC 6455 binary frame opcode (websocket.ABNF.OPCODE_BINARY).
OPCODE_BINARY = 0x2


# CueMix 5 dev.js: kMainTrim {id:5011, t:kTByte, "range is 0-100 range is 0 to
# -100 dB"} and kMainGroup {id:5012, t:kTInt16, "Each bit is the enable for
# it's index"}.
MOTU_MAIN_TRIM_PARAM = 5011
MOTU_MAIN_GROUP_PARAM = 5012
# CueMix 5 dev_common.js: kMetersID = 6000. The meter stream starts once the
# full state push is done.
MOTU_METERS_PARAM = 6000
# Attenuation byte CueMix shows as -inf (kMainVolCtrlInfo.min).
MAIN_TRIM_MAX_ATTENUATION = 100
# Every analog line output DAC: Main 1-2 and Line 3-10 (LocalOutputs 0x0-0x9).
# The crossover plays on Main 1-2 (high), Line 3-4 (mid) and Line 5-6 (low);
# requiring the whole set, not just those six, keeps the check independent of
# the routing: whatever feeds the analog outputs, the main volume moves all of
# them together and cannot tilt the crossover.
REQUIRED_MAIN_GROUP = 0x03FF

# The device's own maximum: 0 dB is the top of the MOTU's main attenuator, the
# same range the front-panel knob already covers, so this control can do
# nothing the knob cannot. MOTU_MAIN_VOLUME_MAX_DB can still impose a lower
# ceiling where one is wanted.
DEFAULT_MAX_DB = 0.0

# Written by the control UI (root), read and removed by the switcher; the
# switcher's runtime directory holds both, like its other request and status
# files.
REQUEST_PATH = settings.MOTU_VOLUME_REQUEST_PATH
STATUS_PATH = settings.MOTU_VOLUME_STATUS_PATH


class MotuVolumeRefused(Exception):
    """A request the configuration forbids (an unusable ceiling)."""


# ---------------------------------------------------------------- encoding


def decode_byte_push(frame: bytes, param: int) -> int | None:
    """Value of a pushed one-byte parameter ``param`` (index 0), else None."""
    if len(frame) != 5:
        return None
    if int.from_bytes(frame[0:2], "big") != param:
        return None
    if int.from_bytes(frame[2:4], "big") != 0:
        return None
    return frame[4]


def decode_main_trim(frame: bytes) -> int | None:
    """Attenuation byte if ``frame`` is the device's pushed kMainTrim frame."""
    return decode_byte_push(frame, MOTU_MAIN_TRIM_PARAM)


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


# ---------------------------------------------------------- request / status


def parse_request(payload: Any) -> tuple[int, int]:
    """(target, expected) attenuation for a UI request, clamped to the ceiling.

    ``expected_db`` must be the level the caller believes is current (what it
    last read). The switcher refuses the write unless the device still reports
    exactly that, so a control can never jump from a stale or default value.
    Raises ValueError for a malformed request and MotuVolumeRefused for an
    unusable ceiling.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    requested = _finite_db(payload.get("volume_db"), "volume_db")
    expected = _finite_db(payload.get("expected_db"), "expected_db")
    floor = min_attenuation(configured_max_db())  # raises on a bad ceiling
    target = min(MAIN_TRIM_MAX_ATTENUATION, max(floor, db_to_attenuation(requested)))
    return target, db_to_attenuation(expected)


def status_payload(
    attenuation: int | None,
    group: int | None,
    *,
    confirmed: bool,
    reason: str | None,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    """What the switcher publishes in STATUS_PATH for the control UI."""
    try:
        max_db: float | None = attenuation_to_db(min_attenuation(configured_max_db()))
        config_error = None
    except MotuVolumeRefused as exc:
        max_db = None
        config_error = str(exc)
    known = attenuation is not None and group is not None
    group_ok = known and (group & REQUIRED_MAIN_GROUP) == REQUIRED_MAIN_GROUP
    reason = config_error or reason
    if known and not group_ok:
        reason = (
            f"main group 0x{group:04x} does not cover every analog "
            "output; a change would unbalance the crossover"
        )
    return {
        "known": known,
        "volume_db": attenuation_to_db(attenuation) if known else None,
        "silent": bool(known and attenuation >= MAIN_TRIM_MAX_ATTENUATION),
        "confirmed": confirmed if known else False,
        "group": f"0x{group:04x}" if known else None,
        "max_db": max_db,
        "min_db": attenuation_to_db(MAIN_TRIM_MAX_ATTENUATION),
        "writable": known and group_ok and config_error is None,
        "reason": reason,
        "result": result,
    }


def unknown_status(reason: str) -> dict[str, Any]:
    return status_payload(None, None, confirmed=False, reason=reason, result=None)


def read_status() -> dict[str, Any]:
    """The switcher's last published status, or an unknown one."""
    try:
        status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return unknown_status("the source switcher has not reported the MOTU")
    if not isinstance(status, dict):
        return unknown_status("the source switcher has not reported the MOTU")
    return status


def take_request() -> dict[str, Any] | None:
    """Read and remove the pending UI request (switcher side)."""
    try:
        raw = REQUEST_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        REQUEST_PATH.unlink()
    except OSError:
        pass
    try:
        request = json.loads(raw)
    except ValueError:
        return {"id": None}
    return request if isinstance(request, dict) else {"id": None}


def submit_request(payload: dict[str, Any], timeout: float) -> tuple[str, dict | None]:
    """Hand a request to the switcher and wait for its result (UI side).

    Only the newest request is kept: one written while an older one is still
    pending replaces it. Returns the request id and the published result, or
    None when the switcher did not answer within ``timeout`` (it is busy, for
    instance inside a source change, and will still apply the request).
    """
    parse_request(payload)  # refuse a malformed request here, synchronously
    request_id = secrets.token_hex(8)
    atomic_write_json(
        REQUEST_PATH,
        {
            "id": request_id,
            "volume_db": payload["volume_db"],
            "expected_db": payload["expected_db"],
        },
    )
    deadline = time.monotonic() + timeout
    while True:
        result = read_status().get("result")
        if isinstance(result, dict) and result.get("id") == request_id:
            return request_id, result
        if time.monotonic() >= deadline:
            return request_id, None
        time.sleep(0.1)
