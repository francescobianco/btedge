"""Room tracking and movement history in SQLite.

Every node of the mesh is a "room". The tracker periodically takes the mesh
view, puts each device in the room whose node hears it best (with a margin so
it does not flap between two rooms of similar strength) and records:

    stays  one row per continuous presence of a device in a room
    moves  one row per transition: room -> room, nothing -> room (arrived),
           room -> nothing (left the mesh)

Each node computes this from the same gossiped data, so every panel tells the
same story. Writes happen on transitions plus one batched flush every
`flush_interval` seconds, to spare SD cards.
"""
import logging
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

OUTSIDE = "_out"  # virtual room: out of range of every node

SCHEMA = """
CREATE TABLE IF NOT EXISTS stays (
    id INTEGER PRIMARY KEY,
    address TEXT NOT NULL,
    node_id TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    rssi INTEGER,
    open INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS stays_address ON stays(address, start);
CREATE INDEX IF NOT EXISTS stays_end ON stays(end);
CREATE TABLE IF NOT EXISTS moves (
    id INTEGER PRIMARY KEY,
    address TEXT NOT NULL,
    from_node TEXT,
    to_node TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS moves_ts ON moves(ts);
CREATE INDEX IF NOT EXISTS moves_address ON moves(address, ts);
CREATE TABLE IF NOT EXISTS devices (
    address TEXT PRIMARY KEY,
    name TEXT,
    label TEXT,
    label_ts REAL,
    manufacturer INTEGER,
    first_seen REAL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    name TEXT,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS layout (
    node_id TEXT PRIMARY KEY,
    x REAL NOT NULL,
    y REAL NOT NULL
);
"""


