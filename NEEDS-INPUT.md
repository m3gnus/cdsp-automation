# Needs input

Items an automated review pass found but deliberately did **not** implement,
because each changes behavior, removes/adds a capability, or is a genuine
design choice for the owner. Each was verified by reading (and where noted,
running) the code — none is speculative. Tick an item once decided.

Two independent reviewers (Codex and Opus) then proposed a fix for each item.
Their condensed recommendations are recorded under every item, verdicts
included, along with the points where they disagree.

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

      **Codex recommends — OWNER-TASTE.** Move the step to the end of option 1
      *and* add a preflight that inspects `camilladsp.service`'s `MainPID` and
      requires `/proc/<pid>/exe` to resolve to `/usr/local/bin/camilladsp`
      before cloning or compiling; option 1 then visibly `SKIPPED`s an
      incompatible engine while explicit option 10 still fails immediately.
      Keep the builder's post-install verification as the authority, print a
      conspicuous skip summary, and restart managed services after a
      successful replacement. ~25-40 lines plus fake-`systemctl`/`readlink`
      tests. OWNER-TASTE because "may Install All omit an incompatible
      optional component" is product policy.

      **Opus recommends — OBVIOUS.** Reorder to last and make the failure
      non-fatal in the *bundled* paths only (`install_iso226_engine || echo …`
      in option 1 and in the `update_utilities` rebuild at `:557`); option 10
      keeps failing hard. Explicitly rejects Codex's ExecStart/`MainPID`
      preflight: parsing `systemctl show -p ExecStart` takes a dependency on
      the `{ path=… ; argv[]=… }` record format, and a false negative would
      newly block a rebuild that works on uglan today — use bare unit
      existence (`systemctl cat`) plus a README prerequisite instead. The
      decisive argument is uglan's own hazard: the unguarded call in
      `update_utilities` aborts a routine update before `refresh_installed_units`
      and `restart_all`, leaving new scripts that no running unit has picked up.

      Status: awaiting owner (Opus calls it OBVIOUS, Codex OWNER-TASTE; they
      disagree on whether to add a compile-time preflight at all).

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

      **Codex recommends — OWNER-TASTE.** Skip with a prominent warning when
      `raspotify.service` is absent, still completing the AirPlay half. Replace
      the hard-coded PCM with `SPOTIFY_ALSA_DEVICE` in the env file, migrated
      out of an existing drop-in on upgrade and otherwise prompted for once.
      Then **require** the configured PCM to appear in `aplay -L` before
      compiling, and print an explicit `Spotify volume sync: SKIPPED` line with
      a remediation command. ~35-50 lines across the installer, the builder and
      the README. OWNER-TASTE because an owner could reasonably want Install
      All to fail loudly when promised Spotify support is unavailable.

      **Opus recommends — OBVIOUS (as scoped).** Same skip, written as an early
      return using the `systemctl list-unit-files` idiom already in the file and
      mirroring the shairport precedent at `:399-402`; same `SPOTIFY_ALSA_DEVICE`
      key defaulting to `uglan_main`, so uglan's rendered drop-in stays
      byte-identical. Differs on the PCM probe: it must be a **warning, not a
      gate** — whether a custom `pcm.uglan_main` in `asound.conf` is enumerated
      by `aplay -L` cannot be settled off-Pi, so a false negative must cost a
      log line, never a blocked install. Notes the health check structurally
      cannot catch a missing PCM (librespot opens ALSA lazily at first
      playback). Turning the probe into a gate would itself be OWNER-TASTE.

      Status: awaiting owner (Opus calls it OBVIOUS as scoped, Codex
      OWNER-TASTE; the live disagreement is whether `aplay -L` blocks or warns).

