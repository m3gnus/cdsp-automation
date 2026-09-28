#!/bin/bash
# CamillaDSP Utilities Setup Script
# Installs: Trigger Control, Source Switcher (which also drives the MOTU), and Remote Control
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  echo "Run this installer as your normal user, not with sudo."
  exit 1
fi

BASE_DIR="${CDSP_AUTOMATION_BASE_DIR:-$HOME/camilladsp}"
SCRIPTS_DIR="$BASE_DIR/scripts"
CONFIGS_DIR="$BASE_DIR/configs"
VENV_DIR="$BASE_DIR/.venv"
ENV_FILE="$BASE_DIR/cdsp-automation.env"
# The historical control-UI exposure.  Kept as the fallback everywhere so an
# existing deployment does not silently lose its UI on upgrade.
CONTROL_UI_HOST_DEFAULT="0.0.0.0"
CONTROL_UI_PORT_DEFAULT="8088"
BASE_URL="https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/scripts"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_USER="$(/usr/bin/id -un)"
if [[ ! "$INSTALL_USER" =~ ^[a-zA-Z0-9._-]+$ ]]; then
  echo "Could not determine a safe install username"
  exit 1
fi
# The root control UI runs with this group so the lock files it creates stay
# openable by the daemons, which run as $INSTALL_USER.
INSTALL_GROUP="$(/usr/bin/id -gn)"
if [[ ! "$INSTALL_GROUP" =~ ^[a-zA-Z0-9._-]+$ ]]; then
  echo "Could not determine a safe install group"
  exit 1
