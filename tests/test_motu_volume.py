"""MOTU UltraLite mk5 main output volume: decoding, write encoding, guards.

Every device frame here comes from the real connect dump in
tests/fixtures/motu_ultralite_mk5_connect_dump.json. No test talks to a MOTU.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

import motu_access
import motu_volume


FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAIN_TRIM_FRAME = bytes.fromhex("1393000006")
MAIN_GROUP_FRAME = bytes.fromhex("1394000003ff")


def connect_dump() -> list[bytes]:
    """The frames a real UltraLite mk5 pushed to a fresh WebSocket client."""
    document = json.loads(
        (FIXTURES / "motu_ultralite_mk5_connect_dump.json").read_text()
    )
    return [bytes.fromhex(frame) for frame in document["frames_hex"]]


class ReplaySocket:
    """A WebSocket stand-in replaying captured device frames, recording sends."""

    def __init__(self, frames: list[bytes], send_error: Exception | None = None) -> None:
        self.frames = list(frames)
        self.sent: list[bytes] = []
        self.closed = False
        self.send_error = send_error

    def connect(self, *_args: object, **_kwargs: object) -> None:
        pass

    def settimeout(self, _timeout: float) -> None:
        pass

    def recv_data(self, *_args: object, **_kwargs: object) -> tuple[int, bytes]:
        if not self.frames:
            raise TimeoutError("timed out")
        return motu_volume.OPCODE_BINARY, self.frames.pop(0)

    def send(self, payload: bytes, *_args: object, **_kwargs: object) -> None:
        self.sent.append(payload)
        if self.send_error is not None:
            # Recorded first: the frame may well have reached the device.
            raise self.send_error

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Device:
    """Hands out one ReplaySocket per connection and remembers them all."""

    def __init__(
        self,
        frames: list[bytes] | None = None,
        fail: bool = False,
        send_error: Exception | None = None,
    ) -> None:
        self.frames = connect_dump() if frames is None else frames
        self.fail = fail
        self.send_error = send_error
        self.sockets: list[ReplaySocket] = []

    def connect(self, _url: str, _timeout: float) -> ReplaySocket:
        if self.fail:
            raise OSError("no route to host")
        ws = ReplaySocket(self.frames, self.send_error)
        self.sockets.append(ws)
        return ws

    @property
    def sent(self) -> list[bytes]:
        return [frame for ws in self.sockets for frame in ws.sent]


def control(device: Device, clock: FakeClock | None = None) -> motu_volume.MotuMainVolume:
    return motu_volume.MotuMainVolume(connect=device.connect, clock=clock or FakeClock())


@pytest.fixture(autouse=True)
def default_settings():
    names = (
        "MOTU_MAIN_VOLUME_MAX_DB",
        "MOTU_ACCESS_WINDOW_SECONDS",
        "MOTU_VOLUME_CACHE_SECONDS",
    )
    env = {k: v for k, v in os.environ.items() if k not in names}
    with mock.patch.dict(os.environ, env, clear=True):
        yield


# ------------------------------------------------------------------ decoding


def test_real_connect_dump_decodes_main_volume_as_minus_six_db() -> None:
    """The captured device reports kMainTrim=6: -6 dB, the knob's reading."""
    frames = connect_dump()
    assert MAIN_TRIM_FRAME in frames and MAIN_GROUP_FRAME in frames

    state = motu_volume.read_device_state(ReplaySocket(frames))

    assert state.attenuation == 6
    assert motu_volume.attenuation_to_db(state.attenuation) == -6.0
    # Main 1-2 and Line 3-10: every analog line DAC, so all six crossover
    # outputs (Main 1-2, Line 3-4, Line 5-6) move together.
    assert state.group == 0x03FF == motu_volume.REQUIRED_MAIN_GROUP


def test_only_the_pushed_main_trim_frame_decodes_as_the_main_volume() -> None:
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
    assert groups == [0x03FF]


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
    assert motu_volume.decode_main_trim_write(written) == pushed
    # Neither layout parses as the other.
    assert motu_volume.decode_main_trim(written) is None
    assert motu_volume.decode_main_trim_write(MAIN_TRIM_FRAME) is None
    # Same shape as the clock write already proven on this device.
    assert len(written) == len(bytes.fromhex("000b0000000103"))

    for attenuation in range(0, 101):
        frame = motu_volume.encode_main_trim_write(attenuation)
        assert motu_volume.decode_main_trim_write(frame) == attenuation
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


