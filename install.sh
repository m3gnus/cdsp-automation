#!/bin/bash
# CamillaDSP Utilities Setup Script
# Installs: Trigger Control, MOTU Clock Sync, Source Switcher, and Remote Control
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
LEGACY_UNIT_DIR="${CDSP_AUTOMATION_LEGACY_UNIT_DIR:-/lib/systemd/system}"
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
SPOTIFY_COMMAND_SOCKET_DEFAULT="/run/raspotify/cdsp-volume.sock"
AUDIO_EQ_BACKUP_DEFAULT="/var/lib/cdsp-automation/audio-eq-backups"
ISO226_CAPABILITY_DEFAULT="/var/lib/cdsp-automation/iso226-engine.json"
# Paths an earlier release compiled a single site's name into, and the retired
# state tree it used.  The name is assembled from fragments so the literal never
# appears in this repository; every comparison against it is exact, so files
# this installer did not create are never rewritten or removed.
LEGACY_TAG="ug""lan"
LEGACY_SPOTIFY_DROPIN="$RASPOTIFY_DROPIN_DIR/$LEGACY_TAG-volume-sync.conf"
LEGACY_SPOTIFY_SOCKET="/run/raspotify/$LEGACY_TAG-volume.sock"
LEGACY_AUDIO_EQ_BACKUP_DIR="/var/lib/installation/audio-eq-backups"

CDSP_SERVICES=(
  cdsp-trigger
  cdsp-motu-sync
  cdsp-source-switcher
  cdsp-remote
  airplay-volume-bridge
)