fi
SYSTEMD_UNIT_DIR="${CDSP_AUTOMATION_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${CDSP_AUTOMATION_SUDOERS_DIR:-/etc/sudoers.d}"
SYSTEMCTL_BIN="${CDSP_AUTOMATION_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
VISUDO_BIN="${CDSP_AUTOMATION_VISUDO_BIN:-/usr/sbin/visudo}"
# These land verbatim in a sudoers rule, where a space or comma would change
# which commands the rule authorizes.
for _tool in "$SYSTEMCTL_BIN" "$VISUDO_BIN"; do
  if [[ "$_tool" != /* || "$_tool" =~ [[:space:],\\] ]]; then
    echo "Configured system tool path cannot be used in a sudoers rule: $_tool"
    exit 1
  fi
done
unset _tool

SHAIRPORT_CONFIG="${CDSP_AUTOMATION_SHAIRPORT_CONFIG:-/etc/shairport-sync.conf}"
RASPOTIFY_DROPIN_DIR="${CDSP_AUTOMATION_RASPOTIFY_DROPIN_DIR:-/etc/systemd/system/raspotify.service.d}"
SPOTIFY_DROPIN_PATH="$RASPOTIFY_DROPIN_DIR/cdsp-volume-sync.conf"
# Fixed in scripts/settings.py too.
STATE_DIR="/var/lib/cdsp-automation"
SITE_CONFIG_DIR="/etc/cdsp-automation"

CDSP_SERVICES=(
  cdsp-trigger
  cdsp-source-switcher
  cdsp-remote
  airplay-volume-bridge
)

default_env() {
  cat <<EOF
# CamillaDSP automation settings.
# This file is preserved when scripts are updated.  Paths and timings are fixed
# in the scripts (see scripts/settings.py); only what varies per site is here.
CDSP_HOST=127.0.0.1
CDSP_PORT=1234
# The control UI runs as root, so it cannot derive this from \$HOME.
CDSP_CONFIG_DIR=$CONFIGS_DIR
SOURCE_OVERRIDE_PATH=/run/cdsp-source-switcher/manual_source
POWER_GPIO=4
# The MOTU's control WebSocket.  The source switcher is its only client.
MOTU_WS_URL=ws://169.254.51.193:1280
# MOTU main output level, set from the control UI.  It sits after CamillaDSP,
# so the profile volume limits do not bound it: this ceiling does.
MOTU_MAIN_VOLUME_MAX_DB=0
# Start each source at the CamillaDSP and MOTU levels it last played at
# (per speaker).  0 carries the volume over between sources instead.
SOURCE_VOLUME_MEMORY=1
AIRPLAY_VOLUME_MIN_DB=-50
AIRPLAY_VOLUME_MAX_DB=0
# Comma-separated LMS player names to stop when AirPlay or Spotify starts.
AIRPLAY_INTERRUPTED_LMS_PLAYERS=
# Empty leaves raspotify's own device configuration in force; set an ALSA PCM
# name here (see 'aplay -L') only to override it.
SPOTIFY_ALSA_DEVICE=
# Control UI bind address.  0.0.0.0 is every interface, which is what this
# component has always done, so an upgrade does not take the UI away from a
# working install.  Set 127.0.0.1 to reach it only from the Pi itself, over an
# SSH tunnel: ssh -N -L 8088:127.0.0.1:8088 $INSTALL_USER@<pi>
INSTALLATION_UI_HOST=$CONTROL_UI_HOST_DEFAULT
INSTALLATION_UI_PORT=$CONTROL_UI_PORT_DEFAULT
# Empty leaves the control UI unauthenticated, exactly as before.  Set a long
# random secret (openssl rand -hex 32) to require it on every state-changing
# request; then open the UI once as http://<pi>:8088/#token=<secret> and the
# page keeps it.  Read-only status pages stay open either way.
INSTALLATION_UI_TOKEN=
# Shown in the control UI's page title and header.
SITE_NAME=CamillaDSP
REMOTE_NAME=HID Remote01 Keyboard
EOF
}

ensure_env_file() {
  mkdir -p "$BASE_DIR" "$SCRIPTS_DIR" "$CONFIGS_DIR"
  if [[ ! -f "$ENV_FILE" ]]; then
    default_env > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    return
  fi

  local key value
  while IFS='=' read -r key value; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    if ! grep -q "^${key}=" "$ENV_FILE"; then
      printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
  done < <(default_env)
}

set_env_value() {
  local key="$1"
  local value="$2"
  local tmp line replaced=false
  ensure_env_file
  tmp="$(mktemp)"
  # Rewritten rather than substituted in place: a value may contain any
  # character, so nothing has to be escaped out of a substitution delimiter,
  # and a duplicated key cannot survive to shadow the new value.
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "${key}="* ]]; then
      if [[ "$replaced" == false ]]; then
        printf '%s=%s\n' "$key" "$value"
        replaced=true
      fi
      continue
    fi
    printf '%s\n' "$line"
  done < "$ENV_FILE" > "$tmp"
  if [[ "$replaced" == false ]]; then
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}

get_env_value() {
  local key="$1"
  # An absent key is empty, not a failure: under `set -o pipefail` grep's
  # non-zero status would otherwise abort the caller mid-assignment.
  grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

# Skipped or failed components of one bundled menu action.  Scoped per action:
# reset when the action starts, printed and cleared when it ends, so a note from
# one menu choice can never surface in the next one's summary.
INSTALL_NOTES=()
# Components this run pulled in because another component requires them.  Kept
# apart from INSTALL_NOTES so the skipped/failed count stays a count of things
# that did not happen; these did happen, and the operator is told why.
INSTALL_DEPENDENCIES=()
reset_install_notes() { INSTALL_NOTES=(); INSTALL_DEPENDENCIES=(); }

note_skip() {
  INSTALL_NOTES+=("$1")
  echo "$1" >&2
}

note_dependency() {
  INSTALL_DEPENDENCIES+=("$1")
  echo "$1"
}

print_install_summary() {
  local count=0
  count=${#INSTALL_NOTES[@]}
  echo ""
  echo "============================================="
  if [[ "${#INSTALL_DEPENDENCIES[@]}" -gt 0 ]]; then
    echo "Also installed, because the components you chose require it:"
    printf '  - %s\n' ${INSTALL_DEPENDENCIES[@]+"${INSTALL_DEPENDENCIES[@]}"}
    echo ""
  fi
  if [[ "$count" -eq 0 ]]; then
    echo "All requested components installed."
  else
    echo "Completed with $count skipped or failed component(s):"
    printf '  - %s\n' ${INSTALL_NOTES[@]+"${INSTALL_NOTES[@]}"}
  fi
  echo "============================================="
  INSTALL_NOTES=()
  INSTALL_DEPENDENCIES=()
}

# The control core.  Three components - the HID remote, the AirPlay/Spotify
# volume bridge and the web control UI - persist tone/EQ edits and speaker
# selections for the source switcher to apply, and take their volume ceiling
# from the speaker-profile status only the switcher publishes (without it they
# hold the fail-safe ceiling).  So those three are not standalone, and this
# installer installs the switcher with them.
SOURCE_SWITCHER_INSTALLED_THIS_RUN=0

source_switcher_present() {
  [[ "$SOURCE_SWITCHER_INSTALLED_THIS_RUN" == "1" ]] && return 0
  # Same query the update path uses.  A host without systemctl answers "not
  # installed" quietly: the redirection covers the lookup failure too.
  systemctl list-unit-files --no-legend cdsp-source-switcher.service 2>/dev/null \
    | grep -q '^cdsp-source-switcher.service'
}

ensure_source_switcher() {
  local component="$1"
  if source_switcher_present; then
    return 0
  fi
  echo ""
  echo "REQUIRED DEPENDENCY: $component needs the Source Switcher."
  echo "  Only the Source Switcher applies persisted Bass/Treble/EQ and speaker"
  echo "  changes to the engine and publishes the volume ceiling. Installed alone,"
  echo "  $component would stay at the fail-safe ceiling and its edits would go"
  echo "  nowhere, so the Source Switcher is being installed alongside it."
  echo ""
  install_source_switcher
  note_dependency "Source Switcher: installed because $component requires it (give it its source configs in $CONFIGS_DIR, or it will not run)"
}

ensure_venv() {
  if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating virtualenv at $VENV_DIR"
    python3 -m venv --system-site-packages "$VENV_DIR"
  fi
}

install_dependencies() {
  echo "Installing system and Python dependencies..."
  sudo apt update
  sudo apt install -y python3-venv python3-rpi-lgpio alsa-utils bluez wget curl git cargo build-essential pkg-config libasound2-dev libssl-dev

  export PATH="$HOME/.cargo/bin:$PATH"
  # Without the command check awk sees no input, never runs its action, and
  # exits 0 — so a missing rustc would silently look new enough.
  if ! command -v rustc >/dev/null 2>&1 || ! rustc --version | awk '{print $2}' | awk -F. '{exit !($1 > 1 || ($1 == 1 && $2 >= 90))}'; then
    echo "Installing the pinned Rust 1.90 toolchain required by CamillaDSP 4.1.3..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.90.0
  fi

  ensure_venv
  # Activate venv for pip installs.
  source "${VENV_DIR}/bin/activate"
  pip install --upgrade pip
  pip install --upgrade websocket-client evdev pyyaml git+https://github.com/HEnquist/pycamilladsp.git
  deactivate
  echo "Dependencies installed."
}

download_scripts() {
  echo "Downloading scripts from GitHub..."
  ensure_env_file
  local script tmp
  for script in settings.py trigger.py source_switcher.py cdsp_remote.py audio_eq.py speaker_profiles.py speaker_config.py speaker_xo.py airplay_volume_bridge.py configure_shairport.py motu_volume.py source_volume.py remote_keymap.py diagnose.py web_ui.py; do
    tmp="${SCRIPTS_DIR}/${script}.tmp"
    if [[ -f "$REPO_DIR/scripts/$script" ]]; then
      cp "$REPO_DIR/scripts/$script" "$tmp"
    else
      wget -q "${BASE_URL}/${script}" -O "$tmp"
    fi
    mv "$tmp" "${SCRIPTS_DIR}/${script}"
  done
  if [[ -f "$REPO_DIR/scripts/build_camilladsp_iso226.sh" ]]; then
    cp "$REPO_DIR/scripts/build_camilladsp_iso226.sh" "$SCRIPTS_DIR/build_camilladsp_iso226.sh.tmp"
  else
    wget -q "https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/scripts/build_camilladsp_iso226.sh" -O "$SCRIPTS_DIR/build_camilladsp_iso226.sh.tmp"
  fi
  mv "$SCRIPTS_DIR/build_camilladsp_iso226.sh.tmp" "$SCRIPTS_DIR/build_camilladsp_iso226.sh"
  if [[ -f "$REPO_DIR/scripts/build_librespot_volume_sync.sh" ]]; then
    cp "$REPO_DIR/scripts/build_librespot_volume_sync.sh" "$SCRIPTS_DIR/build_librespot_volume_sync.sh.tmp"
  else
    wget -q "https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/scripts/build_librespot_volume_sync.sh" -O "$SCRIPTS_DIR/build_librespot_volume_sync.sh.tmp"
  fi
  mv "$SCRIPTS_DIR/build_librespot_volume_sync.sh.tmp" "$SCRIPTS_DIR/build_librespot_volume_sync.sh"
  mkdir -p "$BASE_DIR/camilladsp-iso226"
  if [[ -f "$REPO_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch" ]]; then
    cp "$REPO_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch" "$BASE_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch.tmp"
  else
    wget -q "https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch" -O "$BASE_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch.tmp"
  fi
  mkdir -p "$BASE_DIR/librespot-volume-sync"
  if [[ -f "$REPO_DIR/librespot-volume-sync/librespot-v0.8.0-volume-sync.patch" ]]; then
    cp "$REPO_DIR/librespot-volume-sync/librespot-v0.8.0-volume-sync.patch" "$BASE_DIR/librespot-volume-sync/librespot-v0.8.0-volume-sync.patch.tmp"
  else
    wget -q "https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/librespot-volume-sync/librespot-v0.8.0-volume-sync.patch" -O "$BASE_DIR/librespot-volume-sync/librespot-v0.8.0-volume-sync.patch.tmp"
  fi
  mv "$BASE_DIR/librespot-volume-sync/librespot-v0.8.0-volume-sync.patch.tmp" "$BASE_DIR/librespot-volume-sync/librespot-v0.8.0-volume-sync.patch"
  chmod +x "$SCRIPTS_DIR/build_librespot_volume_sync.sh"
  mv "$BASE_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch.tmp" "$BASE_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch"
  chmod +x "$SCRIPTS_DIR"/*.py
  chmod +x "$SCRIPTS_DIR"/*.sh
  echo "Scripts downloaded."
}

prepare_install() {
  install_dependencies
  download_scripts
  ensure_audio_state_storage
}

ensure_user_writable_dir() {
  local dir="$1"
  if [[ ! -d "$dir" ]]; then
    sudo install -d -m 0750 -o "$INSTALL_USER" -g "$INSTALL_USER" "$dir"
  elif ! sudo -u "$INSTALL_USER" test -w "$dir"; then
    echo "Configured state directory is not writable by $INSTALL_USER: $dir" >&2
    echo "Use a dedicated application directory; existing directory ownership is never changed." >&2
    return 1
  fi
}

ensure_audio_state_storage() {
  local lock dir
  ensure_user_writable_dir "$STATE_DIR"
  ensure_user_writable_dir "$STATE_DIR/speaker-audio"
  ensure_user_writable_dir "$STATE_DIR/generated-configs"
  ensure_user_writable_dir "$STATE_DIR/audio-eq-backups"
  for dir in "$SITE_CONFIG_DIR/speaker-profiles" "$SITE_CONFIG_DIR/source-bases"; do
    [[ -d "$dir" ]] || sudo install -d -m 0755 "$dir"
  done
  # The locks every component shares are claimed by whichever process opens
  # them first, which on a fresh install can be the root UI or the root
  # Shairport callback.  Own them here, before anything runs.  Per-speaker
  # locks appear later and are covered by the UI unit's Group= and UMask=.
  for lock in \
    "$STATE_DIR/audio-eq.json.lock" \
    "$STATE_DIR/audio-control.lock" \
    "$STATE_DIR/source-volume.json.lock" \
    "$STATE_DIR/speaker-selection.json.lock"; do
    if [[ ! -e "$lock" ]]; then
      sudo -u "$INSTALL_USER" touch "$lock"
    fi
    sudo chown "$INSTALL_USER:$INSTALL_GROUP" "$lock"
    sudo chmod 0660 "$lock"
  done
}

create_unit() {
  local name="$1"
  local script="$2"
  local sysname="$3"
  local script_args="${4:-}"
  local unit_file
  unit_file="$(mktemp "${TMPDIR:-/tmp}/${sysname}.service.XXXXXX")"
  local runtime_directory=""
  local unit_ordering=""
  if [[ "$sysname" == "cdsp-source-switcher" ]]; then
    runtime_directory=$'RuntimeDirectory=cdsp-source-switcher\nRuntimeDirectoryMode=0755\nRuntimeDirectoryPreserve=yes'
    # Restarting the engine restarts the switcher, which then mutes and
    # re-applies the config.  PartOf propagates only explicit stop/restart
    # jobs on camilladsp.service (an operator restart, or the control UI's),
    # never camilladsp's own Restart= cycles, so it cannot create a restart
    # loop.  An engine that restarts by itself drops the switcher's websocket,
    # and the switcher re-applies on reconnect.
    unit_ordering='PartOf=camilladsp.service'
  elif [[ "$sysname" == "airplay-volume-bridge" ]]; then
    runtime_directory=$'RuntimeDirectory=airplay-volume-bridge\nRuntimeDirectoryMode=0755'
    unit_ordering='Before=shairport-sync.service raspotify.service'
    # The bridge both chowns its own receiver socket to the audio group and
    # writes to librespot's group-owned command socket, so it needs the
    # membership in both directions.  Service-scoped, rather than adding the
    # login account to the group, and effective without a re-login.
    if getent group audio >/dev/null; then
      runtime_directory+=$'\n'"SupplementaryGroups=audio"
    else
      echo "WARNING: group 'audio' does not exist on this system." >&2
      echo "         The AirPlay bridge cannot restrict its socket to it, and the" >&2
      echo "         Spotify drop-in would name a missing group." >&2
    fi
  fi

  cat > "$unit_file" <<EOL
[Unit]
Description=CamillaDSP $name
Wants=network-online.target camilladsp.service
After=network-online.target camilladsp.service
$unit_ordering

[Service]
User=$INSTALL_USER
Type=simple
WorkingDirectory=$BASE_DIR
EnvironmentFile=-$ENV_FILE
Environment=PYTHONUNBUFFERED=1
$runtime_directory
ExecStart=$VENV_DIR/bin/python3 -u $SCRIPTS_DIR/$script $script_args
Restart=always
RestartSec=2
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$sysname

[Install]
WantedBy=multi-user.target
EOL
  sudo install -m 0644 "$unit_file" "${SYSTEMD_UNIT_DIR}/${sysname}.service"
  rm -f "$unit_file"
  sudo systemctl daemon-reload
  sudo systemctl reenable "${sysname}.service"
  sudo systemctl restart "${sysname}.service"
}

install_trigger() {
  echo "Installing Trigger Control..."
  create_unit "Trigger Control" trigger.py cdsp-trigger
  echo "Trigger Control installed."
}

# The MOTU Clock Sync service of earlier releases.  The source switcher is now
# the MOTU's only client (clock, meters and main volume), and a second client
# would keep dropping its connection, so an install or update removes the old
# unit and its scripts.
remove_motu_sync() {
  if [[ -f "$SYSTEMD_UNIT_DIR/cdsp-motu-sync.service" || -f /etc/systemd/system/cdsp-motu-sync.service ]]; then
    echo "Removing the retired MOTU Clock Sync service..."
    sudo systemctl stop cdsp-motu-sync.service || true
    sudo systemctl disable cdsp-motu-sync.service || true
    remove_unit_file cdsp-motu-sync.service
    sudo systemctl daemon-reload
  fi
  rm -f "$SCRIPTS_DIR/clock_sync.py" "$SCRIPTS_DIR/motu_access.py"
}

install_source_switcher() {
  echo "Installing Source Switcher..."
  mkdir -p "$CONFIGS_DIR"
  remove_motu_sync
  create_unit "Source Switcher" source_switcher.py cdsp-source-switcher
  echo ""
  echo "IMPORTANT: create these config files if they do not already exist:"
  echo "   - $CONFIGS_DIR/toslink.yml"
  echo "   - $CONFIGS_DIR/streamer.yml"
  echo "   - $CONFIGS_DIR/gadget.yml"
  echo ""
  echo "Source Switcher installed."
  # Remembered for the rest of this run so a component installed after it never
  # re-installs it just because systemd has not caught up with the new unit.
  SOURCE_SWITCHER_INSTALLED_THIS_RUN=1
}

install_remote_sudoers() {
  local tmp
  if [[ ! -x "$SYSTEMCTL_BIN" || ! -x "$VISUDO_BIN" ]]; then
    echo "Required root-owned system tools are missing"
    return 1
  fi
  tmp="$(mktemp)"

  cat > "$tmp" <<EOF
# Allow the remote control service to perform only its documented actions.
$INSTALL_USER ALL=(root) NOPASSWD: $SYSTEMCTL_BIN restart camilladsp.service, $SYSTEMCTL_BIN restart camillagui.service, $SYSTEMCTL_BIN restart cdsp-source-switcher.service, $SYSTEMCTL_BIN --no-block restart cdsp-remote.service, $SYSTEMCTL_BIN poweroff
EOF
  # Explicit checks: these helpers also run on the left of `||`, where set -e
  # is off and a rejected file would otherwise still be installed.
  sudo "$VISUDO_BIN" -cf "$tmp" || { rm -f "$tmp"; return 1; }
  sudo install -m 0440 "$tmp" "$SUDOERS_DIR/cdsp-automation"
  rm -f "$tmp"
}

# The AirPlay bridge arbitrates the two receivers, so it needs exactly these
# four commands whether or not the remote control is installed.  Its own file,
# so installing one component never depends on another's authorization.
install_receiver_sudoers() {
  local tmp
  if [[ ! -x "$SYSTEMCTL_BIN" || ! -x "$VISUDO_BIN" ]]; then
    echo "Required root-owned system tools are missing"
    return 1
  fi
  tmp="$(mktemp)"

  cat > "$tmp" <<EOF
# Allow the AirPlay volume bridge to hand playback between the receivers.
$INSTALL_USER ALL=(root) NOPASSWD: $SYSTEMCTL_BIN start shairport-sync.service, $SYSTEMCTL_BIN stop shairport-sync.service, $SYSTEMCTL_BIN start raspotify.service, $SYSTEMCTL_BIN stop raspotify.service
EOF
  # Explicit checks: these helpers also run on the left of `||`, where set -e
  # is off and a rejected file would otherwise still be installed.
  sudo "$VISUDO_BIN" -cf "$tmp" || { rm -f "$tmp"; return 1; }
  sudo install -m 0440 "$tmp" "$SUDOERS_DIR/cdsp-automation-receivers"
  rm -f "$tmp"
}

install_remote() {
  echo "Installing Remote Control..."
  # The volume ceiling and the tone keys both run through the switcher.  Pull
  # it in before the remote's own unit starts.
  ensure_source_switcher "Remote Control"

  if getent group input >/dev/null; then
    sudo usermod -aG input "$INSTALL_USER"
  fi
  install_remote_sudoers

  echo ""
  echo "To find your remote's device name, run:"
  echo "  $VENV_DIR/bin/python3 -c \"import evdev; print([d.name for d in [evdev.InputDevice(p) for p in evdev.list_devices()]])\""
  echo ""
  read -r -p "Enter your remote device name (default: HID Remote01 Keyboard): " remote_name
  remote_name=${remote_name:-HID Remote01 Keyboard}
  set_env_value "REMOTE_NAME" "$remote_name"

  create_unit "Remote Control" cdsp_remote.py cdsp-remote
  echo ""
  echo "Remote Control Button Mapping:"
  echo "   - VOLUME UP/DOWN: Adjust volume (+/-1 dB)"
  echo "   - MUTE: Toggle mute"
  echo "   - UP/DOWN arrows: Adjust treble (+/-0.5 dB)"
  echo "   - LEFT/RIGHT arrows: Adjust bass (+/-0.5 dB)"
  echo "   - ENTER (short): Show current status"
  echo "   - ENTER (hold ~1s): Reset bass/treble to 0 dB"
  echo "   - POWER (hold ~1s): Restart CamillaDSP, GUI and the switcher"
  echo "   - POWER (hold ~10s): Shutdown system"
  echo ""
  echo "NOTE: log out/in or reboot if this installer just added your user to the input group."
  echo "Remote Control installed."
}

restore_shairport_config() {
  local shairport_config="$1"
  if ! sudo /usr/bin/python3 "$SCRIPTS_DIR/configure_shairport.py" --remove "$shairport_config"; then
    echo "Automatic restore failed; ${shairport_config}.pre-airplay-volume-bridge holds the pre-install file." >&2
  fi
}

# Also re-run on update, which is what rewrites a managed block written under
# this tool's earlier marker names.
configure_shairport_bridge() {
  local shairport_config="$SHAIRPORT_CONFIG"
  if [[ ! -f "$shairport_config" ]]; then
    echo "Shairport Sync config not found at $shairport_config; script installed, config unchanged."
    return 0
  fi
  sudo /usr/bin/python3 "$SCRIPTS_DIR/configure_shairport.py" "$shairport_config" "/usr/bin/python3 /usr/local/libexec/airplay_volume_bridge.py --notify" || return 1
  # The reason is printed first and unconditionally: under set -e a bare
  # --remove could otherwise take the installer down before it was ever said.
  if command -v shairport-sync >/dev/null && ! sudo timeout 10 shairport-sync --displayConfig >/dev/null; then
    echo "Shairport rejected the managed configuration; restoring its previous volume settings." >&2
    restore_shairport_config "$shairport_config"
    return 1
  fi
  if ! sudo systemctl restart shairport-sync.service; then
    echo "Shairport restart failed; restoring its previous volume settings." >&2
    restore_shairport_config "$shairport_config"
    sudo systemctl restart shairport-sync.service || true
    return 1
  fi
  echo "Shairport Sync now sends unity audio and controls the CamillaDSP master fader."
}

install_airplay_volume_bridge() {
  echo "Installing AirPlay volume bridge daemon and system callback..."
  # Like the remote, the bridge takes its volume ceiling from the switcher.
  ensure_source_switcher "AirPlay/Spotify volume bridge"
  sudo install -d -m 0755 /usr/local/libexec
  sudo install -m 0755 "$SCRIPTS_DIR/airplay_volume_bridge.py" /usr/local/libexec/airplay_volume_bridge.py
  sudo install -m 0644 "$SCRIPTS_DIR/settings.py" "$SCRIPTS_DIR/speaker_profiles.py" "$SCRIPTS_DIR/audio_eq.py" /usr/local/libexec/
  # Before create_unit, which starts the daemon: a bridge that comes up without
  # its receiver authorization cannot hand playback over.
  install_receiver_sudoers || return 1
  create_unit "AirPlay Volume Bridge" airplay_volume_bridge.py airplay-volume-bridge --daemon || return 1
  configure_shairport_bridge
}

install_spotify_volume_sync() {
  if ! systemctl list-unit-files --no-legend raspotify.service 2>/dev/null | grep -q '^raspotify.service'; then
    note_skip "Spotify volume sync: SKIPPED (raspotify.service is not installed; install raspotify, then re-run menu option 8)"
    return 0
  fi
  echo "Building the pinned Spotify Connect volume-sync receiver..."
  SPOTIFY_ALSA_DEVICE="$(get_env_value SPOTIFY_ALSA_DEVICE)" \
  "$SCRIPTS_DIR/build_librespot_volume_sync.sh" "$BASE_DIR/librespot-volume-sync/librespot-v0.8.0-volume-sync.patch"
}

# The control UI's exposure, resolved from the env file with the historical
# values as the fallback so a file written before these keys existed still
# yields the behaviour that install had.
control_ui_bind_host() {
  local host
  host="$(get_env_value INSTALLATION_UI_HOST)"
  printf '%s\n' "${host:-$CONTROL_UI_HOST_DEFAULT}"
}

control_ui_bind_port() {
  local port
  port="$(get_env_value INSTALLATION_UI_PORT)"
  printf '%s\n' "${port:-$CONTROL_UI_PORT_DEFAULT}"
}

install_control_ui() {
  local ui_host ui_port
  ui_host="$(control_ui_bind_host)"
  ui_port="$(control_ui_bind_port)"
  echo "Installing the web control UI (optional)..."
  echo ""
  echo "The UI manages sources, volume, EQ, speaker profiles and services,"
  echo "so its service runs as root."
  echo "It will bind to ${ui_host}:${ui_port} (INSTALLATION_UI_HOST in $ENV_FILE)."
  if [[ -n "$(get_env_value INSTALLATION_UI_TOKEN)" ]]; then
    echo "INSTALLATION_UI_TOKEN is set: state-changing requests need that secret."
  else
    echo "INSTALLATION_UI_TOKEN is unset: it has no authentication."
    echo "Expose its port on a trusted LAN only, or set a token in $ENV_FILE."
  fi
  echo ""
  # The UI's volume ceiling, tone and speaker changes all run through the
  # switcher, so it carries the same dependency as the remote.
  ensure_source_switcher "Web control UI"
  local unit_file
  unit_file="$(mktemp)"
  cat > "$unit_file" <<EOL
[Unit]
Description=CamillaDSP Control UI
Wants=network-online.target camilladsp.service
After=network-online.target camilladsp.service

[Service]
Type=simple
# No User=: the UI needs root for systemctl.  Group= only
# changes the group of the files it creates, so a lock or state file it makes
# first stays writable by the daemons running as $INSTALL_USER.
Group=$INSTALL_GROUP
UMask=0007
WorkingDirectory=$BASE_DIR
EnvironmentFile=-$ENV_FILE
Environment=PYTHONUNBUFFERED=1
# INSTALLATION_UI_HOST / INSTALLATION_UI_PORT / INSTALLATION_UI_TOKEN come from
# the EnvironmentFile above and are deliberately not pinned here: an
# Environment= line would shadow the operator's edit.  web_ui.py falls back to
# $CONTROL_UI_HOST_DEFAULT:$CONTROL_UI_PORT_DEFAULT when they are unset.
ExecStart=$VENV_DIR/bin/python3 -u $SCRIPTS_DIR/web_ui.py
Restart=always
RestartSec=2
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cdsp-control-ui

[Install]
WantedBy=multi-user.target
EOL
  sudo install -m 0644 "$unit_file" "${SYSTEMD_UNIT_DIR}/cdsp-control-ui.service"
  rm -f "$unit_file"
  sudo systemctl daemon-reload
  sudo systemctl reenable cdsp-control-ui.service
  sudo systemctl restart cdsp-control-ui.service
  echo "Control UI installed on ${ui_host}:${ui_port}."
  if [[ "$ui_host" == "127.0.0.1" || "$ui_host" == "localhost" ]]; then
    echo "Reach it with: ssh -N -L ${ui_port}:127.0.0.1:${ui_port} ${INSTALL_USER}@<pi>"
  fi
}

install_iso226_engine() {
  local status=0
  echo "Building the pinned CamillaDSP 4.1.3 ISO 226 engine..."
  # The builder preflights the running engine before it checks for a toolchain
  # or clones anything, and reserves status 3 for "this engine cannot be
  # replaced here" so a skip is never confused with a broken build.
  "$SCRIPTS_DIR/build_camilladsp_iso226.sh" "$BASE_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch" || status=$?
  if [[ "$status" -eq 3 ]]; then
    echo "The ISO 226 engine replaces /usr/local/bin/camilladsp; see the README prerequisite." >&2
    return 3
  fi
  if [[ "$status" -ne 0 ]]; then
    return "$status"
  fi
  if systemctl list-unit-files --no-legend cdsp-source-switcher.service 2>/dev/null | grep -q '^cdsp-source-switcher.service'; then
    sudo systemctl restart cdsp-source-switcher.service
  fi
  echo "ISO 226 engine installed and active."
}

# Bundled callers only: a component that cannot be installed here must not cost
# the operator every other component in the run.
install_iso226_engine_optional() {
  local status=0
  install_iso226_engine || status=$?
  case "$status" in
    0) ;;
    3) note_skip "ISO 226 loudness engine: SKIPPED (CamillaDSP does not run from /usr/local/bin/camilladsp)" ;;
    *) note_skip "ISO 226 loudness engine: FAILED (see the build output above)" ;;
  esac
}

refresh_installed_units() {
  # Before the switcher restarts, so it is the MOTU's only client from its
  # first connection.
  remove_motu_sync
  if systemctl list-unit-files --no-legend cdsp-trigger.service 2>/dev/null | grep -q '^cdsp-trigger.service'; then
    create_unit "Trigger Control" trigger.py cdsp-trigger
  fi
  if systemctl list-unit-files --no-legend cdsp-source-switcher.service 2>/dev/null | grep -q '^cdsp-source-switcher.service'; then
    create_unit "Source Switcher" source_switcher.py cdsp-source-switcher
  fi
  # Before the remote branch, which narrows the remote sudoers file: the
  # receiver authorization is written first, so no window exists where neither
  # file grants the four receiver commands.
  if systemctl list-unit-files --no-legend airplay-volume-bridge.service 2>/dev/null | grep -q '^airplay-volume-bridge.service'; then
    sudo install -d -m 0755 /usr/local/libexec
    sudo install -m 0755 "$SCRIPTS_DIR/airplay_volume_bridge.py" /usr/local/libexec/airplay_volume_bridge.py
    sudo install -m 0644 "$SCRIPTS_DIR/settings.py" "$SCRIPTS_DIR/speaker_profiles.py" "$SCRIPTS_DIR/audio_eq.py" /usr/local/libexec/
    install_receiver_sudoers
    create_unit "AirPlay Volume Bridge" airplay_volume_bridge.py airplay-volume-bridge --daemon
    configure_shairport_bridge || note_skip "AirPlay Shairport configuration: FAILED (previous settings were restored)"
  fi
  if systemctl list-unit-files --no-legend cdsp-remote.service 2>/dev/null | grep -q '^cdsp-remote.service'; then
    install_remote_sudoers
    create_unit "Remote Control" cdsp_remote.py cdsp-remote
  fi
  if [[ -f "$SPOTIFY_DROPIN_PATH" ]]; then
    install_spotify_volume_sync || note_skip "Spotify volume sync: FAILED (see the build output above)"
  fi
  if systemctl list-unit-files --no-legend cdsp-control-ui.service 2>/dev/null | grep -q '^cdsp-control-ui.service'; then
    install_control_ui
  fi
}

run_diagnose() {
  if [[ ! -x "$VENV_DIR/bin/python3" || ! -f "$SCRIPTS_DIR/diagnose.py" ]]; then
    echo "The health check needs the installed utilities: choose option 1 or 2 first."
    return 0
  fi
  echo ""
  # Read-only; a failed check sets the exit status, which the menu ignores.
  "$VENV_DIR/bin/python3" "$SCRIPTS_DIR/diagnose.py" || true
}

show_status() {
  echo ""
  echo "============================================="
  echo "Service Status"
  echo "============================================="
  local service
  for service in "${CDSP_SERVICES[@]}" cdsp-control-ui; do
    systemctl status "${service}.service" --no-pager || true
  done
}

# Both the configured directory and the default one: an operator may have
# installed under one and be uninstalling under another, and rm -f on a path
# that holds no unit costs nothing.
remove_unit_file() {
  sudo rm -f "$SYSTEMD_UNIT_DIR/$1" "/etc/systemd/system/$1"
}

uninstall_all() {
  echo "Uninstalling all utilities..."
  local service
  for service in "${CDSP_SERVICES[@]}"; do
    sudo systemctl stop "${service}.service" || true
    sudo systemctl disable "${service}.service" || true
    remove_unit_file "${service}.service"
  done
  remove_motu_sync
  sudo rm -f "$SUDOERS_DIR/cdsp-automation" "$SUDOERS_DIR/cdsp-automation-receivers"
  if [[ -x "$SCRIPTS_DIR/build_camilladsp_iso226.sh" ]]; then
    "$SCRIPTS_DIR/build_camilladsp_iso226.sh" --uninstall || true
  fi
  if [[ -x "$SCRIPTS_DIR/build_librespot_volume_sync.sh" ]]; then
    "$SCRIPTS_DIR/build_librespot_volume_sync.sh" --uninstall || true
  fi
  if [[ -f "$SHAIRPORT_CONFIG" && -f "$SCRIPTS_DIR/configure_shairport.py" ]]; then
    sudo /usr/bin/python3 "$SCRIPTS_DIR/configure_shairport.py" --remove "$SHAIRPORT_CONFIG" || true
    sudo systemctl restart shairport-sync.service || true
  fi
  sudo systemctl stop cdsp-control-ui.service 2>/dev/null || true
  sudo systemctl disable cdsp-control-ui.service 2>/dev/null || true
  remove_unit_file cdsp-control-ui.service
  sudo rm -f /usr/local/libexec/airplay_volume_bridge.py /usr/local/libexec/settings.py \
    /usr/local/libexec/speaker_profiles.py /usr/local/libexec/audio_eq.py
  sudo systemctl daemon-reload
  echo "Uninstalled."
}

install_all() {
  reset_install_notes
  prepare_install
  install_trigger
  install_source_switcher
  install_remote
  install_airplay_volume_bridge || note_skip "AirPlay volume bridge: FAILED (Shairport settings were restored)"
  install_spotify_volume_sync || note_skip "Spotify volume sync: FAILED (see the build output above)"
  # Last, so a compile that dies on a network hiccup cannot cost every other
  # component in the run.
  install_iso226_engine_optional
  print_install_summary
}

install_network_volume_sync() {
  reset_install_notes
  prepare_install
  install_airplay_volume_bridge || note_skip "AirPlay volume bridge: FAILED (Shairport settings were restored)"
  install_spotify_volume_sync || note_skip "Spotify volume sync: FAILED (see the build output above)"
  print_install_summary
}

update_utilities() {
  reset_install_notes
  echo "Updating utilities (scripts + pycamilladsp)..."
  download_scripts
  ensure_audio_state_storage
  ensure_venv
  source "$VENV_DIR/bin/activate"
  pip install --upgrade git+https://github.com/HEnquist/pycamilladsp.git
  pip install --upgrade websocket-client evdev pyyaml
  deactivate
  # Deliberately no engine rebuild: that would replace the running CamillaDSP
  # binary and restart it, which a routine update must never do.  Menu option 9
  # rebuilds it when the operator asks for it.
  if [[ -f "$STATE_DIR/iso226-engine.json" ]]; then
    echo "An ISO 226 engine is installed and was left running untouched."
    echo "Run menu option 9 to rebuild it against the downloaded pinned patch."
  fi
  audit_operator_volume_limits
  # refresh_installed_units already restarts every installed service once:
  # create_unit does it for each daemon and install_control_ui for the UI.
  # A second pass here restarted each of them again ~2 s later, and the
  # source switcher's second start mistook the first start's temporary
  # validation mute for the listener's setting, so an update left the
  # output muted. Do not add a blanket restart back.
  refresh_installed_units
  echo "Update complete. User settings remain in $ENV_FILE."
  print_install_summary
}

# The profile volume cap is now enforced through the config's own native
# devices.volume_limit rather than a one-time clamp, so an operator-owned
# config without one is refused instead of silently left unprotected.  Name
# the files that need a line added here, while the operator is still at the
# keyboard, rather than letting a speaker go silent at its next transition.
audit_operator_volume_limits() {
  local profile_dir config_dir output
  profile_dir="$SITE_CONFIG_DIR/speaker-profiles"
  config_dir="$(get_env_value CDSP_CONFIG_DIR)"
  : "${config_dir:=$CONFIGS_DIR}"
  [[ -x "$VENV_DIR/bin/python3" ]] || return 0
  [[ -d "$config_dir" ]] || return 0
  if output="$("$VENV_DIR/bin/python3" "$SCRIPTS_DIR/speaker_config.py" \
      --profile-dir "$profile_dir" --config-dir "$config_dir" 2>&1)"; then
    return 0
  fi
  [[ -n "$output" ]] || return 0
  printf '%s\n' "$output" >&2
  note_skip "Operator configs need a devices.volume_limit (listed above)"
}

pair_bluetooth_remote() {
  echo "Pairing Bluetooth Remote..."
  echo ""
  echo "Starting Bluetooth scan for 10 seconds..."

  bluetoothctl power on
  bluetoothctl scan on &
  local scan_pid=$!
  sleep 10
  bluetoothctl scan off
  wait "$scan_pid" 2>/dev/null || true

  echo ""
  echo "Available devices:"
  mapfile -t devices < <(bluetoothctl devices | awk '{print $2 " " substr($0, index($0,$3))}')

  if [[ ${#devices[@]} -eq 0 ]]; then
    echo "No Bluetooth devices found."
    return
  fi

  local i
  for i in "${!devices[@]}"; do
    echo "$((i+1))) ${devices[i]}"
  done

  read -r -p "Enter the number of your Bluetooth remote: " num
  if ! [[ "$num" =~ ^[0-9]+$ ]] || (( num < 1 || num > ${#devices[@]} )); then
    echo "Invalid selection. Pairing aborted."
    return
  fi

  local selected_device mac_address device_name
  selected_device="${devices[num-1]}"
  mac_address=$(echo "$selected_device" | awk '{print $1}')
  device_name=$(echo "$selected_device" | cut -d' ' -f2-)

  echo "Pairing with $device_name ($mac_address)..."

  bluetoothctl pair "$mac_address"
  bluetoothctl connect "$mac_address"
  bluetoothctl trust "$mac_address"

  echo "Bluetooth remote paired successfully."
  echo ""
  echo "Your remote should now appear when you run:"
  echo "  $VENV_DIR/bin/python3 -c \"import evdev; print([d.name for d in [evdev.InputDevice(p) for p in evdev.list_devices()]])\""
}

# Defaults to No, and survives EOF: a bare read returning non-zero would
# otherwise abort the whole installer under set -e.
confirm_action() {
  local answer=""
  read -r -p "$1 [y/N]: " answer || answer=""
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

# Menu option 11.  Names the address the UI is about to bind to before asking
# for anything, and offers loopback-only first so the LAN-wide default is a
# choice rather than an accident.  Answering No to both questions changes
# nothing.
confirm_control_ui_exposure() {
  local ui_host ui_port exposure auth
  ui_host="$(control_ui_bind_host)"
  ui_port="$(control_ui_bind_port)"
  echo ""
  echo "The web control UI is a root web server: it restarts services and"
  echo "changes volume."
  echo "It will bind to ${ui_host}:${ui_port} (INSTALLATION_UI_HOST in $ENV_FILE)."
  if [[ -n "$(get_env_value INSTALLATION_UI_TOKEN)" ]]; then
    auth="a shared secret is required"
    echo "INSTALLATION_UI_TOKEN is set, so state-changing requests need it."
  else
    auth="unauthenticated"
    echo "INSTALLATION_UI_TOKEN is unset, so it has no authentication."
    echo "Set one in $ENV_FILE (openssl rand -hex 32) to require a secret."
  fi
  if [[ "$ui_host" == "$CONTROL_UI_HOST_DEFAULT" ]]; then
    echo "$CONTROL_UI_HOST_DEFAULT is every interface: anyone on the LAN can reach it."
    echo ""
    if confirm_action "Bind it to 127.0.0.1 instead (loopback only, reach it over an SSH tunnel)?"; then
      set_env_value INSTALLATION_UI_HOST 127.0.0.1
      ui_host="127.0.0.1"
      echo "INSTALLATION_UI_HOST set to 127.0.0.1 in $ENV_FILE."
    fi
  fi
  if [[ "$ui_host" == "$CONTROL_UI_HOST_DEFAULT" ]]; then
    exposure="every interface"
  else
    exposure="loopback only"
  fi
  echo ""
  confirm_action "Install a root web server on ${ui_host}:${ui_port} (${exposure}, ${auth})?"
}

print_menu() {
  cat <<MENU
=============================================
CamillaDSP Utilities - Choose an Option
=============================================
1)  Install All Utilities
2)  Update Utilities
3)  Install Trigger Control
4)  Install Source Switcher
5)  Install Remote Control
6)  Pair Bluetooth Remote
7)  Show Service Status
8)  Install AirPlay + Spotify Volume Sync
9)  Install ISO 226 Loudness Engine
10) Uninstall All Utilities
11) Install Web Control UI (optional)
12) Run Health Check (diagnose)
0)  Exit
Options 5, 8 and 11 also install option 4 when it is missing: the Source
Switcher is the only thing that applies tone/EQ and speaker changes.
Options 10 and 11 ask for confirmation before acting.
=============================================
MENU
}

main() {
  ensure_env_file
  while true; do
    print_menu
    read -r -p "Enter your choice: " choice
    case "$choice" in
      1) install_all ;;
      2) update_utilities ;;
      3) prepare_install; install_trigger ;;
      4) prepare_install; install_source_switcher ;;
      5) reset_install_notes; prepare_install; install_remote; print_install_summary ;;
      6) pair_bluetooth_remote ;;
      7) show_status ;;
      8) install_network_volume_sync ;;
      9) prepare_install; install_iso226_engine ;;
      10) if confirm_action "Remove all CamillaDSP utility services, units and sudoers rules?"; then uninstall_all; else echo "Cancelled."; fi ;;
      11) if confirm_control_ui_exposure; then prepare_install; install_control_ui; print_install_summary; else echo "Cancelled."; fi ;;
      12) run_diagnose ;;
      0) echo "Exiting."; exit 0 ;;
      *) echo "Invalid choice" ;;
    esac
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
