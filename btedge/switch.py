"""`btedge bluetooth [status|on|release|off]`: the node's Bluetooth mode, from its host.

    on       btedge scans, advertises its beacon and keeps devices connected
    release  btedge keeps its hands off the adapter, which stays powered for the host:
             development, other applications
    off      the radio is switched off

The same switch is in the panel. The mode is a file in the node's state directory that
the running node watches, so this needs no login, works within a second and, with the
node stopped, decides how it starts. Run it as the user of the service (sudo on Linux).
"""
import argparse
import os
import urllib.request

from . import config, trust
from .actions import read_mode, write_mode

MODE_FILE = "bluetooth_mode"
WORDS = {"on": "on", "release": "released", "off": "off"}  # command word -> mode
MEANING = {
    "on": "on: btedge is using the adapter",
    "released": "released: the adapter is powered and left to the host",
    "off": "off: the radio is switched off",
}


def _running(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=2):
            return True
    except OSError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog="btedge bluetooth", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", default="status", choices=["status"] + list(WORDS))
    parser.add_argument("-c", "--config", help="JSON config file (default: %s)" % config.DEFAULT_PATH)
    args = parser.parse_args(argv)
    try:
        cfg = config.load(args.config)
    except PermissionError as exc:
        raise SystemExit("btedge: %s: run it as the user of the service (sudo)" % exc)
    except (OSError, ValueError, trust.TrustError) as exc:
        raise SystemExit("btedge: %s" % exc)
    backend = cfg["scanner"]["backend"]
    if backend == "none":
        raise SystemExit("btedge: this node has no Bluetooth")
    # config.load falls back to a private directory when the node's one is not writable
    if not os.path.exists(os.path.join(cfg["state_dir"], trust.KEY_FILE)):
        raise SystemExit("btedge: no node in %s: run it as the user of the service (sudo)" % cfg["state_dir"])
    path = os.path.join(cfg["state_dir"], MODE_FILE)
    if args.mode != "status":
        mode = WORDS[args.mode]
        if mode == "off" and backend == "corebluetooth":
            raise SystemExit("btedge: macOS does not let btedge switch the radio off: use `release`")
        try:
            write_mode(path, mode)
        except OSError as exc:
            raise SystemExit("btedge: %s" % exc)
    print("Bluetooth " + MEANING[read_mode(path)])
    if not _running(cfg["http"]["port"]):
        print("the node is not running: this is how it will start")
