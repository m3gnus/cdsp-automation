#!/bin/bash
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"

UPSTREAM_URL="https://github.com/librespot-org/librespot.git"
UPSTREAM_COMMIT="d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="${1:-$SCRIPT_DIR/../librespot-volume-sync/librespot-v0.8.0-volume-sync.patch}"
BUILD_DIR="$(mktemp -d)"
TARGET="/usr/local/bin/librespot-uglan"
DROPIN_DIR="/etc/systemd/system/raspotify.service.d"
DROPIN="$DROPIN_DIR/uglan-volume-sync.conf"
CALLBACK="/usr/local/libexec/airplay_volume_bridge.py"
trap 'rm -rf "$BUILD_DIR"' EXIT

if [[ "${1:-}" == "--uninstall" ]]; then
  sudo rm -f "$DROPIN" "$TARGET"
  sudo systemctl daemon-reload
  sudo systemctl restart raspotify.service
  exit 0
fi

for required in git cargo rustc pkg-config; do
  command -v "$required" >/dev/null || { echo "Missing build prerequisite: $required" >&2; exit 1; }
done
[[ -f "$PATCH_FILE" ]] || { echo "librespot volume-sync patch not found: $PATCH_FILE" >&2; exit 1; }
[[ -x "$CALLBACK" ]] || { echo "Install the network volume bridge before librespot volume sync" >&2; exit 1; }

git clone --filter=blob:none "$UPSTREAM_URL" "$BUILD_DIR/librespot"
git -C "$BUILD_DIR/librespot" checkout --detach "$UPSTREAM_COMMIT"
git -C "$BUILD_DIR/librespot" apply --check "$PATCH_FILE"
git -C "$BUILD_DIR/librespot" apply "$PATCH_FILE"
cargo test --manifest-path "$BUILD_DIR/librespot/Cargo.toml" \
  -p librespot-playback --no-default-features --features alsa-backend
cargo build --release --manifest-path "$BUILD_DIR/librespot/Cargo.toml" \
  --no-default-features --features alsa-backend,native-tls,with-libmdns
CANDIDATE="$BUILD_DIR/librespot/target/release/librespot"
"$CANDIDATE" --version | grep -q 'librespot 0.8.0'

had_target=false
had_dropin=false
if [[ -f "$TARGET" ]]; then
  had_target=true
  cp -p "$TARGET" "$BUILD_DIR/previous-librespot"
fi
if [[ -f "$DROPIN" ]]; then
  had_dropin=true
  cp -p "$DROPIN" "$BUILD_DIR/previous-dropin"
fi
rollback() {
  if [[ "$had_target" == true ]]; then
    sudo install -m 0755 "$BUILD_DIR/previous-librespot" "$TARGET"
  else
    sudo rm -f "$TARGET"
  fi
  if [[ "$had_dropin" == true ]]; then
    sudo install -m 0644 "$BUILD_DIR/previous-dropin" "$DROPIN"
  else
    sudo rm -f "$DROPIN"
  fi
  sudo systemctl daemon-reload
  sudo systemctl restart raspotify.service || true
}

dropin_candidate="$BUILD_DIR/uglan-volume-sync.conf"
cat > "$dropin_candidate" <<EOF
[Unit]
Wants=airplay-volume-bridge.service
After=airplay-volume-bridge.service

[Service]
ExecStart=
ExecStart=$TARGET
Environment=LIBRESPOT_MIXER=softvol
Environment=LIBRESPOT_VOLUME_CTRL=fixed
Environment="LIBRESPOT_ONEVENT=/usr/bin/python3 $CALLBACK --notify-spotify"
Environment=UGLAN_SPOTIFY_VOLUME_SOCKET=/run/raspotify/uglan-volume.sock
EOF

sudo install -m 0755 "$CANDIDATE" "$TARGET.new"
sudo mv "$TARGET.new" "$TARGET"
sudo install -d -m 0755 "$DROPIN_DIR"
sudo install -m 0644 "$dropin_candidate" "$DROPIN"
sudo systemctl daemon-reload
if ! sudo systemctl restart airplay-volume-bridge.service raspotify.service; then rollback; exit 1; fi
sleep 3
pid="$(systemctl show -p MainPID --value raspotify.service)"
if ! systemctl is-active --quiet raspotify.service \
  || [[ ! "$pid" =~ ^[1-9][0-9]*$ ]] \
  || [[ "$(sudo readlink -f "/proc/$pid/exe")" != "$TARGET" ]] \
  || [[ ! -S /run/raspotify/uglan-volume.sock ]]; then
  echo "Patched librespot did not become healthy; rolling back." >&2
  rollback
  exit 1
fi
echo "Installed bidirectional Spotify Connect volume sync through CamillaDSP."
