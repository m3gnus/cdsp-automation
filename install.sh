#!/bin/bash
# CamillaDSP Utilities Setup Script
# Installs: Trigger Control, MOTU Clock Sync, Source Switcher, and Remote Control
set -e
BASE_DIR="$HOME/camilladsp"
SCRIPTS_DIR="$BASE_DIR/scripts"
CONFIGS_DIR="$BASE_DIR/configs"
VENV_DIR="$BASE_DIR/.venv"
BASE_URL="https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/scripts"

ensure_venv() {
  if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtualenv at $VENV_DIR"
    python3 -m venv --system-site-packages "$VENV_DIR"
  fi
}

install_dependencies() {
  echo "📦 Installing Python dependencies..."
  ensure_venv
  # Activate venv for pip installs
  source "${VENV_DIR}/bin/activate"
  pip install --upgrade pip
  pip install websocket-client
  pip install evdev
  pip install git+https://github.com/HEnquist/pycamilladsp.git
  deactivate
  sudo apt update || true
  sudo apt install -y python3-rpi-lgpio || true
  echo "✅ Dependencies installed!"
}

download_scripts() {
  echo "📥 Downloading scripts from GitHub..."
  mkdir -p "$SCRIPTS_DIR"
  mkdir -p "$CONFIGS_DIR"
  wget -q "$BASE_URL/trigger.py" -O "$SCRIPTS_DIR/trigger.py"
  wget -q "$BASE_URL/clock_sync.py" -O "$SCRIPTS_DIR/clock_sync.py"
  wget -q "$BASE_URL/source_switcher.py" -O "$SCRIPTS_DIR/source_switcher.py"
  wget -q "$BASE_URL/cdsp_remote.py" -O "$SCRIPTS_DIR/cdsp_remote.py"
  chmod +x "$SCRIPTS_DIR"/*.py
  echo "✅ Scripts downloaded!"
}

create_unit() {
  local name="$1"; shift
  local exec="$1"; shift
  local sysname="$1"; shift
  cat > "$HOME/${sysname}.service" <<EOL
[Unit]
Description=CamillaDSP $name
After=default.target camilladsp.service
[Service]
User=$USER
Type=simple
WorkingDirectory=/home/$USER
ExecStart=$exec
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$sysname
[Install]
WantedBy=default.target
EOL
  sudo mv "$HOME/${sysname}.service" /lib/systemd/system/${sysname}.service
  sudo systemctl daemon-reload
  # enable and start immediately
  sudo systemctl enable --now ${sysname}.service
}

install_trigger() {
  echo "🔌 Installing Trigger Control..."
  create_unit "Trigger Control" "$VENV_DIR/bin/python3 $SCRIPTS_DIR/trigger.py" cdsp-trigger
  echo "✅ Trigger Control installed!"
}

install_motu_sync() {
  echo "🎚️ Installing MOTU Clock Sync..."
  read -p "Enter your MOTU device IP address (found under Settings -> About on the MOTU device web UI) (default: 169.254.51.193): " motu_ip
  motu_ip=${motu_ip:-169.254.51.193}
  # Update IP in the script if present
  if [ -f "$SCRIPTS_DIR/clock_sync.py" ]; then
    sed -i "s/169.254.51.193/$motu_ip/g" "$SCRIPTS_DIR/clock_sync.py" || true
  fi
  create_unit "MOTU Clock Sync" "$VENV_DIR/bin/python3 $SCRIPTS_DIR/clock_sync.py" cdsp-motu-sync
  echo "✅ MOTU Clock Sync installed!"
}

install_source_switcher() {
  echo "🔄 Installing Source Switcher..."
  mkdir -p "$CONFIGS_DIR"
  create_unit "Source Switcher" "$VENV_DIR/bin/python3 $SCRIPTS_DIR/source_switcher.py" cdsp-source-switcher
  echo ""
  echo "⚠️  IMPORTANT: You need to create three config files:"
  echo "   - $CONFIGS_DIR/toslink.yml"
  echo "   - $CONFIGS_DIR/streamer.yml"
  echo "   - $CONFIGS_DIR/gadget.yml"
  echo ""
  echo "✅ Source Switcher installed!"
}

install_remote() {
  echo "🎮 Installing Remote Control..."

  # Check if evdev is installed
  source "${VENV_DIR}/bin/activate"
  pip install evdev
  deactivate

  # Ask for remote name
  echo ""
  echo "To find your remote's device name, run:"
  echo "  python3 -c \"import evdev; print([d.name for d in [evdev.InputDevice(p) for p in evdev.list_devices()]])\""
  echo ""
  read -p "Enter your remote device name (default: HID Remote01 Keyboard): " remote_name
  remote_name=${remote_name:-HID Remote01 Keyboard}

  # Update remote name in the script
  if [ -f "$SCRIPTS_DIR/cdsp_remote.py" ]; then
    sed -i "s/REMOTE_NAME = \"HID Remote01 Keyboard\"/REMOTE_NAME = \"$remote_name\"/g" "$SCRIPTS_DIR/cdsp_remote.py" || true
  fi

  create_unit "Remote Control" "$VENV_DIR/bin/python3 $SCRIPTS_DIR/cdsp_remote.py" cdsp-remote
  echo ""
  echo "📋 Remote Control Button Mapping:"
  echo "   - VOLUME UP/DOWN: Adjust volume (±1 dB)"
  echo "   - MUTE: Toggle mute"
  echo "   - UP/DOWN arrows: Adjust treble (±0.5 dB)"
  echo "   - LEFT/RIGHT arrows: Adjust bass (±0.5 dB)"
  echo "   - ENTER (short): Show current status"
  echo "   - ENTER (hold ~1s): Reset bass/treble to 0 dB"
  echo "   - POWER (hold ~1s): Restart all services"
  echo "   - POWER (hold ~10s): Shutdown system"
  echo ""
  echo "⚠️  NOTE: Your CamillaDSP config must have 'Bass' and 'Treble' filters"
  echo "   for tone controls to work."
  echo ""
  echo "✅ Remote Control installed!"
}

start_all() {
  sudo systemctl start cdsp-trigger || true
  sudo systemctl start cdsp-motu-sync || true
  sudo systemctl start cdsp-source-switcher || true
  sudo systemctl start cdsp-remote || true
}

show_status() {
  echo "\n=============================================
📊 Service Status:
=============================================
"
  systemctl status cdsp-trigger --no-pager || true
  systemctl status cdsp-motu-sync --no-pager || true
  systemctl status cdsp-source-switcher --no-pager || true
  systemctl status cdsp-remote --no-pager || true
}

uninstall_all() {
  echo "🧹 Uninstalling all utilities..."
  sudo systemctl stop cdsp-trigger cdsp-motu-sync cdsp-source-switcher cdsp-remote || true
  sudo systemctl disable cdsp-trigger cdsp-motu-sync cdsp-source-switcher cdsp-remote || true
  sudo rm -f /lib/systemd/system/cdsp-trigger.service /lib/systemd/system/cdsp-motu-sync.service /lib/systemd/system/cdsp-source-switcher.service /lib/systemd/system/cdsp-remote.service || true
  sudo systemctl daemon-reload
  echo "✅ Uninstalled."
}

update_utilities() {
  echo "🔄 Updating utilities (scripts + pycamilladsp)..."
  download_scripts
  ensure_venv
  source "$VENV_DIR/bin/activate"
  pip install --upgrade git+https://github.com/HEnquist/pycamilladsp.git || true
  pip install --upgrade evdev || true
  deactivate
  sudo systemctl restart cdsp-trigger cdsp-motu-sync cdsp-source-switcher cdsp-remote || true
  echo "✅ Update complete."
}

pair_bluetooth_remote() {
  echo "🔍 Pairing Bluetooth Remote..."
  echo ""
  echo "Starting Bluetooth scan for 10 seconds..."

  bluetoothctl power on
  bluetoothctl scan on &
  sleep 10
  bluetoothctl scan off

  echo ""
  echo "📜 Available devices:"
  mapfile -t devices < <(bluetoothctl devices | awk '{print $2 " " substr($0, index($0,$3))}')

  if [[ ${#devices[@]} -eq 0 ]]; then
    echo "❌ No Bluetooth devices found."
    return
  fi

  # Display devices with numbers
  for i in "${!devices[@]}"; do
    echo "$((i+1))) ${devices[i]}"
  done

  # Ask the user to select a device
  read -p "Enter the number of your Bluetooth remote: " num
  if ! [[ "$num" =~ ^[0-9]+$ ]] || (( num < 1 || num > ${#devices[@]} )); then
    echo "❌ Invalid selection. Pairing aborted."
    return
  fi

  # Extract selected MAC address and device name
  selected_device="${devices[num-1]}"
  mac_address=$(echo "$selected_device" | awk '{print $1}')
  device_name=$(echo "$selected_device" | cut -d' ' -f2-)

  echo "🔗 Pairing with $device_name ($mac_address)..."

  bluetoothctl pair "$mac_address"
  bluetoothctl connect "$mac_address"
  bluetoothctl trust "$mac_address"

  echo "✅ Bluetooth remote paired successfully!"
  echo ""
  echo "Your remote should now appear when you run:"
  echo "  python3 -c \"import evdev; print([d.name for d in [evdev.InputDevice(p) for p in evdev.list_devices()]])\""
}

print_menu() {
  cat <<MENU
=============================================
🎵 CamillaDSP Utilities Installation
=============================================

=============================================
🎵 CamillaDSP Utilities - Choose an Option:
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
  while true; do
    print_menu
    read -p "Enter your choice: " choice
    case "$choice" in
      1) install_dependencies; download_scripts; install_trigger; install_motu_sync; install_source_switcher; install_remote; ;;
      2) update_utilities; ;;
      3) install_trigger; ;;
      4) install_motu_sync; ;;
      5) install_source_switcher; ;;
      6) install_dependencies; download_scripts; install_remote; ;;
      7) pair_bluetooth_remote; ;;
      8) show_status; ;;
      9) uninstall_all; ;;
      0) echo "Exiting."; exit 0; ;;
      *) echo "Invalid choice"; ;;
    esac
  done
}

main
