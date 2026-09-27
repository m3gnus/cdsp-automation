"""MOTU UltraLite mk5 main output volume: decoding, write encoding, guards,
and the control UI's request -> source switcher -> status path.

Every device frame here comes from the real connect dump in
tests/fixtures/motu_ultralite_mk5_connect_dump.json. No test talks to a MOTU.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

if "camilladsp" not in sys.modules:
    camilladsp = types.ModuleType("camilladsp")
    camilladsp.CamillaClient = object
    sys.modules["camilladsp"] = camilladsp

import motu_volume
import source_switcher


FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAIN_TRIM_FRAME = bytes.fromhex("1393000006")
MAIN_GROUP_FRAME = bytes.fromhex("1394000003ff")
CLOCK_FRAME = bytes.fromhex("000b000003")  # kClockSource = 3, internal


def connect_dump() -> list[bytes]:
    """The frames a real UltraLite mk5 pushed to a fresh WebSocket client."""
    document = json.loads(
        (FIXTURES / "motu_ultralite_mk5_connect_dump.json").read_text()
    )
    return [bytes.fromhex(frame) for frame in document["frames_hex"]]


class ReplaySocket:
    """A WebSocket stand-in replaying device frames, recording sends."""

    def __init__(self, device: "Device") -> None:
        self.device = device
        self.frames: list[bytes] = []
        self.sent: list[bytes] = []
        self.closed = False

    def connect(self, *_args: object, **_kwargs: object) -> None:
        if self.device.fail:
            raise OSError("no route to host")
        self.device.connections += 1
        self.frames = list(self.device.frames)

    def settimeout(self, _timeout: float) -> None:
        pass

    def recv_data(self, *_args: object, **_kwargs: object) -> tuple[int, bytes]:
        if self.closed:
            raise ConnectionResetError("closed")
        if not self.frames:
            raise TimeoutError("timed out")
        return motu_volume.OPCODE_BINARY, self.frames.pop(0)

    def send(self, payload: bytes, *_args: object, **_kwargs: object) -> None:
        self.sent.append(payload)
        if self.device.send_error is not None:
            # Recorded first: the frame may well have reached the device.
            raise self.device.send_error
        self.device.apply(payload)

    def close(self) -> None:
        self.closed = True


class Device:
    """A one-client MOTU: every connection gets the current state dump.

    A write changes what the *next* connection is told; nothing is echoed to
    the writer, the conservative reading of the real device.
    """

    def __init__(
        self,
        frames: list[bytes] | None = None,
        *,
        fail: bool = False,
        send_error: Exception | None = None,
        ignores_writes: bool = False,
    ) -> None:
        self.frames = connect_dump() if frames is None else frames
        self.fail = fail
        self.send_error = send_error
        self.ignores_writes = ignores_writes
        self.connections = 0
        self.sockets: list[ReplaySocket] = []

    def socket(self) -> ReplaySocket:
        ws = ReplaySocket(self)
        self.sockets.append(ws)
        return ws

    def apply(self, write: bytes) -> None:
        if self.ignores_writes or len(write) != 7:
            return
        pushed = write[:4] + write[6:]  # the push layout has no length field
        self.frames = [pushed if f[:4] == write[:4] and len(f) == 5 else f for f in self.frames]

    @property
    def sent(self) -> list[bytes]:
        return [frame for ws in self.sockets for frame in ws.sent]

    def module(self) -> SimpleNamespace:
        """What source_switcher sees as the websocket-client module."""
        return SimpleNamespace(WebSocket=self.socket, WebSocketTimeoutException=TimeoutError)


@contextlib.contextmanager
def switcher_on(device: Device):
    """A source-switcher MOTU connection talking to ``device``."""
    motu = source_switcher.MotuConnection("ws://motu:1280")
    with (
        mock.patch.object(source_switcher, "websocket", device.module()),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        yield motu


def request(motu, payload: dict) -> dict:
    """UI side writes a request, switcher serves it; the published status."""
    request_id, result = motu_volume.submit_request(payload, timeout=0)
    assert result is None  # nothing answered before the switcher ran
    motu.serve_volume_request()
    status = motu_volume.read_status()
    assert status["result"]["id"] == request_id
    assert not motu_volume.REQUEST_PATH.exists()
    return status


@pytest.fixture(autouse=True)
def default_settings():
    env = {k: v for k, v in os.environ.items() if k != "MOTU_MAIN_VOLUME_MAX_DB"}
    with mock.patch.dict(os.environ, env, clear=True):
        yield


# ------------------------------------------------------------------ decoding


def test_only_the_pushed_main_trim_frame_decodes_as_the_main_volume() -> None:
    """The captured device reports kMainTrim=6: -6 dB, the knob's reading."""
    decoded = [
        (frame.hex(), motu_volume.decode_main_trim(frame))
        for frame in connect_dump()
        if motu_volume.decode_main_trim(frame) is not None
    ]
    assert decoded == [("1393000006", 6)]
    groups = [
        motu_volume.decode_main_group(frame)
        for frame in connect_dump()
        if motu_volume.decode_main_group(frame) is not None
    ]
    # Main 1-2 and Line 3-10: every analog line DAC, so all six crossover
    # outputs (Main 1-2, Line 3-4, Line 5-6) move together.
    assert groups == [0x03FF] == [motu_volume.REQUIRED_MAIN_GROUP]


