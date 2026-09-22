"""The shared MOTU access window across clock_sync, the UI and the meters.

The MOTU serves one WebSocket client at a time. Every extra connection drops
the source switcher's meter reader, which then refuses to reconnect for 10 s
after its previous connect. These tests pin the coordination that keeps two
extra accesses from landing inside that backoff.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

if "camilladsp" not in sys.modules:
    camilladsp = types.ModuleType("camilladsp")
    camilladsp.CamillaClient = object
    sys.modules["camilladsp"] = camilladsp

import motu_access
import motu_volume
from scripts import clock_sync, source_switcher
from test_motu_volume import Device, FakeClock, ReplaySocket


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def access_at(clock: FakeClock) -> motu_access.MotuAccess:
    return motu_access.MotuAccess(clock=clock, boot_id=lambda: "boot-a")


# ------------------------------------------------------------------ record


def test_deferrable_accesses_keep_the_window_from_any_recorded_access() -> None:
    clock = FakeClock()
    access = access_at(clock)

    window = motu_access.DEFAULT_WINDOW_SECONDS
    access.claim("clock-readback", deferrable=True)
    clock.now += window - 1
    with pytest.raises(motu_access.AccessDeferred) as deferred:
        access.claim("ui-volume", deferrable=True)
    assert deferred.value.retry_after == pytest.approx(1.0)
    assert deferred.value.last_kind == "clock-readback"
    assert access.retry_after() == pytest.approx(1.0)

    clock.now += 1
    access.claim("ui-volume", deferrable=True)
    assert access.last_access() == (clock.now, "ui-volume")


def test_a_clock_write_is_never_deferred_but_is_respected() -> None:
    clock = FakeClock()
    access = access_at(clock)

    window = motu_access.DEFAULT_WINDOW_SECONDS
    access.claim("ui-volume", deferrable=True)
    clock.now += 1
    # A source change's clock write goes straight through the window...
    assert access.claim("clock-write", deferrable=False) == clock.now
    # ...and restarts it for everything deferrable: measured from the write,
    # one second is still left, where from the earlier access none would be.
    clock.now += window - 1
    with pytest.raises(motu_access.AccessDeferred) as deferred:
        access.claim("clock-readback", deferrable=True)
    assert deferred.value.last_kind == "clock-write"
    assert deferred.value.retry_after == pytest.approx(1.0)



# ------------------------------------------- is the window safe for TOSLINK?
#
# Runs the real meter reader, meter parsing, TOSLINK timers and access record
# through minutes of playback in simulated time. Only the MOTU socket and the
# clock are fakes: the socket sends the device's ~0.12 s settings dump and then
# genuine 104-byte meter frames with TOSLINK present, and -- like the real
# one-client device -- drops the reader mid-read whenever an extra access lands.

def _toslink_passes_lost(window, pass_seconds, phase, *, minutes=2.0):
    """Passes on which TOSLINK was unavailable, accesses ``window`` s apart."""
    import pathlib
    import tempfile
    import types
    from unittest import mock

    class Clock:
        now = 1000.0

    clock = Clock()
    body = bytearray([255] * 100)
    for pair in source_switcher.TOSLINK_METER_PAIRS:
        body[pair * 2] = body[pair * 2 + 1] = 0  # present: below the threshold
    frame = bytes.fromhex("17700000") + bytes(body)
    record = pathlib.Path(tempfile.mkdtemp()) / "motu-access.lock"

    def access():
        return motu_access.MotuAccess(
            clock=lambda: clock.now, path=record, boot_id=lambda: "sim"
        )

    other = access()
    # Worst case the window allows: deferrable accesses exactly ``window`` s
    # apart, each chased by a clock write (which the window never delays).
    due = []
    t = clock.now + phase
    while t < clock.now + minutes * 60 + window:
        due += [t, t + 0.3]
        t += window
    live = {"socket": None}

    def land(until):
        while due and due[0] <= until:
            due.pop(0)
            other.claim("ui-volume", deferrable=False)
            if live["socket"] is not None:
                live["socket"].dropped = True

    class Socket:
        dropped, opened = False, 0.0

        def settimeout(self, _timeout):
            pass

        def connect(self, _url, timeout=1):
            self.opened, self.dropped = clock.now, False
            live["socket"] = self

        def recv_data(self, control_frame=True):
            clock.now += 0.03
            land(clock.now)
            if self.dropped:
                raise ConnectionResetError("[Errno 104] Connection reset by peer")
            if clock.now < self.opened + 0.12:
                return 2, bytes.fromhex("000b000003")  # settings dump
            return 2, frame

        def close(self):
            pass

    fake_websocket = types.SimpleNamespace(
        WebSocket=Socket,
        WebSocketTimeoutException=TimeoutError,
        ABNF=types.SimpleNamespace(OPCODE_BINARY=2),
    )
    lost = 0
    with mock.patch.object(source_switcher, "websocket", fake_websocket), \
            mock.patch("time.monotonic", lambda: clock.now), \
            contextlib.redirect_stdout(io.StringIO()):
        reader = source_switcher.MotuMeterReader("ws://motu:1280", access=access())
        active, idle = 0.0, source_switcher.TOSLINK_IDLE_SECONDS
        settled, end = clock.now + 10, clock.now + minutes * 60
        while clock.now < end:
            started = clock.now
            land(clock.now)
            present = source_switcher.meter_pairs_active(
                reader.read(), source_switcher.TOSLINK_METER_PAIRS
            )
            active, idle = source_switcher.update_meter_timers(
                present, active, idle, source_switcher.TOSLINK_IDLE_SECONDS
            )
            available = (
                active >= source_switcher.TOSLINK_ACTIVE_SECONDS
                and idle < source_switcher.TOSLINK_IDLE_SECONDS
            )
            if clock.now > settled and not available:
                lost += 1
            land(started + pass_seconds)
            clock.now = max(clock.now, started + pass_seconds)
    return lost


def test_default_window_keeps_toslink_through_back_to_back_accesses() -> None:
    """At the default window TOSLINK never loses a pass during normal playback.

    A normal switcher pass is a 1 s sleep plus a 0.2 s meter read, and runs a
    little longer under load. Every landing moment across a pass is tried.
    """
    window = motu_access.DEFAULT_WINDOW_SECONDS
    for pass_seconds in (1.0, 1.2, 1.5, 2.0):
        for phase in (0.0, 0.3, 0.6, 0.9, 1.2, 1.5, 1.8):
            lost = _toslink_passes_lost(window, pass_seconds, phase)
            assert lost == 0, (
                f"TOSLINK lost {lost} passes: window {window} s, "
                f"pass {pass_seconds} s, phase {phase} s"
            )


def test_toslink_simulation_detects_accesses_landing_once_per_pass() -> None:
    """The harness above must be able to fail, or its passing proves nothing.

    Accesses landing about once per switcher pass leave the reader no pass in
    which to read a fresh frame, so TOSLINK drops. This is also the real limit
    on the window: it must stay comfortably wider than a switcher pass. The
    failure needs the accesses to line up with the reads, so it appears at only
    a few landing moments -- sweep them all rather than pick one.
    """
    worst = max(_toslink_passes_lost(2.0, 2.0, i * 0.1) for i in range(20))
    assert worst > 0


def test_records_from_another_boot_or_the_future_or_garbage_are_ignored() -> None:
    clock = FakeClock()
    path = Path(os.environ["MOTU_ACCESS_PATH"])
    access_at(clock).claim("clock-write", deferrable=False)

    # A reboot restarts CLOCK_MONOTONIC: the old reading means nothing.
    other_boot = motu_access.MotuAccess(clock=clock, boot_id=lambda: "boot-b")
    assert other_boot.last_access() == (None, "")
    other_boot.claim("ui-volume", deferrable=True)

    path.write_text(json.dumps({"boot_id": "boot-a", "monotonic": clock.now + 50}))
    assert access_at(clock).last_access() == (None, "")
    path.write_text("not json")
    assert access_at(clock).last_access() == (None, "")
    access_at(clock).claim("ui-volume", deferrable=True)


def test_the_record_is_shared_with_another_process() -> None:
    """clock_sync and the UI are separate processes on one boot clock."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import motu_access; motu_access.MotuAccess().claim('clock-write', deferrable=False)",
        ],
        check=True,
        env=env,
    )
    with pytest.raises(motu_access.AccessDeferred) as deferred:
        motu_access.MotuAccess().claim("ui-volume", deferrable=True)
    assert deferred.value.last_kind == "clock-write"
    assert 0 < deferred.value.retry_after <= motu_access.DEFAULT_WINDOW_SECONDS