class History:
    def __init__(self, path, retention_days=30):
        self.retention = retention_days * 86400
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def resume(self, now, max_age):
        """Open stays from a previous run: recent ones are returned to carry on, older ones closed."""
        def run(cur):
            cur.execute("UPDATE stays SET open = 0 WHERE open = 1 AND end < ?", (now - max_age,))
            return [dict(r) for r in cur.execute(
                "SELECT id, address, node_id, start, end, rssi FROM stays WHERE open = 1").fetchall()]
        return self.write(run)

    def _query(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def write(self, fn):
        """Run fn(cursor) in one transaction."""
        with self._lock:
            cur = self._db.cursor()
            cur.execute("BEGIN")
            try:
                result = fn(cur)
                cur.execute("COMMIT")
                return result
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def purge(self, now=None):
        cutoff = (time.time() if now is None else now) - self.retention
        self.write(lambda c: (c.execute("DELETE FROM stays WHERE end < ? AND open = 0", (cutoff,)),
                              c.execute("DELETE FROM moves WHERE ts < ?", (cutoff,))))

    # -- labels and layout --------------------------------------------------

    def labels(self):
        rows = self._query("SELECT address, label, label_ts FROM devices WHERE label_ts IS NOT NULL")
        return {r["address"]: [r["label"] or "", r["label_ts"]] for r in rows}

    def layout(self):
        return {r["node_id"]: {"x": r["x"], "y": r["y"]}
                for r in self._query("SELECT node_id, x, y FROM layout")}

    def set_layout(self, node_id, x, y):
        self.write(lambda c: c.execute(
            "INSERT INTO layout(node_id, x, y) VALUES (?, ?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET x = excluded.x, y = excluded.y", (node_id, x, y)))

    # -- reads for the panel ------------------------------------------------

    def node_names(self):
        return {r["node_id"]: r["name"] for r in self._query("SELECT node_id, name FROM nodes")}

    def nodes_since(self, since):
        return self._query("SELECT node_id, name, last_seen FROM nodes WHERE last_seen >= ?", (since,))

    def flows(self, since):
        """Moves between rooms; arrivals and departures use the OUTSIDE room."""
        return self._query(
            "SELECT COALESCE(m.from_node, ?) AS from_node, COALESCE(m.to_node, ?) AS to_node, "
            "COUNT(*) AS count, SUM(d.name IS NOT NULL OR d.label IS NOT NULL) AS named "
            "FROM moves m LEFT JOIN devices d ON d.address = m.address WHERE m.ts >= ? "
            "GROUP BY 1, 2", (OUTSIDE, OUTSIDE, since))

    def departed(self, since):
        """Devices whose latest move in the period was leaving the mesh."""
        return self._query(
            "SELECT m.address, m.from_node, m.ts, d.name, d.label, d.manufacturer FROM moves m "
            "JOIN (SELECT address, MAX(ts) AS ts FROM moves WHERE ts >= ? GROUP BY address) l "
            "ON l.address = m.address AND l.ts = m.ts "
            "LEFT JOIN devices d ON d.address = m.address WHERE m.to_node IS NULL "
            "ORDER BY m.ts DESC", (since,))

    def moves(self, since, limit=200, address=None):
        sql = ("SELECT m.address, m.from_node, m.to_node, m.ts, d.name, d.label FROM moves m "
               "LEFT JOIN devices d ON d.address = m.address WHERE m.ts >= ?")
        args = [since]
        if address:
            sql += " AND m.address = ?"
            args.append(address)
        sql += " ORDER BY m.ts DESC LIMIT ?"
        args.append(limit)
        return self._query(sql, args)

    def stays(self, address, since):
        return self._query(
            "SELECT node_id, start, end, rssi, open FROM stays "
            "WHERE address = ? AND end >= ? ORDER BY start", (address, since))

    def device(self, address):
        rows = self._query("SELECT * FROM devices WHERE address = ?", (address,))
        return rows[0] if rows else None


class Tracker:
    def __init__(self, registry, history, interval=5, margin=6, stale=45, flush_interval=30):
        self.registry = registry
        self.history = history
        self.interval = interval
        self.margin = margin
        self.stale = stale
        self.flush_interval = flush_interval
        self._current = {}   # address -> {"node", "stay", "start", "end", "rssi"}
        self._devices = {}   # address -> (name, manufacturer, last_seen) pending flush
        self._nodes = {}     # node_id -> (name, last_seen) pending flush
        self._labels = {}    # address -> [label, ts] as last stored
        self._last_flush = 0.0
        self._last_purge = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self, resume_age=300):
        self._labels = self.history.labels()
        self.registry.load_labels(self._labels)
        # After a restart, devices still around continue their stay instead of "arriving" again.
        for stay in self.history.resume(time.time(), resume_age):
            self._current[stay["address"]] = {"node": stay["node_id"], "stay": stay["id"], "start": stay["start"],
                                              "end": stay["end"], "rssi": stay["rssi"]}
        threading.Thread(target=self._run, name="tracker", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("tracker step failed")
            self._stop.wait(self.interval)

    def _pick(self, device, current):
        heard = [s for s in device["seen_by"] if s["age"] <= self.stale and s["rssi"] is not None]
        if not heard:
            return None
        best = max(heard, key=lambda s: s["rssi"])
        if current is not None and best["node"] != current["node"]:
            here = next((s for s in heard if s["node"] == current["node"]), None)
            if here is not None and best["rssi"] < here["rssi"] + self.margin:
                return here
        return best

    def step(self, now=None):
        now = time.time() if now is None else now
        view = self.registry.view()
        transitions = []
        with self._lock:
            for node in view["nodes"]:
                self._nodes[node["id"]] = (node["name"], now - node["age"])
            present = set()
            for dev in view["devices"]:
                address = dev["address"]
                current = self._current.get(address)
                best = self._pick(dev, current)
                if best is None:
                    continue
                present.add(address)
                seen_at = now - best["age"]
                manufacturer = (dev.get("manufacturer") or [None])[0]
                self._devices[address] = (dev.get("name"), manufacturer, seen_at)
                if current is None or current["node"] != best["node"]:
                    transitions.append((address, current, best["node"], seen_at, best["rssi"]))
                else:
                    current["end"] = max(current["end"], seen_at)
                    current["rssi"] = max(current["rssi"] or -999, best["rssi"])
            for address in [a for a in self._current if a not in present]:
                transitions.append((address, self._current[address], None, None, None))

            changed_labels = {a: v for a, v in view["labels"].items() if self._labels.get(a) != v}
            flush = transitions or changed_labels or now - self._last_flush >= self.flush_interval
            if flush:
                self.history.write(lambda c: self._flush(c, transitions, changed_labels, now))
                self._labels.update(changed_labels)
        if now - self._last_purge > 3600:
            self._last_purge = now
            self.history.purge(now)

    def _flush(self, cur, transitions, labels, now):
        for address, current, node, ts, rssi in transitions:
            from_node = current["node"] if current else None
            if current:
                cur.execute("UPDATE stays SET end = ?, rssi = ?, open = 0 WHERE id = ?",
                            (current["end"], current["rssi"], current["stay"]))
                del self._current[address]
            if node is None:
                cur.execute("INSERT INTO moves(address, from_node, to_node, ts) VALUES (?, ?, NULL, ?)",
                            (address, from_node, current["end"]))
                continue
            cur.execute("INSERT INTO stays(address, node_id, start, end, rssi) VALUES (?, ?, ?, ?, ?)",
                        (address, node, ts, ts, rssi))
            self._current[address] = {"node": node, "stay": cur.lastrowid, "start": ts, "end": ts, "rssi": rssi}
            cur.execute("INSERT INTO moves(address, from_node, to_node, ts) VALUES (?, ?, ?, ?)",
                        (address, from_node, node, ts))
        for state in self._current.values():
            cur.execute("UPDATE stays SET end = ?, rssi = ? WHERE id = ?",
                        (state["end"], state["rssi"], state["stay"]))
        for address, (name, manufacturer, seen) in self._devices.items():
            cur.execute(
                "INSERT INTO devices(address, name, manufacturer, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(address) DO UPDATE SET name = COALESCE(excluded.name, devices.name), "
                "manufacturer = COALESCE(excluded.manufacturer, devices.manufacturer), "
                "last_seen = MAX(devices.last_seen, excluded.last_seen)",
                (address, name, manufacturer, seen, seen))
        for address, (label, ts) in labels.items():
            cur.execute(
                "INSERT INTO devices(address, label, label_ts, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(address) DO UPDATE SET label = excluded.label, label_ts = excluded.label_ts",
                (address, label or None, ts, now, now))
        for node_id, (name, seen) in self._nodes.items():
            cur.execute(
                "INSERT INTO nodes(node_id, name, last_seen) VALUES (?, ?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET name = excluded.name, "
                "last_seen = MAX(nodes.last_seen, excluded.last_seen)", (node_id, name, seen))
        self._devices.clear()
        self._nodes.clear()
        self._last_flush = now

    def current(self):
        with self._lock:
            return {a: {"node": s["node"], "since": s["start"], "rssi": s["rssi"]}
                    for a, s in self._current.items()}