def test_write_encoding_round_trips_with_the_pushed_value() -> None:
    """Writing -6 dB carries the very byte the device pushed, in the write
    layout (with the length field), exactly as CueMix's CreateDeviceMessage
    builds a kTByte write."""
    pushed = motu_volume.decode_main_trim(MAIN_TRIM_FRAME)
    written = motu_volume.encode_main_trim_write(motu_volume.db_to_attenuation(-6.0))

    assert written == bytes.fromhex("13930000000106")
    #                      id 5011 | index 0 | length 1 | value 6
    assert written[:4] == MAIN_TRIM_FRAME[:4]
    assert written[4:6] == b"\x00\x01"
    assert written[6] == MAIN_TRIM_FRAME[4] == pushed == 6
    # A write never parses as a push.
    assert motu_volume.decode_main_trim(written) is None
    # Same shape as the clock write already proven on this device.
    assert len(written) == len(source_switcher.MOTU_CLOCK_WRITES["internal"])

    for attenuation in range(0, 101):
        frame = motu_volume.encode_main_trim_write(attenuation)
        assert frame[6] == attenuation
        assert motu_volume.db_to_attenuation(
            motu_volume.attenuation_to_db(attenuation)
        ) == attenuation


@pytest.mark.parametrize("bad", [-1, 101, 255, 6.0, True, None])
def test_write_encoding_refuses_values_outside_the_trim_range(bad: object) -> None:
    with pytest.raises(ValueError):
        motu_volume.encode_main_trim_write(bad)  # type: ignore[arg-type]


# ------------------------------------------------------------------- ceiling


def test_default_ceiling_is_the_full_hardware_range() -> None:
    """Unset, the control reaches everything the front-panel knob can."""
    assert motu_volume.configured_max_db() == 0.0
    assert motu_volume.min_attenuation(0.0) == 0
    # An explicit ceiling still caps it.
    assert motu_volume.min_attenuation(-6.0) == 6
    # Fractional ceilings round toward quieter, never louder.
    assert motu_volume.min_attenuation(-6.4) == 7
    # A positive ceiling means nothing on an attenuator: 0 dB is the top.
    with mock.patch.dict(os.environ, {"MOTU_MAIN_VOLUME_MAX_DB": "12"}):
        assert motu_volume.configured_max_db() == 0.0


def test_ceiling_is_enforced_by_the_switcher_that_writes() -> None:
    device = Device()
    with switcher_on(device) as motu:
        motu.connect()
        status = request(motu, {"volume_db": 0, "expected_db": -6})
    # Full range by default: from -6 dB, a request for 0 dB is written as asked.
    assert device.sent == [motu_volume.encode_main_trim_write(0)]
    assert status["volume_db"] == 0.0 and status["result"]["written"] is True

    device = Device()
    with (
        mock.patch.dict(os.environ, {"MOTU_MAIN_VOLUME_MAX_DB": "-10"}),
        switcher_on(device) as motu,
    ):
        motu.connect()
        status = request(motu, {"volume_db": 0, "expected_db": -6})
    # Clamped to the ceiling: the only write is -10 dB.
    assert device.sent == [motu_volume.encode_main_trim_write(10)]
    assert status["volume_db"] == -10.0
    assert status["max_db"] == -10.0


