"""Entry point: python3 -m btedge [--config FILE] [--fake]"""
import argparse
import json
import logging
import os
import signal
import sys
import threading

from . import config, trust
from .actions import Dispatcher
from .profiler import Profiler
from .bluez import BluezBackend, FakeBackend
from .mesh import Mesh
from .registry import Registry
from .web import Panel

log = logging.getLogger("btedge")


def credential(argv):
    parser = argparse.ArgumentParser(prog="btedge credential",
                                     description="sign the mesh credential with your SSH key (it never leaves this machine)")
    parser.add_argument("-f", "--key", default="~/.ssh/id_ed25519.pub" if os.path.exists(
        os.path.expanduser("~/.ssh/id_ed25519.pub")) else "~/.ssh/id_rsa.pub", help="SSH public key (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        owner, cred = trust.issue(args.key)
    except (OSError, trust.TrustError) as exc:
        raise SystemExit("error: %s" % exc)
    print(json.dumps({"mesh": {"owner": owner, "credential": cred}}, indent=2))
    print("\nmesh %s - keep the credential secret: whoever has it joins the mesh" % trust.fingerprint(owner),
          file=sys.stderr)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["credential"]:
        return credential(argv[1:])
    parser = argparse.ArgumentParser(prog="btedge", description="Bluetooth edge mesh node")
    parser.add_argument("-c", "--config", help="JSON config file (default: %s if present)" % config.DEFAULT_PATH)
    parser.add_argument("--fake", action="store_true", help="simulate Bluetooth devices")
    parser.add_argument("--port", type=int, help="override the HTTP port")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = config.load(args.config)
    except (ValueError, trust.TrustError) as exc:
        raise SystemExit("btedge: %s" % exc)
    if args.fake:
        cfg["scanner"]["backend"] = "fake"
    if args.port:
        cfg["http"]["port"] = args.port

    node = {"id": cfg["node_id"], "name": cfg["node_name"], "http": cfg["http"]["port"], "role": cfg["role"]}
    registry = Registry(node, device_ttl=cfg["scanner"]["device_ttl"],
                        node_ttl=cfg["mesh"]["node_ttl"], max_hops=cfg["mesh"]["max_hops"],
                        labels_path=os.path.join(cfg["state_dir"], "labels.json"))
    scanner = cfg["scanner"]
    if scanner["backend"] == "none":
        backend = None
    elif scanner["backend"] == "fake":
        backend = FakeBackend(registry, room=scanner["fake_room"], rooms=scanner["fake_rooms"])
    else:
        backend = BluezBackend(registry, node["id"], adapter=scanner["adapter"],
                               advertise=scanner["advertise"], cache_ttl=scanner["cache_ttl"],
                               rescan_interval=scanner["rescan_interval"],
                               keep_connected=scanner["keep_connected"])
    mesh = Mesh(registry, cfg["mesh"])
    profiler = Profiler(os.path.join(cfg["state_dir"], "profiles.json"))
    dispatcher = Dispatcher(registry, backend, profiler, on_result=mesh.kick) if backend else None
    panel = Panel(registry, mesh, backend, cfg["http"]["host"], cfg["http"]["port"], profiler=profiler,
                  admin_password=cfg["admin"]["password"], admin_user=cfg["admin"]["user"],
                  join={"url": cfg["public_url"], "owner": cfg["mesh"]["owner"]} if cfg["role"] == "control" else None)

    if backend:
        backend.start()
        dispatcher.start()
    mesh.start()
    panel.start()
    if cfg["role"] == "control":
        log.info("control node: panel user %r, password from config/env or %s/admin_password",
                 cfg["admin"]["user"], cfg["state_dir"])
    log.info("%s node %s (%s) panel on http://%s:%d", cfg["role"], node["name"], node["id"],
             cfg["http"]["host"], panel.port)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    mesh.stop()
    panel.stop()


if __name__ == "__main__":
    main()