default_env() {
  cat <<EOF
# CamillaDSP automation settings.
# This file is preserved when scripts are updated.
CDSP_HOST=127.0.0.1
CDSP_PORT=1234
# The control UI runs as root, so it cannot derive these from \$HOME.
CDSP_CONFIG_DIR=$CONFIGS_DIR
CDSP_AUTOMATION_ENV=$ENV_FILE
POWER_GPIO=4
TRIGGER_DELAY_SECONDS=320
TRIGGER_CHECK_INTERVAL=0.2
TRIGGER_AUDIO_THRESHOLD_DB=-80
MOTU_WS_URL=ws://169.254.51.193:1280
MOTU_CLOCK_STATE_PATH=/var/lib/cdsp-automation/motu-clock-source
SOURCE_CHECK_INTERVAL=1.0
SOURCE_IDLE_TIMEOUT=60
SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT=0
SOURCE_AUDIO_THRESHOLD_DB=-80
SOURCE_OVERRIDE_PATH=/run/cdsp-source-switcher/manual_source
SOURCE_TOSLINK_MOTU_METERS=true
SOURCE_ANALOG_MOTU_METERS=false
SOURCE_IDLE_MODE=keep-last
SOURCE_TOSLINK_METER_PAIRS=12,13
SOURCE_ANALOG_METER_PAIRS=16,18
SOURCE_MOTU_METER_ACTIVE_BELOW=250
SOURCE_TOSLINK_ACTIVE_SECONDS=0.5
SOURCE_TOSLINK_IDLE_SECONDS=5
SOURCE_ANALOG_ACTIVE_SECONDS=5
SOURCE_ANALOG_IDLE_SECONDS=30
SOURCE_DEBUG=false
SOURCE_RECOVERY_RETRY_SECONDS=10
SOURCE_RECOVERY_LOG_SECONDS=30
AUDIO_EQ_PATH=/var/lib/cdsp-automation/audio-eq.json
AUDIO_EQ_STATUS_PATH=/run/cdsp-source-switcher/audio-eq-status.json
AUDIO_EQ_BACKUP_DIR=$AUDIO_EQ_BACKUP_DEFAULT
AUDIO_CONTROL_LOCK_PATH=/var/lib/cdsp-automation/audio-control.lock
AUDIO_READY_PATH=/run/cdsp-source-switcher/audio-ready.json
AUDIO_EQ_REAPPLY_SECONDS=1.0
SPEAKER_SELECTION_PATH=/var/lib/cdsp-automation/speaker-selection.json
SPEAKER_CATALOG_PATH=/etc/cdsp-automation/speaker-catalog.json
SPEAKER_AUDIO_DIR=/var/lib/cdsp-automation/speaker-audio
SPEAKER_PROFILE_DIR=/etc/cdsp-automation/speaker-profiles
SOURCE_BASE_DIR=/etc/cdsp-automation/source-bases
SPEAKER_GENERATED_DIR=/var/lib/cdsp-automation/generated-configs
SPEAKER_STATUS_PATH=/run/cdsp-source-switcher/speaker-profile-status.json
SPEAKER_TRANSITION_PATH=/var/lib/cdsp-automation/speaker-transition.json
CAMILLA_BINARY=camilladsp
CONFIG_VALIDATE_TIMEOUT=10
AIRPLAY_VOLUME_MIN_DB=-50
AIRPLAY_VOLUME_MAX_DB=0
AIRPLAY_VOLUME_CURVE=1.0
AIRPLAY_VOLUME_STATUS_PATH=/run/airplay-volume-bridge/status.json
AIRPLAY_VOLUME_SOCKET_PATH=/run/airplay-volume-bridge/input.sock
SPOTIFY_VOLUME_COMMAND_SOCKET_PATH=$SPOTIFY_COMMAND_SOCKET_DEFAULT
# Empty leaves raspotify's own device configuration in force; set an ALSA PCM
# name here (see 'aplay -L') only to override it.
SPOTIFY_ALSA_DEVICE=
VOLUME_SYNC_POLL_INTERVAL=0.25
VOLUME_SYNC_COMMAND_RETRY_SECONDS=1.0
VOLUME_SYNC_COMMAND_ACK_TIMEOUT=3.0
VOLUME_SYNC_HEARTBEAT_SECONDS=10.0
VOLUME_SYNC_GROUP=audio
ISO226_CAPABILITY_PATH=$ISO226_CAPABILITY_DEFAULT
# Shown in the control UI's page title and header.
SITE_NAME=CamillaDSP
REMOTE_NAME=HID Remote01 Keyboard
REMOTE_DEVICE_RETRY_SECONDS=2
REMOTE_STATUS_LOG_SECONDS=300
REMOTE_RESTART_HOLD_SECONDS=1
REMOTE_SHUTDOWN_HOLD_SECONDS=10
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
reset_install_notes() { INSTALL_NOTES=(); }

note_skip() {
  INSTALL_NOTES+=("$1")
  echo "$1" >&2
}

print_install_summary() {
  local count=0
  count=${#INSTALL_NOTES[@]}
  echo ""
  echo "============================================="
  if [[ "$count" -eq 0 ]]; then
    echo "All requested components installed."
  else
    echo "Completed with $count skipped or failed component(s):"
    printf '  - %s\n' ${INSTALL_NOTES[@]+"${INSTALL_NOTES[@]}"}
  fi
  echo "============================================="
  INSTALL_NOTES=()
}

# Move settings this installer used to hard-code onto their neutral names.
# Only the exact historical defaults are rewritten, so a value the operator
# chose is always preserved.
migrate_env_defaults() {
  local socket dropin device
  ensure_env_file
  socket="$(get_env_value SPOTIFY_VOLUME_COMMAND_SOCKET_PATH)"
  if [[ "$socket" == "$LEGACY_SPOTIFY_SOCKET" ]]; then
    set_env_value SPOTIFY_VOLUME_COMMAND_SOCKET_PATH "$SPOTIFY_COMMAND_SOCKET_DEFAULT"
  fi
  dropin="$(get_env_value SPOTIFY_VOLUME_DROPIN_PATH)"
  if [[ "$dropin" == "$LEGACY_SPOTIFY_DROPIN" ]]; then
    set_env_value SPOTIFY_VOLUME_DROPIN_PATH "$SPOTIFY_DROPIN_PATH"
  fi
  # Lift the output device out of the drop-in this installer generated, so the
  # site's PCM name survives in the env file while the repository stops naming
  # it.  Only that one file is read; an unrelated drop-in is never consulted.
  if [[ -z "$(get_env_value SPOTIFY_ALSA_DEVICE)" && -f "$LEGACY_SPOTIFY_DROPIN" ]]; then
    device="$(grep -oE -- '--device[= ][^ ]+' "$LEGACY_SPOTIFY_DROPIN" | head -n 1 || true)"
    device="${device#--device}"
    device="${device#[= ]}"
    if [[ -n "$device" ]]; then
      set_env_value SPOTIFY_ALSA_DEVICE "$device"
      echo "Kept the existing Spotify output device as SPOTIFY_ALSA_DEVICE=$device"
    fi
  fi
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
  for script in trigger.py clock_sync.py source_switcher.py cdsp_remote.py audio_eq.py speaker_profiles.py speaker_config.py speaker_xo.py airplay_volume_bridge.py configure_shairport.py web_ui.py; do
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
  migrate_env_defaults
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
  local lock audio_eq_path audio_control_lock_path motu_clock_state_path speaker_selection_path speaker_transition_path speaker_audio_dir speaker_profile_dir source_base_dir generated_dir audio_eq_backup_dir
  audio_eq_path="$(get_env_value AUDIO_EQ_PATH)"
  audio_eq_backup_dir="$(get_env_value AUDIO_EQ_BACKUP_DIR)"
  audio_control_lock_path="$(get_env_value AUDIO_CONTROL_LOCK_PATH)"
  motu_clock_state_path="$(get_env_value MOTU_CLOCK_STATE_PATH)"
  speaker_selection_path="$(get_env_value SPEAKER_SELECTION_PATH)"
  speaker_transition_path="$(get_env_value SPEAKER_TRANSITION_PATH)"
  speaker_audio_dir="$(get_env_value SPEAKER_AUDIO_DIR)"
  speaker_profile_dir="$(get_env_value SPEAKER_PROFILE_DIR)"
  source_base_dir="$(get_env_value SOURCE_BASE_DIR)"
  generated_dir="$(get_env_value SPEAKER_GENERATED_DIR)"
  : "${audio_eq_path:=/var/lib/cdsp-automation/audio-eq.json}"
  : "${audio_control_lock_path:=/var/lib/cdsp-automation/audio-control.lock}"
  : "${motu_clock_state_path:=/var/lib/cdsp-automation/motu-clock-source}"
  : "${speaker_selection_path:=/var/lib/cdsp-automation/speaker-selection.json}"
  : "${speaker_transition_path:=/var/lib/cdsp-automation/speaker-transition.json}"
  : "${speaker_audio_dir:=/var/lib/cdsp-automation/speaker-audio}"
  : "${speaker_profile_dir:=/etc/cdsp-automation/speaker-profiles}"
  : "${source_base_dir:=/etc/cdsp-automation/source-bases}"
  : "${generated_dir:=/var/lib/cdsp-automation/generated-configs}"
  : "${audio_eq_backup_dir:=$AUDIO_EQ_BACKUP_DEFAULT}"
  ensure_user_writable_dir "$(dirname "$audio_eq_path")"
  ensure_user_writable_dir "$(dirname "$audio_control_lock_path")"
  ensure_user_writable_dir "$(dirname "$motu_clock_state_path")"
  ensure_user_writable_dir "$(dirname "$speaker_selection_path")"
  ensure_user_writable_dir "$(dirname "$speaker_transition_path")"
  ensure_user_writable_dir "$speaker_audio_dir"
  ensure_user_writable_dir "$generated_dir"
  ensure_user_writable_dir "$audio_eq_backup_dir"
  if [[ ! -d "$speaker_profile_dir" ]]; then
    sudo install -d -m 0755 "$speaker_profile_dir"
  fi
  if [[ ! -d "$source_base_dir" ]]; then
    sudo install -d -m 0755 "$source_base_dir"
  fi
  # The three locks every component shares are claimed by whichever process
  # opens them first, which on a fresh install can be the root UI or the
  # root Shairport callback.  Own them here, before anything runs.  Per-speaker
  # locks appear later and are covered by the UI unit's Group= and UMask=.
  for lock in \
    "${audio_eq_path}.lock" \
    "$audio_control_lock_path" \
    "${speaker_selection_path}.lock"; do
    if [[ ! -e "$lock" ]]; then
      sudo -u "$INSTALL_USER" touch "$lock"
    fi
    sudo chown "$INSTALL_USER:$INSTALL_GROUP" "$lock"
    sudo chmod 0660 "$lock"
  done
}

# The EQ snapshots are the UI's only undo for persisted audio state, and
# retention is per selected speaker, so a multi-profile deployment can hold well
# over the per-prefix limit.  Copy them forward, never move: the source stays
# untouched, and an existing destination file is always preferred.
migrate_audio_eq_backups() {
  local destination file name copied=0
  destination="$(get_env_value AUDIO_EQ_BACKUP_DIR)"
  : "${destination:=$AUDIO_EQ_BACKUP_DEFAULT}"
  # Only the move this installer performs is migrated; a custom location is a
  # deliberate choice and is left alone.
  [[ "$destination" == "$AUDIO_EQ_BACKUP_DEFAULT" ]] || return 0
  [[ -d "$LEGACY_AUDIO_EQ_BACKUP_DIR" && -d "$destination" ]] || return 0
  for file in "$LEGACY_AUDIO_EQ_BACKUP_DIR"/*.json; do
    [[ -f "$file" ]] || continue
    name="$(basename "$file")"
    if [[ ! -e "$destination/$name" ]]; then
      sudo install -m 0640 -o "$INSTALL_USER" -g "$INSTALL_GROUP" "$file" "$destination/$name"
      copied=$((copied + 1))
    fi
  done
  if [[ "$copied" -gt 0 ]]; then
    echo "Copied $copied EQ backup snapshot(s) into $destination." >&2
  fi
  echo "Note: EQ backups now live in $destination; $LEGACY_AUDIO_EQ_BACKUP_DIR was left untouched." >&2
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
  elif [[ "$sysname" == "airplay-volume-bridge" ]]; then
    runtime_directory=$'RuntimeDirectory=airplay-volume-bridge\nRuntimeDirectoryMode=0755'
    unit_ordering='Before=shairport-sync.service raspotify.service'
    # The bridge both chowns its own receiver socket to this group and writes
    # to librespot's group-owned command socket, so it needs the membership in
    # both directions.  Service-scoped, rather than adding the login account to
    # the group, and effective without a re-login.
    local sync_group
    sync_group="$(get_env_value VOLUME_SYNC_GROUP)"
    : "${sync_group:=audio}"
    if [[ ! "$sync_group" =~ ^[a-zA-Z0-9._-]+$ ]]; then
      echo "WARNING: VOLUME_SYNC_GROUP is not a valid group name: $sync_group" >&2
      echo "         The bridge socket and the Spotify drop-in will disagree; fix it in $ENV_FILE." >&2
    elif getent group "$sync_group" >/dev/null; then
      runtime_directory+=$'\n'"SupplementaryGroups=$sync_group"
    else
      echo "WARNING: group '$sync_group' does not exist on this system." >&2
      echo "         The AirPlay bridge cannot restrict its socket to it, and the" >&2
      echo "         Spotify drop-in would name a missing group; create it or set" >&2
      echo "         VOLUME_SYNC_GROUP in $ENV_FILE." >&2
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
  sudo rm -f "${LEGACY_UNIT_DIR}/${sysname}.service"
  sudo systemctl daemon-reload
  sudo systemctl reenable "${sysname}.service"
  sudo systemctl restart "${sysname}.service"
}

install_trigger() {
  echo "Installing Trigger Control..."
  create_unit "Trigger Control" trigger.py cdsp-trigger
  echo "Trigger Control installed."
}

install_motu_sync() {
  echo "Installing MOTU Clock Sync..."
  read -r -p "Enter your MOTU device IP address (default: 169.254.51.193): " motu_ip
  motu_ip=${motu_ip:-169.254.51.193}
  set_env_value "MOTU_WS_URL" "ws://${motu_ip}:1280"
  create_unit "MOTU Clock Sync" clock_sync.py cdsp-motu-sync
  echo "MOTU Clock Sync installed."
}

install_source_switcher() {
  echo "Installing Source Switcher..."
  mkdir -p "$CONFIGS_DIR"
  create_unit "Source Switcher" source_switcher.py cdsp-source-switcher
  echo ""
  echo "IMPORTANT: create these config files if they do not already exist:"
  echo "   - $CONFIGS_DIR/toslink.yml"
  echo "   - $CONFIGS_DIR/streamer.yml"
  echo "   - $CONFIGS_DIR/gadget.yml"
  echo ""
  echo "Source Switcher installed."
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
$INSTALL_USER ALL=(root) NOPASSWD: $SYSTEMCTL_BIN restart camilladsp.service, $SYSTEMCTL_BIN restart camillagui.service, $SYSTEMCTL_BIN restart cdsp-motu-sync.service, $SYSTEMCTL_BIN restart cdsp-source-switcher.service, $SYSTEMCTL_BIN --no-block restart cdsp-remote.service, $SYSTEMCTL_BIN poweroff
EOF
  sudo "$VISUDO_BIN" -cf "$tmp"
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
  sudo "$VISUDO_BIN" -cf "$tmp"
  sudo install -m 0440 "$tmp" "$SUDOERS_DIR/cdsp-automation-receivers"
  rm -f "$tmp"
}

install_remote() {
  echo "Installing Remote Control..."

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
  echo "   - POWER (hold ~1s): Restart CamillaDSP, GUI, MOTU sync and the switcher"
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
  sudo /usr/bin/python3 "$SCRIPTS_DIR/configure_shairport.py" "$shairport_config" "/usr/bin/python3 /usr/local/libexec/airplay_volume_bridge.py --notify"
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
  sudo install -d -m 0755 /usr/local/libexec
  sudo install -m 0755 "$SCRIPTS_DIR/airplay_volume_bridge.py" /usr/local/libexec/airplay_volume_bridge.py
  sudo install -m 0644 "$SCRIPTS_DIR/speaker_profiles.py" "$SCRIPTS_DIR/audio_eq.py" /usr/local/libexec/
  # Before create_unit, which starts the daemon: a bridge that comes up without
  # its receiver authorization cannot hand playback over.
  install_receiver_sudoers
  create_unit "AirPlay Volume Bridge" airplay_volume_bridge.py airplay-volume-bridge --daemon
  configure_shairport_bridge
}

install_spotify_volume_sync() {
  if ! systemctl list-unit-files --no-legend raspotify.service 2>/dev/null | grep -q '^raspotify.service'; then
    note_skip "Spotify volume sync: SKIPPED (raspotify.service is not installed; install raspotify, then re-run menu option 9)"
    return 0
  fi
  echo "Building the pinned Spotify Connect volume-sync receiver..."
  SPOTIFY_ALSA_DEVICE="$(get_env_value SPOTIFY_ALSA_DEVICE)" \
  SPOTIFY_VOLUME_COMMAND_SOCKET_PATH="$(get_env_value SPOTIFY_VOLUME_COMMAND_SOCKET_PATH)" \
  AIRPLAY_VOLUME_SOCKET_PATH="$(get_env_value AIRPLAY_VOLUME_SOCKET_PATH)" \
  VOLUME_SYNC_GROUP="$(get_env_value VOLUME_SYNC_GROUP)" \
  "$SCRIPTS_DIR/build_librespot_volume_sync.sh" "$BASE_DIR/librespot-volume-sync/librespot-v0.8.0-volume-sync.patch"
}

install_control_ui() {
  echo "Installing the web control UI (optional)..."
  echo ""
  echo "The UI manages sources, volume, EQ, speaker profiles, services,"
  echo "storage and the system clock, so its service runs as root."
  echo "Expose its port on a trusted LAN only; it has no authentication."
  echo ""
  local unit_file
  unit_file="$(mktemp)"
  cat > "$unit_file" <<EOL
[Unit]
Description=CamillaDSP Control UI
Wants=network-online.target camilladsp.service
After=network-online.target camilladsp.service

[Service]
Type=simple
# No User=: the UI needs root for systemctl, date -s and umount.  Group= only
# changes the group of the files it creates, so a lock or state file it makes
# first stays writable by the daemons running as $INSTALL_USER.
Group=$INSTALL_GROUP
UMask=0007
WorkingDirectory=$BASE_DIR
EnvironmentFile=-$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=INSTALLATION_UI_HOST=0.0.0.0
Environment=INSTALLATION_UI_PORT=8088
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
  echo "Control UI installed on port 8088."
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

restart_all() {
  local service
  for service in "${CDSP_SERVICES[@]}"; do
    sudo systemctl restart "${service}.service" || true
  done
}

refresh_installed_units() {
  if systemctl list-unit-files --no-legend cdsp-trigger.service 2>/dev/null | grep -q '^cdsp-trigger.service'; then
    create_unit "Trigger Control" trigger.py cdsp-trigger
  fi
  if systemctl list-unit-files --no-legend cdsp-motu-sync.service 2>/dev/null | grep -q '^cdsp-motu-sync.service'; then
    create_unit "MOTU Clock Sync" clock_sync.py cdsp-motu-sync
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
    sudo install -m 0644 "$SCRIPTS_DIR/speaker_profiles.py" "$SCRIPTS_DIR/audio_eq.py" /usr/local/libexec/
    install_receiver_sudoers
    create_unit "AirPlay Volume Bridge" airplay_volume_bridge.py airplay-volume-bridge --daemon
    configure_shairport_bridge || note_skip "AirPlay Shairport configuration: FAILED (previous settings were restored)"
  fi
  if systemctl list-unit-files --no-legend cdsp-remote.service 2>/dev/null | grep -q '^cdsp-remote.service'; then
    install_remote_sudoers
    create_unit "Remote Control" cdsp_remote.py cdsp-remote
  fi
  if [[ -f "$SPOTIFY_DROPIN_PATH" || -f "$LEGACY_SPOTIFY_DROPIN" ]]; then
    install_spotify_volume_sync || note_skip "Spotify volume sync: FAILED (see the build output above)"
  fi
  if systemctl list-unit-files --no-legend cdsp-control-ui.service 2>/dev/null | grep -q '^cdsp-control-ui.service'; then
    install_control_ui
    # After the UI restarted on the new environment, so the process that owns
    # the old directory can no longer write to it.
    migrate_audio_eq_backups
  fi
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

# Both the configured directories and the historical defaults, deduplicated: an
# operator may well have installed under one and be uninstalling under another,
# and rm -f on a path that holds no unit costs nothing.
unit_search_dirs() {
  local dir existing known
  local seen=()
  for dir in "$SYSTEMD_UNIT_DIR" "$LEGACY_UNIT_DIR" /etc/systemd/system /lib/systemd/system; do
    [[ -n "$dir" ]] || continue
    known=false
    for existing in ${seen[@]+"${seen[@]}"}; do
      if [[ "$existing" == "$dir" ]]; then
        known=true
      fi
    done
    if [[ "$known" == false ]]; then
      seen+=("$dir")
    fi
  done
  printf '%s\n' ${seen[@]+"${seen[@]}"}
}

remove_unit_file() {
  local name="$1" dir
  while IFS= read -r dir; do
    [[ -n "$dir" ]] || continue
    sudo rm -f "$dir/$name"
  done < <(unit_search_dirs)
}

uninstall_all() {
  echo "Uninstalling all utilities..."
  local service
  for service in "${CDSP_SERVICES[@]}"; do
    sudo systemctl stop "${service}.service" || true
    sudo systemctl disable "${service}.service" || true
    remove_unit_file "${service}.service"
  done
  sudo rm -f "$SUDOERS_DIR/cdsp-automation" "$SUDOERS_DIR/cdsp-automation-receivers"
  if [[ -x "$SCRIPTS_DIR/build_camilladsp_iso226.sh" ]]; then
    # The receipt is the builder's only proof that it - and not the operator -
    # installed /usr/local/bin/camilladsp, so it has to look where this
    # deployment actually keeps it or it would find nothing and skip its work.
    local iso_capability
    iso_capability="$(get_env_value ISO226_CAPABILITY_PATH)"
    : "${iso_capability:=$ISO226_CAPABILITY_DEFAULT}"
    ISO226_CAPABILITY_PATH="$iso_capability" \
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
  sudo rm -f /usr/local/libexec/airplay_volume_bridge.py \
    /usr/local/libexec/speaker_profiles.py /usr/local/libexec/audio_eq.py
  sudo systemctl daemon-reload
  echo "Uninstalled."
}

install_all() {
  reset_install_notes
  prepare_install
  install_trigger
  install_motu_sync
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
  migrate_env_defaults
  ensure_audio_state_storage
  ensure_venv
  source "$VENV_DIR/bin/activate"
  pip install --upgrade git+https://github.com/HEnquist/pycamilladsp.git
  pip install --upgrade websocket-client evdev pyyaml
  deactivate
  # Deliberately no engine rebuild: that would replace the running CamillaDSP
  # binary and restart it, which a routine update must never do.  Menu option 10
  # rebuilds it when the operator asks for it.
  local iso_capability
  iso_capability="$(get_env_value ISO226_CAPABILITY_PATH)"
  : "${iso_capability:=$ISO226_CAPABILITY_DEFAULT}"
  if [[ -f "$iso_capability" ]]; then
    echo "An ISO 226 engine is installed and was left running untouched."
    echo "Run menu option 10 to rebuild it against the downloaded pinned patch."
  fi
  refresh_installed_units
  restart_all
  echo "Update complete. User settings remain in $ENV_FILE."
  print_install_summary
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

print_menu() {
  cat <<MENU
=============================================
CamillaDSP Utilities - Choose an Option
=============================================
1)  Install All Utilities
2)  Update Utilities
3)  Install Trigger Control
4)  Install MOTU Clock Sync
5)  Install Source Switcher
6)  Install Remote Control
7)  Pair Bluetooth Remote
8)  Show Service Status
9)  Install AirPlay + Spotify Volume Sync
10) Install ISO 226 Loudness Engine
11) Uninstall All Utilities
12) Install Web Control UI (optional)
0)  Exit
Options 11 and 12 ask for confirmation before acting.
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
      4) prepare_install; install_motu_sync ;;
      5) prepare_install; install_source_switcher ;;
      6) prepare_install; install_remote ;;
      7) pair_bluetooth_remote ;;
      8) show_status ;;
      9) install_network_volume_sync ;;
      10) prepare_install; install_iso226_engine ;;
      11) if confirm_action "Remove all CamillaDSP utility services, units and sudoers rules?"; then uninstall_all; else echo "Cancelled."; fi ;;
      12) if confirm_action "Install an unauthenticated root web server on 0.0.0.0:8088?"; then prepare_install; install_control_ui; else echo "Cancelled."; fi ;;
      0) echo "Exiting."; exit 0 ;;
      *) echo "Invalid choice" ;;
    esac
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
