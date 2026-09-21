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
DEFAULT_WINDOW_SECONDS = 15.0
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")

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

    def _parse(self, handle: Any, now: float) -> tuple[float | None, str]:
        handle.seek(0)
        try:
            record = json.loads(handle.read() or "{}")
        except ValueError:
            return None, ""
        if not isinstance(record, dict) or record.get("boot_id") != self._boot_id():
            return None, ""
        at = record.get("monotonic")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            return None, ""
        at = float(at)
        # A time in the future cannot come from this boot's clock.
        if not math.isfinite(at) or at > now:
            return None, ""
        return at, str(record.get("kind") or "unknown")

    def last_access(self) -> tuple[float | None, str]:
        """(monotonic time, kind) of the last recorded extra access."""
        with self._locked(fcntl.LOCK_SH) as handle:
            return self._parse(handle, self._clock())

    def retry_after(self) -> float:
        """Seconds until a deferrable access would be allowed."""
        now = self._clock()
        at, _kind = self.last_access()
        if at is None:
            return 0.0
        return max(0.0, at + window_seconds() - now)

    def claim(self, kind: str, *, deferrable: bool) -> float:
        """Record an extra access about to be made; return its time.

        A deferrable access raises AccessDeferred inside the window. A
        non-deferrable one (a clock write) always proceeds: if even the record
        cannot be written it is still allowed, and the caller should log it.
        """
        with self._locked(fcntl.LOCK_EX) as handle:
            now = self._clock()
            at, last_kind = self._parse(handle, now)
            if deferrable and at is not None:
                wait = at + window_seconds() - now
                if wait > 0:
                    raise AccessDeferred(wait, last_kind)
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps({"boot_id": self._boot_id(), "monotonic": now, "kind": kind})
            )
            handle.flush()
            return now


def claim_or_log(kind: str) -> None:
    """Record a non-deferrable access; never stop it, only report trouble."""
    try:
        MotuAccess().claim(kind, deferrable=False)
    except AccessUnavailable as exc:
        print(f"MOTU: access not recorded ({exc}); proceeding", flush=True)
