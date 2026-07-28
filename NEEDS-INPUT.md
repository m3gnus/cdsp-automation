# Needs input

Items an automated review pass found but deliberately did **not** implement,
because each changed behavior, removed/added a capability, or was a genuine
design choice for the owner. Each was verified by reading (and where noted,
running) the code — none was speculative.

Two independent reviewers (Codex and Opus) proposed a fix for each item, a
third pass critiqued those designs, and the owner ruled on the open points.
The condensed recommendations are kept under every item for provenance, with
what was finally implemented recorded in the `Status:` line. **Every item below
is now done; nothing here is awaiting input.**

- [x] **"Install All Utilities" built the ISO 226 engine first and aborted the
      whole run on any Pi where CamillaDSP is not already installed at
      `/usr/local/bin/camilladsp`.** Option 1 ordered `install_iso226_engine`
      before the trigger, MOTU sync, switcher, remote and AirPlay steps.
      `scripts/build_camilladsp_iso226.sh` requires
      `systemctl restart camilladsp.service` to succeed *and*
      `readlink -f /proc/<pid>/exe` to equal `/usr/local/bin/camilladsp`;
      otherwise it rolls back and exits 1, and because `install.sh` runs under
      `set -e` with unguarded case-lists the entire installer exited — after a
      full Rust compile, with nothing else installed. Neither README.md nor
      TECHNICAL.md stated that CamillaDSP must live at that exact path.

      **Codex recommended — OWNER-TASTE.** Move the step to the end of option 1
      *and* add a preflight that inspects `camilladsp.service`'s `MainPID` and
      requires `/proc/<pid>/exe` to resolve to `/usr/local/bin/camilladsp`
      before cloning or compiling; option 1 then visibly `SKIPPED`s an
      incompatible engine while explicit option 10 still fails immediately.

      **Opus recommended — OBVIOUS.** Reorder to last and make the failure
      non-fatal in the *bundled* paths only; option 10 keeps failing hard.
      Objected to parsing `systemctl show -p ExecStart`, because that takes a
      dependency on the `{ path=… ; argv[]=… }` record format and a false
      negative would newly block a rebuild that works on the deployed Pi today.
      Rated the update path the sharper hazard: the unguarded call in
      `update_utilities` aborted a routine update before
      `refresh_installed_units` and `restart_all`, leaving new scripts that no
      running unit had picked up.

      Status: done — implemented: `engine_preflight`
      (`scripts/build_camilladsp_iso226.sh:29`) resolves the running engine
      through `/proc/<pid>/exe` and, only when the unit is stopped, the last
      `ExecStart=` line of `systemctl cat` (leading whitespace tolerated, every
      systemd command prefix stripped, never systemd's own record format); it
      runs on `--preflight` *and* at the top of an ordinary build, before the
      toolchain check and any clone, and reserves status 3 for "this engine
      cannot be replaced here". `install_all` (`install.sh:822`) runs the engine
      last through `install_iso226_engine_optional` (`:701`), which turns status
      3 into a `SKIPPED` summary line and any other failure into `FAILED`;
      option 10 still fails hard and prints the reason. `update_utilities`
      (`:845`) no longer rebuilds the engine at all — a routine update must
      never replace or restart the live CamillaDSP binary — and says so,
      pointing at option 10. Summary state is scoped per menu action
      (`reset_install_notes` / `print_install_summary`), so a note from one
      action cannot appear in the next one's summary. README gained the path
      prerequisite.

- [x] **Spotify volume sync hard-required a `raspotify.service` and a
      site-specific ALSA PCM, neither of which this repo installs, creates or
      documents.** `scripts/build_librespot_volume_sync.sh` restarted
      `raspotify.service` unconditionally (missing unit → non-zero → rollback →
      installer exits) and wrote a hard-coded `--device <site PCM>` into the
      drop-in. The post-install health check only verified the unit was active
      and the socket existed, so on a machine without that PCM the install could
      report success while Spotify playback was silently dead. One deployment's
      name was also compiled into the librespot patch's env-var names, the
      receiver binary, the raspotify drop-in, the command socket, the Shairport
      config markers and the EQ filter prefix.

      **Codex recommended — OWNER-TASTE.** Skip with a prominent warning when
      `raspotify.service` is absent, still completing the AirPlay half. Replace
      the hard-coded PCM with `SPOTIFY_ALSA_DEVICE` in the env file, migrated
      out of an existing drop-in on upgrade and otherwise prompted for once.
      Then **require** the configured PCM to appear in `aplay -L` before
      compiling, and print an explicit `Spotify volume sync: SKIPPED` line with
      a remediation command.

      **Opus recommended — OBVIOUS (as scoped).** Same skip, written as an early
      return using the `systemctl list-unit-files` idiom already in the file and
      mirroring the Shairport precedent; same `SPOTIFY_ALSA_DEVICE` key. Differs
      on the PCM probe: it must be a **warning, not a gate** — whether a custom
      `pcm.*` in `asound.conf` is enumerated by `aplay -L` cannot be settled
      off-Pi, so a false negative must cost a log line, never a blocked install.
      Notes the health check structurally cannot catch a missing PCM (librespot
      opens ALSA lazily at first playback).

      Status: done — implemented: the site name is gone from the repository, and
      a guard test (`tests/test_installer.py`) fails the suite if it returns.
      Compatibility with what earlier releases wrote is by **exact** match on
      names assembled from fragments, never by shape, so operator-owned
      drop-ins, sockets, binaries, markers and filters are never claimed.
      Renames: patch env vars → `CDSP_SPOTIFY_VOLUME_{,ACK_}SOCKET`; receiver →
      `/usr/local/bin/librespot-cdsp`; drop-in → `cdsp-volume-sync.conf`;
      command socket → `/run/raspotify/cdsp-volume.sock`; Shairport markers →
      `// CDSP-*` (both spellings recognized, the `original-base64` payload
      preserved, created blocks replaced rather than duplicated); EQ filters →
      `cdsp_ui_eq_*` with the previous prefixes stripped by the same overlay
      pass the switcher already runs, so no installer step ever writes
      CamillaDSP's config. `install_spotify_volume_sync` (`install.sh:621`)
      returns early with a `SKIPPED` note when `raspotify.service` is absent.
      `SPOTIFY_ALSA_DEVICE` is published empty, meaning "leave raspotify's own
      device configuration in force"; `render_dropin`
      (`build_librespot_volume_sync.sh:59`) emits `--device` only when it is
      set, validates it, the socket and the group against allowlists, and
      `aplay -L` only warns. The installer never touches CamillaDSP's live
      config and never restarts `camilladsp.service`: on an already-deployed
      machine the EQ-prefix cutover happens at the next `cdsp-source-switcher`
      restart, which mutes, resolves, reloads and regenerates non-default
      configs exactly as it does on any boot. Run that restart in an idle
      window; the update itself costs one `shairport-sync` restart and one
      `raspotify` restart. `migrate_env_defaults` (`install.sh:219`) rewrites
      the socket and drop-in keys only when they equal the exact historical
      defaults and lifts `--device` out of this installer's own former drop-in;
      custom values survive untouched. The builder is a single transaction: the
      superseded drop-in is removed before `daemon-reload` (its filename sorts
      later and would otherwise win), its binary and socket only after both
      raspotify and the bridge pass health checks, and rollback restores the old
      drop-in while the binary it names still exists. A digest marker
      (`marker_matches` / `source_digest`, `:96`) skips the Rust rebuild when
      the patch and pinned commit are unchanged; unknown or older marker content
      forces exactly one rebuild and is then rewritten in the current format.

- [x] **The control UI runs as root and could permanently lock the user-run
      daemons out of the shared lock files.** `install_control_ui` wrote a unit
      with no `User=`, so the UI took `audio_control_lock` / `audio_state_lock`
      / `speaker_selection_lock` as root. `exclusive_file_lock`
      (`scripts/audio_eq.py`) opens the lock with `O_RDWR|O_CREAT` and never
      chowned it afterwards, so whichever process touched it first owned it. On
      a fresh install where the UI won the race, `cdsp-source-switcher`,
      `cdsp-remote` and the AirPlay bridge got EACCES on every volume and EQ
      operation, permanently.

      *Provenance (folded in from the audit): `cf1c60d` is what created this
      item. Its message — "lock files are created lazily by `audio_state_lock`
      so pre-creating four hardcoded ones is pointless" — is wrong: the deleted
      loop covered **seven** paths, and only four were the stale speaker-name
      locks that the site-replaceable catalog made obsolete. The other three —
      `${audio_eq_path}.lock`, `$audio_control_lock_path` and
      `${speaker_selection_path}.lock` — also ran `sudo chown "$USER:$USER"` and
      `sudo chmod 0660`, which lazy creation does **not** reproduce:
      `exclusive_file_lock` only passed a mode to `os.open`, so it was subject
      to umask and never set ownership. Deleting the four was right; deleting
      the `chown` was not.*

      **Codex recommended — OBVIOUS.** Keep the UI root — its service, clock and
      storage endpoints require it. Make ownership deterministic instead:
      pre-create the control, audio-state and speaker-selection locks as
      `INSTALL_USER`, and run the root UI with the installer user's primary
      `Group=` plus `UMask=0007`. Change the `0644` lock modes to `0660`.

      **Opus recommended — OBVIOUS.** Neither listed alternative: fix it at the
      primitive. `os.fchmod` the descriptor inside `exclusive_file_lock` so
      whichever uid wins the race leaves the lock openable by everyone else.
      Installer pre-creation alone is **incomplete**: `speaker-audio/<id>.json.lock`
      is created lazily by whoever first touches a speaker, a set the installer
      cannot enumerate. Warns never to `install`/replace an existing lock file —
      swapping a live lock's inode lets two holders believe they own it.
      Rejects `User=` outright.

      Status: done — implemented: both mechanisms combined —
      `exclusive_file_lock` now `os.fchmod`s the descriptor to a shared
      `LOCK_FILE_MODE = 0o660` (defeats systemd's 022 umask, covers lazily
      created per-speaker locks), the root UI unit gains
      `Group=$INSTALL_GROUP` + `UMask=0007` (no `User=`, so root is kept), and
      `ensure_audio_state_storage` again claims the three static locks for
      `$INSTALL_USER` (the root Shairport callback also creates
      `audio-control.lock`, and no `Group=` can be added to a third-party unit).

- [x] **The AirPlay bridge's sudoers authorization was installed only by the
      *remote* menu step.** `install_remote_sudoers` was called from
      `install_remote` and from the cdsp-remote branch of
      `refresh_installed_units`, never from `install_airplay_volume_bridge`. But
      `scripts/airplay_volume_bridge.py` runs
      `sudo -n systemctl start|stop shairport-sync.service|raspotify.service`
      to arbitrate receivers, which only `/etc/sudoers.d/cdsp-automation`
      permitted. Installing option 9 alone therefore yielded a bridge that could
      not hand off between receivers. Separately, the installer added the user to
      `input` but never to `audio`, while `secure_socket` chowned its socket to
      `VOLUME_SYNC_GROUP` (default `audio`) and died with `PermissionError` if
      the user was not already a member — masked on Raspberry Pi OS because the
      first user is in `audio` by default.

      **Codex recommended — OBVIOUS.** Give the bridge its own narrowly scoped
      sudoers file holding only the four exact receiver start/stop commands
      `set_receiver_service` uses, leaving remote restart/poweroff in a separate
      remote-only file. For the group, grant the bridge service
      `SupplementaryGroups=<VOLUME_SYNC_GROUP>` rather than adding the login
      user to `audio` globally. Generate and `visudo`-validate the new files and
      install them before removing the legacy one.

      **Opus recommended — OWNER-TASTE.** Same split, but grant the group with
      `getent group audio && usermod -aG audio`, arguing librespot's own
      drop-in `Group=audio` requires the membership anyway. Rates the `audio`
      gap the sharper bug: `secure_socket` runs outside `run_daemon`'s
      `try/except`, so a non-member — or a distro with no `audio` group —
      crash-loops under `Restart=always`.

      Status: done — implemented: Codex's split and service-scoped group, plus
      Opus's fail-soft primitive. `install_receiver_sudoers` (`install.sh:528`)
      writes `/etc/sudoers.d/cdsp-automation-receivers` with exactly the four
      receiver commands, `visudo`-validated; those four are gone from
      `install_remote_sudoers` (`:508`). It runs **before** `create_unit` starts
      the bridge on a fresh install, and before the remote branch narrows the
      remote file on refresh, so no window exists with neither file
      authoritative. `create_unit` adds `SupplementaryGroups=$VOLUME_SYNC_GROUP`
      when the group exists and prints a loud installer warning when it does not
      (the Spotify drop-in would otherwise name a missing group). `usermod -aG
      audio` is not used. `secure_socket`
      (`scripts/airplay_volume_bridge.py:542`) now chmods 0660 first and logs a
      degradation instead of raising, naming both receivers' callbacks as
      affected rather than only Spotify's acknowledgement path. Uninstall
      removes both sudoers files.

- [x] **`AUDIO_EQ_BACKUP_DIR` still defaulted into the retired
      `/var/lib/installation` tree.** `scripts/web_ui.py` was the only surviving
      reference to that path anywhere in the repo; everything else had moved to
      `/var/lib/cdsp-automation`. The key was not written by `default_env()` and
      the directory was not prepared by `ensure_audio_state_storage`, so on a
      deployment where the UI could not create it the rolling backup safety net
      for EQ state silently did nothing (`_backup_file` swallows `OSError`).

      **Codex recommended — OWNER-TASTE.** Move the default to
      `/var/lib/cdsp-automation/audio-eq-backups`, add `AUDIO_EQ_BACKUP_DIR` to
      `default_env()`, and prepare it in `ensure_audio_state_storage`. Then
      migrate: briefly quiesce the control UI (not the audio services), copy the
      old directory across with no-clobber semantics, verify, and retain the
      source until an owner-confirmed cleanup.

      **Opus recommended — OBVIOUS.** Same move, same `default_env()` key, same
      `ensure_user_writable_dir` call — but **no migration code**: one installer
      line pointing at the old directory if it still exists. Corrects the item's
      premise: because the UI is root, `mkdir` succeeds today, so the safety net
      is not dead — it is writing into a retired tree.

      Status: done — implemented: the move, the published key and the prepared
      directory, plus Codex's copy. `migrate_audio_eq_backups`
      (`install.sh:392`) runs from the control-UI branch of
      `refresh_installed_units`, i.e. **after** the UI has restarted on the new
      environment and can no longer write the old directory. It copies `*.json`
      with no-clobber semantics and normalized ownership, retains the source and
      prints where the backups now live; it does nothing at all when the
      configured destination is not the new default, so a custom path is
      respected. The "at most 15 disposable snapshots" framing was wrong:
      retention is 15 *per selected speaker*, so a multi-profile deployment can
      hold considerably more, and they are the UI's only undo for persisted
      audio state.

- [x] **`/api/status` spawned nine `systemctl show` subprocesses per poll.**
      `service_status` called `systemctl_show` once per entry in
      `SERVICE_CATALOG`, and the browser polls `/api/status` every 5 s per open
      page. `systemctl show` accepts several units in one invocation and emits
      blank-line-separated stanzas, so this could collapse to a single
      subprocess, but the parser would then depend on that output format and on
      argument ordering, which could not be verified from the development host.

      **Codex recommended — OWNER-TASTE.** One batched call that also requests
      `Id`, with blank-line-separated stanzas parsed into a map keyed by `Id`
      and never by argument order. Accept the batch only if every requested unit
      appears exactly once — reject duplicates, missing units and unexpected
      IDs — and otherwise fall back to today's one-unit-at-a-time path.

      **Opus recommended — OWNER-TASTE.** Same batching, marginally different
      parser: start a new record at every repeated property, which drops the
      blank-line-separator dependency as well as the ordering one, and fall back
      **per missing unit** instead of per poll.

      Status: done — implemented: Codex's strict stanza parser, with Opus's
      per-unit fallback. The repeated-property rule was rejected on review:
      without `--all` systemd may suppress an empty property, and a record
      missing one would absorb the next record's leading lines.
      `parse_systemctl_show` (`scripts/web_ui.py:1369`) splits on the documented
      blank-line separator and is order-independent within a stanza;
      `systemctl_show_many` (`:1392`) requests `Id,Names,…` with `--all`, maps
      every requested unit through both `Id` **and** the whitespace-separated
      `Names` aliases (`Id` is only the primary name), and drops records whose
      name is claimed twice. `service_status` (`:1428`) makes one call and falls
      back per missing unit to the untouched single-unit `systemctl_show`, which
      stays an independent implementation rather than a wrapper that would
      repeat the alias failure.

## Audit findings

Found by a later audit of `8f54548..HEAD` against the commit messages. The
`cf1c60d` lock-claim finding that used to head this list has been folded into
item 3, where it belongs.

- [x] **Menu option 11 changed meaning: it was "Uninstall All Utilities" and
      became "Install Web Control UI".** The menu and its dispatch renumbered
      uninstall to 12 when the optional UI was added. A returning operator
      typing `11` from habit would have installed an unauthenticated root web
      server on `0.0.0.0:8088`. Unlike `install_motu_sync` and `install_remote`,
      `install_control_ui` printed its warning banner but had no `read -r -p`
      confirmation.

      Status: done — implemented: both remedies, because either alone is unsafe
      in one direction. Uninstall is back at **11** and the UI moved to **12**,
      and `confirm_action` (`install.sh:923`) puts a `y/N` gate on both, naming
      the exact exposure. It defaults to No and survives end-of-input, which a
      bare `read` would not under `set -e`. README's numbered list and
      TECHNICAL.md's "installed by menu option" reference moved with it.

- [x] **Uninstall ignored `$SYSTEMD_UNIT_DIR`.** Units are installed to
      `${SYSTEMD_UNIT_DIR}` (overridable via `CDSP_AUTOMATION_SYSTEMD_UNIT_DIR`)
      but were removed from a hardcoded path, so a non-default install orphaned
      every unit — including the control-UI unit. `LEGACY_UNIT_DIR` was likewise
      unused at the removal site.

      Status: done — implemented: `unit_search_dirs` (`install.sh:768`) yields
      the deduplicated union of the configured directories and the historical
      defaults, with fully quoted expansions, and `remove_unit_file` (`:786`)
      sweeps all of them for each managed unit and for the control-UI unit. An
      operator may have installed under one and be uninstalling under another,
      and `rm -f` on a path that holds no unit costs nothing.

- [x] **The two Shairport rollback branches aborted the installer without
      explaining why.** Both called `configure_shairport.py --remove` bare under
      `set -euo pipefail`, so a failure there killed the run *before* the
      `echo "…restored its previous volume settings." >&2` on the next line.
      `175ef17` fixed the root cause and guarded the uninstall call site, but
      `--remove` could still fail — e.g. a config with no active `general` block.

      Status: done — implemented: the reason is printed first and
      unconditionally, worded as an action in progress rather than a completed
      restore; the `--remove` call is a tested condition, so it can no longer
      take the installer down; and a failed restore is reported separately,
      naming the `.pre-airplay-volume-bridge` backup so the operator always has
      a recovery path. Both branches live in one `configure_shairport_bridge`
      (`install.sh:586`), which the update path now calls too — that is what
      rewrites a managed block written under this tool's earlier marker names.
