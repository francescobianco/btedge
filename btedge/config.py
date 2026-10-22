"""Configuration loading with defaults and a persistent node identity."""
import copy
import json
import logging
import os
import re
import secrets
import socket

log = logging.getLogger(__name__)

DEFAULT_PATH = "/etc/btedge/config.json"
NODE_ID_RE = re.compile(r"^[0-9a-f]{16}$")

DEFAULTS = {
    "node_id": None,
    "node_name": socket.gethostname(),
    "state_dir": "/var/lib/btedge",
    "http": {"host": "0.0.0.0", "port": 8080},
    "scanner": {
        "backend": "bluez",   # "bluez" or "fake"
        "adapter": None,      # e.g. "hci0"; None picks the first adapter
        "device_ttl": 60,     # seconds a device stays listed after its last advert
        "cache_ttl": 300,     # seconds before unpaired devices are dropped from BlueZ
        "advertise": True,    # broadcast a BLE beacon so other nodes see us over the air
    },
    "mesh": {
        "key": "",            # shared secret; nodes with different keys ignore each other
        "gossip_interval": 5,
        "node_ttl": 60,
        "max_hops": 8,
        "lan": {"enabled": True, "group": "239.255.66.77", "port": 47474},
        "peers": [],          # e.g. ["http://node.example.org:8080"]
    },
}


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _state_dir(preferred):
    for path in (preferred, os.path.expanduser("~/.local/state/btedge")):
        try:
            os.makedirs(path, exist_ok=True)
            if os.access(path, os.W_OK):
                return path
        except OSError:
            pass
    raise RuntimeError("no writable state directory")


def _node_id(state_dir):
    path = os.path.join(state_dir, "node_id")
    try:
        with open(path) as fh:
            node_id = fh.read().strip()
        if NODE_ID_RE.match(node_id):
            return node_id
    except FileNotFoundError:
        pass
    node_id = secrets.token_hex(8)
    with open(path, "w") as fh:
        fh.write(node_id + "\n")
    log.info("generated node id %s", node_id)
    return node_id


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    if path is None and os.path.exists(DEFAULT_PATH):
        path = DEFAULT_PATH
    if path:
        with open(path) as fh:
            _merge(cfg, json.load(fh))
    cfg["state_dir"] = _state_dir(cfg["state_dir"])
    if cfg["node_id"]:
        cfg["node_id"] = str(cfg["node_id"]).lower()
        if not NODE_ID_RE.match(cfg["node_id"]):
            raise ValueError("node_id must be 16 hex characters")
    else:
        cfg["node_id"] = _node_id(cfg["state_dir"])
    return cfg
