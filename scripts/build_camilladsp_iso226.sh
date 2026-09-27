#!/bin/bash
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"

UPSTREAM_URL="https://github.com/HEnquist/camilladsp.git"
UPSTREAM_COMMIT="05e9cfcdf43c0dfe078ed3feb8af4c8bd701fd74"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="${1:-$SCRIPT_DIR/../camilladsp-iso226/camilladsp-v4.1.3-iso226.patch}"
TARGET="${CDSP_AUTOMATION_CAMILLADSP_TARGET:-/usr/local/bin/camilladsp}"
BACKUP="${CDSP_AUTOMATION_CAMILLADSP_BACKUP:-$TARGET.pre-iso226}"
# While this receipt names the Iso226 engine, the source switcher and the
# control UI offer ISO 226 loudness.
RECEIPT="${CDSP_AUTOMATION_ISO226_RECEIPT:-/var/lib/cdsp-automation/iso226-engine.json}"

if [[ "${1:-}" == "--uninstall" ]]; then
  # "Uninstall All" runs this on every install.  Without a receipt this helper
  # never installed an engine, and $TARGET is left alone.
  if [[ ! -f "$RECEIPT" ]]; then
    echo "No ISO 226 install receipt at $RECEIPT; leaving $TARGET untouched."
    exit 0
  fi
  if [[ -f "$BACKUP" ]]; then
    sudo install -m 0755 "$BACKUP" "$TARGET"
    sudo rm -f "$BACKUP"
  else
    sudo rm -f "$TARGET"
  fi
  sudo rm -f "$RECEIPT"
  sudo systemctl restart camilladsp.service
  exit 0
fi

# Replacing $TARGET only changes what plays if camilladsp.service actually
# starts that path.  Decide that before any toolchain check, clone or compile,
# so an incompatible layout costs a message instead of a full Rust build.
engine_preflight() {
  local pid exec_line exec_path target resolved
  target="$(readlink -f "$TARGET" 2>/dev/null || printf '%s' "$TARGET")"
  if ! systemctl cat camilladsp.service >/dev/null 2>&1; then
    echo "camilladsp.service is not installed" >&2
    return 1
  fi
  pid="$(systemctl show -p MainPID --value camilladsp.service 2>/dev/null || true)"
  if [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
    # Exactly the evidence the post-install verification below trusts.
    if [[ "$(sudo readlink -f "/proc/$pid/exe" 2>/dev/null || true)" == "$target" ]]; then
      return 0
    fi
    echo "camilladsp.service is running a binary other than $TARGET" >&2
    return 1
  fi
  # Stopped unit: read the unit text rather than systemd's ExecStart record
  # format.  `systemctl cat` prints the main unit then each drop-in, so the last
  # assignment is the effective one.
  exec_line="$(systemctl cat camilladsp.service 2>/dev/null | grep -E '^[[:space:]]*ExecStart=' | tail -n 1 || true)"
  exec_path="${exec_line#"${exec_line%%[![:space:]]*}"}"
  exec_path="${exec_path#ExecStart=}"
  exec_path="${exec_path%% *}"
  # systemd allows any combination of these command prefixes, in any order.
  while [[ -n "$exec_path" && "$exec_path" == [-@+!:]* ]]; do
    exec_path="${exec_path#?}"
  done
  exec_path="${exec_path%\"}"
  exec_path="${exec_path#\"}"
  if [[ -n "$exec_path" ]]; then
    resolved="$(readlink -f "$exec_path" 2>/dev/null || printf '%s' "$exec_path")"
    if [[ "$resolved" == "$target" ]]; then
      return 0
    fi
  fi
  echo "camilladsp.service starts ${exec_path:-an unknown binary}, not $TARGET" >&2
  return 1
}

if [[ "${1:-}" == "--preflight" ]]; then
  if engine_preflight; then exit 0; fi
  exit 1
fi

# A direct invocation gets the same gate as the installer's, so no path can
# reach cargo with an engine this build could never replace.
engine_preflight || exit 3

BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

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
for config in ${configs[@]+"${configs[@]}"}; do
  "$CANDIDATE" --check "$config"
done

# Whether camilladsp.service came back up running $TARGET.
engine_running_target() {
  local pid
  sleep 3
  pid="$(systemctl show -p MainPID --value camilladsp.service || true)"
  systemctl is-active --quiet camilladsp.service \
    && [[ "$pid" =~ ^[1-9][0-9]*$ ]] \
    && [[ "$(sudo readlink -f "/proc/$pid/exe")" == "$TARGET" ]]
}

PREVIOUS="$BUILD_DIR/previous-camilladsp"
had_target=false
if [[ -f "$TARGET" ]]; then
  had_target=true
  cp -p "$TARGET" "$PREVIOUS"
fi
sudo install -m 0755 "$CANDIDATE" "$TARGET.new"
sudo mv "$TARGET.new" "$TARGET"
if ! { sudo systemctl restart camilladsp.service && engine_running_target; }; then
  echo "CamillaDSP did not start from the new engine; restoring the previous engine." >&2
  if [[ "$had_target" == true ]]; then
    sudo install -m 0755 "$PREVIOUS" "$TARGET"
  else
    sudo rm -f "$TARGET"
  fi
  sudo systemctl restart camilladsp.service || echo "camilladsp.service did not restart after the rollback" >&2
  exit 1
fi
# The engine that was here before this helper ever ran, kept once for
# --uninstall.
if [[ "$had_target" == true && ! -f "$BACKUP" ]]; then
  sudo install -m 0755 "$PREVIOUS" "$BACKUP"
fi
sudo install -d -m 0750 -o "$(id -un)" -g "$(id -gn)" "$(dirname "$RECEIPT")"
printf '{"engine":"Iso226","upstream_commit":"%s","installed_at":%s}\n' "$UPSTREAM_COMMIT" "$(date +%s)" > "$BUILD_DIR/iso226-engine.json"
sudo install -m 0644 "$BUILD_DIR/iso226-engine.json" "$RECEIPT"
echo "Installed ISO 226-enabled CamillaDSP at $TARGET"
