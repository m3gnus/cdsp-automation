"""Test imports mirror the flat scripts directory used on the Pi."""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY / "scripts"
sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(SCRIPTS_DIR))