def test_ceiling_is_enforced_server_side() -> None:
    device = Device()
    clock = FakeClock()
    volume = control(device, clock)

    result = volume.set({"volume_db": 0, "expected_db": -6})

    # Full range by default: from -6 dB, a request for 0 dB is written as asked.
    assert device.sent == [motu_volume.encode_main_trim_write(0)]
    assert result["volume_db"] == 0.0 and result["written"] is True

    device = Device()
    clock.now += 60  # past the shared access window
    with mock.patch.dict(os.environ, {"MOTU_MAIN_VOLUME_MAX_DB": "-10"}):
        result = control(device, clock).set({"volume_db": 0, "expected_db": -6})
    # Clamped to the ceiling: the only write is -10 dB.
    assert device.sent == [motu_volume.encode_main_trim_write(10)]
    assert result["volume_db"] == -10.0
    assert result["max_db"] == -10.0


def test_turning_down_below_the_ceiling_writes_the_requested_level() -> None:
    device = Device()
    result = control(device).set({"volume_db": -20.4, "expected_db": -6})
    assert device.sent == [bytes.fromhex("13930000000114")]  # 20 dB attenuation
    assert result["volume_db"] == -20.0
    assert result["confirmed"] is False  # a send is not a read-back


def test_unparseable_ceiling_refuses_to_write_without_touching_the_device() -> None:
    device = Device()
    with mock.patch.dict(os.environ, {"MOTU_MAIN_VOLUME_MAX_DB": "loud"}):
        with pytest.raises(motu_volume.MotuVolumeRefused):
            control(device).set({"volume_db": -20, "expected_db": -6})
        status = control(Device()).status()
    assert device.sockets == []
    assert status["writable"] is False and status["max_db"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"volume_db": -20},  # no expected level: could jump from a default
        {"volume_db": float("nan"), "expected_db": -6},
        {"volume_db": True, "expected_db": -6},
        {"volume_db": "-20", "expected_db": -6},
        {"expected_db": -6},
    ],
)
def test_malformed_requests_are_refused_before_connecting(payload: dict) -> None:
    device = Device()
    with pytest.raises(ValueError):
        control(device).set(payload)
    assert device.sockets == []


# -------------------------------------------------------- unknown / refusals


def test_unreachable_device_reads_unknown_and_refuses_to_write() -> None:
    device = Device(fail=True)
    clock = FakeClock()
    volume = control(device, clock)

    status = volume.status()
    assert status["known"] is False
    assert status["volume_db"] is None
    assert status["writable"] is False

    clock.now += 60
    with pytest.raises(motu_volume.MotuVolumeError):
        volume.set({"volume_db": -20, "expected_db": -6})
    assert device.sent == []


def test_state_push_without_the_main_volume_is_unknown_not_a_default() -> None:
    # The real dump, cut just before the kMainGroup/kMainTrim frames and
    # ending on its real first meter frame.
    frames = connect_dump()
    cut = frames.index(MAIN_GROUP_FRAME)
    meter = next(f for f in frames if f[:2] == (6000).to_bytes(2, "big"))
    device = Device(frames[:cut] + [meter])
    clock = FakeClock()
    volume = control(device, clock)

    assert volume.status()["known"] is False
    clock.now += 60
    with pytest.raises(motu_volume.MotuVolumeError):
        volume.set({"volume_db": -20, "expected_db": -6})
    assert device.sent == []


def test_a_level_changed_on_the_device_is_reported_not_overwritten() -> None:
    """The knob moved: the caller's -10 dB is stale, so nothing is written."""
    device = Device()
    volume = control(device)
    with pytest.raises(motu_volume.MotuVolumeConflict):
        volume.set({"volume_db": -9, "expected_db": -10})
    assert device.sent == []
    status = volume.status()
    assert status["volume_db"] == -6.0 and status["confirmed"] is True


