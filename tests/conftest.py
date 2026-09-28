"""Test imports mirror the flat scripts directory used on the Pi."""

from __future__ import annotations

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
import settings  # noqa: E402

HERMETIC_CATALOG_PATH = SCRIPTS_DIR / "no-such-speaker-catalog.json"
settings.SPEAKER_CATALOG_PATH = HERMETIC_CATALOG_PATH


import pytest


@pytest.fixture(autouse=True)
def hermetic_motu_volume_files(tmp_path, monkeypatch):
    """Give every test its own MOTU volume request and status files.

    The defaults live under /run/cdsp-source-switcher, where a file left by
    one test (or by a deployed switcher) would answer the next test's request.
    """
    import motu_volume

    monkeypatch.setattr(motu_volume, "REQUEST_PATH", tmp_path / "motu-volume-request.json")
    monkeypatch.setattr(motu_volume, "STATUS_PATH", tmp_path / "motu-volume.json")


@pytest.fixture(autouse=True)
def hermetic_source_volume_file(tmp_path, monkeypatch):
    """Keep the per-source volume memory out of /var/lib/cdsp-automation."""
    import source_volume

    monkeypatch.setattr(source_volume, "STATE_PATH", tmp_path / "source-volume.json")


@pytest.fixture(autouse=True)
def hermetic_motu_loudness_level(monkeypatch):
    """The switcher remembers the last MOTU level it saw; start each test clean."""
    for name in ("source_switcher", "scripts.source_switcher"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "_last_motu_loudness_db"):
            monkeypatch.setattr(module, "_last_motu_loudness_db", None)
