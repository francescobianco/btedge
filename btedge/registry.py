"""Thread-safe store of what this node sees and what the mesh reports.

Every node is the only authority on its own observations. A node's state is
versioned by a sequence number that only the owner increments and signed with
its node key (see trust.py); other nodes keep the newest copy and forward the
signed body untouched. Ages inside a body are relative to its creation and the
state's own age travels outside the signature, so the mesh never depends on
synchronised clocks.

Device labels set by users travel with the owner's state and are resolved
last-writer-wins on their (wall clock) timestamp.

Actions on devices travel the same way: the node where someone clicks puts a
command in its own state, addressed to the node that should run it (the one that
hears the device best); that node runs it and publishes the result in its state.
Commands and results carry relative ages and expire after COMMAND_TTL seconds.
Only mesh members can sign states, so only they can issue commands.

Shared settings are the part of the mesh's configuration every node must know and
keep: which devices to connect automatically ("autoconnect:<address>") and the
automation snippets ("snippet:<id>", see automation.py), plus what the mesh wants on a
device: its alarms ("alarms:<address>": {"slots": [{"time": "07:00", "days": [0..6]}],
"tz"}, 0 = Monday) and the digest of the alarms last written on it ("applied:<address>").
Each one is [value, timestamp],
last writer wins, a null value is a deletion. A node adopts every newer entry it hears,
saves it in <state_dir>/shared.json and republishes it in its own state, so the settings
reach every node and survive the node that wrote them.

A node may have several Bluetooth radios (dongles): a device heard by more than one is
still one device, reported with the radio that is connected to it or hears it best.

A command with an empty address is for the node itself: that is how a panel asks
a node to release its Bluetooth adapter, switch the radio off or take it back (see
actions.py). Each node publishes its mode in the "bluetooth" field of its state.
"""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

DEVICE_FIELDS = (
    "name", "rssi", "connected", "paired", "address_type", "icon",
    "tx_power", "manufacturer", "uuids", "btedge_node", "radio",
)
SNIPPET_MAX = 4096       # characters of code in a snippet
SHARED_MAX = 512         # shared settings a node keeps


BLUETOOTH_MODES = ("on", "released", "off")
COMMAND_TTL = 60.0
RESULT_TTL = 120.0
STATUSES = ("running", "ok", "error")


def _clean_commands(commands):
    out = []
    for cmd in commands if isinstance(commands, list) else []:
        try:
            out.append({"id": str(cmd["id"])[:32], "node": str(cmd["node"])[:32],
                        "address": str(cmd["address"])[:64], "action": str(cmd["action"])[:32],
                        "age": float(cmd.get("age", 0))})
        except (KeyError, TypeError, ValueError):
            continue
    return out[:64]


def _clean_results(results):
    out = {}
    for cid, res in (results if isinstance(results, dict) else {}).items():
        if isinstance(res, dict) and res.get("status") in STATUSES:
            try:
                out[str(cid)[:32]] = {"status": res["status"], "error": str(res.get("error") or "")[:200] or None,
                                      "age": float(res.get("age", 0))}
            except (TypeError, ValueError):
                continue
    return out


ALARM_TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
ALARM_SLOTS = 16         # alarm times kept for a device; each adapter has its own, lower, limit


def clean_alarms(value):
    """Alarms as the mesh keeps them, or None if they are not valid."""
    if not isinstance(value, dict) or not isinstance(value.get("slots"), list) or len(value["slots"]) > ALARM_SLOTS:
        return None
    slots = []
    for slot in value["slots"]:
        if not (isinstance(slot, dict) and isinstance(slot.get("time"), str) and ALARM_TIME.fullmatch(slot["time"])
                and isinstance(slot.get("days"), list)
                and all(type(d) is int and 0 <= d <= 6 for d in slot["days"])):
            return None
        slots.append({"time": slot["time"], "days": sorted(set(slot["days"]))})
    tz = value.get("tz")
    return {"slots": slots, "tz": tz[:64] if isinstance(tz, str) else None}


def digest(value):
    return hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()[:12]


