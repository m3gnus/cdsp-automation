#!/bin/bash

# CamillaDSP Utilities Setup Script
# Installs: Trigger Control, MOTU Clock Sync, and Source Switcher

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
After=default.target

[Service]
User=$USER
Type=simple
WorkingDirectory=/home/$USER
ExecStart=$exec
#Restart=always
#RestartSec=10
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

start_all() {
  sudo systemctl start cdsp-trigger || true
  sudo systemctl start cdsp-motu-sync || true
  sudo systemctl start cdsp-source-switcher || true
}

show_status() {
  echo "\n=============================================
📊 Service Status:
=============================================
"
  systemctl status cdsp-trigger --no-pager || true
  systemctl status cdsp-motu-sync --no-pager || true
  systemctl status cdsp-source-switcher --no-pager || true
}

uninstall_all() {
  echo "🧹 Uninstalling all utilities..."
  sudo systemctl stop cdsp-trigger cdsp-motu-sync cdsp-source-switcher || true
  sudo systemctl disable cdsp-trigger cdsp-motu-sync cdsp-source-switcher || true
  sudo rm -f /lib/systemd/system/cdsp-trigger.service /lib/systemd/system/cdsp-motu-sync.service /lib/systemd/system/cdsp-source-switcher.service || true
  sudo systemctl daemon-reload
  echo "✅ Uninstalled."
}

update_utilities() {
  echo "🔄 Updating utilities (scripts + pycamilladsp)..."
  download_scripts
  ensure_venv
  source "$VENV_DIR/bin/activate"
  pip install --upgrade git+https://github.com/HEnquist/pycamilladsp.git || true
  deactivate
  sudo systemctl restart cdsp-trigger cdsp-motu-sync cdsp-source-switcher || true
  echo "✅ Update complete."
}

print_menu() {
  cat <<MENU
=============================================
🎵 CamillaDSP Utilities Installation
=============================================\n\n=============================================
🎵 CamillaDSP Utilities - Choose an Option:
=============================================
1)  Install All Utilities
2)  Update Utilities
3)  Install Trigger Control
4)  Install MOTU Clock Sync
5)  Install Source Switcher
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
      1) install_dependencies; download_scripts; install_trigger; install_motu_sync; install_source_switcher; ;;
      2) update_utilities; ;;
      3) install_trigger; ;;
      4) install_motu_sync; ;;
      5) install_source_switcher; ;;
      8) show_status; ;;
      9) uninstall_all; ;;
      0) echo "Exiting."; exit 0; ;;
      *) echo "Invalid choice"; ;;
    esac
  done
}

main
