"""Runs device actions addressed to this node and publishes their results.

Commands arrive through the mesh (see registry.py). For each one the device is
profiled, its adapter is looked up and run over a GATT session opened by the
Bluetooth backend; the result goes back into our state so the issuing panel sees it.
"""
import logging
import threading

from . import adapters
from .gatt import GattError

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, registry, backend, profiler, on_result=None, interval=0.5):
        self.registry = registry
        self.backend = backend
        self.profiler = profiler
        self.on_result = on_result or (lambda: None)
        self.interval = interval
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, name="actions", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(self.interval):
            for cmd in self.registry.pending():
                self.registry.set_result(cmd["id"], "running")
                threading.Thread(target=self.execute, args=(cmd,), name="action-" + cmd["id"], daemon=True).start()
                self.on_result()

    def execute(self, cmd):
        try:
            self._execute(cmd)
            self.registry.set_result(cmd["id"], "ok")
            log.info("action %s on %s: ok", cmd["action"], cmd["address"])
        except (GattError, adapters.UnknownAction, LookupError) as exc:
            self.registry.set_result(cmd["id"], "error", str(exc))
            log.warning("action %s on %s: %s", cmd["action"], cmd["address"], exc)
        except Exception as exc:  # adapters talk to real hardware: never kill the dispatcher
            self.registry.set_result(cmd["id"], "error", "unexpected: %s" % exc)
            log.exception("action %s on %s", cmd["action"], cmd["address"])
        self.on_result()

    def _execute(self, cmd):
        device = next((d for d in self.registry.view()["devices"] if d["address"] == cmd["address"]), None)
        if device is None:
            raise LookupError("device %s not in range" % cmd["address"])
        profile = self.profiler.profile(device)
        if not profile or not profile["adapter"]:
            raise LookupError("no adapter for this device")
        with self.backend.gatt(cmd["address"]) as gatt:
            adapters.run(profile["adapter"], cmd["action"], gatt)
