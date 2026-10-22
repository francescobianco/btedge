"""Entry point: python3 -m btedge [--config FILE] [--fake]"""
import argparse
import logging
import os
import signal
import threading

from . import config
from .bluez import BluezBackend, FakeBackend
from .history import History, Tracker
from .mesh import Mesh
from .registry import Registry
from .web import Panel

log = logging.getLogger("btedge")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="btedge", description="Bluetooth edge mesh node")
    parser.add_argument("-c", "--config", help="JSON config file (default: %s if present)" % config.DEFAULT_PATH)
    parser.add_argument("--fake", action="store_true", help="simulate Bluetooth devices")
    parser.add_argument("--port", type=int, help="override the HTTP port")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load(args.config)
    if args.fake:
        cfg["scanner"]["backend"] = "fake"
    if args.port:
        cfg["http"]["port"] = args.port

    node = {"id": cfg["node_id"], "name": cfg["node_name"], "http": cfg["http"]["port"]}
    registry = Registry(node, device_ttl=cfg["scanner"]["device_ttl"],
                        node_ttl=cfg["mesh"]["node_ttl"], max_hops=cfg["mesh"]["max_hops"])
    scanner = cfg["scanner"]
    if scanner["backend"] == "fake":
        backend = FakeBackend(registry, room=scanner["fake_room"], rooms=scanner["fake_rooms"])
    else:
        backend = BluezBackend(registry, node["id"], adapter=scanner["adapter"],
                               advertise=scanner["advertise"], cache_ttl=scanner["cache_ttl"],
                               rescan_interval=scanner["rescan_interval"])
    mesh = Mesh(registry, cfg["mesh"])
    hist = cfg["history"]
    history = History(os.path.join(cfg["state_dir"], "btedge.db"), retention_days=hist["retention_days"])
    tracker = Tracker(registry, history, margin=hist["room_margin"], flush_interval=hist["flush_interval"])
    panel = Panel(registry, mesh, backend, cfg["http"]["host"], cfg["http"]["port"],
                  history=history, tracker=tracker)

    tracker.start()
    backend.start()
    mesh.start()
    panel.start()
    log.info("node %s (%s) panel on http://%s:%d", node["name"], node["id"],
             cfg["http"]["host"], panel.port)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    mesh.stop()
    tracker.stop()
    panel.stop()


if __name__ == "__main__":
    main()
