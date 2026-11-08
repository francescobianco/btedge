import io
import json
import time
import unittest

from btedge.bluez import BTEDGE_UUID
from btedge.corebluetooth import CoreBluetoothBackend
from btedge.gatt import GattError
from btedge.registry import Registry

DEVICE = "6E8A1D5C-1111-4222-8333-444455556666"


class CoreBluetoothTest(unittest.TestCase):
    def setUp(self):
        self.registry = Registry({"id": "c" * 16, "name": "mac", "http": 0, "role": "edge"})

    def run_backend(self, *messages):
        lines = [m if isinstance(m, bytes) else json.dumps(m).encode() for m in messages]
        backend = CoreBluetoothBackend(self.registry, io.BytesIO(b"\n".join(lines) + b"\n"))
        backend._run()
        return backend

    def test_advertisement_becomes_a_device(self):
        self.run_backend({"id": DEVICE, "name": "Tag", "rssi": -60, "tx_power": 4, "manufacturer": 76,
                          "uuids": ["180F"], "service_data": {BTEDGE_UUID.upper(): "aabb"}})
        dev = self.registry.view()["devices"][0]
        self.assertEqual(dev["address"], DEVICE)
        self.assertEqual((dev["name"], dev["rssi"], dev["tx_power"]), ("Tag", -60, 4))
        self.assertEqual(dev["manufacturer"], [76])
        self.assertEqual(dev["uuids"], ["0000180f-0000-1000-8000-00805f9b34fb"])

    def test_states_are_reported(self):
        backend = CoreBluetoothBackend(self.registry, io.BytesIO())
        backend.handle({"state": "poweredOn"})
        self.assertEqual((backend.status["state"], backend.status["error"]), ("scanning", None))
        backend.handle({"state": "unauthorized"})
        self.assertEqual(backend.status["state"], "error")
        self.assertIn("Privacy", backend.status["error"])

    def test_bad_lines_do_not_stop_the_reader(self):
        self.run_backend(b"not json", {"no": "id"}, {"id": DEVICE, "rssi": -70})
        self.assertEqual(len(self.registry.view()["devices"]), 1)

    def test_end_of_stream_is_an_error(self):
        self.assertEqual(self.run_backend({"state": "poweredOn"}).status["state"], "error")

    def test_actions_are_refused(self):
        with self.assertRaises(GattError):
            CoreBluetoothBackend(self.registry, io.BytesIO()).gatt(DEVICE)


if __name__ == "__main__":
    unittest.main()