def test_unparseable_ceiling_refuses_to_write() -> None:
    device = Device()
    with (
        mock.patch.dict(os.environ, {"MOTU_MAIN_VOLUME_MAX_DB": "loud"}),
        switcher_on(device) as motu,
    ):
        with pytest.raises(motu_volume.MotuVolumeRefused):
            motu_volume.submit_request({"volume_db": -20, "expected_db": -6}, timeout=0)
        motu.connect()
        motu.serve_volume_request()
    status = motu_volume.read_status()
    assert device.sent == []
    assert status["writable"] is False and status["max_db"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"volume_db": -20},  # no expected level: could jump from a default
        {"volume_db": float("nan"), "expected_db": -6},
        {"volume_db": True, "expected_db": -6},
        {"volume_db": "-20", "expected_db": -6},
        {"expected_db": -6},
        "loud",
    ],
)
def test_malformed_requests_are_refused_before_they_reach_the_switcher(
    payload: object,
) -> None:
    with pytest.raises(ValueError):
        motu_volume.submit_request(payload, timeout=0)  # type: ignore[arg-type]
    assert not motu_volume.REQUEST_PATH.exists()


def test_a_malformed_request_file_is_refused_by_the_switcher_too() -> None:
    device = Device()
    motu_volume.REQUEST_PATH.write_text('{"id": "x", "volume_db": "-20"}')
    with switcher_on(device) as motu:
        motu.connect()
        motu.serve_volume_request()
    result = motu_volume.read_status()["result"]
    assert result["id"] == "x" and result["ok"] is False
    assert device.sent == []


# ----------------------------------------------- request -> switcher -> status


def test_a_ui_request_is_written_on_the_switchers_own_connection() -> None:
    """One socket, no second client: the write rides the meter connection."""
    device = Device()
    with switcher_on(device) as motu:
        motu.read()
        motu.serve_volume_request()
        before = motu_volume.read_status()
        assert before["known"] is True and before["volume_db"] == -6.0
        assert before["confirmed"] is True and before["writable"] is True

        status = request(motu, {"volume_db": -20.4, "expected_db": -6})
        assert device.connections == 1
        assert device.sockets[0].sent == [bytes.fromhex("13930000000114")]  # 20 dB
        assert status["volume_db"] == -20.0
        assert status["confirmed"] is False  # a send is not a push back
        assert status["result"]["ok"] is True

        # The device pushes the level: now it is a reading, not a belief.
        device.sockets[0].frames.append(bytes.fromhex("1393000014"))
        motu.read()
        motu.serve_volume_request()
    assert motu_volume.read_status()["confirmed"] is True


def test_a_knob_moved_since_the_page_read_is_a_conflict_not_a_write() -> None:
    device = Device()
    with switcher_on(device) as motu:
        motu.connect()
        status = request(motu, {"volume_db": -9, "expected_db": -10})
    assert device.sent == []
    assert status["result"]["kind"] == "conflict"
    assert status["volume_db"] == -6.0 and status["confirmed"] is True


def test_unreachable_device_reads_unknown_and_refuses_to_write() -> None:
    device = Device(fail=True)
    with switcher_on(device) as motu:
        motu.read()
        motu.serve_volume_request()
        status = motu_volume.read_status()
        assert status["known"] is False and status["volume_db"] is None
        assert status["writable"] is False
        status = request(motu, {"volume_db": -20, "expected_db": -6})
    assert status["result"]["kind"] == "unavailable"
    assert device.sent == []


def test_state_push_without_the_main_volume_is_unknown_not_a_default() -> None:
    # The real dump, cut just before the kMainGroup/kMainTrim frames and
    # ending on its real first meter frame.
    frames = connect_dump()
    cut = frames.index(MAIN_GROUP_FRAME)
    meter = next(f for f in frames if f[:2] == (6000).to_bytes(2, "big"))
    device = Device(frames[:cut] + [meter])
    with switcher_on(device) as motu:
        motu.connect()
        status = request(motu, {"volume_db": -20, "expected_db": -6})
    assert status["known"] is False
    assert status["result"]["kind"] == "unavailable"
    assert device.sent == []


