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

CDSP_SERVICES=(
  cdsp-trigger
  cdsp-motu-sync
  cdsp-source-switcher
  cdsp-remote
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
MOTU_OPTICAL_RATE=48000
SOURCE_CHECK_INTERVAL=1.0
SOURCE_IDLE_TIMEOUT=60
SOURCE_AUDIO_THRESHOLD_DB=-80
SOURCE_OVERRIDE_PATH=/run/cdsp-source-switcher/manual_source
SOURCE_DEBUG=false
REMOTE_NAME=HID Remote01 Keyboard
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
  sudo apt install -y python3-venv python3-rpi-lgpio alsa-utils bluez wget

  ensure_venv
  # Activate venv for pip installs.
  source "${VENV_DIR}/bin/activate"
  pip install --upgrade pip
  pip install --upgrade websocket-client evdev git+https://github.com/HEnquist/pycamilladsp.git
  deactivate
  echo "Dependencies installed."
}

download_scripts() {
  echo "Downloading scripts from GitHub..."
  ensure_env_file
  migrate_legacy_settings
  local script tmp
  for script in trigger.py clock_sync.py source_switcher.py cdsp_remote.py; do
    tmp="${SCRIPTS_DIR}/${script}.tmp"
    wget -q "${BASE_URL}/${script}" -O "$tmp"
    mv "$tmp" "${SCRIPTS_DIR}/${script}"
  done
  chmod +x "$SCRIPTS_DIR"/*.py
  echo "Scripts downloaded."
}

prepare_install() {
  install_dependencies
  download_scripts
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
  local unit_file="$HOME/${sysname}.service"

  cat > "$unit_file" <<EOL
[Unit]
Description=CamillaDSP $name
Wants=network-online.target camilladsp.service
After=network-online.target camilladsp.service

[Service]
User=$USER
Type=simple
WorkingDirectory=$BASE_DIR
EnvironmentFile=-$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python3 -u $SCRIPTS_DIR/$script
Restart=always
RestartSec=2
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$sysname

[Install]
WantedBy=multi-user.target
EOL
  sudo install -m 0644 "$unit_file" "/etc/systemd/system/${sysname}.service"
  rm -f "$unit_file"
  sudo systemctl daemon-reload
  sudo systemctl enable --now "${sysname}.service"
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
  local systemctl_bin systemd_run_bin shutdown_bin tmp
  systemctl_bin="$(command -v systemctl)"
  systemd_run_bin="$(command -v systemd-run)"
  shutdown_bin="$(command -v shutdown || true)"
  if [[ -z "$shutdown_bin" ]]; then
    for candidate in /sbin/shutdown /usr/sbin/shutdown; do
      if [[ -x "$candidate" ]]; then
        shutdown_bin="$candidate"
        break
      fi
    done
  fi
  if [[ -z "$shutdown_bin" ]]; then
    echo "Could not find shutdown binary"
    return 1
  fi
  tmp="$(mktemp)"

  cat > "$tmp" <<EOF
# Allow cdsp-remote.service to perform only its documented power actions.
$USER ALL=(root) NOPASSWD: $systemctl_bin restart camilladsp.service, $systemctl_bin restart camillagui.service, $systemctl_bin restart cdsp-motu-sync.service, $systemctl_bin restart cdsp-source-switcher.service, $systemctl_bin restart cdsp-trigger.service, $systemctl_bin restart cdsp-remote.service, $systemd_run_bin --no-block --on-active=* --unit=cdsp-trigger-restart-* $systemctl_bin restart cdsp-trigger.service, $shutdown_bin -h now
EOF
  sudo visudo -cf "$tmp"
  sudo install -m 0440 "$tmp" /etc/sudoers.d/cdsp-automation
  rm -f "$tmp"
}

install_remote() {
  echo "Installing Remote Control..."

  if getent group input >/dev/null; then
    sudo usermod -aG input "$USER"
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
  sudo systemctl daemon-reload
  echo "Uninstalled."
}

update_utilities() {
  echo "Updating utilities (scripts + pycamilladsp)..."
  download_scripts
  ensure_venv
  source "$VENV_DIR/bin/activate"
  pip install --upgrade git+https://github.com/HEnquist/pycamilladsp.git
  pip install --upgrade websocket-client evdev
  deactivate
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
9)  Uninstall All Utilities
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
      1) prepare_install; install_trigger; install_motu_sync; install_source_switcher; install_remote ;;
      2) update_utilities ;;
      3) prepare_install; install_trigger ;;
      4) prepare_install; install_motu_sync ;;
      5) prepare_install; install_source_switcher ;;
      6) prepare_install; install_remote ;;
      7) pair_bluetooth_remote ;;
      8) show_status ;;
      9) uninstall_all ;;
      0) echo "Exiting."; exit 0 ;;
      *) echo "Invalid choice" ;;
    esac
  done
}

main