def _clean_shared(shared):
    out = {}
    for key, entry in shared.items() if isinstance(shared, dict) else ():
        if len(out) >= SHARED_MAX:
            break
        if not (isinstance(key, str) and len(key) <= 96 and isinstance(entry, list) and len(entry) == 2
                and isinstance(entry[1], (int, float)) and not isinstance(entry[1], bool)):
            continue
        value = entry[0]
        if key.startswith("autoconnect:"):
            valid = value is True or value is None
        elif key.startswith("snippet:"):
            valid = value is None or (isinstance(value, dict) and isinstance(value.get("name"), str)
                                      and isinstance(value.get("code"), str) and len(value["code"]) <= SNIPPET_MAX
                                      and isinstance(value.get("enabled"), bool))
            if valid and value is not None:
                value = {"name": value["name"][:64], "code": value["code"], "enabled": value["enabled"]}
        elif key.startswith("alarms:"):
            value = None if value is None else clean_alarms(value)
            valid = value is not None or entry[0] is None
        elif key.startswith("applied:"):
            valid = value is None or (isinstance(value, str) and len(value) <= 16)
        else:
            valid = False
        if valid:
            out[key] = [value, float(entry[1])]
    return out


def _clean_radios(radios):
    out = []
    for radio in radios if isinstance(radios, list) else []:
        if isinstance(radio, dict) and isinstance(radio.get("name"), str):
            out.append({"name": radio["name"][:16], "address": str(radio.get("address") or "")[:32],
                        "state": str(radio.get("state") or "")[:16],
                        "error": str(radio.get("error") or "")[:160] or None})
    return out[:8]


def _clean_automation(status):
    out = {}
    for sid, entry in status.items() if isinstance(status, dict) else ():
        if isinstance(entry, dict) and len(out) < 64:
            lines = entry.get("log") if isinstance(entry.get("log"), list) else []
            out[str(sid)[:32]] = {"error": str(entry.get("error") or "")[:300] or None,
                                  "log": [str(line)[:210] for line in lines[:5]]}
    return out


def _clean_labels(labels):
    out = {}
    if isinstance(labels, dict):
        for address, value in labels.items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], (int, float)):
                out[str(address)[:64]] = [str(value[0] or "")[:64], float(value[1])]
    return out


def _clean_device(dev):
    out = {"address": str(dev["address"])[:64], "age": float(dev.get("age", 0))}
    for key in DEVICE_FIELDS:
        if dev.get(key) is not None:
            out[key] = dev[key]
    return out


def _read_labels(path):
    """Own labels from labels.json; the first time, import them from the old history db."""
    try:
        with open(path) as fh:
            return _clean_labels(json.load(fh))
    except FileNotFoundError:
        pass
    except ValueError:
        log.warning("ignoring unreadable %s", path)
        return {}
    old = os.path.join(os.path.dirname(path), "btedge.db")
    if not os.path.exists(old):
        return {}
    try:
        with sqlite3.connect(old) as db:
            rows = db.execute("SELECT address, label, label_ts FROM devices WHERE label_ts IS NOT NULL").fetchall()
    except sqlite3.Error:
        return {}
    labels = {a: [l or "", ts] for a, l, ts in rows}
    with open(path, "w") as fh:
        json.dump(labels, fh)
    log.info("imported %d device names from %s", len(labels), old)
    return labels


