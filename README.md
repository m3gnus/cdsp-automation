# CamillaDSP Utilities for Raspberry Pi

Automation utilities for CamillaDSP on Raspberry Pi: trigger control, MOTU clock sync, and intelligent source switching.

## Quick Start

```bash
wget https://raw.githubusercontent.com/YOUR_USERNAME/camilladsp-utilities/main/install.sh
chmod +x install.sh
./install.sh
```

## Utilities

### 🔌 Trigger Control
Automatically controls a GPIO relay based on audio activity. Turns on immediately when music plays, turns off after 5 minutes of silence.

**Use case:** Power control for amplifiers via relay

### 🎚️ MOTU Clock Sync
Automatically switches MOTU interface clock source based on CamillaDSP sample rate. 48kHz = optical, others = internal.

**Use case:** Eliminates manual clock switching and prevents audio dropouts

### 🔄 Source Switcher
Intelligently switches between CamillaDSP configs based on active audio source.

**Priority:** AirPlay/Streamer → USB Gadget → TOSLINK (fallback)

**Use case:** Seamless switching between multiple audio sources

## Requirements

- Raspberry Pi (any model with GPIO for trigger control)
- CamillaDSP installed and running on port 1234
- Python 3.7+
- For MOTU sync: MOTU UltraLite on network
- For Source Switcher: Three config files (toslink.yml, streamer.yml, gadget.yml)

## Installation

The installer provides a menu-driven interface to:
- Install Python dependencies
- Install utilities individually or all at once
- Set up systemd services
- Start/stop services
- Check service status
- Uninstall utilities

## Manual Installation

If you prefer manual installation, see [MANUAL_INSTALL.md](MANUAL_INSTALL.md)

## Configuration

### Trigger Control
Edit `~/camilladsp/scripts/trigger.py`:
- `PowerGpio`: GPIO pin number (default: 4)
- `delay_time`: Seconds before turning off (default: 320)
- `check_interval`: Polling frequency (default: 0.2)

### MOTU Clock Sync
The installer will prompt for your MOTU IP address. To change it later, edit:
`~/camilladsp/scripts/clock_sync.py`

### Source Switcher
Edit `~/camilladsp/scripts/source_switcher.py`:
- `IDLE_TIMEOUT`: Seconds of silence before switching (default: 60)
- `RMS_DELTA_EPS`: RMS change threshold (default: 0.1)
- `DEBUG_MODE`: Enable verbose logging (default: False)

**Required config files:**
- `~/camilladsp/configs/toslink.yml`
- `~/camilladsp/configs/streamer.yml`
- `~/camilladsp/configs/gadget.yml`

## Managing Services

### View Status
```bash
systemctl status cdsp-trigger
systemctl status cdsp-motu-sync
systemctl status cdsp-source-switcher
```

### View Logs
```bash
journalctl -u cdsp-trigger -f
journalctl -u cdsp-motu-sync -f
journalctl -u cdsp-source-switcher -f
```

### Start/Stop
```bash
sudo systemctl start cdsp-trigger
sudo systemctl stop cdsp-trigger
sudo systemctl restart cdsp-trigger
```

### Enable/Disable Auto-start
```bash
sudo systemctl enable cdsp-trigger
sudo systemctl disable cdsp-trigger
```

## Troubleshooting

### Trigger not working
- Check GPIO wiring and relay connection
- Verify permissions: `ls -l /dev/gpiochip0`
- Check logs: `journalctl -u cdsp-trigger -n 50`

### MOTU sync not working
- Verify MOTU IP address is correct
- Test connection: `ping YOUR_MOTU_IP`
- Check if MOTU web interface is accessible
- Different MOTU models may need different hex payloads

### Source switcher not switching
- Enable DEBUG_MODE to see decision logic
- Verify config files exist and are valid
- Check hardware detection: `cat /proc/asound/Loopback/pcm0p0/sub0/status`
- For USB Gadget: `amixer -c UAC2Gadget contents`

## Technical Details

For detailed explanations of how each utility works and design decisions, see [TECHNICAL.md](TECHNICAL.md)

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Test your changes
4. Submit a pull request

## License

MIT License - see [LICENSE](LICENSE) for details

## Acknowledgments

- [CamillaDSP](https://github.com/HEnquist/camilladsp) by HEnquist
- [pycamilladsp](https://github.com/HEnquist/pycamilladsp) Python library

## Support

- Open an issue for bugs or feature requests
- Discussions welcome for questions and ideas
