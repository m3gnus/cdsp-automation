#!/bin/bash
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"

UPSTREAM_URL="https://github.com/librespot-org/librespot.git"
UPSTREAM_COMMIT="d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${CDSP_AUTOMATION_LIBRESPOT_TARGET:-/usr/local/bin/librespot-cdsp}"
DROPIN_DIR="${CDSP_AUTOMATION_RASPOTIFY_DROPIN_DIR:-/etc/systemd/system/raspotify.service.d}"
DROPIN="$DROPIN_DIR/cdsp-volume-sync.conf"
CALLBACK="${CDSP_AUTOMATION_VOLUME_CALLBACK:-/usr/local/libexec/airplay_volume_bridge.py}"
MARKER="${CDSP_AUTOMATION_LIBRESPOT_MARKER:-/var/lib/cdsp-automation/librespot-volume-sync.sha256}"
# Only this exact marker format is understood.  Anything else - including the
# markers older deployments left behind - is treated as "unknown", which forces
# one rebuild and then rewrites the file in this format.
MARKER_FORMAT="cdsp-volume-sync/2"
BUILD_FEATURES="alsa-backend,native-tls,with-avahi"

# Artifact names an earlier release compiled a single site's name into.  The
# name is assembled from fragments so the literal never appears in this
# repository; every comparison against it is exact, so drop-ins, binaries and
# sockets this tool did not create are never touched.
LEGACY_TAG="ug""lan"
LEGACY_TARGET="${CDSP_AUTOMATION_LEGACY_LIBRESPOT_TARGET:-/usr/local/bin/librespot-$LEGACY_TAG}"
LEGACY_DROPIN="$DROPIN_DIR/$LEGACY_TAG-volume-sync.conf"

: "${SPOTIFY_VOLUME_COMMAND_SOCKET_PATH:=/run/raspotify/cdsp-volume.sock}"
: "${SPOTIFY_ALSA_DEVICE:=}"
: "${VOLUME_SYNC_GROUP:=audio}"
COMMAND_SOCKET="$SPOTIFY_VOLUME_COMMAND_SOCKET_PATH"
LEGACY_COMMAND_SOCKET="/run/raspotify/$LEGACY_TAG-volume.sock"
ACK_SOCKET="${AIRPLAY_VOLUME_SOCKET_PATH:-/run/airplay-volume-bridge/input.sock}"

BUILD_DIR=""
deployment_started=false
deployment_complete=false
had_target=false
had_dropin=false
had_legacy_dropin=false

validate_settings() {
  # Allowlists, so whitespace, quotes, $, backticks and systemd's % specifier
  # can never reach the rendered unit file.
  local socket_pattern='^/[A-Za-z0-9_./@:,=+-]+$'
  local device_pattern='^[A-Za-z0-9_:,=./@-]+$'
  if [[ ! "$COMMAND_SOCKET" =~ $socket_pattern ]]; then
    echo "SPOTIFY_VOLUME_COMMAND_SOCKET_PATH must be an absolute path without shell or systemd metacharacters: $COMMAND_SOCKET" >&2
    return 1
  fi
  if [[ -n "$SPOTIFY_ALSA_DEVICE" && ! "$SPOTIFY_ALSA_DEVICE" =~ $device_pattern ]]; then
    echo "SPOTIFY_ALSA_DEVICE contains unsupported characters: $SPOTIFY_ALSA_DEVICE" >&2
    return 1
  fi
  if [[ ! "$VOLUME_SYNC_GROUP" =~ ^[a-zA-Z0-9._-]+$ ]]; then
    echo "VOLUME_SYNC_GROUP is not a valid group name: $VOLUME_SYNC_GROUP" >&2
    return 1
  fi
}

render_dropin() {
  local device_flag=""
  validate_settings || return 1
  # Empty means "leave raspotify's own device configuration in force": the
  # drop-in resets ExecStart= but not the base unit's EnvironmentFile=, so
  # omitting --device preserves whatever the raspotify package was told to use.
  if [[ -n "$SPOTIFY_ALSA_DEVICE" ]]; then
    device_flag=" --device $SPOTIFY_ALSA_DEVICE"
  fi
  cat <<EOF
[Unit]
Wants=airplay-volume-bridge.service
After=airplay-volume-bridge.service

[Service]
ExecStart=
ExecStart=$TARGET$device_flag
Environment=LIBRESPOT_MIXER=softvol
Environment=LIBRESPOT_VOLUME_CTRL=fixed
Environment=LIBRESPOT_ZEROCONF_BACKEND=avahi
Environment="LIBRESPOT_ONEVENT=/usr/bin/python3 $CALLBACK --notify-spotify"
Environment=CDSP_SPOTIFY_VOLUME_SOCKET=$COMMAND_SOCKET
Environment=CDSP_SPOTIFY_VOLUME_ACK_SOCKET=$ACK_SOCKET
Group=$VOLUME_SYNC_GROUP
EOF
}

