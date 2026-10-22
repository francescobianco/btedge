"""Mesh transports: UDP multicast on the LAN and HTTP push-pull with IP peers.

Both carry the same signed packet (see protocol.py). LAN gossip discovers
neighbours with zero configuration; IP peers let nodes on different networks
(or behind NAT, talking to a public node) join the same mesh. Every node
forwards the states it knows, so the mesh spans all of them.
"""
import fcntl
import logging
import socket
import struct
import threading
import time
import urllib.request
from urllib.parse import urlparse

from . import protocol

log = logging.getLogger(__name__)

UDP_LIMIT = 60000
SIOCGIFADDR = 0x8915


def interface_address(name):
    """IPv4 address of a network interface (name like "eth0", or an address as-is)."""
    try:
        socket.inet_aton(name)
        return name
    except OSError:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        ifreq = struct.pack("256s", name.encode()[:15])
        return socket.inet_ntoa(fcntl.ioctl(sock.fileno(), SIOCGIFADDR, ifreq)[20:24])


class Mesh:
    def __init__(self, registry, cfg):
        self.registry = registry
        self.node_id = registry.node["id"]
        self.key = cfg.get("key") or ""
        self.interval = float(cfg["gossip_interval"])
        self.lan_cfg = cfg["lan"]
        self.peers = [p.rstrip("/") for p in cfg.get("peers", [])]
        self._stop = threading.Event()
        self._sock = None
        self.lan_status = {"enabled": bool(self.lan_cfg.get("enabled")), "error": None,
                           "sent": 0, "received": 0, "rejected": 0}
        self.peer_status = {p: {"ok": None, "error": None, "last_ok": None} for p in self.peers}

    def start(self):
        if self.lan_status["enabled"]:
            try:
                self._open_lan()
                threading.Thread(target=self._lan_receive, name="mesh-lan-rx", daemon=True).start()
                threading.Thread(target=self._lan_gossip, name="mesh-lan-tx", daemon=True).start()
            except OSError as exc:
                self.lan_status["error"] = str(exc)
                log.error("LAN mesh disabled: %s", exc)
        if self.peers:
            threading.Thread(target=self._peer_loop, name="mesh-peers", daemon=True).start()

    def stop(self):
        self._stop.set()

    def packet(self, limit=None):
        states = self.registry.snapshot()
        pkt = protocol.encode(self.node_id, states, self.key)
        if limit and len(pkt) > limit:
            pkt = protocol.encode(self.node_id, states[:1], self.key)
        return pkt

    def receive(self, packet, via, source):
        """Validate and merge a packet; returns the sender id or None if rejected."""
        try:
            msg = protocol.decode(packet, self.key)
        except protocol.ProtocolError as exc:
            log.debug("rejected %s packet from %s: %s", via, source, exc)
            return None
        if msg["from"] == self.node_id:
            return msg["from"]
        self.registry.merge(msg["from"], msg["states"], via=via, source=source)
        return msg["from"]

    # -- LAN multicast ------------------------------------------------------

    def _open_lan(self):
        group, port = self.lan_cfg["group"], int(self.lan_cfg["port"])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
        # Without an explicit interface the kernel follows the routing table, which on
        # machines with a VPN default route sends multicast into the tunnel.
        local = interface_address(self.lan_cfg["interface"]) if self.lan_cfg.get("interface") else "0.0.0.0"
        mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(local))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        if local != "0.0.0.0":
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        self._sock = sock
        self._group = (group, port)

    def _lan_receive(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65535)
            except OSError as exc:
                self.lan_status["error"] = str(exc)
                time.sleep(1)
                continue
            sender = self.receive(data, "lan", addr[0])
            if sender is None:
                self.lan_status["rejected"] += 1
            elif sender != self.node_id:
                self.lan_status["received"] += 1

    def _lan_gossip(self):
        while not self._stop.is_set():
            try:
                self._sock.sendto(self.packet(limit=UDP_LIMIT), self._group)
                self.lan_status["sent"] += 1
                self.lan_status["error"] = None
            except OSError as exc:
                self.lan_status["error"] = str(exc)
            self._stop.wait(self.interval)

    # -- IP peers -----------------------------------------------------------

    def sync_peer(self, url):
        status = self.peer_status.setdefault(url, {"ok": None, "error": None, "last_ok": None})
        req = urllib.request.Request(url + "/api/mesh/sync", data=self.packet(), method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read(protocol.MAX_PACKET + 1)
            if self.receive(body, "ip", urlparse(url).hostname) is None:
                raise ValueError("invalid reply (mesh key mismatch?)")
            status.update(ok=True, error=None, last_ok=time.time())
        except Exception as exc:  # network errors are expected; keep the loop alive
            status.update(ok=False, error=str(exc))
            log.debug("peer %s: %s", url, exc)

    def _peer_loop(self):
        while not self._stop.is_set():
            for url in self.peers:
                self.sync_peer(url)
            self._stop.wait(self.interval)

    def status(self):
        return {
            "lan": dict(self.lan_status, group="%s:%s" % (self.lan_cfg["group"], self.lan_cfg["port"])),
            "peers": [dict(s, url=u) for u, s in self.peer_status.items()],
            "signed": bool(self.key),
        }
