# Needs input

Items an automated review pass found but deliberately did **not** implement,
because each changes behavior, removes/adds a capability, or is a genuine
design choice for the owner. Each was verified by reading (and where noted,
running) the code — none is speculative. Tick an item once decided.

- [ ] **"Install All Utilities" builds the ISO 226 engine first and aborts the
      whole run on any Pi where CamillaDSP is not already installed at
      `/usr/local/bin/camilladsp`.** `install.sh:641` orders
      `install_iso226_engine` before the trigger, MOTU sync, switcher, remote
      and AirPlay steps. `scripts/build_camilladsp_iso226.sh:80` requires
      `systemctl restart camilladsp.service` to succeed *and*
      `readlink -f /proc/<pid>/exe` to equal `/usr/local/bin/camilladsp`;
      otherwise it rolls back and exits 1, and because `install.sh` runs under
      `set -e` with unguarded case-lists the entire installer exits — after a
      full Rust compile, with nothing else installed. Neither README.md nor
      TECHNICAL.md states that CamillaDSP must live at that exact path.
      Decision needed: move `install_iso226_engine` to the end of option 1,
      pre-check the unit's ExecStart before compiling, or document the path
      requirement as a prerequisite. All three change installer behavior.

- [ ] **Spotify volume sync hard-requires a `raspotify.service` and an ALSA PCM
      named `uglan_main`, neither of which this repo installs, creates or
      documents.** `scripts/build_librespot_volume_sync.sh:104` restarts
      `raspotify.service` unconditionally (missing unit → non-zero → rollback →
      installer exits), and `:89` writes `ExecStart=$TARGET --device uglan_main`
      into the drop-in. The post-install health check at `:107` only verifies
      the unit is active and the socket exists, so on a machine without that PCM
      the install can report success while Spotify playback is silently dead.
      Decision needed: skip the step when raspotify is absent (mirroring the
      existing `[[ ! -f "$shairport_config" ]]` early return at
      `install.sh:397`) versus failing loudly, and whether the output device
      should become an env-file setting. Both alter installed behavior, and the
      device name is the last hard-coded site identity in the install path.

- [ ] **The control UI runs as root and can permanently lock the user-run
      daemons out of the shared lock files.** `install_control_ui`
      (`install.sh:423`) writes a unit with no `User=`, so the UI takes
      `audio_control_lock` / `audio_state_lock` / `speaker_selection_lock` as
      root. `exclusive_file_lock` (`scripts/audio_eq.py:305`) opens the lock
      with `O_RDWR|O_CREAT` and never chowns it afterwards, so whichever process
      touches it first owns it. On a fresh install where the UI wins the race,
      `cdsp-source-switcher`, `cdsp-remote` and the AirPlay bridge get EACCES on
      every volume and EQ operation, permanently. The deployed machine is not
      affected because those files predate the UI. Decision needed: pre-create
      the lock files in `ensure_audio_state_storage` (`install.sh:232`) with
      `INSTALL_USER` ownership, or give the UI unit a `User=` — the latter
      removes root privileges the UI currently relies on for `systemctl`,
      `date -s` and `umount`.

- [ ] **The AirPlay bridge's sudoers authorization is installed only by the
      *remote* menu step.** `install_remote_sudoers` is called from
      `install_remote` and from the cdsp-remote branch of
      `refresh_installed_units`, never from `install_airplay_volume_bridge`
      (`install.sh:392`). But `scripts/airplay_volume_bridge.py` runs
      `sudo -n systemctl start|stop shairport-sync.service|raspotify.service`
      to arbitrate receivers, which only `/etc/sudoers.d/cdsp-automation`
      permits. Installing option 9 alone therefore yields a bridge that cannot
      hand off between receivers. Separately, `install.sh:364` adds the user to
      `input` but never to `audio`, while `secure_socket`
      (`scripts/airplay_volume_bridge.py:540`) chowns its socket to
      `VOLUME_SYNC_GROUP` (default `audio`) and dies with `PermissionError` if
      the user is not already a member — masked on Raspberry Pi OS because the
      first user is in `audio` by default. Decision needed: both fixes widen
      what a single menu entry grants, so they are privilege-surface changes.