def test_a_write_whose_send_fails_leaves_the_level_unknown_not_confirmed() -> None:
    """The send raised after the frame may have reached the device: the
    pre-write reading is no longer known to hold, and nothing is resent."""
    device = Device(send_error=ConnectionResetError("connection reset by peer"))
    clock = FakeClock()
    access = motu_access.MotuAccess(clock=clock)
    volume = motu_volume.MotuMainVolume(connect=device.connect, clock=clock, access=access)

    with pytest.raises(motu_volume.MotuVolumeError) as failed:
        volume.set({"volume_db": -20, "expected_db": -6})
    assert "outcome unknown" in str(failed.value)
    assert device.sent == [motu_volume.encode_main_trim_write(20)]
    assert device.sockets[0].closed is True

    # The reservation was released, and the cached -6 dB is not served as
    # confirmed: inside the window the answer is "unknown".
    status = volume.status()
    assert status["known"] is False
    assert status["volume_db"] is None and status["confirmed"] is False
    assert "outcome unknown" in status["reason"]
    assert len(device.sockets) == 1 and len(device.sent) == 1


def test_main_group_missing_a_speaker_output_refuses_the_write() -> None:
    # The real dump with one bit (Line 5, LocalOutputs 0x0 - the low driver's
    # left side) cleared from its real kMainGroup frame.
    partial = bytes.fromhex("1394000003fe")
    frames = [partial if f == MAIN_GROUP_FRAME else f for f in connect_dump()]
    device = Device(frames)
    volume = control(device)
    with pytest.raises(motu_volume.MotuVolumeRefused):
        volume.set({"volume_db": -20, "expected_db": -6})
    assert device.sent == []
    status = volume.status()
    assert status["known"] is True and status["writable"] is False
    assert "crossover" in status["reason"]


# ---------------------------------------------------------------- rate limit


def test_device_access_is_rate_limited_and_reads_come_from_cache() -> None:
    device = Device()
    clock = FakeClock()
    volume = control(device, clock)

    assert volume.status()["volume_db"] == -6.0
    assert len(device.sockets) == 1

    window = motu_access.DEFAULT_WINDOW_SECONDS
    # A second page load inside the window answers from cache.
    clock.now += window - 1
    status = volume.status()
    assert status["volume_db"] == -6.0 and len(device.sockets) == 1
    assert status["retry_after"] == pytest.approx(1.0)

    # A write inside the window is refused without connecting.
    with pytest.raises(motu_volume.MotuVolumeRateLimited) as refused:
        volume.set({"volume_db": -20, "expected_db": -6})
    assert refused.value.retry_after == pytest.approx(1.0)
    assert len(device.sockets) == 1 and device.sent == []

    # Once the window has passed, exactly one connection makes the write.
    clock.now += 1
    volume.set({"volume_db": -20, "expected_db": -6})
    assert len(device.sockets) == 2
    assert device.sent == [motu_volume.encode_main_trim_write(20)]

    # A burst of drag updates right after is all refused, none connect.
    assert 29 * 0.1 < window, "the drag burst must fit inside the window"
    for step in range(1, 30):
        clock.now += 0.1
        with pytest.raises(motu_volume.MotuVolumeRateLimited):
            volume.set({"volume_db": -20 - step, "expected_db": -20})
    assert len(device.sockets) == 2


def test_failed_connection_still_uses_the_access_window() -> None:
    device = Device(fail=True)
    clock = FakeClock()
    volume = control(device, clock)
    volume.status()
    clock.now += 1
    volume.status()
    with pytest.raises(motu_volume.MotuVolumeRateLimited):
        volume.set({"volume_db": -20, "expected_db": -6})


def test_access_window_is_configurable_and_spans_several_switcher_passes() -> None:
    # A recorded access no longer waits out the meter reader's 10 s reconnect
    # backoff (forgive_coordinated_kick), so the window need not exceed it.
    # What it must exceed, comfortably, is one switcher pass, so the meters
    # read a fresh frame between accesses; see test_motu_access for the
    # simulation against the real reader that this default was chosen from.
    import source_switcher

    one_pass = source_switcher.CHECK_INTERVAL + source_switcher.MOTU_READ_WINDOW_SECONDS
    assert motu_access.DEFAULT_WINDOW_SECONDS >= 3 * one_pass
    assert motu_access.window_seconds() == motu_access.DEFAULT_WINDOW_SECONDS
    with mock.patch.dict(os.environ, {"MOTU_ACCESS_WINDOW_SECONDS": "30"}):
        assert motu_access.window_seconds() == 30.0
    with mock.patch.dict(os.environ, {"MOTU_ACCESS_WINDOW_SECONDS": "soon"}):
        assert motu_access.window_seconds() == motu_access.DEFAULT_WINDOW_SECONDS
