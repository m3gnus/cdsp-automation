"""ISO 226 engine preflight: the gate that runs before any Rust toolchain work."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
BUILDER = REPOSITORY / "scripts" / "build_camilladsp_iso226.sh"
INSTALLER = REPOSITORY / "install.sh"


class EnginePreflightTests(unittest.TestCase):
    """The builder runs as a subprocess, so these use real PATH shims."""

    def _shim(self, directory: Path, name: str, body: str) -> None:
        path = directory / name
        path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def _preflight(
        self,
        *,
        main_pid: str,
        exe: str = "",
        unit_text: str = "",
        unit_exists: bool = True,
        arguments: tuple[str, ...] = ("--preflight",),
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            log = root / "toolchain.log"
            self._shim(
                binaries,
                "systemctl",
                "\n".join(
                    [
                        'if [[ "$1" == cat ]]; then',
                        f"  {'cat <<\"UNIT\"' if unit_exists else 'exit 1'}",
                        *( [unit_text, "UNIT"] if unit_exists else []),
                        "  exit 0",
                        "fi",
                        'if [[ "$1" == show ]]; then',
                        f'  echo "{main_pid}"',
                        "  exit 0",
                        "fi",
                        "exit 0",
                    ]
                ),
            )
            self._shim(binaries, "sudo", 'exec "$@"')
            self._shim(
                binaries,
                "readlink",
                "\n".join(
                    [
                        'if [[ "$2" == /proc/* ]]; then',
                        f'  echo "{exe}"',
                        "  exit 0",
                        "fi",
                        'echo "$2"',
                    ]
                ),
            )
            # Any toolchain use at all is a failure of the "preflight first"
            # contract, so these record instead of working.
            for tool in ("git", "cargo", "rustc"):
                self._shim(binaries, tool, f'echo "{tool}" >> "{log}"; exit 1')

            environment = os.environ.copy()
            environment["PATH"] = f"{binaries}:{environment['PATH']}"
            environment["HOME"] = str(root)
            result = subprocess.run(
                ["bash", str(BUILDER), *arguments],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            result.toolchain = log.read_text(encoding="utf-8") if log.exists() else ""
            return result

    def test_preflight_accepts_a_running_target_binary(self) -> None:
        result = self._preflight(main_pid="4321", exe="/usr/local/bin/camilladsp")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.toolchain, "")

    def test_preflight_rejects_a_running_foreign_binary(self) -> None:
        result = self._preflight(main_pid="4321", exe="/opt/camilladsp/bin/camilladsp")
        self.assertEqual(result.returncode, 1)
        self.assertIn("running a binary other than", result.stderr)

    def test_preflight_reads_the_last_execstart_when_the_unit_is_stopped(self) -> None:
        """Last assignment wins, command prefixes are stripped, a reset is honest."""
        result = self._preflight(
            main_pid="0",
            unit_text=(
                "[Service]\n"
                "ExecStart=/usr/bin/camilladsp -p 1234\n"
                "\n"
                "# /etc/systemd/system/camilladsp.service.d/override.conf\n"
                "[Service]\n"
                "ExecStart=\n"
                "  ExecStart=-@/usr/local/bin/camilladsp -p 1234\n"
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.toolchain, "")

    def test_preflight_rejects_a_stopped_unit_that_starts_something_else(self) -> None:
        result = self._preflight(
            main_pid="0", unit_text="[Service]\nExecStart=/opt/bin/camilladsp\n"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("/opt/bin/camilladsp", result.stderr)

    def test_preflight_rejects_a_reset_with_no_reassignment(self) -> None:
        result = self._preflight(
            main_pid="0", unit_text="[Service]\nExecStart=/usr/local/bin/camilladsp\nExecStart=\n"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("an unknown binary", result.stderr)

    def test_preflight_rejects_a_missing_unit(self) -> None:
        result = self._preflight(main_pid="0", unit_exists=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not installed", result.stderr)

    def test_an_ordinary_build_preflights_before_touching_the_toolchain(self) -> None:
        """A direct builder run must not reach cargo on an unreplaceable engine."""
        result = self._preflight(
            main_pid="4321",
            exe="/opt/camilladsp/bin/camilladsp",
            arguments=(),
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.toolchain, "")
        self.assertIn("running a binary other than", result.stderr)

    def test_explicit_engine_install_fails_loudly_with_the_reason(self) -> None:
        """Menu option 10 still fails hard, and says why."""
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory) / "scripts"
            scripts.mkdir()
            builder = scripts / "build_camilladsp_iso226.sh"
            builder.write_text("#!/bin/bash\nexit 3\n", encoding="utf-8")
            builder.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"set -euo pipefail\nsource {INSTALLER!s}\n"
                    f"SCRIPTS_DIR={scripts}\n"
                    "status=0\ninstall_iso226_engine || status=$?\n"
                    'echo "status=$status"',
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env={**os.environ, "HOME": directory, "CDSP_AUTOMATION_BASE_DIR": directory},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("status=3", result.stdout)
            self.assertIn("/usr/local/bin/camilladsp", result.stdout)
            self.assertIn("README prerequisite", result.stdout)


if __name__ == "__main__":
    unittest.main()
