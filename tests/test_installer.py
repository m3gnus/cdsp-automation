from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "install.sh"
# The name earlier releases compiled into generated artifacts, assembled from
# fragments so the literal never appears in this repository.
LEGACY_TAG = "ug" "lan"


class InstallerUnitTests(unittest.TestCase):
    def test_download_set_includes_complete_speaker_compiler(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("speaker_config.py speaker_xo.py", installer)

    def test_network_volume_install_covers_airplay_and_spotify(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("install_network_volume_sync", installer)
        self.assertIn("build_librespot_volume_sync.sh", installer)
        self.assertIn("librespot-v0.8.0-volume-sync.patch", installer)
        self.assertIn("SPOTIFY_VOLUME_COMMAND_SOCKET_PATH", installer)

        builder = (REPOSITORY / "scripts" / "build_librespot_volume_sync.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--features alsa-backend,native-tls", builder)
        self.assertIn("deployment_started=true", builder)
        self.assertIn("deployment_complete=true", builder)
        self.assertIn("CDSP_SPOTIFY_VOLUME_ACK_SOCKET", builder)

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
            # The switcher owns the audio-ready token, so an operator restart
            # of the engine must restart it too.  BindsTo would instead leave
            # the switcher stopped whenever the engine fails.
            self.assertIn("PartOf=camilladsp.service", unit)
            self.assertNotIn("BindsTo=", unit)
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

    def test_control_ui_unit_shares_the_install_group_but_stays_root(self) -> None:
        """Group= alone keeps uid 0 and makes root-created locks group-usable."""
        installer = INSTALLER.read_text(encoding="utf-8")
        heredoc = installer.split("install_control_ui()", 1)[1].split(
            "install_iso226_engine()", 1
        )[0]
        self.assertIn("Group=$INSTALL_GROUP", heredoc)
        self.assertIn("UMask=0007", heredoc)
        self.assertNotIn("\nUser=", heredoc)
        self.assertIn('INSTALL_GROUP="$(/usr/bin/id -gn)"', installer)
        self.assertIn(
            'if [[ ! "$INSTALL_GROUP" =~ ^[a-zA-Z0-9._-]+$ ]]; then', installer
        )

    def test_state_storage_claims_the_three_shared_locks(self) -> None:
        """Lazy creation never chowns, so a fresh install claims these up front."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "site"
            state = root / "state"
            etc = root / "etc"
            base.mkdir()
            log_path = root / "sudo.log"
            (base / "cdsp-automation.env").write_text(
                "\n".join(
                    [
                        f"AUDIO_EQ_PATH={state}/audio-eq.json",
                        f"AUDIO_CONTROL_LOCK_PATH={state}/audio-control.lock",
                        f"MOTU_CLOCK_STATE_PATH={state}/motu-clock-source",
                        f"SPEAKER_SELECTION_PATH={state}/speaker-selection.json",
                        f"SPEAKER_TRANSITION_PATH={state}/speaker-transition.json",
                        f"SPEAKER_AUDIO_DIR={state}/speaker-audio",
                        f"SPEAKER_PROFILE_DIR={etc}/speaker-profiles",
                        f"SOURCE_BASE_DIR={etc}/source-bases",
                        f"SPEAKER_GENERATED_DIR={state}/generated-configs",
                        f"AUDIO_EQ_BACKUP_DIR={state}/audio-eq-backups",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            # sudo is unavailable here: run the command directly, drop the
            # ownership flags only root can honour, and record the chown so its
            # arguments are still asserted.
            command = f"""
set -euo pipefail
export HOME={root!s}
export CDSP_AUTOMATION_BASE_DIR={base!s}
source {INSTALLER!s}
sudo() {{
  local args=()
  case "$1" in
    chown) shift; printf 'chown %s\\n' "$*" >> {log_path!s} ;;
    -u) shift 2; command "$@" ;;
    install)
      shift
      while [[ $# -gt 0 ]]; do
        case "$1" in
          -o|-g) shift 2 ;;
          *) args+=("$1"); shift ;;
        esac
      done
      command install "${{args[@]}}"
      ;;
    *) command "$@" ;;
  esac
}}
ensure_audio_state_storage
"""
            subprocess.run(["bash", "-c", command], check=True, env=os.environ.copy())

            identity = subprocess.run(
                ["/usr/bin/id", "-un"], capture_output=True, text=True, check=True
            ).stdout.strip()
            group = subprocess.run(
                ["/usr/bin/id", "-gn"], capture_output=True, text=True, check=True
            ).stdout.strip()
            chowned = log_path.read_text(encoding="utf-8").splitlines()
            for lock in (
                state / "audio-eq.json.lock",
                state / "audio-control.lock",
                state / "speaker-selection.json.lock",
            ):
                self.assertTrue(lock.is_file(), lock)
                self.assertEqual(lock.stat().st_mode & 0o777, 0o660)
                self.assertIn(f"chown {identity}:{group} {lock}", chowned)
            # The EQ safety net's directory is prepared like its siblings, so a
            # fresh install never depends on the root UI creating it.
            self.assertTrue((state / "audio-eq-backups").is_dir())

    def _run(self, body: str, *, env: dict[str, str] | None = None) -> str:
        """Source the installer and run `body`, returning one ordered stream."""
        environment = os.environ.copy()
        environment.update(env or {})
        result = subprocess.run(
            ["bash", "-c", f"set -euo pipefail\nsource {INSTALLER!s}\n{body}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        return result.stdout

    def test_install_all_skips_an_incompatible_engine_and_installs_the_rest(
        self,
    ) -> None:
        """Status 3 means "this engine cannot be replaced here", not a failure."""
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "steps.log"
            output = self._run(
                "\n".join(
                    [
                        f'record() {{ printf "%s\\n" "$1" >> {log!s}; }}',
                        "prepare_install() { record prepare; }",
                        "install_trigger() { record trigger; }",
                        "install_motu_sync() { record motu; }",
                        "install_source_switcher() { record switcher; }",
                        "install_remote() { record remote; }",
                        "install_airplay_volume_bridge() { record airplay; }",
                        "install_spotify_volume_sync() { record spotify; }",
                        "install_iso226_engine() { record engine; return 3; }",
                        "install_all",
                    ]
                ),
                env={"HOME": directory, "CDSP_AUTOMATION_BASE_DIR": directory},
            )
            steps = log.read_text(encoding="utf-8").split()
            self.assertEqual(
                steps,
                [
                    "prepare",
                    "trigger",
                    "motu",
                    "switcher",
                    "remote",
                    "airplay",
                    "spotify",
                    "engine",
                ],
            )
            self.assertIn("ISO 226 loudness engine: SKIPPED", output)
            self.assertIn("1 skipped or failed component(s)", output)

    def test_install_all_reports_a_failed_engine_build_without_aborting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "steps.log"
            output = self._run(
                "\n".join(
                    [
                        f'record() {{ printf "%s\\n" "$1" >> {log!s}; }}',
                        "prepare_install() { record prepare; }",
                        "install_trigger() { record trigger; }",
                        "install_motu_sync() { :; }",
                        "install_source_switcher() { :; }",
                        "install_remote() { :; }",
                        "install_airplay_volume_bridge() { return 1; }",
                        "install_spotify_volume_sync() { :; }",
                        "install_iso226_engine() { return 1; }",
                        "install_all",
                    ]
                ),
                env={"HOME": directory, "CDSP_AUTOMATION_BASE_DIR": directory},
            )
            self.assertIn("trigger", log.read_text(encoding="utf-8"))
            self.assertIn("AirPlay volume bridge: FAILED", output)
            self.assertIn("ISO 226 loudness engine: FAILED", output)
            self.assertIn("2 skipped or failed component(s)", output)

    def test_uninstall_all_points_the_engine_helper_at_the_configured_receipt(
        self,
    ) -> None:
        """The helper refuses to touch the binary without its receipt, so it has
        to be told where this deployment keeps one."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "site"
            scripts = base / "scripts"
            scripts.mkdir(parents=True)
            capability = root / "custom" / "iso226-engine.json"
            (base / "cdsp-automation.env").write_text(
                f"ISO226_CAPABILITY_PATH={capability}\n", encoding="utf-8"
            )
            seen = root / "helper.log"
            helper = scripts / "build_camilladsp_iso226.sh"
            helper.write_text(
                f'#!/bin/bash\nprintf "%s %s\\n" "$1" "$ISO226_CAPABILITY_PATH" >> {seen!s}\n',
                encoding="utf-8",
            )
            helper.chmod(0o755)

            self._run(
                "\n".join(
                    [
                        "systemctl() { :; }",
                        'sudo() { if [[ "$1" == systemctl ]]; then shift; systemctl "$@"; fi; }',
                        "remove_unit_file() { :; }",
                        f"SCRIPTS_DIR={scripts!s}",
                        f"SHAIRPORT_CONFIG={root!s}/absent.conf",
                        "uninstall_all",
                    ]
                ),
                env={"HOME": str(root), "CDSP_AUTOMATION_BASE_DIR": str(base)},
            )

            self.assertEqual(
                seen.read_text(encoding="utf-8").strip(),
                f"--uninstall {capability}",
            )

    def test_summary_notes_do_not_leak_between_menu_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = self._run(
                "\n".join(
                    [
                        "prepare_install() { :; }",
                        "install_trigger() { :; }",
                        "install_motu_sync() { :; }",
                        "install_source_switcher() { :; }",
                        "install_remote() { :; }",
                        "install_airplay_volume_bridge() { :; }",
                        'install_spotify_volume_sync() { note_skip "Spotify volume sync: SKIPPED (no raspotify)"; }',
                        "install_iso226_engine() { :; }",
                        "install_network_volume_sync",
                        'echo "--- second action ---"',
                        "install_spotify_volume_sync() { :; }",
                        "install_all",
                    ]
                ),
                env={"HOME": directory, "CDSP_AUTOMATION_BASE_DIR": directory},
            )
            first, second = output.split("--- second action ---", 1)
            self.assertIn("Spotify volume sync: SKIPPED", first)
            self.assertNotIn("Spotify volume sync: SKIPPED", second)
            self.assertIn("All requested components installed.", second)

    def test_update_never_rebuilds_or_restarts_the_running_engine(self) -> None:
        """A routine update must not replace the live CamillaDSP binary."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "site"
            (base / ".venv" / "bin").mkdir(parents=True)
            capability = root / "iso226-engine.json"
            capability.write_text("{}", encoding="utf-8")
            log = root / "calls.log"
            (base / ".venv" / "bin" / "activate").write_text(
                f'pip() {{ printf "pip %s\\n" "$*" >> {log!s}; }}\n'
                "deactivate() { :; }\n",
                encoding="utf-8",
            )
            (base / "cdsp-automation.env").write_text(
                f"ISO226_CAPABILITY_PATH={capability}\n", encoding="utf-8"
            )

            output = self._run(
                "\n".join(
                    [
                        f'systemctl() {{ printf "systemctl %s\\n" "$*" >> {log!s}; }}',
                        'sudo() { if [[ "$1" == systemctl ]]; then shift; systemctl "$@"; fi; }',
                        "download_scripts() { :; }",
                        "migrate_env_defaults() { :; }",
                        "ensure_audio_state_storage() { :; }",
                        "ensure_venv() { :; }",
                        f'install_iso226_engine() {{ printf "ENGINE-REBUILT\\n" >> {log!s}; }}',
                        f'refresh_installed_units() {{ printf "refresh\\n" >> {log!s}; }}',
                        "update_utilities",
                    ]
                ),
                env={"HOME": str(root), "CDSP_AUTOMATION_BASE_DIR": str(base)},
            )
            calls = log.read_text(encoding="utf-8")
            self.assertNotIn("ENGINE-REBUILT", calls)
            self.assertNotIn("restart camilladsp", calls)
            self.assertNotIn("build_camilladsp_iso226", calls)
            self.assertIn("refresh", calls)
            self.assertIn("restart cdsp-source-switcher.service", calls)
            self.assertIn("left running untouched", output)
            self.assertIn("menu option 10", output)

    def test_spotify_sync_skips_loudly_when_raspotify_is_absent(self) -> None:
        """No unit means no clone, no cargo, and no aborted installer."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "built"
            builder = root / "build_librespot_volume_sync.sh"
            builder.write_text(
                f"#!/bin/bash\ntouch {sentinel!s}\n", encoding="utf-8"
            )
            builder.chmod(0o755)
            output = self._run(
                "\n".join(
                    [
                        "systemctl() { return 0; }",
                        f"SCRIPTS_DIR={root!s}",
                        "install_spotify_volume_sync",
                        'echo "status=$?"',
                    ]
                ),
                env={"HOME": directory, "CDSP_AUTOMATION_BASE_DIR": directory},
            )
            self.assertIn("Spotify volume sync: SKIPPED", output)
            self.assertIn("re-run menu option 9", output)
            self.assertIn("status=0", output)
            self.assertFalse(sentinel.exists())

    def test_update_path_migrates_env_receivers_and_backups_in_order(self) -> None:
        """The whole deployed-Pi migration, in the order it actually happens."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "site"
            base.mkdir()
            dropin_dir = root / "raspotify.service.d"
            dropin_dir.mkdir()
            legacy_dropin = dropin_dir / f"{LEGACY_TAG}-volume-sync.conf"
            legacy_dropin.write_text(
                f"ExecStart=/usr/local/bin/librespot-{LEGACY_TAG} --device site_main\n",
                encoding="utf-8",
            )
            env_file = base / "cdsp-automation.env"
            env_file.write_text(
                f"SPOTIFY_VOLUME_COMMAND_SOCKET_PATH=/run/raspotify/{LEGACY_TAG}-volume.sock\n"
                "SPOTIFY_ALSA_DEVICE=\n",
                encoding="utf-8",
            )
            log = root / "order.log"

            self._run(
                "\n".join(
                    [
                        f'record() {{ printf "%s\\n" "$1" >> {log!s}; }}',
                        # Every managed unit is reported installed.
                        "systemctl() { echo \"$3 enabled\"; }",
                        "sudo() { :; }",
                        "create_unit() { record \"unit:$3\"; }",
                        "install_remote_sudoers() { record remote-sudoers; }",
                        "install_receiver_sudoers() { record receiver-sudoers; }",
                        "configure_shairport_bridge() { record shairport; }",
                        "install_spotify_volume_sync() { record spotify; }",
                        "install_control_ui() { record ui; }",
                        "migrate_audio_eq_backups() { record backups; }",
                        "migrate_env_defaults",
                        "refresh_installed_units",
                    ]
                ),
                env={
                    "HOME": str(root),
                    "CDSP_AUTOMATION_BASE_DIR": str(base),
                    "CDSP_AUTOMATION_RASPOTIFY_DROPIN_DIR": str(dropin_dir),
                },
            )
            values = env_file.read_text(encoding="utf-8")
            self.assertIn(
                "SPOTIFY_VOLUME_COMMAND_SOCKET_PATH=/run/raspotify/cdsp-volume.sock",
                values,
            )
            self.assertIn("SPOTIFY_ALSA_DEVICE=site_main", values)
            steps = log.read_text(encoding="utf-8").split()
            self.assertEqual(
                steps,
                [
                    "unit:cdsp-trigger",
                    "unit:cdsp-motu-sync",
                    "unit:cdsp-source-switcher",
                    "receiver-sudoers",
                    "unit:airplay-volume-bridge",
                    "shairport",
                    "remote-sudoers",
                    "unit:cdsp-remote",
                    "spotify",
                    "ui",
                    "backups",
                ],
            )

    def test_env_migration_moves_only_the_exact_historical_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "site"
            dropin_dir = root / "raspotify.service.d"
            dropin_dir.mkdir(parents=True)
            base.mkdir()
            legacy_dropin = dropin_dir / f"{LEGACY_TAG}-volume-sync.conf"
            legacy_dropin.write_text(
                f"[Service]\nExecStart=\n"
                f"ExecStart=/usr/local/bin/librespot-{LEGACY_TAG} --device site_main\n",
                encoding="utf-8",
            )
            (dropin_dir / "10-operator.conf").write_text(
                "[Service]\nExecStart=/opt/other --device operator_pcm\n",
                encoding="utf-8",
            )
            env_file = base / "cdsp-automation.env"
            env_file.write_text(
                f"SPOTIFY_VOLUME_COMMAND_SOCKET_PATH=/run/raspotify/{LEGACY_TAG}-volume.sock\n"
                f"SPOTIFY_VOLUME_DROPIN_PATH={legacy_dropin}\n"
                "SPOTIFY_ALSA_DEVICE=\n",
                encoding="utf-8",
            )

            self._run(
                "migrate_env_defaults\nmigrate_env_defaults",
                env={
                    "HOME": str(root),
                    "CDSP_AUTOMATION_BASE_DIR": str(base),
                    "CDSP_AUTOMATION_RASPOTIFY_DROPIN_DIR": str(dropin_dir),
                },
            )
            values = env_file.read_text(encoding="utf-8")
            self.assertIn(
                "SPOTIFY_VOLUME_COMMAND_SOCKET_PATH=/run/raspotify/cdsp-volume.sock",
                values,
            )
            self.assertIn(
                f"SPOTIFY_VOLUME_DROPIN_PATH={dropin_dir}/cdsp-volume-sync.conf", values
            )
            # Lifted out of this installer's own drop-in, never the operator's.
            self.assertIn("SPOTIFY_ALSA_DEVICE=site_main", values)
            self.assertNotIn("operator_pcm", values)
            self.assertEqual(values.count("SPOTIFY_ALSA_DEVICE="), 1)

    def test_env_migration_preserves_operator_chosen_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "site"
            base.mkdir()
            env_file = base / "cdsp-automation.env"
            env_file.write_text(
                "SPOTIFY_VOLUME_COMMAND_SOCKET_PATH=/run/raspotify/custom.sock\n"
                "SPOTIFY_VOLUME_DROPIN_PATH=/etc/systemd/system/raspotify.service.d/99-mine.conf\n"
                "SPOTIFY_ALSA_DEVICE=default\n",
                encoding="utf-8",
            )
            self._run(
                "migrate_env_defaults",
                env={"HOME": str(root), "CDSP_AUTOMATION_BASE_DIR": str(base)},
            )
            values = env_file.read_text(encoding="utf-8")
            self.assertIn(
                "SPOTIFY_VOLUME_COMMAND_SOCKET_PATH=/run/raspotify/custom.sock", values
            )
            self.assertIn("99-mine.conf", values)
            self.assertIn("SPOTIFY_ALSA_DEVICE=default", values)

    def test_refresh_rebuilds_spotify_sync_from_either_dropin_generation(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        refresh = installer.split("refresh_installed_units()", 1)[1].split(
            "show_status()", 1
        )[0]
        self.assertIn(
            '[[ -f "$SPOTIFY_DROPIN_PATH" || -f "$LEGACY_SPOTIFY_DROPIN" ]]', refresh
        )
        # Exact names only: no glob may claim an administrator's own drop-in.
        self.assertNotIn("*volume-sync.conf", installer)

    def test_receiver_sudoers_authorizes_only_the_four_receiver_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sudoers = Path(directory) / "sudoers.d"
            sudoers.mkdir()
            tools = Path(directory) / "bin"
            tools.mkdir()
            systemctl = tools / "systemctl"
            visudo = tools / "visudo"
            for tool in (systemctl, visudo):
                tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                tool.chmod(0o755)

            self._run(
                "sudo() { command \"$@\"; }\ninstall_receiver_sudoers\ninstall_remote_sudoers",
                env={
                    "HOME": directory,
                    "CDSP_AUTOMATION_BASE_DIR": directory,
                    "CDSP_AUTOMATION_SUDOERS_DIR": str(sudoers),
                    "CDSP_AUTOMATION_SYSTEMCTL_BIN": str(systemctl),
                    "CDSP_AUTOMATION_VISUDO_BIN": str(visudo),
                },
            )
            receivers = (sudoers / "cdsp-automation-receivers").read_text(
                encoding="utf-8"
            )
            remote = (sudoers / "cdsp-automation").read_text(encoding="utf-8")
            rule = receivers.strip().splitlines()[-1]
            commands = [
                item.strip()
                for item in rule.split("NOPASSWD:", 1)[1].split(",")
            ]
            self.assertEqual(
                commands,
                [
                    f"{systemctl} start shairport-sync.service",
                    f"{systemctl} stop shairport-sync.service",
                    f"{systemctl} start raspotify.service",
                    f"{systemctl} stop raspotify.service",
                ],
            )
            self.assertNotIn("poweroff", receivers)
            # The four move out of the remote file rather than being duplicated.
            self.assertNotIn("shairport-sync.service", remote)
            self.assertNotIn("raspotify.service", remote)
            self.assertIn("poweroff", remote)
            self.assertIn("restart camilladsp.service", remote)

    def test_airplay_install_authorizes_receivers_before_starting_the_bridge(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "order.log"
            self._run(
                "\n".join(
                    [
                        "sudo() { :; }",
                        f'install_receiver_sudoers() {{ printf "sudoers\\n" >> {log!s}; }}',
                        f'create_unit() {{ printf "unit\\n" >> {log!s}; }}',
                        f'configure_shairport_bridge() {{ printf "shairport\\n" >> {log!s}; }}',
                        "install_airplay_volume_bridge",
                    ]
                ),
                env={"HOME": directory, "CDSP_AUTOMATION_BASE_DIR": directory},
            )
            self.assertEqual(
                log.read_text(encoding="utf-8").split(),
                ["sudoers", "unit", "shairport"],
            )

    def test_refresh_writes_receiver_authorization_before_narrowing_the_remote_file(
        self,
    ) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        refresh = installer.split("refresh_installed_units()", 1)[1].split(
            "show_status()", 1
        )[0]
        self.assertLess(
            refresh.index("install_receiver_sudoers"),
            refresh.index("install_remote_sudoers"),
        )

    def test_bridge_unit_carries_the_volume_sync_supplementary_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "site"
            unit_dir = root / "units"
            base.mkdir()
            unit_dir.mkdir()
            (base / "cdsp-automation.env").write_text(
                "VOLUME_SYNC_GROUP=studio\n", encoding="utf-8"
            )
            body = "\n".join(
                [
                    "systemctl() { :; }",
                    'sudo() { if [[ "$1" == install ]]; then command "$@"; fi; }',
                    "getent() { [[ \"$2\" == studio ]]; }",
                    "create_unit 'AirPlay Volume Bridge' airplay_volume_bridge.py airplay-volume-bridge --daemon",
                ]
            )
            environment = {
                "HOME": str(root),
                "CDSP_AUTOMATION_BASE_DIR": str(base),
                "CDSP_AUTOMATION_SYSTEMD_UNIT_DIR": str(unit_dir),
                "CDSP_AUTOMATION_LEGACY_UNIT_DIR": str(root / "legacy"),
            }
            self._run(body, env=environment)
            unit = (unit_dir / "airplay-volume-bridge.service").read_text(
                encoding="utf-8"
            )
            self.assertIn("SupplementaryGroups=studio", unit)

            missing = self._run(
                body.replace('[[ "$2" == studio ]]', "false"), env=environment
            )
            unit = (unit_dir / "airplay-volume-bridge.service").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("SupplementaryGroups", unit)
            self.assertIn("group 'studio' does not exist", missing)

    def test_uninstall_removes_both_sudoers_files_and_sweeps_unit_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit_dir = root / "units"
            legacy_dir = root / "legacy-units"
            sudoers = root / "sudoers.d"
            for path in (unit_dir, legacy_dir, sudoers):
                path.mkdir()
            units = [f"{name}.service" for name in
                     ("cdsp-trigger", "cdsp-motu-sync", "cdsp-source-switcher",
                      "cdsp-remote", "airplay-volume-bridge", "cdsp-control-ui")]
            for name in units:
                (unit_dir / name).write_text("[Unit]\n", encoding="utf-8")
                (legacy_dir / name).write_text("[Unit]\n", encoding="utf-8")
            (sudoers / "cdsp-automation").write_text("rule\n", encoding="utf-8")
            (sudoers / "cdsp-automation-receivers").write_text("rule\n", encoding="utf-8")
            (unit_dir / "unrelated.service").write_text("[Unit]\n", encoding="utf-8")

            self._run(
                "\n".join(
                    [
                        "systemctl() { :; }",
                        'sudo() { if [[ "$1" == rm ]]; then command "$@"; fi; }',
                        "SCRIPTS_DIR=/nonexistent",
                        "uninstall_all",
                    ]
                ),
                env={
                    "HOME": str(root),
                    "CDSP_AUTOMATION_BASE_DIR": str(root / "site"),
                    "CDSP_AUTOMATION_SYSTEMD_UNIT_DIR": str(unit_dir),
                    "CDSP_AUTOMATION_LEGACY_UNIT_DIR": str(legacy_dir),
                    "CDSP_AUTOMATION_SUDOERS_DIR": str(sudoers),
                },
            )
            for name in units:
                self.assertFalse((unit_dir / name).exists(), name)
                self.assertFalse((legacy_dir / name).exists(), name)
            self.assertTrue((unit_dir / "unrelated.service").exists())
            self.assertFalse((sudoers / "cdsp-automation").exists())
            self.assertFalse((sudoers / "cdsp-automation-receivers").exists())

    def test_unit_search_dirs_deduplicates_configured_and_default_paths(self) -> None:
        output = self._run(
            "unit_search_dirs",
            env={
                "CDSP_AUTOMATION_SYSTEMD_UNIT_DIR": "/etc/systemd/system",
                "CDSP_AUTOMATION_LEGACY_UNIT_DIR": "/lib/systemd/system",
            },
        )
        self.assertEqual(
            output.split(), ["/etc/systemd/system", "/lib/systemd/system"]
        )

    def test_default_env_publishes_the_backup_directory_and_site_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = self._run(
                "default_env",
                env={"HOME": directory, "CDSP_AUTOMATION_BASE_DIR": f"{directory}/site"},
            )
            rendered = output.splitlines()
            self.assertIn(
                "AUDIO_EQ_BACKUP_DIR=/var/lib/cdsp-automation/audio-eq-backups",
                rendered,
            )
            self.assertIn("SITE_NAME=CamillaDSP", rendered)
            self.assertIn("SPOTIFY_ALSA_DEVICE=", rendered)
            self.assertIn(
                "SPOTIFY_VOLUME_COMMAND_SOCKET_PATH=/run/raspotify/cdsp-volume.sock",
                rendered,
            )

    def test_backup_migration_copies_without_clobbering_and_keeps_the_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "site"
            base.mkdir()
            destination = root / "audio-eq-backups"
            destination.mkdir()
            source = root / "old-backups"
            source.mkdir()
            for name, payload in (
                ("audio-eq-kantarellen-1.json", "one"),
                ("audio-eq-partymeh-2.json", "two"),
                ("audio-eq-kantarellen-3.json", "stale"),
            ):
                (source / name).write_text(payload, encoding="utf-8")
            (destination / "audio-eq-kantarellen-3.json").write_text(
                "newer", encoding="utf-8"
            )
            (base / "cdsp-automation.env").write_text(
                f"AUDIO_EQ_BACKUP_DIR={destination}\n", encoding="utf-8"
            )

            output = self._run(
                "\n".join(
                    [
                        f'AUDIO_EQ_BACKUP_DEFAULT={destination!s}',
                        f'LEGACY_AUDIO_EQ_BACKUP_DIR={source!s}',
                        'sudo() { local a=(); shift; while [[ $# -gt 0 ]]; do case "$1" in -o|-g) shift 2 ;; *) a+=("$1"); shift ;; esac; done; command install "${a[@]}"; }',
                        "migrate_audio_eq_backups",
                    ]
                ),
                env={"HOME": str(root), "CDSP_AUTOMATION_BASE_DIR": str(base)},
            )
            self.assertEqual(
                (destination / "audio-eq-kantarellen-1.json").read_text(
                    encoding="utf-8"
                ),
                "one",
            )
            self.assertEqual(
                (destination / "audio-eq-partymeh-2.json").read_text(encoding="utf-8"),
                "two",
            )
            # An existing destination file always wins, and nothing is moved.
            self.assertEqual(
                (destination / "audio-eq-kantarellen-3.json").read_text(
                    encoding="utf-8"
                ),
                "newer",
            )
            self.assertEqual(len(list(source.glob("*.json"))), 3)
            self.assertIn("Copied 2 EQ backup snapshot(s)", output)
            self.assertIn("left untouched", output)

    def test_backup_migration_respects_a_custom_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "site"
            base.mkdir()
            destination = root / "elsewhere"
            destination.mkdir()
            source = root / "old-backups"
            source.mkdir()
            (source / "audio-eq-default-1.json").write_text("x", encoding="utf-8")
            (base / "cdsp-automation.env").write_text(
                f"AUDIO_EQ_BACKUP_DIR={destination}\n", encoding="utf-8"
            )
            self._run(
                "\n".join(
                    [
                        f'LEGACY_AUDIO_EQ_BACKUP_DIR={source!s}',
                        "sudo() { command \"$@\"; }",
                        "migrate_audio_eq_backups",
                    ]
                ),
                env={"HOME": str(root), "CDSP_AUTOMATION_BASE_DIR": str(base)},
            )
            self.assertEqual(list(destination.glob("*.json")), [])

    def test_menu_lists_uninstall_at_eleven_and_the_ui_at_twelve(self) -> None:
        output = self._run("print_menu")
        self.assertIn("11) Uninstall All Utilities", output)
        self.assertIn("12) Install Web Control UI (optional)", output)
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        technical = (REPOSITORY / "TECHNICAL.md").read_text(encoding="utf-8")
        self.assertIn("11. **Uninstall All Utilities**", readme)
        self.assertIn("12. **Install Web Control UI**", readme)
        self.assertIn("menu option 12", technical)

    def test_destructive_and_exposing_options_require_an_explicit_yes(self) -> None:
        for answer, expected in (("y\n", "ACTED"), ("n\n", "Cancelled"), ("", "Cancelled")):
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"set -euo pipefail\nsource {INSTALLER!s}\n"
                    'if confirm_action "Do the thing?"; then echo ACTED; else echo Cancelled; fi',
                ],
                input=answer,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
                check=False,
            )
            # End of input must default to No without aborting under set -e.
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(expected, result.stdout)

    def test_menu_dispatch_gates_uninstall_and_the_root_web_server(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        dispatch = installer.split("read -r -p \"Enter your choice: \"", 1)[1]
        self.assertIn(
            '11) if confirm_action "Remove all CamillaDSP utility services, units and sudoers rules?"; then uninstall_all;',
            dispatch,
        )
        self.assertIn(
            '12) if confirm_action "Install an unauthenticated root web server on 0.0.0.0:8088?"; then prepare_install; install_control_ui;',
            dispatch,
        )

    def test_shairport_failure_explains_before_restoring_and_names_the_backup(
        self,
    ) -> None:
        """One ordered stream, because the ordering is the whole fix here."""
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "shairport-sync.conf"
            config.write_text("general =\n{\n};\n", encoding="utf-8")
            body = "\n".join(
                [
                    "shairport-sync() { :; }",
                    "sudo() {",
                    '  if [[ "$*" == *displayConfig* ]]; then echo "EVENT validate" >&2; return 1; fi',
                    '  if [[ "$*" == *--remove* ]]; then echo "EVENT restore" >&2; return $RESTORE_STATUS; fi',
                    '  echo "EVENT other" >&2',
                    "  return 0",
                    "}",
                    "status=0",
                    "configure_shairport_bridge || status=$?",
                    'echo "status=$status"',
                ]
            )
            environment = {
                "HOME": directory,
                "CDSP_AUTOMATION_BASE_DIR": directory,
                "CDSP_AUTOMATION_SHAIRPORT_CONFIG": str(config),
                "RESTORE_STATUS": "1",
            }
            output = self._run(body, env=environment)
            events = [
                "EVENT"
                if line.startswith("EVENT other")
                else line.split(" ", 1)[1]
                if line.startswith("EVENT")
                else "explained"
                if "restoring its previous volume settings" in line
                else "restore-failed"
                if line.startswith("Automatic restore failed")
                else ""
                for line in output.splitlines()
            ]
            # The reason is printed before the restore is even attempted, and
            # the failure of the restore is reported separately afterwards.
            self.assertEqual(
                [event for event in events if event],
                ["EVENT", "validate", "explained", "restore", "restore-failed"],
                output,
            )
            self.assertIn(
                f"{config}.pre-airplay-volume-bridge holds the pre-install file", output
            )
            self.assertIn("status=1", output)

            # A restore that succeeds must not claim a failure.
            environment["RESTORE_STATUS"] = "0"
            recovered = self._run(body, env=environment)
            self.assertIn("Shairport rejected the managed configuration", recovered)
            self.assertNotIn("Automatic restore failed", recovered)
            self.assertIn("status=1", recovered)

    def test_repository_never_names_a_single_deployment(self) -> None:
        """The installer is public; no one deployment's name may appear in it.

        Split on purpose: this file is scanned too, so a contiguous literal
        would make the assertion fail against its own source.  The baseline it
        replaced was 72 matching lines carrying 75 occurrences across 15 files.
        """
        token = "ug" "lan"
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        self.assertGreater(len(tracked), 30)
        offenders = [
            name
            for name in tracked
            if token
            in (REPOSITORY / name)
            .read_text(encoding="utf-8", errors="ignore")
            .lower()
        ]
        self.assertEqual(offenders, [])

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

    def test_update_audits_operator_volume_limits_before_restarting(self) -> None:
        """The operator learns which configs need a cap before services bounce."""
        source = INSTALLER.read_text(encoding="utf-8")
        body = source.split("update_utilities()", 1)[1].split("\n}", 1)[0]
        self.assertIn("audit_operator_volume_limits", body)
        self.assertLess(
            body.index("audit_operator_volume_limits"), body.index("restart_all")
        )
        # The audit is advisory: a finding must not abort the update.
        audit = source.split("audit_operator_volume_limits() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("note_skip", audit)


if __name__ == "__main__":
    unittest.main()

