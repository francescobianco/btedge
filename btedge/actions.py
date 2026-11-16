"""Runs device actions addressed to this node and publishes their results.

Commands arrive through the mesh (see registry.py). For each one the device is
profiled, its adapter is looked up and run over a GATT session opened by the
Bluetooth backend; the result goes back into our state so the issuing panel sees it.

Commands without a device address are for the node and set its Bluetooth mode:

    on        btedge scans, advertises its beacon, keeps devices connected
    released  btedge keeps its hands off the adapter, which stays powered for the host:
              development, other applications
    off       the radio is switched off

The mode lives in <state_dir>/bluetooth_mode, survives restarts and is watched: the
`btedge bluetooth` command on the host (switch.py) just writes that file.
"""
import logging
import os
import threading

from . import adapters
from .bluez import Unsupported
from .gatt import GattError
from .registry import BLUETOOTH_MODES, digest

log = logging.getLogger(__name__)

BLUETOOTH = {"bluetooth_" + mode: mode for mode in BLUETOOTH_MODES}  # command -> mode


def read_mode(path):
    """The Bluetooth mode asked for in the mode file; "on" when there is none."""
    try:
        with open(path) as fh:
            mode = fh.read().strip()
    except FileNotFoundError:
        return "on"
    return mode if mode in BLUETOOTH_MODES else "on"


def write_mode(path, mode):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(mode + "\n")
    os.replace(tmp, path)


class Dispatcher:
    def __init__(self, registry, backend, profiler, on_result=None, interval=0.5, mode_file=None):
        self.registry = registry
        self.backend = backend
        self.profiler = profiler
        self.on_result = on_result or (lambda: None)
        self.interval = interval
        self._stop = threading.Event()
        self.mode_file = mode_file
        self.mode = "on"
        wanted = read_mode(mode_file) if mode_file else "on"
        if wanted != "on":
            self.apply(wanted)
            log.info("Bluetooth %s (%s): change it from the panel or with `btedge bluetooth on`", self.mode, mode_file)
        registry.set_bluetooth(self.mode)

    def start(self):
        threading.Thread(target=self._run, name="actions", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(self.interval):
            self.follow_file()
            # oldest first, so a release followed by an on ends up on
            for cmd in sorted(self.registry.pending(), key=lambda c: -c["age"]):
                if not cmd["address"] and cmd["action"] in BLUETOOTH:
                    self.switch(cmd)
                    continue
                self.registry.set_result(cmd["id"], "running")
                threading.Thread(target=self.execute, args=(cmd,), name="action-" + cmd["id"], daemon=True).start()
                self.on_result()

    def apply(self, mode):
        """Put the backend in a Bluetooth mode; returns the error, if it cannot."""
        try:
            self.backend.set_mode(mode)
        except Unsupported as exc:
            return str(exc)
        self.mode = mode
        self.registry.set_bluetooth(mode)

    def follow_file(self):
        """Apply a mode written by `btedge bluetooth` on the host."""
        if not self.mode_file:
            return
        try:
            wanted = read_mode(self.mode_file)
            if wanted == self.mode:
                return
            error = self.apply(wanted)
            if error:
                log.warning("Bluetooth %s refused: %s", wanted, error)
                write_mode(self.mode_file, self.mode)
            else:
                log.info("Bluetooth %s from the host", wanted)
                self.on_result()
        except OSError as exc:
            log.debug("Bluetooth mode file: %s", exc)

    def switch(self, cmd):
        """Run a Bluetooth mode command coming from a panel."""
        mode = BLUETOOTH[cmd["action"]]
        error = self.apply(mode)
        if not error:
            log.info("Bluetooth %s from the panel", mode)
            try:
                if self.mode_file:
                    write_mode(self.mode_file, mode)
            except OSError as exc:
                log.warning("cannot save the Bluetooth mode: %s", exc)
                error = "done, but it will not survive a restart: %s" % exc
        self.registry.set_result(cmd["id"], "error" if error else "ok", error)
        self.on_result()

    def execute(self, cmd):
        try:
            self._execute(cmd)
            self.registry.set_result(cmd["id"], "ok")
            log.info("action %s on %s: ok", cmd["action"], cmd["address"])
        except (GattError, ValueError, LookupError) as exc:  # ValueError: refused by the adapter
            self.registry.set_result(cmd["id"], "error", str(exc))
            log.warning("action %s on %s: %s", cmd["action"], cmd["address"], exc)
        except Exception as exc:  # adapters talk to real hardware: never kill the dispatcher
            self.registry.set_result(cmd["id"], "error", "unexpected: %s" % exc)
            log.exception("action %s on %s", cmd["action"], cmd["address"])
        self.on_result()

    def _execute(self, cmd):
        self.run(cmd["address"], cmd["action"])

    def run(self, address, action):
        """Run an adapter action on a device from this node (also what snippets call)."""
        if self.mode != "on":
            raise LookupError("Bluetooth is %s on this node" % self.mode)
        device = next((d for d in self.registry.view()["devices"] if d["address"] == address), None)
        if device is None:
            raise LookupError("device %s not in range" % address)
        profile = self.profiler.profile(device)
        if not profile or not profile["adapter"]:
            raise LookupError("no adapter for this device")
        settings = {"alarms": self.registry.alarms(address)}
        with self.backend.gatt(address) as gatt:
            written = adapters.run(profile["adapter"], action, gatt, settings)
        if "alarms" in written:  # remembered, so panels can tell when the device is behind
            self.registry.set_shared("applied:" + address, digest(settings["alarms"]))
