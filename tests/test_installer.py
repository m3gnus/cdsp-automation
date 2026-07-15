from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "install.sh"


class InstallerUnitTests(unittest.TestCase):
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
