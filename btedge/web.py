"""HTTP server: the panel, its JSON API and the mesh sync endpoint."""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, protocol
from .history import OUTSIDE

log = logging.getLogger(__name__)

STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_WINDOW = 30 * 86400
MAX_BODY = 64 * 1024


class Panel:
    def __init__(self, registry, mesh, backend, host="0.0.0.0", port=4646, history=None, tracker=None):
        self.registry = registry
        self.mesh = mesh
        self.backend = backend
        self.history = history
        self.tracker = tracker
        self.server = ThreadingHTTPServer((host, port), self._handler())
        self.server.daemon_threads = True

    @property
    def port(self):
        return self.server.server_address[1]

    def start(self):
        threading.Thread(target=self.server.serve_forever, name="http", daemon=True).start()

    def stop(self):
        self.server.shutdown()

    def state(self):
        view = self.registry.view()
        view.update(version=__version__, time=time.time(),
                    scanner=dict(self.backend.status) if self.backend else None,
                    mesh=self.mesh.status() if self.mesh else None)
        return view

    def rooms(self, window):
        """Rooms (nodes) with the devices currently inside, flows and recent moves."""
        now = time.time()
        since = now - window
        view = self.registry.view()
        names = self.history.node_names()
        live = {d["address"]: d for d in view["devices"]}
        labels = {a: v[0] for a, v in view["labels"].items() if v[0]}
        current = self.tracker.current()
        rooms = {}
        for node in view["nodes"]:
            rooms[node["id"]] = {"id": node["id"], "name": node["name"], "self": node["self"],
                                 "online": True, "last_seen": now - node["age"], "devices": []}

        def room(node_id):
            if node_id not in rooms:
                rooms[node_id] = {"id": node_id, "name": names.get(node_id, node_id), "self": False,
                                  "online": False, "last_seen": None, "devices": []}
            return rooms[node_id]

        for node in self.history.nodes_since(since):
            room(node["node_id"])["last_seen"] = node["last_seen"]
        for address, cur in current.items():
            dev = live.get(address, {})
            room(cur["node"])["devices"].append({
                "address": address, "name": dev.get("name"), "label": labels.get(address),
                "rssi": dev.get("rssi"), "since": cur["since"], "manufacturer": dev.get("manufacturer"),
            })
        outside = {"id": OUTSIDE, "name": "Fuori portata", "self": False, "online": True,
                   "virtual": True, "last_seen": now, "devices": []}
        for gone in self.history.departed(since):
            if gone["address"] not in current:
                outside["devices"].append({
                    "address": gone["address"], "name": gone["name"],
                    "label": labels.get(gone["address"]) or gone["label"], "rssi": None,
                    "since": gone["ts"], "from": gone["from_node"],
                    "manufacturer": [gone["manufacturer"]] if gone["manufacturer"] is not None else None,
                })
        flows = self.history.flows(since)
        moves = self.history.moves(since, limit=150)
        for item in flows + moves:
            for key in ("from_node", "to_node"):
                if item.get(key) and item[key] != OUTSIDE:
                    room(item[key])
        for move in moves:
            move["label"] = labels.get(move["address"]) or move["label"]
        for r in rooms.values():
            r["devices"].sort(key=lambda d: (not d["label"], not d["name"], -(d["rssi"] or -999)))
        outside["devices"].sort(key=lambda d: (not d["label"], not d["name"], -d["since"]))
        return {"now": now, "window": window, "self": self.registry.node["id"], "outside": OUTSIDE,
                "rooms": list(rooms.values()) + [outside], "layout": self.history.layout(),
                "flows": flows, "moves": moves}

    def journey(self, address, window):
        now = time.time()
        since = now - window
        view = self.registry.view()
        names = self.history.node_names()
        names.update({n["id"]: n["name"] for n in view["nodes"]})
        live = next((d for d in view["devices"] if d["address"] == address), None)
        stored = self.history.device(address) or {}
        label = view["labels"].get(address, [stored.get("label")])[0] or None
        stays = self.history.stays(address, since)
        for stay in stays:
            stay["name"] = names.get(stay["node_id"], stay["node_id"])
        current = self.tracker.current().get(address)
        return {
            "now": now, "window": window,
            "device": {"address": address, "label": label,
                       "name": (live or {}).get("name") or stored.get("name"),
                       "manufacturer": (live or {}).get("manufacturer") or
                       ([stored["manufacturer"]] if stored.get("manufacturer") is not None else None),
                       "first_seen": stored.get("first_seen"), "last_seen": stored.get("last_seen"),
                       "room": current["node"] if current else None,
                       "seen_by": (live or {}).get("seen_by", [])},
            "stays": stays,
            "moves": self.history.moves(since, limit=500, address=address),
        }

    def _handler(self):
        panel = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "btedge/" + __version__

            def log_message(self, fmt, *args):
                log.debug("%s %s", self.address_string(), fmt % args)

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, data, code=200):
                self._send(code, json.dumps(data).encode(), "application/json")

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_BODY:
                    raise ValueError("bad length")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("expected an object")
                return data

            def do_GET(self):
                url = urlparse(self.path)
                path = url.path
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                try:
                    window = min(MAX_WINDOW, max(60.0, float(query.get("window", 3600))))
                except ValueError:
                    window = 3600.0
                if path in ("/", "/index.html"):
                    with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                        self._send(200, fh.read(), "text/html; charset=utf-8")
                elif path == "/api/state":
                    self._send(200, json.dumps(panel.state()).encode(), "application/json")
                elif path == "/api/rooms" and panel.history:
                    self._json(panel.rooms(window))
                elif path == "/api/journey" and panel.history and query.get("address"):
                    self._json(panel.journey(query["address"], window))
                elif path == "/api/health":
                    self._send(200, b'{"ok":true}', "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
                if self.path in ("/api/label", "/api/layout") and panel.history:
                    try:
                        data = self._body()
                        if self.path == "/api/label":
                            panel.registry.set_label(str(data["address"])[:64], str(data.get("label") or ""))
                        else:
                            panel.history.set_layout(str(data["node_id"])[:64], float(data["x"]), float(data["y"]))
                    except (KeyError, TypeError, ValueError) as exc:
                        self._json({"error": str(exc)}, 400)
                        return
                    self._json({"ok": True})
                    return
                if self.path != "/api/mesh/sync" or panel.mesh is None:
                    self._send(404, b"not found", "text/plain")
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > protocol.MAX_PACKET:
                    self._send(413, b"bad length", "text/plain")
                    return
                if panel.mesh.receive(self.rfile.read(length), "ip", self.client_address[0]) is None:
                    self._send(403, b"rejected", "text/plain")
                    return
                self._send(200, panel.mesh.packet(), "application/octet-stream")

        return Handler