def test_a_write_whose_send_fails_leaves_the_level_unknown_not_confirmed() -> None:
    """The send raised after the frame may have reached the device: the
    pre-write reading is no longer known to hold, and nothing is resent."""
    device = Device(send_error=ConnectionResetError("connection reset by peer"))
    with switcher_on(device) as motu:
        motu.connect()
        status = request(motu, {"volume_db": -20, "expected_db": -6})
    assert "outcome unknown" in status["result"]["error"]
    assert device.sent == [motu_volume.encode_main_trim_write(20)]
    assert device.sockets[0].closed is True
    assert status["known"] is False
    assert status["volume_db"] is None and status["confirmed"] is False
    assert "outcome unknown" in status["reason"]


def test_main_group_missing_a_speaker_output_refuses_the_write() -> None:
    # The real dump with one bit (Line 5, LocalOutputs 0x0 - the low driver's
    # left side) cleared from its real kMainGroup frame.
    partial = bytes.fromhex("1394000003fe")
    device = Device([partial if f == MAIN_GROUP_FRAME else f for f in connect_dump()])
    with switcher_on(device) as motu:
        motu.connect()
        status = request(motu, {"volume_db": -20, "expected_db": -6})
    assert device.sent == []
    assert status["result"]["kind"] == "refused"
    assert status["known"] is True and status["writable"] is False
    assert "crossover" in status["reason"]


def test_only_the_newest_pending_request_is_applied() -> None:
    device = Device()
    with switcher_on(device) as motu:
        motu.connect()
        motu_volume.submit_request({"volume_db": -30, "expected_db": -6}, timeout=0)
        status = request(motu, {"volume_db": -12, "expected_db": -6})
    assert device.sent == [motu_volume.encode_main_trim_write(12)]
    assert status["volume_db"] == -12.0


# ------------------------------------------------ clock on the same connection


@contextlib.contextmanager
def clock_writes_on(device: Device):
    """The switcher's connection as ``_motu``, with silence already reached."""
    with (
        switcher_on(device) as motu,
        mock.patch.object(source_switcher, "_motu", motu),
        mock.patch.object(source_switcher, "_await_output_silence", return_value=True),
        mock.patch.object(source_switcher, "MOTU_CLOCK_SETTLE_SECONDS", 0.0),
    ):
        yield motu


def test_the_connect_dump_names_the_clock_without_asking() -> None:
    device = Device()
    with switcher_on(device) as motu:
        motu.read()
    assert motu.clock == "internal"
    assert device.sent == []


def test_a_clock_write_rides_the_meter_connection_and_is_checked_afresh() -> None:
    device = Device()
    with clock_writes_on(device) as motu:
        motu.read()  # the meter connection
        assert source_switcher._set_motu_clock_muted(SimpleNamespace(), "optical")
    # Written on the connection the meters already held, not a second client.
    assert device.sockets[0].sent == [bytes.fromhex("000b0000000102")]
    # Then reconnected: the fresh dump, not our belief, names the clock.
    assert device.connections == 2 and device.sockets[1].sent == []
    assert motu.clock == "optical"


def test_a_clock_the_device_did_not_take_is_reported_for_correction() -> None:
    device = Device(ignores_writes=True)
    with clock_writes_on(device) as motu:
        motu.read()
        source_switcher._set_motu_clock_muted(SimpleNamespace(), "optical")
    assert device.sent == [bytes.fromhex("000b0000000102")]
    # ClockReconciler sees the difference on its next pass.
    assert motu.clock == "internal"


def test_a_clock_the_device_reports_is_not_rewritten() -> None:
    device = Device()
    with clock_writes_on(device) as motu:
        with mock.patch.object(source_switcher, "_await_output_silence") as silence:
            assert source_switcher._set_motu_clock_muted(SimpleNamespace(), "internal")
    silence.assert_not_called()
    assert device.sent == [] and device.connections == 1


def test_an_unreachable_motu_is_not_written_and_not_waited_for() -> None:
    device = Device(fail=True)
    with clock_writes_on(device) as motu:
        with mock.patch.object(source_switcher, "_await_output_silence") as silence:
            motu.connect(force=True)
            assert not source_switcher._set_motu_clock_muted(SimpleNamespace(), "optical")
    silence.assert_not_called()
    assert motu.clock is None and device.sent == []
