"""Configuration loading with defaults and a persistent node identity."""
import copy
import json
import logging
import os
import re
import secrets
import socket

from . import trust

log = logging.getLogger(__name__)

DEFAULT_PATH = "/etc/btedge/config.json"
NODE_ID_RE = re.compile(r"^[0-9a-f]{16}$")

ROLES = ("edge", "control")

DEFAULTS = {
    "node_id": None,
    # edge: scans Bluetooth and is a room on the map.
    # control: no Bluetooth; a hub other nodes sync with (e.g. on a VPS) and the admin panel.
    "role": "edge",
    "public_url": None,       # control: URL edge nodes put in mesh.peers (shown in the panel)
    "node_name": socket.gethostname(),
    "state_dir": "/var/lib/btedge",
    "http": {"host": "0.0.0.0", "port": 4646},
    "scanner": {
        "backend": "bluez",   # "bluez", "fake" or "none" (no Bluetooth)
        "adapter": None,      # e.g. "hci0"; None picks the first adapter
        "device_ttl": 60,     # seconds a device stays listed after its last advert
        "cache_ttl": 300,     # seconds before unpaired devices are dropped from BlueZ
        "rescan_interval": 20,  # seconds per discovery session; must stay well below device_ttl
        "advertise": True,    # broadcast a BLE beacon so other nodes see us over the air
        "keep_connected": [], # addresses of paired devices to (re)connect whenever they drop
        "fake_room": 0,       # fake backend: which simulated room this node is
        "fake_rooms": 1,      # fake backend: how many simulated rooms devices move across
    },
    "admin": {
        # HTTP Basic auth for the panel. Control nodes always require it and generate a
        # password in <state_dir>/admin_password when none is set.
        "user": "admin",
        "password": None,
    },
    "mesh": {
        # Membership (see trust.py): owner = the mesh owner's SSH public key; credential =
        # the owner's signature from `python3 -m btedge credential` (secret). The packet key
        # is derived from the credential after checking it against the owner key.
        "owner": None,
        "credential": None,
        "gossip_interval": 5,
        "node_ttl": 60,
        "max_hops": 8,
        # interface: name or IPv4 of the LAN interface; None follows the routing table
        "lan": {"enabled": True, "group": "239.255.66.77", "port": 47474, "interface": None},
        "peers": [],          # e.g. ["http://node.example.org:4646"]
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


def _secret(state_dir, name, nbytes):
    """Read <state_dir>/<name>, generating it on first use."""
    path = os.path.join(state_dir, name)
    try:
        with open(path) as fh:
            value = fh.read().strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    value = secrets.token_hex(nbytes)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")
    log.warning("generated %s in %s", name, path)
    return value


# Environment overrides, handy in containers: BTEDGE_<NAME> -> config path.
ENV = {
    "ROLE": ("role",), "NODE_NAME": ("node_name",), "NODE_ID": ("node_id",),
    "PUBLIC_URL": ("public_url",), "STATE_DIR": ("state_dir",), "PORT": ("http", "port"),
    "MESH_OWNER": ("mesh", "owner"), "MESH_CREDENTIAL": ("mesh", "credential"), "PEERS": ("mesh", "peers"), "LAN": ("mesh", "lan", "enabled"),
    "LAN_INTERFACE": ("mesh", "lan", "interface"), "ADMIN_USER": ("admin", "user"),
    "ADMIN_PASSWORD": ("admin", "password"),
}


def _apply_env(cfg, environ):
    for name, keys in ENV.items():
        raw = environ.get("BTEDGE_" + name)
        if raw is None or raw == "":
            continue
        if keys == ("http", "port"):
            value = int(raw)
        elif keys == ("mesh", "peers"):
            value = [p.strip() for p in raw.split(",") if p.strip()]
        elif keys == ("mesh", "lan", "enabled"):
            value = raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            value = raw
        target = cfg
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value


def load(path=None, environ=None):
    cfg = copy.deepcopy(DEFAULTS)
    if path is None and os.path.exists(DEFAULT_PATH):
        path = DEFAULT_PATH
    if path:
        with open(path) as fh:
            _merge(cfg, json.load(fh))
    _apply_env(cfg, os.environ if environ is None else environ)
    if cfg["role"] not in ROLES:
        raise ValueError("role must be one of %s" % ", ".join(ROLES))
    cfg["state_dir"] = _state_dir(cfg["state_dir"])
    if cfg["node_id"]:
        cfg["node_id"] = str(cfg["node_id"]).lower()
        if not NODE_ID_RE.match(cfg["node_id"]):
            raise ValueError("node_id must be 16 hex characters")
    else:
        cfg["node_id"] = _node_id(cfg["state_dir"])
    mesh = cfg["mesh"]
    if not mesh["owner"]:
        raise ValueError("mesh.owner is required: the SSH public key of the mesh owner")
    mesh["owner"] = trust.owner_key(mesh["owner"])
    mesh["key"] = trust.verify(mesh["owner"], mesh["credential"])
    mesh["fingerprint"] = trust.fingerprint(mesh["owner"])
    if cfg["role"] == "control":
        cfg["scanner"]["backend"] = "none"
        if not cfg["admin"]["password"]:
            cfg["admin"]["password"] = _secret(cfg["state_dir"], "admin_password", 12)
    return cfg
