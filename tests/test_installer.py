from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "install.sh"


class InstallerUnitTests(unittest.TestCase):
    def test_download_set_includes_complete_speaker_compiler(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("speaker_config.py speaker_xo.py", installer)

    def test_network_volume_install_covers_airplay_and_spotify(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("install_airplay_volume_bridge; install_spotify_volume_sync", installer)
        self.assertIn("build_librespot_volume_sync.sh", installer)
        self.assertIn("librespot-v0.8.0-volume-sync.patch", installer)
        self.assertIn("SPOTIFY_VOLUME_COMMAND_SOCKET_PATH", installer)

        builder = (REPOSITORY / "scripts" / "build_librespot_volume_sync.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--features alsa-backend,native-tls", builder)
        self.assertIn("deployment_started=true", builder)
        self.assertIn("deployment_complete=true", builder)
        self.assertIn("UGLAN_SPOTIFY_VOLUME_ACK_SOCKET", builder)

    def test_create_unit_migrates_legacy_enablement_and_creates_runtime_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            unit_dir = root / "etc-systemd"
            legacy_dir = root / "lib-systemd"
            home.mkdir()
            unit_dir.mkdir()
            legacy_dir.mkdir()
            (legacy_dir / "cdsp-source-switcher.service").touch()
            log_path = root / "systemctl.log"

            command = f"""
set -euo pipefail
export HOME={home!s}
export USER=tester
export CDSP_AUTOMATION_SYSTEMD_UNIT_DIR={unit_dir!s}
export CDSP_AUTOMATION_LEGACY_UNIT_DIR={legacy_dir!s}
source {INSTALLER!s}
systemctl() {{ printf '%s\\n' "$*" >> {log_path!s}; }}
sudo() {{
  if [[ "$1" == systemctl ]]; then
    shift
    systemctl "$@"
  else
    command "$@"
  fi
}}
create_unit "Source Switcher" source_switcher.py cdsp-source-switcher
"""
            subprocess.run(["bash", "-c", command], check=True, env=os.environ.copy())

            unit = (unit_dir / "cdsp-source-switcher.service").read_text(encoding="utf-8")
            self.assertIn("RuntimeDirectory=cdsp-source-switcher", unit)
            self.assertIn("RuntimeDirectoryPreserve=yes", unit)
            self.assertIn("WantedBy=multi-user.target", unit)
            self.assertFalse((legacy_dir / "cdsp-source-switcher.service").exists())

            calls = log_path.read_text(encoding="utf-8").splitlines()
            self.assertIn("daemon-reload", calls)
            self.assertIn("reenable cdsp-source-switcher.service", calls)
            self.assertIn("restart cdsp-source-switcher.service", calls)

    def test_update_refreshes_installed_airplay_callback_bundle(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        refresh = installer.split("refresh_installed_units()", 1)[1].split(
            "show_status()", 1
        )[0]
        self.assertIn("/usr/local/libexec/airplay_volume_bridge.py", refresh)
        self.assertIn(
            '"$SCRIPTS_DIR/speaker_profiles.py" "$SCRIPTS_DIR/audio_eq.py"', refresh
        )

    def test_state_storage_prepares_configured_lock_and_clock_parents(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        storage = installer.split("ensure_audio_state_storage()", 1)[1].split(
            "create_unit()", 1
        )[0]

        self.assertIn(
            "MOTU_CLOCK_STATE_PATH=/var/lib/cdsp-automation/motu-clock-source",
            installer,
        )
        self.assertIn(
            'ensure_user_writable_dir "$(dirname "$audio_control_lock_path")"',
            storage,
        )
        self.assertIn(
            'ensure_user_writable_dir "$(dirname "$motu_clock_state_path")"',
            storage,
        )

    def test_default_env_points_the_control_ui_at_the_installed_base_dir(self) -> None:
        """The UI unit has no User=, so it cannot resolve these from $HOME."""
        with tempfile.TemporaryDirectory() as directory:
            command = f"""
set -euo pipefail
export HOME={directory!s}
export CDSP_AUTOMATION_BASE_DIR={directory!s}/site
source {INSTALLER!s}
default_env
"""
            result = subprocess.run(
                ["bash", "-c", command],
                check=True,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )
            rendered = result.stdout.splitlines()
            self.assertIn(f"CDSP_CONFIG_DIR={directory}/site/configs", rendered)
            self.assertIn(
                f"CDSP_AUTOMATION_ENV={directory}/site/cdsp-automation.env", rendered
            )

    def test_control_ui_unit_runs_web_ui_from_the_managed_venv(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        heredoc = installer.split("install_control_ui()", 1)[1].split(
            "install_iso226_engine()", 1
        )[0]
        self.assertIn(
            "ExecStart=$VENV_DIR/bin/python3 -u $SCRIPTS_DIR/web_ui.py", heredoc
        )
        self.assertIn("SyslogIdentifier=cdsp-control-ui", heredoc)
        self.assertIn("WantedBy=multi-user.target", heredoc)
        self.assertIn("cdsp-control-ui.service", heredoc)

    def test_sudoers_has_no_wildcard_root_command_authorization(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn("--on-active=*", installer)
        self.assertNotIn("--unit=cdsp-trigger-restart-*", installer)
        self.assertNotIn("command -v systemctl", installer)
        self.assertNotIn("command -v shutdown", installer)
        self.assertNotIn("REMOTE_TRIGGER_RESTART_DELAY_SECONDS", installer)
        self.assertNotIn("$USER ALL=(root)", installer)
        self.assertIn('INSTALL_USER="$(/usr/bin/id -un)"', installer)
        self.assertIn(
            "$SYSTEMCTL_BIN --no-block restart cdsp-remote.service",
            installer,
        )
        self.assertIn("$SYSTEMCTL_BIN poweroff", installer)


if __name__ == "__main__":
    unittest.main()
