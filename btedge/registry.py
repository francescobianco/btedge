"""Thread-safe store of what this node sees and what the mesh reports.

Every node is the only authority on its own observations. A node's state is
versioned by a sequence number that only the owner increments; other nodes keep
the newest copy and forward it. Ages are relative (seconds since seen) so the
mesh never depends on synchronised clocks.

Device labels set by users travel with the owner's state and are resolved
last-writer-wins on their (wall clock) timestamp.

Actions on devices travel the same way: the node where someone clicks puts a
command in its own state, addressed to the node that should run it (the one that
hears the device best); that node runs it and publishes the result in its state.
Commands and results carry relative ages and expire after COMMAND_TTL seconds.
Only mesh members can sign states, so only they can issue commands.
"""
import json
import logging
import os
import secrets
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

DEVICE_FIELDS = (
    "name", "rssi", "connected", "paired", "address_type", "icon",
    "tx_power", "manufacturer", "uuids", "btedge_node",
)


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
                 labels_path=None):
        self.node = node
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
        self.labels_path = labels_path
        if labels_path:
            self.load_labels(_read_labels(labels_path))

    # -- local observations -------------------------------------------------

    def observe(self, address, fields):
        now = self._clock()
        with self._lock:
            dev = self._local.setdefault(address, {"address": address})
            for key in DEVICE_FIELDS:
                if fields.get(key) is not None:
                    dev[key] = fields[key]
            dev["_seen"] = now

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
        """States to gossip: our own (with a fresh seq) followed by every known remote."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._seq = max(self._seq + 1, int(time.time() * 1000))
            return [self._local_state(now)] + list(self._remote_states(now))

    def merge(self, sender, states, via, source=None):
        """Merge states received from `sender` over transport `via`. Returns how many were accepted."""
        now = self._clock()
        accepted = 0
        with self._lock:
            if sender != self.node["id"]:
                self._links[sender] = {"via": via, "addr": source, "_seen": now}
            for state in states:
                try:
                    node_id = str(state["id"])
                    seq = int(state["seq"])
                    age = float(state.get("age", 0))
                    hops = int(state.get("hops", 0)) + 1
                    devices = [_clean_device(d) for d in state.get("devices", [])
                               if isinstance(d, dict) and d.get("address")]
                except (KeyError, TypeError, ValueError):
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
                    "seq": seq, "age": age, "hops": hops, "devices": devices, "_received": now,
                    "labels": _clean_labels(state.get("labels")),
                    "commands": _clean_commands(state.get("commands")),
                    "results": _clean_results(state.get("results")),
                }
                accepted += 1
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
                "hops": state["hops"], "age": state["age"], "devices": 0,
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
                    "age": dev["age"], "connected": bool(dev.get("connected")),
                })

        for agg in devices.values():
            agg["seen_by"].sort(key=lambda s: (s["rssi"] is None, -(s["rssi"] or 0)))
            agg["rssi"] = agg["seen_by"][0]["rssi"]
            agg["age"] = min(s["age"] for s in agg["seen_by"])
            agg["local"] = any(s["node"] == self.node["id"] for s in agg["seen_by"])
            agg["label"] = (labels[agg["address"]][0] or None) if agg["address"] in labels else None
        ordered = sorted(devices.values(),
                         key=lambda d: (d["age"] > 15, d["rssi"] is None, -(d["rssi"] or 0)))
        return {"node": dict(self.node), "nodes": nodes, "devices": ordered, "labels": labels}