- [x] **The control UI runs as root and can permanently lock the user-run
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

      *Provenance (folded in from the audit): `cf1c60d` is what created this
      item. Its message — "lock files are created lazily by `audio_state_lock`
      so pre-creating four hardcoded ones is pointless" — is wrong: the deleted
      loop covered **seven** paths, and only four were the stale speaker-name
      locks (`kantarellen`, `partymeh`, `measurement`, `partymeh_bird`) that the
      site-replaceable catalog made obsolete. The other three —
      `${audio_eq_path}.lock`, `$audio_control_lock_path` and
      `${speaker_selection_path}.lock` — also ran `sudo chown "$USER:$USER"` and
      `sudo chmod 0660`, which lazy creation does **not** reproduce:
      `exclusive_file_lock` only passed a mode to `os.open`, so it was subject
      to umask and never set ownership. Deleting the four was right; deleting
      the `chown` was not.*

      **Codex recommends — OBVIOUS.** Keep the UI root — its service, clock and
      storage endpoints require it. Make ownership deterministic instead:
      pre-create the control, audio-state and speaker-selection locks as
      `INSTALL_USER`, and run the root UI with the installer user's primary
      `Group=` plus `UMask=0007`. Change the `0644` lock modes to `0660` so
      lazily created per-speaker locks stay usable too. Derive the group with
      `id -gn` and validate it; test the rendered unit, the exact lock
      ownership/modes, and real two-process serialization.

      **Opus recommends — OBVIOUS.** Neither listed alternative: fix it at the
      primitive. `os.fchmod` the descriptor inside `exclusive_file_lock` (mode
      group/other-writable, `PermissionError` swallowed) so whichever uid wins
      the race leaves the lock openable by everyone else — no installer change,
      no unit change. Installer pre-creation alone is **incomplete**:
      `speaker-audio/<id>.json.lock` is created lazily by whoever first touches
      a speaker, a set the installer cannot enumerate. Warns never to
      `install`/replace an existing lock file — swapping a live lock's inode
      lets two holders believe they own it. Rejects `User=` outright.

      Status: done — implemented: both mechanisms combined —
      `exclusive_file_lock` now `os.fchmod`s the descriptor to a shared
      `LOCK_FILE_MODE = 0o660` (defeats systemd's 022 umask, covers lazily
      created per-speaker locks), the root UI unit gains
      `Group=$INSTALL_GROUP` + `UMask=0007` (no `User=`, so root is kept), and
      `ensure_audio_state_storage` again claims the three static locks for
      `$INSTALL_USER` (the root Shairport callback also creates
      `audio-control.lock`, and no `Group=` can be added to a third-party unit).

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

      **Codex recommends — OBVIOUS.** Give the bridge its own narrowly scoped
      sudoers file holding only the four exact receiver start/stop commands
      `set_receiver_service` uses, leaving remote restart/poweroff in a separate
      remote-only file. For the group, grant the bridge service
      `SupplementaryGroups=<VOLUME_SYNC_GROUP>` rather than adding the login
      user to `audio` globally. Generate and `visudo`-validate the new files and
      install them before removing the legacy one. OBVIOUS because installing a
      component without its authorization is not a viable alternative, and
      service-scoped group membership is narrower than changing the account.

      **Opus recommends — OWNER-TASTE.** Same split (`install_receiver_sudoers`
      → `/etc/sudoers.d/cdsp-automation-receivers`, four commands dropped from
      the remote file, both removed by uninstall), but grants the group with
      `getent group audio && usermod -aG audio`, arguing librespot's own
      drop-in `Group=audio` requires the membership anyway. Rates the `audio`
      gap the sharper bug: `secure_socket` runs outside `run_daemon`'s
      `try/except`, so a non-member — or a distro with no `audio` group —
      crash-loops under `Restart=always`. OWNER-TASTE because the fix moves
      privilege in both directions: it adds a group membership on fresh
      installs and *removes* four commands from already-deployed sudoers, a
      security-posture call for a public repo.

      Status: awaiting owner (Codex calls it OBVIOUS, Opus OWNER-TASTE; they
      disagree on `SupplementaryGroups=` on the unit vs. `usermod -aG audio`).

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

      **Codex recommends — OWNER-TASTE.** Move the default to
      `/var/lib/cdsp-automation/audio-eq-backups`, add `AUDIO_EQ_BACKUP_DIR` to
      `default_env()`, and prepare it in `ensure_audio_state_storage` — merely
      changing the Python fallback would not make fresh installs reliable, since
      `_backup_file` swallows directory failures. Then migrate: briefly quiesce
      the control UI (not the audio services), copy the old directory across
      with no-clobber semantics, verify, and retain the source until an
      owner-confirmed cleanup. OWNER-TASTE because keeping the old configured
      path is simpler and defensible.

      **Opus recommends — OBVIOUS.** Same move, same `default_env()` key, same
      `ensure_user_writable_dir` call — but **no migration code**: one installer
      line pointing at `/var/lib/installation/audio-eq-backups` if it still
      exists, leaving the owner to copy or delete at most 15 disposable
      snapshots by hand. Corrects the item's premise: because the UI is root,
      `mkdir` succeeds today, so the safety net is not dead — it is writing into
      a retired tree, and it only dies if the UI is ever de-rooted (item 3's
      rejected option). Rejects "add the key at the current path": that would
      enshrine `/var/lib/installation` in a public repo's default surface.

      Status: awaiting owner (Opus calls it OBVIOUS, Codex OWNER-TASTE; they
      disagree on copying the old backups vs. only pointing at them).

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

      **Codex recommends — OWNER-TASTE.** One batched call that also requests
      `Id`, with blank-line-separated stanzas parsed into a map keyed by `Id`
      and never by argument order. Accept the batch only if every requested unit
      appears exactly once — reject duplicates, missing units and unexpected
      IDs — and otherwise fall back to today's one-unit-at-a-time path for that
      whole poll rather than returning partial results. ~30-45 lines with
      fixtures for `not-found`, blank values, `=` in descriptions, aliases and
      malformed output. OWNER-TASTE: the fallback makes it safe, but the extra
      parser and tests are real complexity in a deliberately minimal UI.

      **Opus recommends — OWNER-TASTE.** Same batching, marginally different
      parser: start a new record at every `Id=` line, which drops the
      blank-line-separator dependency as well as the ordering one, and fall back
      **per missing unit** instead of per poll. Refactor into
      `systemctl_show_many()` with the single-unit function kept as a thin
      wrapper, and raise the batched timeout from 4 s to ~6 s. Notes the item's
      stated blocker no longer holds — a pure text-in/dict-out parser is
      verifiable off-Pi against a recorded transcript. Still OWNER-TASTE: pure
      performance, no correctness benefit, in a codebase that has consistently
      chosen less code.

      Status: awaiting owner (both consider it optional).

## Audit findings (unfixed)

Found by a later audit of `8f54548..HEAD` against the commit messages. Left
unfixed because each is either an owner decision or already tracked above. The
`cf1c60d` lock-claim finding that used to head this list has been folded into
item 3, where it belongs.

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
