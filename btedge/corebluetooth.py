"""macOS backend: the advertisements seen by the BtedgeBluetooth helper.

The helper (deploy/macos/main.swift) owns the CoreBluetooth session, because macOS grants
Bluetooth only to a bundle declaring it, and starts this process with a pipe on stdin:
one JSON object per line.

CoreBluetooth hides MAC addresses. Devices are keyed by a UUID valid on this Mac only, so
a device heard by a Mac and by a BlueZ node shows up as two devices in the mesh.

Releasing Bluetooth only makes btedge ignore what the helper hears: the helper keeps
its scan, which on macOS takes nothing away from other applications. The radio cannot
be switched off from here: macOS has no public API for that.
"""
import json
import logging
import sys
import threading

from .bluez import BTEDGE_UUID, Unsupported
from .gatt import GattError, uuid128

log = logging.getLogger(__name__)

ERRORS = {
    "poweredOff": "Bluetooth is off",
    "unauthorized": "Bluetooth not allowed: System Settings > Privacy & Security > Bluetooth",
}


def device_fields(message):
    """Convert one advertisement from the helper into registry fields."""
    maker = message.get("manufacturer")
    fields = {
        "name": message.get("name"),
        "rssi": message.get("rssi"),
        "tx_power": message.get("tx_power"),
        "manufacturer": [maker] if maker is not None else None,
        "uuids": [uuid128(u) for u in message["uuids"]][:8] if message.get("uuids") else None,
    }
    for uuid, value in (message.get("service_data") or {}).items():
        if uuid.lower() == BTEDGE_UUID:
            fields["btedge_node"] = value
    return fields


class CoreBluetoothBackend:
    def __init__(self, registry, stream=None):
        self.registry = registry
        self.stream = sys.stdin.buffer if stream is None else stream
        self.status = {"backend": "corebluetooth", "state": "starting", "adapter": "CoreBluetooth",
                       "advertising": False, "error": None}
        self.mode = "on"
        self._helper = ("starting", None)  # what the helper last reported, shown while enabled

    def start(self):
        threading.Thread(target=self._run, name="corebluetooth", daemon=True).start()

    def gatt(self, address):
        raise GattError("device actions are not supported on macOS")

    def connect(self, address):
        raise Unsupported("connections are not supported on macOS")

    disconnect = connect

    def set_mode(self, mode):
        if mode == "off":
            raise Unsupported("macOS does not let btedge switch the radio off: release it instead")
        self.mode = mode
        self._set(*self._helper)

    def _set(self, state, error=None):
        if error and error != self._helper[1]:
            log.warning("bluetooth error: %s", error)
        self._helper = (state, error)
        if self.mode == "on":
            self.status.update(state=state, error=error)
        else:
            self.status.update(state="paused", error=None)
        # in the node's state too: a panel elsewhere can tell why this Mac sees nothing
        self.registry.set_radios([{"name": "CoreBluetooth", "address": "", "state": self.status["state"],
                                   "error": self.status["error"]}])

    def handle(self, message):
        state = message.get("state")
        if state is None:
            self.registry.observe(str(message["id"]), device_fields(message))
        elif state == "poweredOn":
            self._set("scanning")
        elif state == "unsupported":
            self._set("no_adapter", "this Mac has no Bluetooth LE adapter")
        elif state in ERRORS:
            self._set("error", ERRORS[state])
        else:
            self._set("starting")

    def _run(self):
        for line in self.stream:
            try:
                self.handle(json.loads(line))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                log.debug("bad message from the Bluetooth helper: %s", exc)
        self._set("error", "Bluetooth helper not running: start btedge through BtedgeBluetooth.app")
