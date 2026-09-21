"""Test imports mirror the flat scripts directory used on the Pi."""

from __future__ import annotations

import os
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY / "scripts"
sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(SCRIPTS_DIR))

# speaker_profiles loads the site catalog at import time and rewrites its
# built-in speakers from it.  Pin the lookup at a path that cannot exist,
# before any test module imports it: otherwise the suite silently asserts
# against whatever /etc/cdsp-automation/speaker-catalog.json happens to hold,
# which makes it unrunnable on exactly the deployed hosts it should verify.
HERMETIC_CATALOG_PATH = str(SCRIPTS_DIR / "no-such-speaker-catalog.json")
os.environ["SPEAKER_CATALOG_PATH"] = HERMETIC_CATALOG_PATH


import pytest


@pytest.fixture(autouse=True)
def hermetic_motu_access_record(tmp_path, monkeypatch):
    """Give every test its own MOTU access record (motu_access.py).

    The default lives under /var/lib, and a record shared between tests would
    let one test's MOTU access defer the next test's read-back.
    """
    monkeypatch.setenv("MOTU_ACCESS_PATH", str(tmp_path / "motu-access.lock"))
    monkeypatch.delenv("MOTU_ACCESS_WINDOW_SECONDS", raising=False)
