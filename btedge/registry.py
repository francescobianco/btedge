"""Thread-safe store of what this node sees and what the mesh reports.

Every node is the only authority on its own observations. A node's state is
versioned by a sequence number that only the owner increments; other nodes keep
the newest copy and forward it. Ages are relative (seconds since seen) so the
mesh never depends on synchronised clocks.
"""
import threading
import time

DEVICE_FIELDS = (
    "name", "rssi", "connected", "paired", "address_type", "icon",
    "tx_power", "manufacturer", "uuids", "btedge_node",
)


def _clean_device(dev):
    out = {"address": str(dev["address"])[:64], "age": float(dev.get("age", 0))}
    for key in DEVICE_FIELDS:
        if dev.get(key) is not None:
            out[key] = dev[key]
    return out


class Registry:
    def __init__(self, node, device_ttl=60.0, node_ttl=60.0, max_hops=8, clock=time.monotonic):
        self.node = node
        self.device_ttl = device_ttl
        self.node_ttl = node_ttl
        self.max_hops = max_hops
        self._clock = clock
        self._lock = threading.Lock()
        self._local = {}
        self._remote = {}
        self._links = {}
        self._seq = 0

    # -- local observations -------------------------------------------------

    def observe(self, address, fields):
        now = self._clock()
        with self._lock:
            dev = self._local.setdefault(address, {"address": address})
            for key in DEVICE_FIELDS:
                if fields.get(key) is not None:
                    dev[key] = fields[key]
            dev["_seen"] = now

    def _prune(self, now):
        stale = [a for a, d in self._local.items()
                 if now - d["_seen"] > self.device_ttl and not d.get("connected")]
        for address in stale:
            del self._local[address]
        for node_id in [n for n, s in self._remote.items() if self._age(s, now) > self.node_ttl]:
            del self._remote[node_id]
        for node_id in [n for n, l in self._links.items() if now - l["_seen"] > self.node_ttl]:
            del self._links[node_id]

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
            "seq": self._seq, "age": 0.0, "hops": 0, "devices": devices,
        }

    def _remote_states(self, now):
        for state in self._remote.values():
            elapsed = now - state["_received"]
            yield {
                "id": state["id"], "name": state["name"], "http": state["http"],
                "seq": state["seq"], "age": round(state["age"] + elapsed, 1), "hops": state["hops"],
                "devices": [dict(d, age=round(d["age"] + elapsed, 1)) for d in state["devices"]],
            }

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
                self._remote[node_id] = {
                    "id": node_id, "name": str(state.get("name") or node_id)[:64],
                    "http": http if isinstance(http, int) else None,
                    "seq": seq, "age": age, "hops": hops, "devices": devices, "_received": now,
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

        known = {s["id"] for s in states}
        beacons = {d["btedge_node"]: d for d in local["devices"] if d.get("btedge_node")}
        nodes, devices = [], {}
        for state in states:
            beacon = beacons.get(state["id"])
            nodes.append({
                "id": state["id"], "name": state["name"], "self": state is local,
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
        ordered = sorted(devices.values(),
                         key=lambda d: (d["age"] > 15, d["rssi"] is None, -(d["rssi"] or 0)))
        return {"node": dict(self.node), "nodes": nodes, "devices": ordered}
