"""ISO 226 engine preflight: the gate that runs before any Rust toolchain work."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
BUILDER = REPOSITORY / "scripts" / "build_camilladsp_iso226.sh"
INSTALLER = REPOSITORY / "install.sh"


def _shim(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


class EnginePreflightTests(unittest.TestCase):
    """The builder runs as a subprocess, so these use real PATH shims."""

    def _shim(self, directory: Path, name: str, body: str) -> None:
        _shim(directory, name, body)

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


class EngineUninstallTests(unittest.TestCase):
    """`--uninstall` fires on every "Uninstall All", even on installs that never
    built an engine, so it has to prove ownership before removing anything."""

    STOCK = b"stock camilladsp from the distribution\n"
    OURS = b"ISO 226 camilladsp built by this helper\n"

    def _receipt(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        return (
            '{"engine":"Iso226","upstream_commit":"05e9cfcd",'
            f'"binary_sha256":"{digest}","installed_at":1758326400}}\n'
        )

    def _uninstall(self, root: Path) -> subprocess.CompletedProcess[str]:
        """Run the real script against sandboxed paths; sudo is a pass-through."""
        binaries = root / "bin"
        binaries.mkdir(exist_ok=True)
        _shim(binaries, "sudo", 'exec "$@"')
        _shim(
            binaries,
            "systemctl",
            f'printf "%s\\n" "$*" >> {root / "systemctl.log"}',
        )
        environment = os.environ.copy()
        environment.update(
            PATH=f"{binaries}:{environment['PATH']}",
            HOME=str(root),
            CDSP_AUTOMATION_CAMILLADSP_TARGET=str(root / "camilladsp"),
            CDSP_AUTOMATION_CAMILLADSP_BACKUP=str(root / "camilladsp.pre-iso226"),
            ISO226_CAPABILITY_PATH=str(root / "iso226-engine.json"),
        )
        return subprocess.run(
            ["bash", str(BUILDER), "--uninstall"],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def _service_calls(self, root: Path) -> str:
        log = root / "systemctl.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def test_uninstall_without_a_receipt_leaves_a_stock_binary_untouched(self) -> None:
        """Install the remote only, then "Uninstall All": the engine is not ours."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "camilladsp"
            target.write_bytes(self.STOCK)
            target.chmod(0o755)

            result = self._uninstall(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_bytes(), self.STOCK)
            self.assertIn("No ISO 226 install receipt", result.stdout)
            self.assertEqual(self._service_calls(root), "")

    def test_uninstall_keeps_a_replaced_binary_and_drops_the_stale_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "camilladsp"
            replacement = b"a newer camilladsp the operator installed later\n"
            target.write_bytes(replacement)
            target.chmod(0o755)
            capability = root / "iso226-engine.json"
            capability.write_text(self._receipt(self.OURS), encoding="utf-8")

            result = self._uninstall(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_bytes(), replacement)
            self.assertFalse(capability.exists())
            self.assertIn("does not match the ISO 226 receipt", result.stderr)
            self.assertEqual(self._service_calls(root), "")

    def test_uninstall_keeps_the_binary_when_the_receipt_is_unreadable(self) -> None:
        """A receipt without a usable digest proves nothing, so it decides nothing."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "camilladsp"
            target.write_bytes(self.OURS)
            target.chmod(0o755)
            capability = root / "iso226-engine.json"
            capability.write_text('{"engine":"Iso226"}\n', encoding="utf-8")

            result = self._uninstall(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_bytes(), self.OURS)
            self.assertFalse(capability.exists())
            self.assertEqual(self._service_calls(root), "")

    def test_uninstall_restores_the_backup_it_took_and_then_consumes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "camilladsp"
            target.write_bytes(self.OURS)
            target.chmod(0o755)
            backup = root / "camilladsp.pre-iso226"
            backup.write_bytes(self.STOCK)
            backup.chmod(0o755)
            capability = root / "iso226-engine.json"
            capability.write_text(self._receipt(self.OURS), encoding="utf-8")

            result = self._uninstall(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_bytes(), self.STOCK)
            # A stale backup would let the next cycle roll back two generations.
            self.assertFalse(backup.exists())
            self.assertFalse(capability.exists())
            self.assertIn("restart camilladsp.service", self._service_calls(root))

    def test_uninstall_removes_an_engine_that_had_no_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "camilladsp"
            target.write_bytes(self.OURS)
            target.chmod(0o755)
            capability = root / "iso226-engine.json"
            capability.write_text(self._receipt(self.OURS), encoding="utf-8")

            result = self._uninstall(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(target.exists())
            self.assertFalse(capability.exists())
            self.assertIn("restart camilladsp.service", self._service_calls(root))

    def test_uninstall_is_silent_about_services_when_nothing_was_installed(self) -> None:
        """No binary and no receipt: the common case on a remote-only install."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            result = self._uninstall(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._service_calls(root), "")


