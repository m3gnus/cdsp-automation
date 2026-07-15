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
SYSTEMD_UNIT_DIR="${CDSP_AUTOMATION_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
LEGACY_UNIT_DIR="${CDSP_AUTOMATION_LEGACY_UNIT_DIR:-/lib/systemd/system}"
SYSTEMCTL_BIN="/usr/bin/systemctl"
VISUDO_BIN="/usr/sbin/visudo"

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
POWER_GPIO=4
TRIGGER_DELAY_SECONDS=320
TRIGGER_CHECK_INTERVAL=0.2
TRIGGER_AUDIO_THRESHOLD_DB=-80
MOTU_WS_URL=ws://169.254.51.193:1280
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
AUDIO_CONTROL_LOCK_PATH=/var/lib/cdsp-automation/audio-control.lock
AUDIO_READY_PATH=/run/cdsp-source-switcher/audio-ready.json
AUDIO_EQ_REAPPLY_SECONDS=1.0
SPEAKER_SELECTION_PATH=/var/lib/cdsp-automation/speaker-selection.json
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
ISO226_CAPABILITY_PATH=/var/lib/cdsp-automation/iso226-engine.json
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
  local escaped_value
  ensure_env_file
  escaped_value=${value//\\/\\\\}
  escaped_value=${escaped_value//&/\\&}
  escaped_value=${escaped_value//|/\\|}
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${escaped_value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

get_env_value() {
  local key="$1"
  grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2-
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
  if ! rustc --version 2>/dev/null | awk '{print $2}' | awk -F. '{exit !($1 > 1 || ($1 == 1 && $2 >= 90))}'; then
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
  migrate_legacy_settings
  local script tmp
  for script in trigger.py clock_sync.py source_switcher.py cdsp_remote.py audio_eq.py speaker_profiles.py speaker_config.py airplay_volume_bridge.py configure_shairport.py; do
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
  mkdir -p "$BASE_DIR/camilladsp-iso226"
  if [[ -f "$REPO_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch" ]]; then
    cp "$REPO_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch" "$BASE_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch.tmp"
  else
    wget -q "https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch" -O "$BASE_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch.tmp"
  fi
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
    sudo install -d -m 0750 -o "$USER" -g "$USER" "$dir"
  elif ! sudo -u "$USER" test -w "$dir"; then
    echo "Configured state directory is not writable by $USER: $dir" >&2
    echo "Use a dedicated application directory; existing directory ownership is never changed." >&2
    return 1
  fi
}

ensure_audio_state_storage() {
  local audio_eq_path audio_control_lock_path speaker_selection_path speaker_transition_path speaker_audio_dir speaker_profile_dir source_base_dir generated_dir lock
  audio_eq_path="$(get_env_value AUDIO_EQ_PATH)"
  audio_control_lock_path="$(get_env_value AUDIO_CONTROL_LOCK_PATH)"
  speaker_selection_path="$(get_env_value SPEAKER_SELECTION_PATH)"
  speaker_transition_path="$(get_env_value SPEAKER_TRANSITION_PATH)"
  speaker_audio_dir="$(get_env_value SPEAKER_AUDIO_DIR)"
  speaker_profile_dir="$(get_env_value SPEAKER_PROFILE_DIR)"
  source_base_dir="$(get_env_value SOURCE_BASE_DIR)"
  generated_dir="$(get_env_value SPEAKER_GENERATED_DIR)"
  : "${audio_eq_path:=/var/lib/cdsp-automation/audio-eq.json}"
  : "${audio_control_lock_path:=/var/lib/cdsp-automation/audio-control.lock}"
  : "${speaker_selection_path:=/var/lib/cdsp-automation/speaker-selection.json}"
  : "${speaker_transition_path:=/var/lib/cdsp-automation/speaker-transition.json}"
  : "${speaker_audio_dir:=/var/lib/cdsp-automation/speaker-audio}"
  : "${speaker_profile_dir:=/etc/cdsp-automation/speaker-profiles}"
  : "${source_base_dir:=/etc/cdsp-automation/source-bases}"
  : "${generated_dir:=/var/lib/cdsp-automation/generated-configs}"
  ensure_user_writable_dir "$(dirname "$audio_eq_path")"
  ensure_user_writable_dir "$(dirname "$speaker_selection_path")"
  ensure_user_writable_dir "$(dirname "$speaker_transition_path")"
  ensure_user_writable_dir "$speaker_audio_dir"
  ensure_user_writable_dir "$generated_dir"
  if [[ ! -d "$speaker_profile_dir" ]]; then
    sudo install -d -m 0755 "$speaker_profile_dir"
  fi
  if [[ ! -d "$source_base_dir" ]]; then
    sudo install -d -m 0755 "$source_base_dir"
  fi
  for lock in \
    "${audio_eq_path}.lock" \
    "$audio_control_lock_path" \
    "${speaker_selection_path}.lock" \
    "${speaker_audio_dir}/kantarellen.json.lock" \
    "${speaker_audio_dir}/partymeh.json.lock" \
    "${speaker_audio_dir}/measurement.json.lock" \
    "${speaker_audio_dir}/partymeh_bird.json.lock"; do
    if [[ ! -e "$lock" ]]; then
      sudo -u "$USER" touch "$lock"
    fi
    sudo chown "$USER:$USER" "$lock"
    sudo chmod 0660 "$lock"
  done
}

migrate_legacy_settings() {
  local legacy_motu legacy_remote

  if [[ -f "$SCRIPTS_DIR/clock_sync.py" ]]; then
    legacy_motu=$(sed -n 's/^MOTU_WS_URL = "\(.*\)"/\1/p' "$SCRIPTS_DIR/clock_sync.py" | head -n 1)
    if [[ -n "$legacy_motu" && "$(get_env_value MOTU_WS_URL)" == "ws://169.254.51.193:1280" ]]; then
      set_env_value "MOTU_WS_URL" "$legacy_motu"
    fi
  fi

  if [[ -f "$SCRIPTS_DIR/cdsp_remote.py" ]]; then
    legacy_remote=$(sed -n 's/^REMOTE_NAME = "\(.*\)"/\1/p' "$SCRIPTS_DIR/cdsp_remote.py" | head -n 1)
    if [[ -n "$legacy_remote" && "$(get_env_value REMOTE_NAME)" == "HID Remote01 Keyboard" ]]; then
      set_env_value "REMOTE_NAME" "$legacy_remote"
    fi
  fi
}

create_unit() {
  local name="$1"
  local script="$2"
  local sysname="$3"
  local script_args="${4:-}"
  local unit_file
  unit_file="$(mktemp "${TMPDIR:-/tmp}/${sysname}.service.XXXXXX")"
  local runtime_directory=""
  if [[ "$sysname" == "cdsp-source-switcher" ]]; then
    runtime_directory=$'RuntimeDirectory=cdsp-source-switcher\nRuntimeDirectoryMode=0755\nRuntimeDirectoryPreserve=yes'
  elif [[ "$sysname" == "airplay-volume-bridge" ]]; then
    runtime_directory=$'RuntimeDirectory=airplay-volume-bridge\nRuntimeDirectoryMode=0755'
  fi

  cat > "$unit_file" <<EOL
[Unit]
Description=CamillaDSP $name
Wants=network-online.target camilladsp.service
After=network-online.target camilladsp.service

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
# Allow cdsp-remote.service to perform only its documented power actions.
$INSTALL_USER ALL=(root) NOPASSWD: $SYSTEMCTL_BIN restart camilladsp.service, $SYSTEMCTL_BIN restart camillagui.service, $SYSTEMCTL_BIN restart cdsp-motu-sync.service, $SYSTEMCTL_BIN restart cdsp-source-switcher.service, $SYSTEMCTL_BIN --no-block restart cdsp-remote.service, $SYSTEMCTL_BIN poweroff
EOF
  sudo "$VISUDO_BIN" -cf "$tmp"
  sudo install -m 0440 "$tmp" /etc/sudoers.d/cdsp-automation
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
  echo "   - POWER (hold ~1s): Restart all services"
  echo "   - POWER (hold ~10s): Shutdown system"
  echo ""
  echo "NOTE: log out/in or reboot if this installer just added your user to the input group."
  echo "Remote Control installed."
}

install_airplay_volume_bridge() {
  local shairport_config="/etc/shairport-sync.conf"
  echo "Installing AirPlay volume bridge daemon and system callback..."
  sudo install -d -m 0755 /usr/local/libexec
  sudo install -m 0755 "$SCRIPTS_DIR/airplay_volume_bridge.py" /usr/local/libexec/airplay_volume_bridge.py
  sudo install -m 0644 "$SCRIPTS_DIR/speaker_profiles.py" "$SCRIPTS_DIR/audio_eq.py" /usr/local/libexec/
  create_unit "AirPlay Volume Bridge" airplay_volume_bridge.py airplay-volume-bridge --daemon
  sudo systemctl daemon-reload
  sudo systemctl enable --now airplay-volume-bridge.service
  if [[ ! -f "$shairport_config" ]]; then
    echo "Shairport Sync config not found at $shairport_config; script installed, config unchanged."
    return
  fi
  sudo /usr/bin/python3 "$SCRIPTS_DIR/configure_shairport.py" "$shairport_config" "/usr/bin/python3 /usr/local/libexec/airplay_volume_bridge.py --notify"
  if command -v shairport-sync >/dev/null && ! sudo timeout 10 shairport-sync --displayConfig >/dev/null; then
    sudo /usr/bin/python3 "$SCRIPTS_DIR/configure_shairport.py" --remove "$shairport_config"
    echo "Shairport validation failed; restored its previous volume settings." >&2
    return 1
  fi
  if ! sudo systemctl restart shairport-sync.service; then
    sudo /usr/bin/python3 "$SCRIPTS_DIR/configure_shairport.py" --remove "$shairport_config"
    sudo systemctl restart shairport-sync.service || true
    echo "Shairport restart failed; restored its previous volume settings." >&2
    return 1
  fi
  echo "Shairport Sync now sends unity audio and controls the CamillaDSP master fader."
}

install_iso226_engine() {
  echo "Building the pinned CamillaDSP 4.1.3 ISO 226 engine..."
  "$SCRIPTS_DIR/build_camilladsp_iso226.sh" "$BASE_DIR/camilladsp-iso226/camilladsp-v4.1.3-iso226.patch"
  if systemctl list-unit-files --no-legend cdsp-source-switcher.service 2>/dev/null | grep -q '^cdsp-source-switcher.service'; then
    sudo systemctl restart cdsp-source-switcher.service
  fi
  echo "ISO 226 engine installed and active."
}

start_all() {
  local service
  for service in "${CDSP_SERVICES[@]}"; do
    sudo systemctl start "${service}.service" || true
  done
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
  if systemctl list-unit-files --no-legend cdsp-remote.service 2>/dev/null | grep -q '^cdsp-remote.service'; then
    install_remote_sudoers
    create_unit "Remote Control" cdsp_remote.py cdsp-remote
  fi
  if systemctl list-unit-files --no-legend airplay-volume-bridge.service 2>/dev/null | grep -q '^airplay-volume-bridge.service'; then
    sudo install -d -m 0755 /usr/local/libexec
    sudo install -m 0755 "$SCRIPTS_DIR/airplay_volume_bridge.py" /usr/local/libexec/airplay_volume_bridge.py
    sudo install -m 0644 "$SCRIPTS_DIR/speaker_profiles.py" "$SCRIPTS_DIR/audio_eq.py" /usr/local/libexec/
    create_unit "AirPlay Volume Bridge" airplay_volume_bridge.py airplay-volume-bridge --daemon
  fi
}

show_status() {
  echo ""
  echo "============================================="
  echo "Service Status"
  echo "============================================="
  local service
  for service in "${CDSP_SERVICES[@]}"; do
    systemctl status "${service}.service" --no-pager || true
  done
}

uninstall_all() {
  echo "Uninstalling all utilities..."
  local service
  for service in "${CDSP_SERVICES[@]}"; do
    sudo systemctl stop "${service}.service" || true
    sudo systemctl disable "${service}.service" || true
    sudo rm -f "/etc/systemd/system/${service}.service" "/lib/systemd/system/${service}.service"
  done
  sudo rm -f /etc/sudoers.d/cdsp-automation
  if [[ -x "$SCRIPTS_DIR/build_camilladsp_iso226.sh" ]]; then
    "$SCRIPTS_DIR/build_camilladsp_iso226.sh" --uninstall || true
  fi
  if [[ -f /etc/shairport-sync.conf && -f "$SCRIPTS_DIR/configure_shairport.py" ]]; then
    sudo /usr/bin/python3 "$SCRIPTS_DIR/configure_shairport.py" --remove /etc/shairport-sync.conf
    sudo systemctl restart shairport-sync.service || true
  fi
  sudo rm -f /usr/local/libexec/airplay_volume_bridge.py \
    /usr/local/libexec/speaker_profiles.py /usr/local/libexec/audio_eq.py
  sudo systemctl daemon-reload
  echo "Uninstalled."
}

update_utilities() {
  echo "Updating utilities (scripts + pycamilladsp)..."
  download_scripts
  ensure_audio_state_storage
  ensure_venv
  source "$VENV_DIR/bin/activate"
  pip install --upgrade git+https://github.com/HEnquist/pycamilladsp.git
  pip install --upgrade websocket-client evdev pyyaml
  deactivate
  if [[ -f /var/lib/cdsp-automation/iso226-engine.json ]]; then
    echo "An ISO 226 engine is installed; rebuilding it against the downloaded pinned patch."
    install_iso226_engine
  fi
  refresh_installed_units
  restart_all
  echo "Update complete. User settings remain in $ENV_FILE."
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
9)  Install AirPlay Volume Bridge
10) Install ISO 226 Loudness Engine
11) Uninstall All Utilities
0)  Exit
=============================================
MENU
}

main() {
  ensure_env_file
  while true; do
    print_menu
    read -r -p "Enter your choice: " choice
    case "$choice" in
      1) prepare_install; install_iso226_engine; install_trigger; install_motu_sync; install_source_switcher; install_remote; install_airplay_volume_bridge ;;
      2) update_utilities ;;
      3) prepare_install; install_trigger ;;
      4) prepare_install; install_motu_sync ;;
      5) prepare_install; install_source_switcher ;;
      6) prepare_install; install_remote ;;
      7) pair_bluetooth_remote ;;
      8) show_status ;;
      9) prepare_install; install_airplay_volume_bridge ;;
      10) prepare_install; install_iso226_engine ;;
      11) uninstall_all ;;
      0) echo "Exiting."; exit 0 ;;
      *) echo "Invalid choice" ;;
    esac
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
