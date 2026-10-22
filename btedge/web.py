"""HTTP server: the panel, its JSON API and the mesh sync endpoint."""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, protocol

log = logging.getLogger(__name__)

STATIC = os.path.join(os.path.dirname(__file__), "static")


class Panel:
    def __init__(self, registry, mesh, backend, host="0.0.0.0", port=4646):
        self.registry = registry
        self.mesh = mesh
        self.backend = backend
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

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                        self._send(200, fh.read(), "text/html; charset=utf-8")
                elif path == "/api/state":
                    self._send(200, json.dumps(panel.state()).encode(), "application/json")
                elif path == "/api/health":
                    self._send(200, b'{"ok":true}', "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
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