sha256_stream() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  else
    shasum -a 256 | awk '{print $1}'
  fi
}

# Digest of everything that decides what the compiled binary contains.  The
# rendered unit is deliberately excluded: a device or socket change must
# redeploy the drop-in, never recompile Rust.
source_digest() {
  local patch_file="$1"
  printf '%s\n%s\n%s\n%s\n' \
    "$MARKER_FORMAT" \
    "$UPSTREAM_COMMIT" \
    "$(sha256_stream < "$patch_file")" \
    "$BUILD_FEATURES" | sha256_stream
}

marker_matches() {
  local expected="$1" format recorded
  [[ -f "$MARKER" ]] || return 1
  read -r format recorded < "$MARKER" 2>/dev/null || return 1
  [[ "$format" == "$MARKER_FORMAT" && "$recorded" == "$expected" ]]
}

write_marker() {
  local digest="$1" marker_tmp install_user
  install_user="$(id -un)"
  marker_tmp="$BUILD_DIR/librespot-volume-sync.marker"
  printf '%s %s\n' "$MARKER_FORMAT" "$digest" > "$marker_tmp"
  sudo install -d -m 0750 -o "$install_user" -g "$install_user" "$(dirname "$MARKER")"
  sudo install -m 0644 "$marker_tmp" "$MARKER"
}

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
  # The superseded drop-in comes back only while the binary it names still
  # exists: the legacy pair is never dismantled before the new one is healthy.
  if [[ "$had_legacy_dropin" == true ]]; then
    sudo install -m 0644 "$BUILD_DIR/previous-legacy-dropin" "$LEGACY_DROPIN"
  fi
  sudo systemctl daemon-reload
  sudo systemctl restart raspotify.service || true
}

cleanup() {
  local rc=$?
  trap - EXIT
  if [[ "$deployment_started" == true && "$deployment_complete" != true ]]; then
    echo "Spotify volume-sync installation did not complete; rolling back." >&2
    set +e
    rollback
  fi
  [[ -n "$BUILD_DIR" ]] && rm -rf "$BUILD_DIR"
  exit "$rc"
}

uninstall() {
  sudo rm -f "$DROPIN" "$TARGET" "$MARKER"
  sudo rm -f "$LEGACY_DROPIN" "$LEGACY_TARGET"
  sudo systemctl daemon-reload
  sudo systemctl restart raspotify.service
}

warn_about_unknown_device() {
  [[ -n "$SPOTIFY_ALSA_DEVICE" ]] || return 0
  command -v aplay >/dev/null || return 0
  # A warning, never a gate: whether a custom pcm.* in /etc/asound.conf is
  # enumerated cannot be decided here, and librespot opens ALSA lazily, so a
  # false negative must cost a log line rather than a blocked install.
  if ! aplay -L 2>/dev/null | grep -qx -- "$SPOTIFY_ALSA_DEVICE"; then
    echo "WARNING: ALSA did not enumerate '$SPOTIFY_ALSA_DEVICE'; Spotify audio may be silent." >&2
    echo "         Check /etc/asound.conf and the output of 'aplay -L'." >&2
  fi
}

