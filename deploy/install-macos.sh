#!/bin/sh
# Install or update btedge on macOS as a LaunchAgent of the current user (no sudo).
# Needs the Xcode Command Line Tools. Run from a checkout of the repo.
# Usage: install-macos.sh [config.json]   (the file is used only if ~/.btedge/config.json is missing)
set -eu
CONFIG=${1:-}

SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=$HOME/.btedge
APP=$DEST/BtedgeBluetooth.app
AGENT=$HOME/Library/LaunchAgents/btedge.plist

mkdir -p "$DEST/app" "$DEST/state" "$APP/Contents/MacOS" "$HOME/Library/LaunchAgents"
chmod 700 "$DEST"

if [ ! -f "$DEST/config.json" ]; then
    if [ -z "$CONFIG" ]; then
        echo "no $DEST/config.json: pass a config file with \"scanner\": {\"backend\": \"corebluetooth\"}" >&2
        exit 1
    fi
    install -m 600 "$CONFIG" "$DEST/config.json"
fi

rm -rf "$DEST/app/btedge"
cp -R "$SRC/btedge" "$DEST/app/"
[ -x "$DEST/venv/bin/python" ] || python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --disable-pip-version-check cryptography

swiftc -O -o "$APP/Contents/MacOS/BtedgeBluetooth" "$SRC/deploy/macos/main.swift" \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$SRC/deploy/macos/Info.plist"
cp "$SRC/deploy/macos/Info.plist" "$APP/Contents/Info.plist"
codesign --force --sign - --identifier net.yafb.btedge.bluetooth "$APP"

cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>btedge</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP/Contents/MacOS/BtedgeBluetooth</string>
    <string>$DEST/venv/bin/python</string>
    <string>-m</string><string>btedge</string>
    <string>--config</string><string>$DEST/config.json</string>
  </array>
  <key>WorkingDirectory</key><string>$DEST/app</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DEST/btedge.log</string>
  <key>StandardErrorPath</key><string>$DEST/btedge.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/btedge" 2>/dev/null || true
while launchctl print "gui/$(id -u)/btedge" >/dev/null 2>&1; do sleep 1; done  # bootout is asynchronous
launchctl bootstrap "gui/$(id -u)" "$AGENT"
echo "btedge running: http://127.0.0.1:4646 (allow Bluetooth when macOS asks)"
echo "admit it into the mesh, once: python3 -m btedge enroll $(id -un)@$(hostname) --state-dir $DEST/state"
