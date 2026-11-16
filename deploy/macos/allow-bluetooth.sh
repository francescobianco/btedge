#!/bin/sh
# Make macOS ask again whether btedge may use Bluetooth, and wait for the answer.
#
# macOS lets nobody grant the permission from a script: the click on "Allow" has to be
# yours, on the Mac's screen. Everything around it is done here: forget the earlier
# refusal, restart the node so that macOS asks again, open the right settings page and
# check that the node really starts scanning.
#
# On the Mac:        deploy/macos/allow-bluetooth.sh
# From elsewhere:    ssh user@mac 'sh -s' < deploy/macos/allow-bluetooth.sh
set -eu

BUNDLE=net.yafb.btedge.bluetooth
DEST=$HOME/.btedge
SERVICE="gui/$(id -u)/btedge"
PAGE="x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth"

[ "$(uname)" = Darwin ] || { echo "this is for macOS" >&2; exit 1; }
launchctl print "$SERVICE" >/dev/null 2>&1 || { echo "btedge is not installed here: run deploy/install-macos.sh first" >&2; exit 1; }

PORT=$("$DEST/venv/bin/python" -c 'import json, sys
print(json.load(open(sys.argv[1])).get("http", {}).get("port", 4646))' "$DEST/config.json" 2>/dev/null || echo 4646)

scanner() {  # "<state> <error>" as the running node reports it
    curl -s -m 3 "http://127.0.0.1:$PORT/api/state" | "$DEST/venv/bin/python" -c 'import json, sys
scanner = json.load(sys.stdin).get("scanner") or {}
print(scanner.get("state"), scanner.get("error") or "")' 2>/dev/null || echo "down"
}

echo "forgetting the earlier answer for $BUNDLE"
tccutil reset BluetoothAlways "$BUNDLE" >/dev/null
echo "restarting btedge: macOS now asks for Bluetooth on this Mac's screen"
launchctl kickstart -k "$SERVICE"
open "$PAGE" 2>/dev/null || true   # in case the question was missed: the switch is on this page

echo "waiting for you to allow it (2 minutes)..."
i=0
while [ $i -lt 60 ]; do
    sleep 2
    i=$((i + 1))
    state=$(scanner)
    case $state in
        scanning*)
            # an old helper says "scanning" even when refused: believe it once it hears something
            sleep 6
            devices=$(curl -s -m 3 "http://127.0.0.1:$PORT/api/state" | "$DEST/venv/bin/python" -c 'import json, sys
state = json.load(sys.stdin)
print(next((n["devices"] for n in state["nodes"] if n["self"]), 0))' 2>/dev/null || echo 0)
            echo "Bluetooth allowed: btedge is scanning ($devices devices so far)"
            exit 0 ;;
    esac
done
echo "still not scanning: $(scanner)" >&2
echo "allow BtedgeBluetooth in System Settings > Privacy & Security > Bluetooth, then run this again" >&2
exit 1
