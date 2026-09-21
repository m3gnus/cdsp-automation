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

    access.claim("clock-readback", deferrable=True)
    clock.now += 4
    with pytest.raises(motu_access.AccessDeferred) as deferred:
        access.claim("ui-volume", deferrable=True)
    assert deferred.value.retry_after == pytest.approx(11.0)
    assert deferred.value.last_kind == "clock-readback"
    assert access.retry_after() == pytest.approx(11.0)

    clock.now += 11
    access.claim("ui-volume", deferrable=True)
    assert access.last_access() == (clock.now, "ui-volume")


def test_a_clock_write_is_never_deferred_but_is_respected() -> None:
    clock = FakeClock()
    access = access_at(clock)

    access.claim("ui-volume", deferrable=True)
    clock.now += 1
    # A source change's clock write goes straight through the window...
    assert access.claim("clock-write", deferrable=False) == clock.now
    # ...and restarts it for everything deferrable.
    clock.now += 14
    with pytest.raises(motu_access.AccessDeferred) as deferred:
        access.claim("clock-readback", deferrable=True)
    assert deferred.value.last_kind == "clock-write"
    assert deferred.value.retry_after == pytest.approx(1.0)


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