class Registry:
    def __init__(self, node, device_ttl=60.0, node_ttl=60.0, max_hops=8, clock=time.monotonic,
                 labels_path=None, identity=None, verifier=None, shared_path=None):
        self.node = node
        self.identity = identity
        self.verifier = verifier
        self.device_ttl = device_ttl
        self.node_ttl = node_ttl
        self.max_hops = max_hops
        self._clock = clock
        self._lock = threading.Lock()
        self._local = {}
        self._remote = {}
        self._links = {}
        self._labels = {}
        self._commands = {}  # id -> command issued here, with "_at"
        self._results = {}   # id -> result of a command run here, with "_at"
        self._seq = 0
        self._bluetooth = None  # one of BLUETOOTH_MODES; None on a node without Bluetooth
        self._radios = []       # this node's Bluetooth radios: [{"name", "address", "state", "error"}]
        self._automation = {}   # snippet id -> {"error", "log"} on this node
        self._shared = {}       # key -> [value, timestamp], see the module docstring
        self._autoconnect = frozenset()
        self.shared_version = 0  # bumped whenever a shared setting changes
        self.shared_path = shared_path
        if shared_path:
            try:
                with open(shared_path) as fh:
                    self._adopt(json.load(fh))
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                log.warning("ignoring unreadable %s", shared_path)
        self.labels_path = labels_path
        if labels_path:
            self.load_labels(_read_labels(labels_path))

    # -- local observations -------------------------------------------------

    def observe(self, address, fields, radio=None):
        """A device was heard, or changed, on one of this node's radios."""
        now = self._clock()
        with self._lock:
            if self._bluetooth not in (None, "on"):
                return  # released or off: whatever the adapter still hears is the host's business
            dev = self._local.setdefault(address, {"address": address, "_radios": {}})
            for key in DEVICE_FIELDS:
                if fields.get(key) is not None:
                    dev[key] = fields[key]
            dev["_seen"] = now
            heard = dev["_radios"].setdefault(radio, {})
            heard.update({k: fields[k] for k in ("rssi", "connected") if fields.get(k) is not None}, _seen=now)
            if len(dev["_radios"]) > 1:
                # several radios: the device is where it is connected, else where it is loudest
                live = {r: h for r, h in dev["_radios"].items()
                        if h.get("connected") or now - h["_seen"] <= self.device_ttl}
                dev["_radios"] = live
                radio, best = max(live.items(), key=lambda item: (bool(item[1].get("connected")),
                                                                  item[1].get("rssi", -999)))
                if "rssi" in best:
                    dev["rssi"] = best["rssi"]
                if any("connected" in h for h in live.values()):
                    dev["connected"] = any(h.get("connected") for h in live.values())
            if radio is not None:
                dev["radio"] = radio

    def local_devices(self):
        """What this node itself sees now: {address: device}."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            return {d["address"]: d for d in self._local_state(now)["devices"]}

    @property
    def radios(self):
        return self._radios

    def set_radios(self, radios):
        self._radios = list(radios)

    def set_automation(self, status):
        self._automation = dict(status)

    # -- shared settings ----------------------------------------------------

    def _adopt(self, shared):
        """Take the entries newer than ours (lock held); True if anything changed."""
        limit = time.time() + 86400  # a clock far in the future must not win forever
        changed = False
        for key, (value, ts) in _clean_shared(shared).items():
            current = self._shared.get(key)
            if ts <= limit and (current is None or ts > current[1]) \
                    and (current is not None or len(self._shared) < SHARED_MAX):
                self._shared[key] = [value, ts]
                changed = True
        if changed:
            self.shared_version += 1
            self._autoconnect = frozenset(k.split(":", 1)[1] for k, v in self._shared.items()
                                          if k.startswith("autoconnect:") and v[0])
        return changed

    def _save_shared(self):
        if not self.shared_path:
            return
        with self._lock:
            shared = {k: list(v) for k, v in self._shared.items()}
        try:
            tmp = self.shared_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(shared, fh)
            os.replace(tmp, self.shared_path)
        except OSError as exc:
            log.warning("cannot save %s: %s", self.shared_path, exc)

    def set_shared(self, key, value):
        """Set a shared setting for the whole mesh (None deletes it); False if it is not valid."""
        with self._lock:
            current = self._shared.get(key)
            ts = max(time.time(), current[1] + 0.001) if current else time.time()
            changed = self._adopt({key: [value, ts]})
        if changed:
            self._save_shared()
        return changed

    def shared(self, prefix):
        """Live shared settings under a prefix: {key without the prefix: value}."""
        with self._lock:
            return {k[len(prefix):]: v[0] for k, v in self._shared.items()
                    if k.startswith(prefix) and v[0] is not None}

    def alarms(self, address):
        """The alarms the mesh wants on a device, or None if they were never set."""
        with self._lock:
            entry = self._shared.get("alarms:" + address)
        return entry[0] if entry else None

    @property
    def autoconnect(self):
        """Addresses every node connects to as soon as it hears them."""
        return self._autoconnect

    def connected_elsewhere(self, address):
        """Is another node already connected to this device?"""
        with self._lock:
            return any(d["address"] == address and d.get("connected")
                       for state in self._remote.values() for d in state["devices"])

    def label(self, address):
        """The name the mesh gave to a device, or None."""
        with self._lock:
            found = [labels[address] for labels in [self._labels] + [s["labels"] for s in self._remote.values()]
                     if address in labels]
        return (max(found, key=lambda entry: entry[1])[0] or None) if found else None

    @property
    def bluetooth(self):
        return self._bluetooth

    def set_bluetooth(self, mode):
        """Publish this node's Bluetooth mode; a node that is not scanning sees nothing."""
        with self._lock:
            self._bluetooth = mode
            if mode != "on":
                self._local.clear()

    def set_label(self, address, label, ts=None):
        with self._lock:
            self._labels[address] = [(label or "").strip()[:64], ts if ts is not None else time.time()]
            labels = dict(self._labels)
        if self.labels_path:
            tmp = self.labels_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(labels, fh)
            os.replace(tmp, self.labels_path)

    def load_labels(self, labels):
        with self._lock:
            for address, (label, ts) in labels.items():
                current = self._labels.get(address)
                if current is None or ts > current[1]:
                    self._labels[address] = [label, ts]

    def _prune(self, now):
        stale = [a for a, d in self._local.items()
                 if now - d["_seen"] > self.device_ttl and not d.get("connected")]
        for address in stale:
            del self._local[address]
        for node_id in [n for n, s in self._remote.items() if self._age(s, now) > self.node_ttl]:
            del self._remote[node_id]
        for node_id in [n for n, l in self._links.items() if now - l["_seen"] > self.node_ttl]:
            del self._links[node_id]
        for cid in [c for c, v in self._commands.items() if now - v["_at"] > COMMAND_TTL]:
            del self._commands[cid]
        for cid in [c for c, v in self._results.items() if now - v["_at"] > RESULT_TTL]:
            del self._results[cid]

    @staticmethod
    def _age(state, now):
        return state["age"] + (now - state["_received"])

    def _local_state(self, now):
        devices = []
        for dev in self._local.values():
            out = {k: v for k, v in dev.items() if not k.startswith("_")}
            out["age"] = round(now - dev["_seen"], 1)
            devices.append(out)
        return {
            "id": self.node["id"], "name": self.node["name"], "http": self.node.get("http"),
            "role": self.node.get("role", "edge"), "seq": self._seq, "age": 0.0, "hops": 0, "devices": devices,
            "bluetooth": self._bluetooth, "radios": self._radios, "automation": self._automation,
            "shared": {k: list(v) for k, v in self._shared.items()},
            "labels": {a: list(v) for a, v in self._labels.items()},
            "commands": [dict({k: v for k, v in c.items() if k != "_at"}, age=round(now - c["_at"], 1))
                         for c in self._commands.values()],
            "results": {cid: {"status": r["status"], "error": r["error"], "age": round(now - r["_at"], 1)}
                        for cid, r in self._results.items()},
        }

    def _remote_states(self, now):
        for state in self._remote.values():
            elapsed = now - state["_received"]
            yield {
                "id": state["id"], "name": state["name"], "http": state["http"], "role": state["role"],
                "bluetooth": state["bluetooth"], "radios": state["radios"], "automation": state["automation"],
                "seq": state["seq"], "age": round(state["age"] + elapsed, 1), "hops": state["hops"],
                "devices": [dict(d, age=round(d["age"] + elapsed, 1)) for d in state["devices"]],
                "labels": state["labels"],
                "commands": [dict(c, age=round(c["age"] + elapsed, 1)) for c in state["commands"]],
                "results": {cid: dict(r, age=round(r["age"] + elapsed, 1)) for cid, r in state["results"].items()},
            }

    # -- device actions -----------------------------------------------------

    def issue(self, node_id, address, action):
        """Ask node_id to run action on address; returns the command id."""
        cid = secrets.token_hex(6)
        with self._lock:
            self._commands[cid] = {"id": cid, "node": node_id, "address": address,
                                   "action": action, "_at": self._clock()}
        return cid

    def pending(self):
        """Commands addressed to this node, from any node (us included), not handled yet."""
        now = self._clock()
        me = self.node["id"]
        with self._lock:
            self._prune(now)
            states = [self._local_state(now)] + list(self._remote_states(now))
            return [c for s in states for c in s["commands"]
                    if c["node"] == me and c["age"] < COMMAND_TTL and c["id"] not in self._results]

    def set_result(self, cid, status, error=None):
        with self._lock:
            self._results[cid] = {"status": status, "error": error, "_at": self._clock()}

    def result(self, cid):
        """Freshest known result of a command anywhere in the mesh, or None."""
        now = self._clock()
        with self._lock:
            states = [self._local_state(now)] + list(self._remote_states(now))
        found = [s["results"][cid] for s in states if cid in s["results"]]
        return min(found, key=lambda r: r["age"]) if found else None

    # -- mesh exchange ------------------------------------------------------

    def snapshot(self):
        """Wire states to gossip: our own, freshly signed, then every known remote as received."""
        now = self._clock()
        ident = self.identity
        with self._lock:
            self._prune(now)
            self._seq = max(self._seq + 1, int(time.time() * 1000))
            body = {k: v for k, v in self._local_state(now).items() if k not in ("age", "hops")}
            remotes = [{"body": s["_body"], "sig": s["_sig"], "age": round(self._age(s, now), 1), "hops": s["hops"]}
                       for s in self._remote.values()]
        body.update(key=ident.public, cert=ident.cert, machine=ident.machine)
        text = json.dumps(body, separators=(",", ":"), sort_keys=True)
        own = {"body": text, "sig": base64.b64encode(ident.sign(text.encode())).decode(), "age": 0.0, "hops": 0}
        return [own] + remotes

    def merge(self, sender, states, via, source=None):
        """Merge states received from `sender` over transport `via`. Returns how many were accepted."""
        now = self._clock()
        accepted, adopted = 0, False
        with self._lock:
            if sender != self.node["id"]:
                self._links[sender] = {"via": via, "addr": source, "_seen": now}
            for wire in states:
                try:
                    state = self.verifier.state(wire)
                    node_id = str(state["id"])
                    seq = int(state["seq"])
                    age = max(0.0, float(wire.get("age", 0)))
                    hops = int(wire.get("hops", 0)) + 1
                    devices = [_clean_device(d) for d in state.get("devices", [])
                               if isinstance(d, dict) and d.get("address")]
                except (KeyError, TypeError, ValueError):  # TrustError is a ValueError
                    continue
                if node_id == self.node["id"] or hops > self.max_hops or age > self.node_ttl:
                    continue
                current = self._remote.get(node_id)
                if current is not None and (seq < current["seq"]
                                            or (seq == current["seq"] and hops >= current["hops"])):
                    continue
                http = state.get("http")
                role = state.get("role") if state.get("role") in ("edge", "control") else "edge"
                self._remote[node_id] = {
                    "id": node_id, "name": str(state.get("name") or node_id)[:64],
                    "http": http if isinstance(http, int) else None, "role": role,
                    "bluetooth": state["bluetooth"] if state.get("bluetooth") in BLUETOOTH_MODES else None,
                    "radios": _clean_radios(state.get("radios")),
                    "automation": _clean_automation(state.get("automation")),
                    "seq": seq, "age": age, "hops": hops, "_received": now,
                    "_body": wire["body"], "_sig": wire["sig"],
                    # body ages are relative to its creation: add the state's age once here
                    "devices": [dict(d, age=d["age"] + age) for d in devices],
                    "labels": _clean_labels(state.get("labels")),
                    "commands": [dict(c, age=c["age"] + age) for c in _clean_commands(state.get("commands"))],
                    "results": {cid: dict(r, age=r["age"] + age)
                                for cid, r in _clean_results(state.get("results")).items()},
                }
                adopted = self._adopt(state.get("shared")) or adopted
                accepted += 1
        if adopted:
            self._save_shared()
        return accepted

    # -- panel view ---------------------------------------------------------

    def view(self):
        now = self._clock()
        with self._lock:
            self._prune(now)
            local = self._local_state(now)
            states = [local] + list(self._remote_states(now))
            links = {n: {"via": l["via"], "addr": l["addr"], "age": round(now - l["_seen"], 1)}
                     for n, l in self._links.items()}
            snippets = {k[8:]: v[0] for k, v in self._shared.items() if k.startswith("snippet:") and v[0]}
            alarms = {k[7:]: v[0] for k, v in self._shared.items() if k.startswith("alarms:") and v[0]}
            applied = {k[8:]: v for k, v in self._shared.items() if k.startswith("applied:") and v[0]}
            autoconnect = self._autoconnect

        labels = {}
        for state in states:
            for address, (label, ts) in state.get("labels", {}).items():
                if address not in labels or ts > labels[address][1]:
                    labels[address] = [label, ts]

        known = {s["id"] for s in states}
        beacons = {d["btedge_node"]: d for d in local["devices"] if d.get("btedge_node")}
        nodes, devices = [], {}
        for state in states:
            beacon = beacons.get(state["id"])
            nodes.append({
                "id": state["id"], "name": state["name"], "self": state is local, "role": state["role"],
                "hops": state["hops"], "age": state["age"], "devices": 0, "bluetooth": state["bluetooth"],
                "radios": state["radios"],
                "link": links.get(state["id"]),
                "bt": {"rssi": beacon.get("rssi"), "age": beacon["age"]} if beacon else None,
            })
            for dev in state["devices"]:
                if dev.get("btedge_node") in known:
                    continue  # another node's beacon: shown in the node list instead
                nodes[-1]["devices"] += 1
                agg = devices.setdefault(dev["address"], {"address": dev["address"], "seen_by": []})
                for key in DEVICE_FIELDS:
                    if agg.get(key) is None and dev.get(key) is not None:
                        agg[key] = dev[key]
                agg["seen_by"].append({
                    "node": state["id"], "name": state["name"], "rssi": dev.get("rssi"),
                    "age": dev["age"], "connected": bool(dev.get("connected")), "radio": dev.get("radio"),
                })

        for agg in devices.values():
            agg["seen_by"].sort(key=lambda s: (s["rssi"] is None, -(s["rssi"] or 0)))
            agg["rssi"] = agg["seen_by"][0]["rssi"]
            agg["age"] = min(s["age"] for s in agg["seen_by"])
            agg["local"] = any(s["node"] == self.node["id"] for s in agg["seen_by"])
            agg["label"] = (labels[agg["address"]][0] or None) if agg["address"] in labels else None
            agg["autoconnect"] = agg["address"] in autoconnect
            wanted, done = alarms.get(agg["address"]), applied.get(agg["address"])
            # the device cannot be read: "pending" compares what is wanted with what was last written
            agg["alarms"] = dict(wanted, applied=done[1] if done else None,
                                 pending=not done or done[0] != digest(wanted)) if wanted else None
        listed = [dict(snippet, id=sid, hosts=[dict(s["automation"][sid], node=s["id"], name=s["name"])
                                               for s in states if sid in s["automation"]])
                  for sid, snippet in snippets.items()]
        listed.sort(key=lambda s: (s["name"].lower(), s["id"]))
        ordered = sorted(devices.values(),
                         key=lambda d: (d["age"] > 15, d["rssi"] is None, -(d["rssi"] or 0)))
        return {"node": dict(self.node), "nodes": nodes, "devices": ordered, "labels": labels,
                "snippets": listed, "autoconnect": sorted(autoconnect)}