- [ ] **`AUDIO_EQ_BACKUP_DIR` still defaults into the retired
      `/var/lib/installation` tree.** `scripts/web_ui.py:80` is the only
      surviving reference to that path anywhere in the repo; everything else
      moved to `/var/lib/cdsp-automation`. The key is not written by
      `default_env()` and the directory is not prepared by
      `ensure_audio_state_storage`, so on a deployment where the UI cannot
      create it the rolling 15-backup safety net for EQ state silently does
      nothing (`_backup_file` swallows `OSError`). Decision needed: relocating
      the default under `/var/lib/cdsp-automation` orphans whatever backups
      already exist at the old path on the live machine, so the owner should
      choose between moving the default, adding the key to the installer at the
      current path, or migrating the existing directory.

- [ ] **`/api/status` spawns nine `systemctl show` subprocesses per poll.**
      `service_status` (`scripts/web_ui.py:1339`) calls `systemctl_show`
      (`:1316`) once per entry in `SERVICE_CATALOG`, and the browser polls
      `/api/status` every 5 s per open page. `systemctl show` accepts several
      units in one invocation and emits blank-line-separated stanzas, so this
      could collapse to a single subprocess, but the parser would then depend on
      that output format and on argument ordering, which cannot be verified from
      the development host. Decision needed: whether to take that dependency on
      a live audio system for the sizeable reduction in per-poll load, or leave
      the sequential calls as-is.

## Audit findings (unfixed)

Found by a later audit of `8f54548..HEAD` against the commit messages. Left
unfixed because each is either an owner decision or already tracked above.

- [ ] **`cf1c60d`'s lock claim is wrong, and it is what created item 3 above.**
      The message says "lock files are created lazily by `audio_state_lock` so
      pre-creating four hardcoded ones is pointless". The deleted loop covered
      **seven** paths, and only four were the stale speaker-name locks
      (`kantarellen`, `partymeh`, `measurement`, `partymeh_bird`) that the
      site-replaceable catalog made obsolete. The other three —
      `${audio_eq_path}.lock`, `$audio_control_lock_path` and
      `${speaker_selection_path}.lock` — also ran `sudo chown "$USER:$USER"`
      and `sudo chmod 0660`, which lazy creation does **not** reproduce:
      `exclusive_file_lock` (`scripts/audio_eq.py:305`) only passes a mode to
      `os.open`, so it is subject to umask and never sets ownership. Deleting
      the four was right; deleting the `chown` was not, and the `0o660` mode at
      `scripts/speaker_profiles.py:251` is now a fossil that buys nothing. The
      resulting root-vs-INSTALL_USER lock failure is item 3 above, so this entry
      only records the provenance — fixing it means deciding item 3.

- [ ] **Menu option 11 changed meaning: it was "Uninstall All Utilities" and is
      now "Install Web Control UI".** `install.sh:628-629` and the dispatch at
      `:651-652` renumbered uninstall to 12 when the optional UI was added. A
      returning operator typing `11` from habit now installs an unauthenticated
      root web server on `0.0.0.0:8088` (`install.sh:443-444`). Unlike
      `install_motu_sync` and `install_remote`, `install_control_ui` prints its
      warning banner (`:425-428`) but has no `read -r -p` confirmation.
      Decision needed: add a `y/N` gate, or keep uninstall at 11 and move the
      UI to 12 — both change the published menu contract.

- [ ] **Uninstall ignores `$SYSTEMD_UNIT_DIR`.** Units are installed to
      `${SYSTEMD_UNIT_DIR}` (`install.sh:307`, `:455`, overridable via
      `CDSP_AUTOMATION_SYSTEMD_UNIT_DIR` at `:23`) but removed from a hardcoded
      path (`install.sh:524`, `:539`), so a non-default install orphans every
      unit — including the control-UI unit this range added. `LEGACY_UNIT_DIR`
      (`:24`) is likewise unused at the removal site. Decision needed: whether
      uninstall should sweep only the configured dirs or both those and the
      historical defaults, since an operator may have installed under one and
      uninstalled under the other.

- [ ] **The two Shairport rollback branches still abort the installer without
      explaining why.** `install.sh:405` and `:410` call
      `configure_shairport.py --remove` bare under `set -euo pipefail`, so a
      failure there kills the run *before* the `echo "…restored its previous
      volume settings." >&2` on the next line. `175ef17` fixed the root cause
      (`--remove` no longer demands an `alsa` block) and guarded the uninstall
      call site with `|| true` (`:534`), but `--remove` can still fail — e.g. a
      config with no active `general` block. Decision needed: printing the
      reason before attempting the restore changes the message's meaning (the
      restore may not have happened), so the wording is the owner's call.
