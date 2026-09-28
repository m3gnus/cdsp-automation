"""Settings every script shares.

Paths are fixed here, in one place.  Only what a deployment genuinely varies
comes from the environment file the units share (cdsp-automation.env); a
script reads its own device names and limits there itself.  Keys in that file
that nothing reads any more are ignored.
"""

from __future__ import annotations

import os
from pathlib import Path


# ------------------------------------------------------- from the env file

CDSP_HOST = os.environ.get("CDSP_HOST", "127.0.0.1")
CDSP_PORT = int(os.environ.get("CDSP_PORT", "1234"))
# The control UI runs as root, so it cannot derive this from $HOME: the
# installer writes it into the env file.  The env file sits beside it.
CONFIG_DIR = Path(
    os.environ.get("CDSP_CONFIG_DIR")
    or os.path.join(os.path.expanduser("~"), "camilladsp/configs")
)
ENV_FILE = CONFIG_DIR.parent / "cdsp-automation.env"
SOURCE_OVERRIDE_PATH = Path(
    os.environ.get("SOURCE_OVERRIDE_PATH")
    or "/run/cdsp-source-switcher/manual_source"
)

# ---------------------------------------------------------------- fixed

CAMILLA_BINARY = "camilladsp"

STATE_DIR = Path("/var/lib/cdsp-automation")
AUDIO_CONTROL_LOCK_PATH = STATE_DIR / "audio-control.lock"
AUDIO_EQ_PATH = STATE_DIR / "audio-eq.json"
AUDIO_EQ_BACKUP_DIR = STATE_DIR / "audio-eq-backups"
SPEAKER_SELECTION_PATH = STATE_DIR / "speaker-selection.json"
SPEAKER_AUDIO_DIR = STATE_DIR / "speaker-audio"
SPEAKER_GENERATED_DIR = STATE_DIR / "generated-configs"
# The level each speaker/source pair was last played at (source_volume.py).
SOURCE_VOLUME_PATH = STATE_DIR / "source-volume.json"
# Written by the ISO 226 engine installer once the engine it built is running.
ISO226_CAPABILITY_PATH = STATE_DIR / "iso226-engine.json"

SITE_CONFIG_DIR = Path("/etc/cdsp-automation")
SPEAKER_PROFILE_DIR = SITE_CONFIG_DIR / "speaker-profiles"
SPEAKER_CATALOG_PATH = SITE_CONFIG_DIR / "speaker-catalog.json"
SOURCE_BASE_DIR = SITE_CONFIG_DIR / "source-bases"
# Optional: the HID remote's button map (remote_keymap.py).
REMOTE_KEYMAP_PATH = SITE_CONFIG_DIR / "remote-keymap.json"

SWITCHER_RUN_DIR = Path("/run/cdsp-source-switcher")
AUDIO_EQ_STATUS_PATH = SWITCHER_RUN_DIR / "audio-eq-status.json"
SPEAKER_STATUS_PATH = SWITCHER_RUN_DIR / "speaker-profile-status.json"
MOTU_VOLUME_REQUEST_PATH = SWITCHER_RUN_DIR / "motu-volume-request.json"
MOTU_VOLUME_STATUS_PATH = SWITCHER_RUN_DIR / "motu-volume.json"

AIRPLAY_VOLUME_STATUS_PATH = Path("/run/airplay-volume-bridge/status.json")
AIRPLAY_VOLUME_SOCKET_PATH = Path("/run/airplay-volume-bridge/input.sock")
AIRPLAY_ACTIVE_PATH = Path("/run/airplay-volume-bridge/playback-active")
SPOTIFY_VOLUME_COMMAND_SOCKET_PATH = Path("/run/raspotify/cdsp-volume.sock")
# The group both volume-sync sockets are restricted to.
VOLUME_SYNC_GROUP = "audio"