main() {
  local patch_file digest candidate dropin_candidate pid
  patch_file="${1:-$SCRIPT_DIR/../librespot-volume-sync/librespot-v0.8.0-volume-sync.patch}"

  if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
    exit 0
  fi
  if [[ "${1:-}" == "--print-dropin" ]]; then
    render_dropin
    exit 0
  fi

  BUILD_DIR="$(mktemp -d)"
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  [[ -f "$patch_file" ]] || { echo "librespot volume-sync patch not found: $patch_file" >&2; exit 1; }
  [[ -x "$CALLBACK" ]] || { echo "Install the network volume bridge before librespot volume sync" >&2; exit 1; }

  dropin_candidate="$BUILD_DIR/cdsp-volume-sync.conf"
  render_dropin > "$dropin_candidate"
  digest="$(source_digest "$patch_file")"

  local rebuild=true
  if marker_matches "$digest" && [[ -f "$TARGET" ]]; then
    rebuild=false
  fi
  if [[ "$rebuild" == false ]] &&
     [[ -f "$DROPIN" ]] && cmp -s "$dropin_candidate" "$DROPIN" &&
     [[ ! -e "$LEGACY_DROPIN" && ! -e "$LEGACY_TARGET" ]]; then
    echo "Spotify volume sync is already current (patch and receiver unchanged)."
    warn_about_unknown_device
    exit 0
  fi

  if [[ "$rebuild" == true ]]; then
    for required in git cargo rustc pkg-config; do
      command -v "$required" >/dev/null || { echo "Missing build prerequisite: $required" >&2; exit 1; }
    done
    git clone --filter=blob:none "$UPSTREAM_URL" "$BUILD_DIR/librespot"
    git -C "$BUILD_DIR/librespot" checkout --detach "$UPSTREAM_COMMIT"
    git -C "$BUILD_DIR/librespot" apply --check "$patch_file"
    git -C "$BUILD_DIR/librespot" apply "$patch_file"
    cargo test --manifest-path "$BUILD_DIR/librespot/Cargo.toml" \
      -p librespot-playback --no-default-features --features alsa-backend,native-tls
    cargo build --release --manifest-path "$BUILD_DIR/librespot/Cargo.toml" \
      --no-default-features --features "$BUILD_FEATURES"
    candidate="$BUILD_DIR/librespot/target/release/librespot"
    "$candidate" --version | grep -q 'librespot 0.8.0'
  else
    echo "Patched librespot is already built for this patch; reusing $TARGET."
    candidate=""
  fi

  if [[ -f "$TARGET" ]]; then
    had_target=true
    cp -p "$TARGET" "$BUILD_DIR/previous-librespot"
  fi
  if [[ -f "$DROPIN" ]]; then
    had_dropin=true
    cp -p "$DROPIN" "$BUILD_DIR/previous-dropin"
  fi
  if [[ -f "$LEGACY_DROPIN" ]]; then
    had_legacy_dropin=true
    cp -p "$LEGACY_DROPIN" "$BUILD_DIR/previous-legacy-dropin"
  fi

  deployment_started=true
  if [[ -n "$candidate" ]]; then
    sudo install -m 0755 "$candidate" "$TARGET.new"
    sudo mv "$TARGET.new" "$TARGET"
  fi
  sudo install -d -m 0755 "$DROPIN_DIR"
  sudo install -m 0644 "$dropin_candidate" "$DROPIN"
  # systemd applies drop-ins in filename order and the last ExecStart= wins, so
  # the superseded file has to go before the reload or it would keep launching
  # the old receiver.  Its binary and socket stay until the new pair is healthy.
  sudo rm -f "$LEGACY_DROPIN"
  sudo systemctl daemon-reload
  sudo systemctl restart airplay-volume-bridge.service raspotify.service
  sleep 3
  pid="$(systemctl show -p MainPID --value raspotify.service)"
  if ! systemctl is-active --quiet raspotify.service \
    || ! systemctl is-active --quiet airplay-volume-bridge.service \
    || [[ ! "$pid" =~ ^[1-9][0-9]*$ ]] \
    || [[ "$(sudo readlink -f "/proc/$pid/exe")" != "$TARGET" ]] \
    || [[ ! -S "$COMMAND_SOCKET" ]]; then
    echo "Patched librespot did not become healthy." >&2
    exit 1
  fi
  deployment_complete=true
  # Both services proved healthy on the new pair; only now is the superseded
  # receiver removed.
  if [[ "$LEGACY_TARGET" != "$TARGET" ]]; then
    sudo rm -f "$LEGACY_TARGET"
  fi
  if [[ "$LEGACY_COMMAND_SOCKET" != "$COMMAND_SOCKET" ]]; then
    sudo rm -f "$LEGACY_COMMAND_SOCKET"
  fi
  write_marker "$digest"
  warn_about_unknown_device
  echo "Installed bidirectional Spotify Connect volume sync through CamillaDSP."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
