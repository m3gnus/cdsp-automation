import websocket
import binascii
import time
from camilladsp import CamillaClient

# --- CONFIGURATION ---
MOTU_WS_URL = "ws://169.254.51.193:1280"
CAMILLA_IP = "127.0.0.1"
CAMILLA_PORT = 1234

# MOTU Clock Hex Payloads
CLOCK_PAYLOADS = {
    "internal": "000b0000000103",
    "optical":  "000b0000000102",
}

def set_motu_clock(source):
    if source not in CLOCK_PAYLOADS:
        return
    try:
        payload = binascii.unhexlify(CLOCK_PAYLOADS[source])
        ws = websocket.WebSocket()
        ws.connect(MOTU_WS_URL, timeout=3)
        ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
        ws.close()
        print(f"MOTU: Sample rate changed. Clock source set to {source}")
    except Exception as e:
        print(f"MOTU Error: {e}")

def main():
    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    last_rate = None

    print("MOTU Clock Sync (Sample Rate Mode) Started...")

    while True:
        try:
            if not cdsp.is_connected():
                cdsp.connect()
            
            # Get the full active configuration dictionary
            active_config = cdsp.config.active()
            
            if active_config:
                # Extract the samplerate from the config
                # Structure: active_config['devices']['samplerate']
                current_rate = active_config.get('devices', {}).get('samplerate')

                if current_rate != last_rate:
                    print(f"CamillaDSP: New Sample Rate detected: {current_rate} Hz")
                    
                    if current_rate == 48000:
                        set_motu_clock("optical")
                    else:
                        set_motu_clock("internal")
                        
                    last_rate = current_rate

        except Exception as e:
            # If CamillaDSP is unreachable, reset last_rate and wait
            last_rate = None
            time.sleep(2)
            continue

        time.sleep(1) # Check once per second

if __name__ == "__main__":
    main()