class EngineInstallRollbackTests(unittest.TestCase):
    """Every failure after the live engine is replaced must put back what was
    running a moment ago - binary and receipt together.

    The toolchain and the service are shimmed.  ``failpoint`` picks the one
    post-swap step that fails; the rest behave like a healthy Pi."""

    STOCK = b"stock camilladsp from the distribution\n"
    PREVIOUS = b"ISO 226 camilladsp from an earlier successful install\n"
    CANDIDATE = b"ISO 226 camilladsp candidate being installed\n"
    FAILPOINTS = ("restart", "health", "show", "receipt")

    def _receipt(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        return (
            '{"engine":"Iso226","upstream_commit":"05e9cfcd",'
            f'"binary_sha256":"{digest}","installed_at":1758326400}}\n'
        )

    def _install(self, root: Path, failpoint: str = "") -> subprocess.CompletedProcess[str]:
        binaries = root / "bin"
        binaries.mkdir(exist_ok=True)
        target = root / "camilladsp"
        candidate = root / "candidate"
        candidate.write_bytes(self.CANDIDATE)
        restarts = root / "restarts"
        # The receipt publish is the only 0644 install whose source is the
        # freshly written marker; rollback's copy is previous-iso226-engine.json.
        _shim(
            binaries,
            "sudo",
            'if [[ "$FAILPOINT" == receipt && "$1" == install '
            '&& "$(basename "${@: -2:1}")" == iso226-engine.json ]]; then exit 1; fi\n'
            'exec "$@"',
        )
        _shim(binaries, "sleep", "exit 0")
        _shim(binaries, "rustc", 'echo "rustc 1.90.0 (shim)"')
        _shim(
            binaries,
            "readlink",
            f'if [[ "${{@: -1}}" == /proc/* ]]; then echo {target}; '
            'else exec /usr/bin/readlink "$@"; fi',
        )
        _shim(
            binaries,
            "git",
            'if [[ "$1" == clone ]]; then mkdir -p "${@: -1}"; fi; exit 0',
        )
        _shim(
            binaries,
            "cargo",
            'if [[ "$1" == build ]]; then\n'
            '  manifest="${@: -1}"; out="$(dirname "$manifest")/target/release"\n'
            f'  mkdir -p "$out" && cp {candidate} "$out/camilladsp"\n'
            "fi\nexit 0",
        )
        _shim(
            binaries,
            "systemctl",
            f'printf "%s\\n" "$*" >> {root / "systemctl.log"}\n'
            'case "$1" in\n'
            f'  cat) echo "ExecStart={target} -p 1234 config.yml" ;;\n'
            # Preflight tolerates a failing show; the post-swap one must not.
            '  show) [[ "$FAILPOINT" == show && -e ' + str(restarts) + ' ]] && exit 1\n'
            "        echo 1234 ;;\n"
            f'  restart) echo x >> {restarts}\n'
            f'           [[ "$FAILPOINT" == restart && $(wc -l < {restarts}) -eq 1 ]] && exit 1 ;;\n'
            '  is-active) [[ "$FAILPOINT" == health ]] && exit 3 ;;\n'
            "esac\nexit 0",
        )
        environment = os.environ.copy()
        environment.update(
            PATH=f"{binaries}:{environment['PATH']}",
            HOME=str(root),
            FAILPOINT=failpoint,
            CDSP_CONFIG_DIR=str(root / "configs"),
            CDSP_AUTOMATION_CAMILLADSP_TARGET=str(target),
            CDSP_AUTOMATION_CAMILLADSP_BACKUP=str(root / "camilladsp.pre-iso226"),
            ISO226_CAPABILITY_PATH=str(root / "iso226-engine.json"),
        )
        return subprocess.run(
            ["bash", str(BUILDER)],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def _upgrade_fixture(self, root: Path) -> str:
        target = root / "camilladsp"
        target.write_bytes(self.PREVIOUS)
        target.chmod(0o755)
        (root / "camilladsp.pre-iso226").write_bytes(self.STOCK)
        receipt = self._receipt(self.PREVIOUS)
        (root / "iso226-engine.json").write_text(receipt, encoding="utf-8")
        return receipt

    def test_a_healthy_upgrade_publishes_the_new_build_and_its_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._upgrade_fixture(root)

            result = self._install(root)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / "camilladsp").read_bytes(), self.CANDIDATE)
            receipt = (root / "iso226-engine.json").read_text(encoding="utf-8")
            self.assertIn(hashlib.sha256(self.CANDIDATE).hexdigest(), receipt)
            self.assertEqual((root / "camilladsp.pre-iso226").read_bytes(), self.STOCK)

    def test_every_post_swap_failure_restores_the_previous_build_and_receipt(
        self,
    ) -> None:
        for failpoint in self.FAILPOINTS:
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                receipt = self._upgrade_fixture(root)

                result = self._install(root, failpoint)

                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("restoring the previous engine", result.stderr)
                self.assertNotIn("ROLLBACK INCOMPLETE", result.stderr)
                self.assertEqual((root / "camilladsp").read_bytes(), self.PREVIOUS)
                self.assertFalse((root / "camilladsp.new").exists())
                self.assertEqual(
                    (root / "iso226-engine.json").read_text(encoding="utf-8"), receipt
                )
                # The uninstall copy still names the stock engine.
                self.assertEqual(
                    (root / "camilladsp.pre-iso226").read_bytes(), self.STOCK
                )

    def test_failed_first_install_leaves_the_stock_engine_and_no_bookkeeping(
        self,
    ) -> None:
        for failpoint in self.FAILPOINTS:
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                target = root / "camilladsp"
                target.write_bytes(self.STOCK)
                target.chmod(0o755)

                result = self._install(root, failpoint)

                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(target.read_bytes(), self.STOCK)
                self.assertFalse((root / "iso226-engine.json").exists())
                self.assertFalse((root / "camilladsp.pre-iso226").exists())


if __name__ == "__main__":
    unittest.main()
