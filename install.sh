#!/bin/bash

###  SSH to Raspberry Pi 
###  wget https://raw.githubusercontent.com/GITHUB_USER/REPO_NAME/main/install.sh -O install.sh
###  chmod +x install.sh && ./install.sh

# CamillaDSP Utilities Setup Script
# Installs: Trigger Control, MOTU Clock Sync, and Source Switcher

echo "============================================="
echo "🎵 CamillaDSP Utilities Installation"
echo "============================================="

# Function to install Python dependencies
install_dependencies() {
    echo "📦 Installing Python dependencies..."
    
    if [ ! -d ~/camilladsp/.venv ]; then
        python3 -m venv --system-site-packages ~/camilladsp/.venv
    fi
    
    source ~/camilladsp/.venv/bin/activate
    
    pip3 install --upgrade pip
    pip3 install websocket-client
    pip3 install git+https://github.com/HEnquist/pycamilladsp.git
    
    sudo apt install -y python3-rpi-lgpio
    
    deactivate
    
    echo "✅ Dependencies installed!"
}

# Function to download scripts from GitHub
download_scripts() {
    echo "📥 Downloading scripts from GitHub..."
    mkdir -p ~/camilladsp/scripts
    
    BASE_URL="https://raw.githubusercontent.com/GITHUB_USER/REPO_NAME/main/scripts"
    
    wget -q "$BASE_URL/trigger.py" -O ~/camilladsp/scripts/trigger.py
    wget -q "$BASE_URL/clock_sync.py" -O ~/camilladsp/scripts/clock_sync.py
    wget -q "$BASE_URL/source_switcher.py" -O ~/camilladsp/scripts/source_switcher.py
    
    chmod +x ~/camilladsp/scripts/*.py
    
    echo "✅ Scripts downloaded!"
}

# Function to install Trigger Control
install_trigger() {
    echo "🔌 Installing Trigger Control..."
    
    cat > ~/trigger.service <<EOL
[Unit]
Description=CamillaDSP Trigger Control
After=camilladsp.service
Requires=camilladsp.service
StartLimitIntervalSec=10
StartLimitBurst=10

[Service]
User=$USER
Type=simple
WorkingDirectory=/home/$USER
ExecStart=/home/$USER/camilladsp/.venv/bin/python3 /home/$USER/camilladsp/scripts/trigger.py
Restart=always
RestartSec=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cdsp-trigger

[Install]
WantedBy=default.target
EOL

    sudo mv ~/trigger.service /lib/systemd/system/cdsp-trigger.service
    sudo systemctl daemon-reload
    sudo systemctl enable cdsp-trigger
    
    echo "✅ Trigger Control installed!"
}

# Function to install MOTU Clock Sync
install_motu_sync() {
    echo "🎚️ Installing MOTU Clock Sync..."
    
    read -p "Enter your MOTU device IP address (default: 169.254.51.193): " motu_ip
    motu_ip=${motu_ip:-169.254.51.193}
    
    # Update IP in the script
    sed -i "s/169.254.51.193/$motu_ip/g" ~/camilladsp/scripts/clock_sync.py
    
    cat > ~/motu-sync.service <<EOL
[Unit]
Description=CamillaDSP MOTU Clock Sync
After=camilladsp.service
Requires=camilladsp.service
StartLimitIntervalSec=10
StartLimitBurst=10

[Service]
User=$USER
Type=simple
WorkingDirectory=/home/$USER
ExecStart=/home/$USER/camilladsp/.venv/bin/python3 /home/$USER/camilladsp/scripts/clock_sync.py
Restart=always
RestartSec=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=motu-sync

[Install]
WantedBy=default.target
EOL

    sudo mv ~/motu-sync.service /lib/systemd/system/cdsp-motu-sync.service
    sudo systemctl daemon-reload
    sudo systemctl enable cdsp-motu-sync
    
    echo "✅ MOTU Clock Sync installed!"
}

# Function to install Source Switcher
install_source_switcher() {
    echo "🔄 Installing Source Switcher..."
    
    mkdir -p ~/camilladsp/configs
    
    cat > ~/source-switcher.service <<EOL
[Unit]
Description=CamillaDSP Source Switcher
After=camilladsp.service
Requires=camilladsp.service
StartLimitIntervalSec=10
StartLimitBurst=10

[Service]
User=$USER
Type=simple
WorkingDirectory=/home/$USER
ExecStart=/home/$USER/camilladsp/.venv/bin/python3 /home/$USER/camilladsp/scripts/source_switcher.py
Restart=always
RestartSec=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=source-switcher

[Install]
WantedBy=default.target
EOL

    sudo mv ~/source-switcher.service /lib/systemd/system/cdsp-source-switcher.service
    sudo systemctl daemon-reload
    sudo systemctl enable cdsp-source-switcher
    
    echo ""
    echo "⚠️  IMPORTANT: You need to create three config files:"
    echo "   - ~/camilladsp/configs/toslink.yml"
    echo "   - ~/camilladsp/configs/streamer.yml"
    echo "   - ~/camilladsp/configs/gadget.yml"
    echo ""
    
    echo "✅ Source Switcher installed!"
}

# Function to start all services
start_services() {
    echo "🚀 Starting services..."
    
    if systemctl list-unit-files | grep -q cdsp-trigger.service; then
        sudo systemctl start cdsp-trigger
        echo "✅ Trigger service started"
    fi
    
    if systemctl list-unit-files | grep -q cdsp-motu-sync.service; then
        sudo systemctl start cdsp-motu-sync
        echo "✅ MOTU sync service started"
    fi
    
    if systemctl list-unit-files | grep -q cdsp-source-switcher.service; then
        sudo systemctl start cdsp-source-switcher
        echo "✅ Source switcher service started"
    fi
}

# Function to show service status
show_status() {
    echo "============================================="
    echo "📊 Service Status:"
    echo "============================================="
    
    for service in cdsp-trigger cdsp-motu-sync cdsp-source-switcher; do
        if systemctl list-unit-files | grep -q ${service}.service; then
            echo ""
            echo "${service}:"
            systemctl status ${service} --no-pager -l | head -n 10
        fi
    done
}

# Function to uninstall utilities
uninstall_utilities() {
    echo "🗑️  Uninstalling CamillaDSP utilities..."
    
    for service in cdsp-trigger cdsp-motu-sync cdsp-source-switcher; do
        sudo systemctl stop ${service} 2>/dev/null
        sudo systemctl disable ${service} 2>/dev/null
        sudo rm -f /lib/systemd/system/${service}.service
    done
    
    rm -f ~/camilladsp/scripts/trigger.py
    rm -f ~/camilladsp/scripts/clock_sync.py
    rm -f ~/camilladsp/scripts/source_switcher.py
    
    sudo systemctl daemon-reload
    
    echo "✅ Utilities uninstalled!"
}

# Main menu loop
while true; do
    echo ""
    echo "============================================="
    echo "🎵 CamillaDSP Utilities - Choose an Option:"
    echo "============================================="
    echo "1)  Install Python Dependencies"
    echo "2)  Download Scripts from GitHub"
    echo "3)  Install Trigger Control"
    echo "4)  Install MOTU Clock Sync"
    echo "5)  Install Source Switcher"
    echo "6)  Install All Utilities"
    echo "7)  Start All Services"
    echo "8)  Show Service Status"
    echo "9)  Uninstall All Utilities"
    echo "0)  Exit"
    echo "============================================="
    read -p "Enter your choice: " choice

    case $choice in
        1) install_dependencies ;;
        2) download_scripts ;;
        3) install_dependencies && download_scripts && install_trigger ;;
        4) install_dependencies && download_scripts && install_motu_sync ;;
        5) install_dependencies && download_scripts && install_source_switcher ;;
        6) install_dependencies && download_scripts && install_trigger && install_motu_sync && install_source_switcher ;;
        7) start_services ;;
        8) show_status ;;
        9) uninstall_utilities ;;
        0) echo "👋 Setup complete!"; exit ;;
        *) echo "❌ Invalid option, please try again." ;;
    esac

    echo ""
    echo "🔁 Returning to the main menu..."
    sleep 2
done