def test_an_unusable_record_stops_deferrable_accesses_but_never_a_clock_write(
    tmp_path: Path,
) -> None:
    with mock.patch.dict(
        os.environ, {"MOTU_ACCESS_PATH": str(tmp_path / "missing" / "motu-access.lock")}
    ):
        with pytest.raises(motu_access.AccessUnavailable):
            motu_access.MotuAccess().claim("clock-readback", deferrable=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            motu_access.claim_or_log("clock-write")
        assert "proceeding" in out.getvalue()

        device = Device()
        volume = motu_volume.MotuMainVolume(connect=device.connect)
        with pytest.raises(motu_volume.MotuVolumeError):
            volume.set({"volume_db": -20, "expected_db": -6})
        assert device.sockets == []
        assert volume.status()["known"] is False


# ------------------------------------------------------------ clock_sync


def test_clock_write_goes_through_right_after_a_ui_volume_access() -> None:
    motu_access.MotuAccess().claim("ui-volume", deferrable=True)
    sock = ReplaySocket([])
    with (
        mock.patch.object(clock_sync.websocket, "WebSocket", return_value=sock),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        assert clock_sync.set_motu_clock("optical") is True
    assert sock.sent == [bytes.fromhex(clock_sync.CLOCK_PAYLOADS["optical"])]
    assert motu_access.MotuAccess().last_access()[1] == "clock-write"


def test_read_back_right_after_a_clock_write_is_deferred_without_connecting() -> None:
    with (
        mock.patch.object(clock_sync.websocket, "WebSocket", return_value=ReplaySocket([])),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        clock_sync.set_motu_clock("optical")
    connect = mock.Mock()
    with mock.patch.object(clock_sync.websocket, "WebSocket", connect):
        with pytest.raises(motu_access.AccessDeferred):
            clock_sync.read_motu_clock()
    connect.assert_not_called()


def test_ui_volume_access_right_after_a_clock_write_is_refused_429() -> None:
    motu_access.claim_or_log("clock-write")
    device = Device()
    volume = motu_volume.MotuMainVolume(connect=device.connect)
    with pytest.raises(motu_volume.MotuVolumeRateLimited) as refused:
        volume.set({"volume_db": -20, "expected_db": -6})
    assert "clock-write" in str(refused.value)
    assert device.sockets == []
    # A page load in the same window gets "unknown", not a connection.
    assert volume.status()["known"] is False
    assert device.sockets == []


def test_main_defers_a_blocked_read_back_instead_of_writing_it_off() -> None:
    """A deferred read-back is retried when the window opens, not 300 s later,
    and it is never mistaken for a failed read."""
    client = mock.Mock()
    client.is_connected.return_value = True
    client.config.active.return_value = {"devices": {"samplerate": 48000}}
    client.config.file_path.return_value = "/tmp/toslink.yml"
    reads = [motu_access.AccessDeferred(3.0, "ui-volume"), "optical"]
    ticks = itertools.count(1000.0, 1.0)

    def read_motu_clock() -> str:
        item = reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    sleeps = itertools.count()

    def sleep(_seconds: float) -> None:
        if next(sleeps) >= 6:
            raise KeyboardInterrupt

    state = Path(os.environ["MOTU_ACCESS_PATH"]).with_name("motu-clock-source")
    state.write_text("optical\n")
    with (
        mock.patch.object(clock_sync, "STATE_PATH", state),
        mock.patch.object(clock_sync, "CamillaClient", return_value=client),
        mock.patch.object(clock_sync, "read_motu_clock", side_effect=read_motu_clock) as read,
        mock.patch.object(clock_sync, "set_motu_clock") as set_clock,
        mock.patch.object(clock_sync.time, "monotonic", side_effect=lambda: next(ticks)),
        mock.patch.object(clock_sync.time, "sleep", side_effect=sleep),
        contextlib.redirect_stdout(io.StringIO()),
        pytest.raises(KeyboardInterrupt),
    ):
        clock_sync.main()
    assert read.call_count == 2
    set_clock.assert_not_called()


# ------------------------------------------------------------ meter reader


class DroppedSocket:
    def recv_data(self, *_args: object, **_kwargs: object) -> tuple[int, bytes]:
        raise ConnectionResetError("Connection reset by peer")

    def settimeout(self, _timeout: float) -> None:
        pass

    def close(self) -> None:
        pass


def reader_connected_at(connected_at: float) -> source_switcher.MotuMeterReader:
    reader = source_switcher.MotuMeterReader("ws://motu:1280")
    reader.ws = DroppedSocket()
    reader.connected_at = connected_at
    # As connect() leaves it: no reconnect before the 10 s backoff.
    reader.next_connect_attempt = connected_at + source_switcher.MOTU_CONNECT_RETRY_SECONDS
    return reader


def test_meter_drop_caused_by_a_recorded_access_reconnects_next_pass() -> None:
    """A UI access, then a clock write for a switch to TOSLINK a few seconds
    later: each drop is forgiven, so neither waits out the 10 s backoff."""
    import time

    connected = time.monotonic() - 5
    reader = reader_connected_at(connected)
    motu_access.MotuAccess(clock=lambda: connected + 0.5).claim("ui-volume", deferrable=True)

    with contextlib.redirect_stdout(io.StringIO()) as out:
        reader.read()
    assert reader.ws is None
    assert reader.next_connect_attempt <= time.monotonic()
    assert "displaced by a ui-volume access" in out.getvalue()

    # Reconnected; then the clock write lands inside what used to be the
    # backoff window, and is forgiven too.
    reconnected = connected + 2.0
    reader.ws = DroppedSocket()
    reader.connected_at = reconnected
    reader.next_connect_attempt = reconnected + source_switcher.MOTU_CONNECT_RETRY_SECONDS
    motu_access.claim_or_log("clock-write")
    with contextlib.redirect_stdout(io.StringIO()):
        reader.read()
    assert reader.next_connect_attempt <= time.monotonic()


def test_unexplained_meter_drop_keeps_the_reconnect_backoff() -> None:
    import time

    connected = time.monotonic() - 5
    # An access recorded before this connection was made did not drop it.
    motu_access.MotuAccess(clock=lambda: connected - 1).claim("ui-volume", deferrable=True)
    reader = reader_connected_at(connected)
    backoff = reader.next_connect_attempt
    with contextlib.redirect_stdout(io.StringIO()):
        reader.read()
    assert reader.next_connect_attempt == backoff

    # One recorded access forgives one drop, not every later one.
    motu_access.MotuAccess(clock=lambda: connected + 1).claim("clock-write", deferrable=False)
    with contextlib.redirect_stdout(io.StringIO()):
        reader.ws = DroppedSocket()
        reader.read()
        forgiven = reader.next_connect_attempt
        reader.ws = DroppedSocket()
        reader.next_connect_attempt = backoff
        reader.read()
    assert forgiven <= time.monotonic()
    assert reader.next_connect_attempt == backoff


def test_meter_reader_without_a_usable_record_keeps_the_backoff(tmp_path: Path) -> None:
    import time

    with mock.patch.dict(
        os.environ, {"MOTU_ACCESS_PATH": str(tmp_path / "missing" / "record.lock")}
    ):
        reader = reader_connected_at(time.monotonic())
        backoff = reader.next_connect_attempt
        with contextlib.redirect_stdout(io.StringIO()):
            reader.read()
    assert reader.next_connect_attempt == backoff


# ------------------------------------------------------------------ busy span


def test_an_access_in_progress_keeps_the_meter_reader_off_the_device() -> None:
    clock = FakeClock()
    access = access_at(clock)
    connects: list[float] = []

    def connect() -> str:
        connects.append(clock.now)
        return "ws"

    claimed = access.claim("clock-readback", deferrable=True, hold=7.0)
    clock.now += 1.0
    assert access.connect_when_idle(connect) == (False, None)
    # Done early: the device is handed back at once, not after the hold.
    access.release(claimed)
    assert access.connect_when_idle(connect) == (True, "ws")
    # The deferral window itself is unchanged by the release.
    with pytest.raises(motu_access.AccessDeferred):
        access.claim("ui-volume", deferrable=True)

    # An access that never releases (it crashed) holds only for its span.
    clock.now += 10.0
    access.claim("clock-write", deferrable=False, hold=4.0)
    clock.now += 3.9
    assert access.connect_when_idle(connect)[0] is False
    clock.now += 0.2
    assert access.connect_when_idle(connect)[0] is True
    assert len(connects) == 2


def test_release_only_ends_its_own_span_and_a_forged_span_is_capped() -> None:
    clock = FakeClock()
    access = access_at(clock)
    first = access.claim("clock-write", deferrable=False, hold=4.0)
    clock.now += 0.5
    access.claim("clock-readback", deferrable=False, hold=7.0)
    access.release(first)  # superseded: must not free the read-back
    assert access.connect_when_idle(lambda: "ws")[0] is False

    record = json.loads(access.path.read_text())
    record["busy_until"] = record["monotonic"] + 1e9
    access.path.write_text(json.dumps(record))
    clock.now += motu_access.MAX_HOLD_SECONDS + 0.1
    assert access.connect_when_idle(lambda: "ws") == (True, "ws")


def test_a_meter_reconnect_cannot_reset_a_read_back_in_progress() -> None:
    """The live failure: the switcher noticed a clock write's drop late, and
    its reconnect landed on a read-back that had just started, which lost
    its settings dump to a connection reset."""
    reader = source_switcher.MotuMeterReader("ws://motu:1280")
    attempts: list[str] = []

    class Meter:
        def __init__(self) -> None:
            attempts.append("meter-connect")

        def settimeout(self, _t: float) -> None:
            pass

        def connect(self, *_a, **_k) -> None:
            pass

        def close(self) -> None:
            pass

    class ReadBack:
        """The device's settings dump; mid-read, the meter reader tries."""

        def __init__(self) -> None:
            self.frames = [bytes.fromhex("000b000003")]

        def connect(self, *_a, **_k) -> None:
            pass

        def settimeout(self, _t: float) -> None:
            pass

        def recv_data(self):
            with contextlib.redirect_stdout(io.StringIO()):
                assert reader.connect() is False
            return 2, self.frames.pop(0)

        def close(self) -> None:
            pass

    meter_ws = types.SimpleNamespace(
        WebSocket=Meter, WebSocketTimeoutException=TimeoutError,
        ABNF=types.SimpleNamespace(OPCODE_BINARY=2),
    )
    with mock.patch.object(source_switcher, "websocket", meter_ws), \
            mock.patch.object(clock_sync.websocket, "WebSocket", ReadBack):
        assert clock_sync.read_motu_clock() == "internal"
        assert attempts == []
        # The read-back released the device: the next pass connects, with no
        # 10 s backoff charged for the pass it had to skip.
        with contextlib.redirect_stdout(io.StringIO()):
            assert reader.connect() is True
    assert attempts == ["meter-connect"]


def test_a_ui_volume_access_holds_the_device_only_while_it_runs() -> None:
    clock = FakeClock()
    device = Device()
    access = motu_access.MotuAccess(clock=clock)
    during: list[tuple[bool, object]] = []

    def connect(url: str, timeout: float):
        during.append(access.connect_when_idle(lambda: "ws"))
        return device.connect(url, timeout)

    control = motu_volume.MotuMainVolume(connect=connect, clock=clock)
    assert control.status()["known"] is True
    assert during == [(False, None)]
    assert access.connect_when_idle(lambda: "ws") == (True, "ws")


def test_a_deferrable_access_waits_for_one_still_holding_the_device() -> None:
    """A read-back may hold the MOTU for 7 s, longer than the 5 s window."""
    clock = FakeClock()
    readback = access_at(clock)
    ui = access_at(clock)  # a separate claimant sharing the same record
    claimed = readback.claim("clock-readback", deferrable=True, hold=7.0)
    clock.now += 5.1
    assert ui.retry_after() == pytest.approx(1.9)
    with pytest.raises(motu_access.AccessDeferred) as deferred:
        ui.claim("ui-volume", deferrable=True)
    assert deferred.value.retry_after == pytest.approx(1.9)
    assert deferred.value.last_kind == "clock-readback"
    assert ui.connect_when_idle(lambda: "ws")[0] is False

    # Released early: the window (already past) is all that remains.
    readback.release(claimed)
    assert ui.retry_after() == 0.0
    ui.claim("ui-volume", deferrable=True)

    # An abandoned reservation still expires on its own.
    clock.now += 10.0
    readback.claim("clock-readback", deferrable=True, hold=7.0)
    clock.now += 7.01
    assert ui.retry_after() == 0.0
    ui.claim("ui-volume", deferrable=True)

    # A clock write keeps its priority over any reservation.
    clock.now += 10.0
    readback.claim("clock-readback", deferrable=True, hold=7.0)
    clock.now += 1.0
    ui.claim("clock-write", deferrable=False)
