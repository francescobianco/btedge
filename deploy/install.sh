#!/bin/sh
# Install or update btedge as a systemd service. Run as root from a checkout of the repo.
# Usage: install.sh [config.json]   (the file is used only if /etc/btedge/config.json is missing)
set -eu
CONFIG=${1:-}

SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/btedge

apt-get install -y --no-install-recommends bluez python3 python3-dbus python3-gi

if [ "$SRC" != "$DEST" ]; then
    mkdir -p "$DEST"
    rm -rf "$DEST/btedge"
    cp -r "$SRC/btedge" "$DEST/"
fi

mkdir -p /etc/btedge
if [ ! -f /etc/btedge/config.json ] && [ -n "$CONFIG" ]; then
    install -m 600 "$CONFIG" /etc/btedge/config.json
    echo "installed /etc/btedge/config.json from $CONFIG"
elif [ ! -f /etc/btedge/config.json ]; then
    sed "s/\"orangepizero\"/\"$(hostname)\"/; s/\"change-me\"/\"$(head -c 16 /dev/urandom | od -An -tx1 | tr -d " \n")\"/" \
        "$SRC/deploy/config.example.json" > /etc/btedge/config.json
    chmod 600 /etc/btedge/config.json
    echo "created /etc/btedge/config.json (copy mesh.key to every node of the same mesh)"
fi

install -m 644 "$SRC/deploy/btedge.service" /etc/systemd/system/btedge.service
systemctl daemon-reload
systemctl enable btedge.service
systemctl restart btedge.service
echo "btedge running: http://$(hostname -I | awk '{print $1}'):4646"
