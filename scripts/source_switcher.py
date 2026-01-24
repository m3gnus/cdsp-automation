import time
import os
import glob
import subprocess
from camilladsp import CamillaClient

# --- CONFIGURATION ---
CAMILLA_IP = "127.0.0.1"
CAMILLA_PORT = 1234
CHECK_INTERVAL = 1.0
RMS_DELTA_EPS = 0.1        # RMS change threshold to detect active audio
IDLE_TIMEOUT = 60          # Seconds of silence before switching to TOSLINK
SETTLE_TIME = 2.0          # Wait time after config switch for hardware
DEBUG_MODE = False         # Set to True for verbose logging

# Universal home directory detection
HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, "camilladsp/configs/")

TOSLINK_CFG  = os.path.join(CONFIG_DIR, "toslink.yml")
STREAMER_CFG = os.path.join(CONFIG_DIR, "streamer.yml")
GADGET_CFG   = os.path.join(CONFIG_DIR, "gadget.yml")

def is_alsa_active(card_name):
    """Checks if ALSA card is in RUNNING state."""
    paths = glob.glob(f"/proc/asound/{card_name}/pcm*/sub*/status")
    for path in paths:
        try:
            with open(path, "r") as f:
                if "state: RUNNING" in f.read():
                    return True
        except Exception:
            continue
    return False

def is_gadget_available():
    """Checks if USB Gadget capture rate is non-zero (device connected & ready)."""
    try:
        cmd = ["amixer", "-c", "UAC2Gadget", "contents"]
        res = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        
        in_capture_rate = False
        for line in res.splitlines():
            if "name='Capture Rate'" in line:
                in_capture_rate = True
            elif in_capture_rate and ": values=" in line:
                rate = int(line.split("values=")[1].strip())
                return rate > 0
    except Exception:
        pass
    return False

def apply_config(cdsp, file_path, settle_time=SETTLE_TIME):
    """Applies config and waits for hardware to settle."""
    if os.path.exists(file_path):
        config_name = os.path.basename(file_path)
        print(f">>> Switching to: {config_name}")
        cdsp.config.set_file_path(file_path)
        cdsp.general.reload()
        time.sleep(settle_time)
    else:
        print(f"ERROR: Config not found: {file_path}")

def main():
    print(">>> CamillaDSP Source Switcher Started <<<")
    print("Priority: 1) Streamer (AirPlay) → 2) USB Gadget → 3) TOSLINK")
    
    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    streamer_silence_timer = 0
    gadget_silence_timer = 0
    last_rms = None
    last_active_source = None  # Track which source was last active

    while True:
        try:
            if not cdsp.is_connected():
                cdsp.connect()
                print("Connected to CamillaDSP")

            current_config = cdsp.config.file_path()
            
            # Check hardware status
            streamer_hw_active = is_alsa_active("Loopback")
            gadget_hw_available = is_gadget_available()
            
            if DEBUG_MODE:
                print(f"DEBUG: Streamer HW={streamer_hw_active}, Gadget HW={gadget_hw_available}, Last={last_active_source}, ST={streamer_silence_timer}, GT={gadget_silence_timer}, Config={os.path.basename(current_config)}")
            
            # --- PRIORITY 1: STREAMER (AirPlay via ALSA Loopback) ---
            if streamer_hw_active:
                last_active_source = "streamer"
                
                # Switch to streamer if not already there
                if current_config != STREAMER_CFG:
                    apply_config(cdsp, STREAMER_CFG)
                    streamer_silence_timer = 0
                    gadget_silence_timer = 0
                    last_rms = None
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                # Already on streamer - check if audio is actually playing
                current_rms = cdsp.levels.capture_rms()
                
                # Check if RMS has changed (indicates active audio)
                rms_changed = False
                if last_rms is None and current_rms:
                    rms_changed = True
                elif current_rms and last_rms:
                    for a, b in zip(current_rms, last_rms):
                        if abs(a - b) > RMS_DELTA_EPS:
                            rms_changed = True
                            break
                
                last_rms = current_rms
                
                if rms_changed:
                    streamer_silence_timer = 0
                    if DEBUG_MODE:
                        print("→ Streamer: Audio active")
                else:
                    streamer_silence_timer += 1
                    if DEBUG_MODE and streamer_silence_timer % 5 == 0:
                        print(f"→ Streamer: Idle {streamer_silence_timer}/{IDLE_TIMEOUT}s")
                
                # Stay on streamer if still within timeout
                if streamer_silence_timer < IDLE_TIMEOUT:
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                # Timeout reached - clear last active and check other sources
                if DEBUG_MODE:
                    print("Streamer idle timeout - checking other sources")
                last_active_source = None
            
            # If streamer was last active but hardware stopped, give it grace period
            elif last_active_source == "streamer" and current_config == STREAMER_CFG:
                streamer_silence_timer += 1
                if DEBUG_MODE and streamer_silence_timer % 5 == 0:
                    print(f"→ Streamer: Grace period {streamer_silence_timer}/{IDLE_TIMEOUT}s")
                
                if streamer_silence_timer < IDLE_TIMEOUT:
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                if DEBUG_MODE:
                    print("Streamer grace period ended")
                last_active_source = None

            # --- PRIORITY 2: USB GADGET ---
            if gadget_hw_available:
                last_active_source = "gadget"
                
                # Switch to gadget if not already there
                if current_config != GADGET_CFG:
                    apply_config(cdsp, GADGET_CFG, settle_time=1.5)
                    gadget_silence_timer = 0
                    streamer_silence_timer = 0
                    last_rms = None
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                # Already on gadget - check if audio is actually playing
                current_rms = cdsp.levels.capture_rms()
                
                # Check if RMS has changed (indicates active audio)
                rms_changed = False
                if last_rms is None and current_rms:
                    rms_changed = True
                elif current_rms and last_rms:
                    for a, b in zip(current_rms, last_rms):
                        if abs(a - b) > RMS_DELTA_EPS:
                            rms_changed = True
                            break
                
                last_rms = current_rms
                
                if rms_changed:
                    gadget_silence_timer = 0
                    if DEBUG_MODE:
                        print("→ Gadget: Audio active")
                else:
                    gadget_silence_timer += 1
                    if DEBUG_MODE and gadget_silence_timer % 5 == 0:
                        print(f"→ Gadget: Idle {gadget_silence_timer}/{IDLE_TIMEOUT}s")
                
                # Stay on gadget if still within timeout
                if gadget_silence_timer < IDLE_TIMEOUT:
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                # Timeout reached
                if DEBUG_MODE:
                    print("Gadget idle timeout - switching to TOSLINK")
                last_active_source = None
            
            # If gadget was last active but hardware stopped, give it grace period
            elif last_active_source == "gadget" and current_config == GADGET_CFG:
                gadget_silence_timer += 1
                if DEBUG_MODE and gadget_silence_timer % 5 == 0:
                    print(f"→ Gadget: Grace period {gadget_silence_timer}/{IDLE_TIMEOUT}s")
                
                if gadget_silence_timer < IDLE_TIMEOUT:
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                if DEBUG_MODE:
                    print("Gadget grace period ended")
                last_active_source = None

            # --- PRIORITY 3: TOSLINK (Default fallback) ---
            if current_config != TOSLINK_CFG:
                apply_config(cdsp, TOSLINK_CFG)
                streamer_silence_timer = 0
                gadget_silence_timer = 0
                last_rms = None
                last_active_source = None
            elif DEBUG_MODE:
                print("→ TOSLINK (default)")

        except Exception as e:
            print(f"Error: {e}")
            time.sleep(2)

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
