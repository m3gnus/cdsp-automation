#!/usr/bin/env python3
"""One shared access window for every extra connection to the MOTU.

The UltraLite mk5 serves one WebSocket client at a time. The source switcher's
meter reader normally holds that slot, and any other connection (a clock
write, a clock read-back, a control-UI volume read or write) drops it. The
reader then reconnects, but it refuses to connect again sooner than
SOURCE_MOTU_CONNECT_RETRY_SECONDS (10 s) after its previous connect. Two extra
connections inside that span keep the meters dark for about 10 s, past the
TOSLINK tolerance, and TOSLINK drops mid-song.

So every extra connection is recorded here, across processes (clock_sync runs
as the install user, the control UI as root), and access is ranked:

* a clock **write** follows a source change and is audible if wrong: it never
  waits on the window, it only records itself so the others respect it;
* a clock **read-back** and a UI **volume** access are deferrable: they are
  refused while any extra access happened within the window, and told when to
  come back.

The record also lets the meter reader tell a coordinated kick from a device
that went away (see source_switcher.MotuMeterReader): a drop that a recorded
access explains is reconnected on the next pass instead of after the 10 s
backoff.

The file holds the last access as a CLOCK_MONOTONIC reading (shared by every
process on one boot, and immune to the wall-clock jumps of a Pi without an
RTC) together with the kernel boot id, so a record from before a reboot is
ignored rather than compared against an unrelated clock.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

DEFAULT_ACCESS_PATH = "/var/lib/cdsp-automation/motu-access.lock"
# Must exceed the meter reader's 10 s reconnect backoff plus the time it takes
# to notice a drop and reconnect (a switcher pass or two), so that even a
# reader that did back off is connectable again before the next deferrable
# access.
# Deferrable accesses are kept at least this far apart. Each one drops the
# source switcher's meters, and the reader reconnects on its next pass because
# the drop is recorded here (forgive_coordinated_kick), so it no longer waits
# out its 10 s reconnect backoff. The window therefore only has to exceed one
# switcher pass (about 1.2 s: a 1 s sleep plus a 0.2 s meter read), so that
# the meters read a fresh frame between accesses. 5 s was checked against the
# real reader, timers and access record: at normal passes TOSLINK never loses
# a pass of meter data, and it only drops when accesses land roughly once per
# pass. See TECHNICAL.md, "Choosing the window".
DEFAULT_WINDOW_SECONDS = 5.0
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
# The longest any access may hold the device from the meter reader: the
# longest claimant (a clock read-back: connect + frame, 3 s each) plus margin.
MAX_HOLD_SECONDS = 10.0

# Bound at import: tests (and callers) that patch time.monotonic to script
# their own timing must not have it consumed by the access bookkeeping.
_monotonic = time.monotonic


class AccessDeferred(Exception):
    """A deferrable access that must wait for the shared window."""

    def __init__(self, retry_after: float, last_kind: str) -> None:
        super().__init__(
            f"the MOTU was accessed ({last_kind}) moments ago; "
            f"next access in {retry_after:.0f} s"
        )
        self.retry_after = retry_after
        self.last_kind = last_kind


class AccessUnavailable(Exception):
    """The shared record cannot be read or written."""


def access_path() -> Path:
    return Path(os.environ.get("MOTU_ACCESS_PATH", "").strip() or DEFAULT_ACCESS_PATH)


def window_seconds() -> float:
    raw = os.environ.get("MOTU_ACCESS_WINDOW_SECONDS", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_WINDOW_SECONDS
    except ValueError:
        return DEFAULT_WINDOW_SECONDS
    return value if math.isfinite(value) and value >= 0 else DEFAULT_WINDOW_SECONDS


def current_boot_id() -> str:
    try:
        return BOOT_ID_PATH.read_text().strip()
    except OSError:
        return ""


class MotuAccess:
    def __init__(
        self,
        *,
        path: Path | None = None,
        clock: Callable[[], float] | None = None,
        boot_id: Callable[[], str] = current_boot_id,
    ) -> None:
        self._path = path
        self._clock = clock or _monotonic
        self._boot_id = boot_id

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else access_path()

    @contextmanager
    def _locked(self, operation: int) -> Iterator[Any]:
        try:
            # 0o660 and the installer's pre-created owner/group let both the
            # install user (clock_sync, source switcher) and the root UI use
            # one file; open() never changes an existing file's mode.
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o660)
        except OSError as exc:
            raise AccessUnavailable(f"MOTU access record {self.path}: {exc}") from None
        with os.fdopen(fd, "r+") as handle:
            try:
                fcntl.flock(handle.fileno(), operation)
            except OSError as exc:
                raise AccessUnavailable(f"MOTU access record {self.path}: {exc}") from None
            try:
                yield handle
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _record(self, handle: Any, now: float) -> dict[str, Any] | None:
        """The current boot's record, or None for anything unusable."""
        handle.seek(0)
        try:
            record = json.loads(handle.read() or "{}")
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get("boot_id") != self._boot_id():
            return None
        at = record.get("monotonic")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            return None
        # A time in the future cannot come from this boot's clock.
        if not math.isfinite(float(at)) or float(at) > now:
            return None
        return record

    def _parse(self, handle: Any, now: float) -> tuple[float | None, str]:
        record = self._record(handle, now)
        if record is None:
            return None, ""
        return float(record["monotonic"]), str(record.get("kind") or "unknown")

    @staticmethod
    def _busy_until(record: dict[str, Any] | None) -> float:
        value = record.get("busy_until") if record else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        value = float(value)
        if not math.isfinite(value):
            return 0.0
        # A span is bounded by its claimant's own timeouts; never trust one
        # far beyond that, so a crashed claimant cannot starve the meters.
        return min(value, float(record["monotonic"]) + MAX_HOLD_SECONDS)

    def _write(self, handle: Any, record: dict[str, Any]) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(record))
        handle.flush()

    def last_access(self) -> tuple[float | None, str]:
        """(monotonic time, kind) of the last recorded extra access."""
        with self._locked(fcntl.LOCK_SH) as handle:
            return self._parse(handle, self._clock())

    def _deferral(self, record: dict[str, Any] | None, now: float) -> float:
        """Seconds a deferrable access must still wait.

        Both conditions hold at once: the window since the last access, and
        the span an access still in progress holds the device for (a
        read-back or UI access may hold it longer than the window).
        """
        if record is None:
            return 0.0
        allowed_at = max(
            float(record["monotonic"]) + window_seconds(), self._busy_until(record)
        )
        return max(0.0, allowed_at - now)

    def retry_after(self) -> float:
        """Seconds until a deferrable access would be allowed."""
        with self._locked(fcntl.LOCK_SH) as handle:
            now = self._clock()
            return self._deferral(self._record(handle, now), now)

    def claim(self, kind: str, *, deferrable: bool, hold: float = 0.0) -> float:
        """Record an extra access about to be made; return its time.

        A deferrable access raises AccessDeferred inside the window. A
        non-deferrable one (a clock write) always proceeds: if even the record
        cannot be written it is still allowed, and the caller should log it.

        ``hold`` is the longest the access can keep the device (its own
        timeouts).  Until it calls :meth:`release`, or that span runs out, the
        switcher's meter reader will not reconnect: the device serves one
        client, and a reconnect landing mid-access resets the access instead.
        """
        with self._locked(fcntl.LOCK_EX) as handle:
            now = self._clock()
            record = self._record(handle, now)
            if deferrable:
                wait = self._deferral(record, now)
                if wait > 0:
                    assert record is not None
                    raise AccessDeferred(wait, str(record.get("kind") or "unknown"))
            self._write(
                handle,
                {
                    "boot_id": self._boot_id(),
                    "monotonic": now,
                    "kind": kind,
                    "busy_until": now + max(float(hold), 0.0),
                },
            )
            return now

    def release(self, claimed_at: float | None) -> None:
        """End the busy span of the access claimed at ``claimed_at``.

        Best effort: a record that has since been replaced, or cannot be
        used, is left alone -- the span then simply runs out.
        """
        if claimed_at is None:
            return
        try:
            with self._locked(fcntl.LOCK_EX) as handle:
                now = self._clock()
                record = self._record(handle, now)
                if record is None or float(record["monotonic"]) != claimed_at:
                    return
                record["busy_until"] = min(self._busy_until(record), now)
                self._write(handle, record)
        except AccessUnavailable:
            pass

    def connect_when_idle(self, connect: Callable[[], Any]) -> tuple[bool, Any]:
        """Run the meter reader's (re)connect unless an access holds the device.

        Checked and connected under the record's lock, so an access cannot
        claim the device between the check and the connect.  Returns
        ``(False, None)`` while an access is in progress; the reader simply
        tries again on its next pass.  Without a usable record the reader
        connects as it always did.
        """
        try:
            with self._locked(fcntl.LOCK_EX) as handle:
                now = self._clock()
                if self._busy_until(self._record(handle, now)) > now:
                    return False, None
                return True, connect()
        except AccessUnavailable:
            return True, connect()


def claim_or_log(kind: str, *, hold: float = 0.0) -> float | None:
    """Record a non-deferrable access; never stop it, only report trouble."""
    try:
        return MotuAccess().claim(kind, deferrable=False, hold=hold)
    except AccessUnavailable as exc:
        print(f"MOTU: access not recorded ({exc}); proceeding", flush=True)
        return None
