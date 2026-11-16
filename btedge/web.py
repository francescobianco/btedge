"""HTTP server: the panel, its JSON API and the mesh sync endpoint."""
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, protocol
from . import automation
from .actions import BLUETOOTH
from .registry import SNIPPET_MAX, clean_alarms
from .auth import Login, LoginError

log = logging.getLogger(__name__)

STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_BODY = 64 * 1024


class Panel:
    def __init__(self, registry, mesh, backend, host="0.0.0.0", port=4646,
                 admin_password=None, join=None, admin_user="admin", profiler=None, password_file=None):
        self.registry = registry
        self.profiler = profiler
        self.login = Login(admin_user, admin_password, password_file)
        self.join = join
        self.mesh = mesh
        self.backend = backend
        with open(os.path.join(STATIC, "index.html"), "rb") as fh:
            # lets a panel left open notice that the node was updated, and reload itself
            self.build = hashlib.sha1(fh.read()).hexdigest()[:12]
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
        if self.profiler:
            for dev in view["devices"]:
                dev["profile"] = self.profiler.profile(dev)
        view.update(version=__version__, build=self.build, time=time.time(),
                    scanner=dict(self.backend.status) if self.backend else None,
                    mesh=self.mesh.status() if self.mesh else None, join=self.join,
                    user=self.login.user if self.login.required else None)
        return view

    def act(self, address, action):
        """Send an action to the node that hears the device best; returns {"id", "node"}."""
        view = self.registry.view()
        dev = next((d for d in view["devices"] if d["address"] == address), None)
        if dev is None:
            raise LookupError("device not in range")
        profile = self.profiler.profile(dev) if self.profiler else None
        if not profile or action not in {a["id"] for a in profile["actions"]}:
            raise LookupError("action %r not available for this device" % action)
        # connected first (no need to connect), then the strongest signal
        best = sorted(dev["seen_by"], key=lambda s: (not s["connected"], s["age"] > 30, -(s["rssi"] or -999)))[0]
        return {"id": self.registry.issue(best["node"], address, action), "node": best["node"], "name": best["name"]}

    def switch(self, node_id, mode):
        """Ask a node to put its Bluetooth in a mode: "on", "released" or "off" (see actions.py)."""
        node = next((n for n in self.registry.view()["nodes"] if n["id"] == node_id), None)
        if node is None or node["bluetooth"] is None:
            raise LookupError("node not in the mesh, without Bluetooth or too old")
        action = next(a for a, m in BLUETOOTH.items() if m == mode)
        sent = {"id": self.registry.issue(node_id, "", action), "node": node_id, "name": node["name"]}
        if self.mesh:
            self.mesh.kick()
        return sent

    def save_snippet(self, data):
        """Create, change or delete ({"id", "delete": true}) a snippet for the whole mesh."""
        sid = str(data.get("id") or secrets.token_hex(4))[:32]
        if not re.fullmatch(r"[0-9a-zA-Z_-]+", sid):
            raise ValueError("bad snippet id")
        if data.get("delete"):
            self.registry.set_shared("snippet:" + sid, None)
            return {"id": sid}
        name, code = str(data.get("name") or "").strip()[:64], data.get("code")
        if not name:
            raise ValueError("Dai un nome allo snippet")
        if not isinstance(code, str) or len(code) > SNIPPET_MAX:
            raise ValueError("Lo script può avere al massimo %d caratteri" % SNIPPET_MAX)
        error = automation.check(code)
        if error:
            raise ValueError(error)
        if not self.registry.set_shared("snippet:" + sid, {"name": name, "code": code,
                                                           "enabled": data.get("enabled") is not False}):
            raise ValueError("Troppe impostazioni condivise nella mesh")
        if self.mesh:
            self.mesh.kick()
        return {"id": sid}

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

            def _authorized(self):
                """Everything except health and the (HMAC-signed) mesh sync needs the admin login."""
                if panel.login.authorized(self.headers.get("Authorization")):
                    return True
                body = b"authentication required"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="btedge", charset="UTF-8"')
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return False

            def do_GET(self):
                url = urlparse(self.path)
                path = url.path
                if path != "/api/health" and not self._authorized():
                    return
                if path in ("/", "/index.html"):
                    with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                        self._send(200, fh.read(), "text/html; charset=utf-8")
                elif path == "/api/state":
                    self._send(200, json.dumps(panel.state()).encode(), "application/json")
                elif path == "/api/action":
                    cid = parse_qs(url.query).get("id", [""])[0]
                    self._json(panel.registry.result(cid) or {"status": "pending"})
                elif path == "/api/health":
                    self._send(200, b'{"ok":true}', "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
                if self.path != "/api/mesh/sync" and not self._authorized():
                    return
                if self.path == "/api/action":
                    try:
                        data = self._body()
                        self._json(panel.act(str(data["address"])[:64], str(data["action"])[:32]))
                    except LookupError as exc:
                        self._json({"error": str(exc)}, 409)
                    except (KeyError, TypeError, ValueError) as exc:
                        self._json({"error": str(exc)}, 400)
                    return
                if self.path == "/api/bluetooth":
                    try:
                        data = self._body()
                        if data["mode"] not in BLUETOOTH.values():
                            raise ValueError("mode must be one of %s" % ", ".join(BLUETOOTH.values()))
                        self._json(panel.switch(str(data["node"])[:32], data["mode"]))
                    except KeyError as exc:
                        self._json({"error": "missing %s" % exc}, 400)
                    except LookupError as exc:
                        self._json({"error": str(exc)}, 409)
                    except (TypeError, ValueError) as exc:
                        self._json({"error": str(exc)}, 400)
                    return
                if self.path == "/api/alarms":
                    try:
                        data = self._body()
                        alarms = clean_alarms(data)
                        if alarms is None:
                            raise ValueError("bad alarms")
                        if not panel.registry.set_shared("alarms:" + str(data["address"])[:64], alarms):
                            raise ValueError("Troppe impostazioni condivise nella mesh")
                        if panel.mesh:
                            panel.mesh.kick()
                        self._json({"ok": True})
                    except (KeyError, TypeError, ValueError) as exc:
                        self._json({"error": str(exc)}, 400)
                    return
                if self.path in ("/api/autoconnect", "/api/snippet"):
                    try:
                        data = self._body()
                        if self.path == "/api/snippet":
                            self._json(panel.save_snippet(data))
                        else:
                            if not isinstance(data["enabled"], bool):
                                raise ValueError("enabled must be true or false")
                            panel.registry.set_shared("autoconnect:" + str(data["address"])[:64],
                                                      True if data["enabled"] else None)
                            self._json({"ok": True})
                    except (KeyError, TypeError, ValueError) as exc:
                        self._json({"error": str(exc)}, 400)
                    return
                if self.path == "/api/password":
                    try:
                        data = self._body()
                        panel.login.change(data["current"], data["new"])
                    except LoginError as exc:
                        self._json({"error": exc.message}, exc.status)
                    except (KeyError, TypeError, ValueError) as exc:
                        self._json({"error": str(exc)}, 400)
                    except OSError:
                        log.exception("cannot save the panel password")
                        self._json({"error": "Impossibile salvare la password sul nodo"}, 500)
                    else:
                        self._json({"ok": True})
                    return
                if self.path == "/api/label":
                    try:
                        data = self._body()
                        panel.registry.set_label(str(data["address"])[:64], str(data.get("label") or ""))
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
                reply = panel.mesh.packet()
                if reply is None:
                    self._send(503, b"this node is not enrolled", "text/plain")
                    return
                self._send(200, reply, "application/octet-stream")

        return Handler
