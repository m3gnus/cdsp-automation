#!/bin/bash
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"

UPSTREAM_URL="https://github.com/HEnquist/camilladsp.git"
UPSTREAM_COMMIT="05e9cfcdf43c0dfe078ed3feb8af4c8bd701fd74"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="${1:-$SCRIPT_DIR/../camilladsp-iso226/camilladsp-v4.1.3-iso226.patch}"
BUILD_DIR="$(mktemp -d)"
TARGET="${CDSP_AUTOMATION_CAMILLADSP_TARGET:-/usr/local/bin/camilladsp}"
BACKUP="${CDSP_AUTOMATION_CAMILLADSP_BACKUP:-$TARGET.pre-iso226}"
CAPABILITY="${ISO226_CAPABILITY_PATH:-/var/lib/cdsp-automation/iso226-engine.json}"
trap 'rm -rf "$BUILD_DIR"' EXIT

# The Pi only ever has sha256sum; the test-suite also runs on machines that
# ship shasum instead, and both must agree with the digest in the receipt.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

if [[ "${1:-}" == "--uninstall" ]]; then
  # $TARGET is the canonical CamillaDSP path, so it is far more likely to hold
  # a stock binary the operator installed than one of ours.  The receipt is the
  # only evidence that this helper ever wrote it, and "Uninstall All" invokes
  # us on every install path - including ones that never built an engine.
  if [[ ! -f "$CAPABILITY" ]]; then
    echo "No ISO 226 install receipt at $CAPABILITY; leaving $TARGET untouched."
    exit 0
  fi
  # A 64-hex match or nothing: a receipt we cannot read is a receipt we cannot
  # act on, and falls through to the same "not ours" branch as a mismatch.
  recorded_sha="$(sed -n 's/.*"binary_sha256"[[:space:]]*:[[:space:]]*"\([0-9a-fA-F]\{64\}\)".*/\1/p' "$CAPABILITY")"
  recorded_sha="${recorded_sha%%$'\n'*}"
  # $TARGET is mode 0755 and world-readable, so this needs no sudo.
  current_sha=""
  if [[ -f "$TARGET" ]]; then
    current_sha="$(sha256_of "$TARGET")"
  fi
  if [[ -z "$recorded_sha" || "$current_sha" != "$recorded_sha" ]]; then
    # The binary changed hands after our install - a package upgrade, a manual
    # replacement, a newer build.  Whatever is there now belongs to the
    # operator; only our own bookkeeping is stale.
    echo "$TARGET does not match the ISO 226 receipt; leaving it in place and removing the stale receipt." >&2
    sudo rm -f "$CAPABILITY"
    exit 0
  fi
  if [[ -f "$BACKUP" ]]; then
    sudo install -m 0755 "$BACKUP" "$TARGET"
    # The pre-install binary is live again, so the copy has served its purpose.
    # Keeping it would let a later install/uninstall cycle roll back to a build
    # that is by then two generations old.
    sudo rm -f "$BACKUP"
  else
    sudo rm -f "$TARGET"
  fi
  sudo rm -f "$CAPABILITY"
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
install_user="$(id -un)"
sudo install -d -m 0750 -o "$install_user" -g "$install_user" /var/lib/cdsp-automation
marker="$BUILD_DIR/iso226-engine.json"
binary_sha256="$(sha256_of "$CANDIDATE")"
printf '{"engine":"Iso226","upstream_commit":"%s","binary_sha256":"%s","installed_at":%s}\n' "$UPSTREAM_COMMIT" "$binary_sha256" "$(date +%s)" > "$marker"
sudo install -m 0644 "$marker" "$CAPABILITY"
echo "Installed ISO 226-enabled CamillaDSP at /usr/local/bin/camilladsp"
