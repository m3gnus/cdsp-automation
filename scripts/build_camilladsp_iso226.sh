#!/bin/bash
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"

UPSTREAM_URL="https://github.com/HEnquist/camilladsp.git"
UPSTREAM_COMMIT="05e9cfcdf43c0dfe078ed3feb8af4c8bd701fd74"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="${1:-$SCRIPT_DIR/../camilladsp-iso226/camilladsp-v4.1.3-iso226.patch}"
BUILD_DIR="$(mktemp -d)"
TARGET="/usr/local/bin/camilladsp"
BACKUP="/usr/local/bin/camilladsp.pre-iso226"
CAPABILITY="/var/lib/cdsp-automation/iso226-engine.json"
trap 'rm -rf "$BUILD_DIR"' EXIT

if [[ "${1:-}" == "--uninstall" ]]; then
  if [[ -f "$BACKUP" ]]; then
    sudo install -m 0755 "$BACKUP" "$TARGET"
  else
    sudo rm -f "$TARGET"
  fi
  sudo rm -f "$CAPABILITY"
  sudo systemctl restart camilladsp.service
  exit 0
fi

for required in git cargo rustc; do
  command -v "$required" >/dev/null || { echo "Missing build prerequisite: $required" >&2; exit 1; }
done
rust_version="$(rustc --version | awk '{print $2}')"
if [[ "$(printf '%s\n' 1.90.0 "$rust_version" | sort -V | head -n1)" != "1.90.0" ]]; then
  echo "Rust 1.90 or newer is required; found $rust_version" >&2
  exit 1
fi

if [[ ! -f "$PATCH_FILE" ]]; then
  echo "ISO 226 patch not found: $PATCH_FILE" >&2
  exit 1
fi

git clone --filter=blob:none "$UPSTREAM_URL" "$BUILD_DIR/camilladsp"
git -C "$BUILD_DIR/camilladsp" checkout --detach "$UPSTREAM_COMMIT"
git -C "$BUILD_DIR/camilladsp" apply --check "$PATCH_FILE"
git -C "$BUILD_DIR/camilladsp" apply "$PATCH_FILE"
cargo test --manifest-path "$BUILD_DIR/camilladsp/Cargo.toml" --lib
cargo build --release --manifest-path "$BUILD_DIR/camilladsp/Cargo.toml"
CANDIDATE="$BUILD_DIR/camilladsp/target/release/camilladsp"

config_dir="${CDSP_CONFIG_DIR:-$HOME/camilladsp/configs}"
shopt -s nullglob
configs=("$config_dir"/*.yml "$config_dir"/*.yaml)
if [[ ${#configs[@]} -eq 0 ]]; then
  echo "WARNING: no deployed YAML configs found in $config_dir; candidate config smoke-test skipped." >&2
fi
for config in "${configs[@]}"; do
  "$CANDIDATE" --check "$config"
done

had_target=false
if [[ -f "$TARGET" ]]; then
  had_target=true
  if [[ ! -f "$BACKUP" ]]; then
    sudo cp -p "$TARGET" "$BACKUP"
  fi
fi
rollback() {
  if [[ -f "$BACKUP" ]]; then
    sudo install -m 0755 "$BACKUP" "$TARGET"
  elif [[ "$had_target" == false ]]; then
    sudo rm -f "$TARGET"
  fi
  sudo rm -f "$CAPABILITY"
  sudo systemctl restart camilladsp.service || true
}

sudo install -m 0755 "$CANDIDATE" "$TARGET.new"
sudo mv "$TARGET.new" "$TARGET"
if ! sudo systemctl restart camilladsp.service; then rollback; exit 1; fi
sleep 3
pid="$(systemctl show -p MainPID --value camilladsp.service)"
if ! systemctl is-active --quiet camilladsp.service || [[ ! "$pid" =~ ^[1-9][0-9]*$ ]] || [[ "$(sudo readlink -f "/proc/$pid/exe")" != "$TARGET" ]]; then
  echo "CamillaDSP did not start from the tested candidate; rolling back." >&2
  rollback
  exit 1
fi
sudo install -d -m 0750 -o "$USER" -g "$USER" /var/lib/cdsp-automation
marker="$BUILD_DIR/iso226-engine.json"
binary_sha256="$(sha256sum "$CANDIDATE" | awk '{print $1}')"
printf '{"engine":"Iso226","upstream_commit":"%s","binary_sha256":"%s","installed_at":%s}\n' "$UPSTREAM_COMMIT" "$binary_sha256" "$(date +%s)" > "$marker"
sudo install -m 0644 "$marker" "$CAPABILITY"
echo "Installed ISO 226-enabled CamillaDSP at /usr/local/bin/camilladsp"
