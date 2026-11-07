import json
import unittest
import urllib.request

from btedge.actions import Dispatcher
from btedge.gatt import FakeGatt
from btedge.profiler import Profiler
from btedge.registry import Registry
from btedge.web import Panel

MSF0 = {"address": "BA:03:5C:0B:21:E1", "name": "MSF0", "connected": True,
        "uuids": ["00001800-0000-1000-8000-00805f9b34fb", "0000cc00-0000-1000-8000-00805f9b34fb",
                  "0000fee7-0000-1000-8000-00805f9b34fb"], "manufacturer": [954]}


class FakeBackend:
    def gatt(self, address):
        return FakeGatt(address)


def node(node_id, name, role="edge"):
    return {"id": node_id, "name": name, "http": 0, "role": role}


class ProfilerTest(unittest.TestCase):
    def setUp(self):
        self.p = Profiler()

    def test_id115_plus_is_a_wristband_that_vibrates(self):
        prof = self.p.profile(MSF0)
        self.assertEqual((prof["id"], prof["class"], prof["adapter"]), ("id115-plus", "wristband", "yoho"))
        self.assertEqual([a["id"] for a in prof["actions"]], ["vibrate"])

    def test_find_me_service_gives_generic_vibration(self):
        prof = self.p.profile({"address": "X", "name": "Mi Smart Band 5", "uuids": ["00001802-0000-1000-8000-00805f9b34fb"]})
        self.assertEqual((prof["id"], prof["class"], prof["adapter"]), ("mi-band", "wristband", "immediate_alert"))

    def test_class_only_and_unknown(self):
        self.assertEqual(self.p.profile({"address": "X", "icon": "phone"})["actions"], [])
        self.assertIsNone(self.p.profile({"address": "X", "name": "boh"}))


class ActionFlowTest(unittest.TestCase):
    def test_command_travels_the_mesh_and_the_edge_vibrates_the_band(self):
        FakeGatt.writes.clear()
        control = Registry(node("c" * 16, "ctl", "control"))
        edge = Registry(node("e" * 16, "yoga7"))
        edge.observe(MSF0["address"], {k: v for k, v in MSF0.items() if k != "address"})
        control.merge("e" * 16, edge.snapshot(), via="ip")

        panel = Panel(control, None, None, "127.0.0.1", 0, profiler=Profiler())
        sent = panel.act(MSF0["address"], "vibrate")
        self.assertEqual(sent["node"], "e" * 16)
        with self.assertRaises(LookupError):
            panel.act(MSF0["address"], "explode")

        edge.merge("c" * 16, control.snapshot(), via="ip")
        dispatcher = Dispatcher(edge, FakeBackend(), Profiler())
        (cmd,) = edge.pending()
        edge.set_result(cmd["id"], "running")
        self.assertEqual(edge.pending(), [])  # never run twice
        dispatcher.execute(cmd)
        self.assertEqual(FakeGatt.writes, [(MSF0["address"], "0000cc06-0000-1000-8000-00805f9b34fb", b"\x08\x01")])

        control.merge("e" * 16, edge.snapshot(), via="ip")
        self.assertEqual(control.result(sent["id"])["status"], "ok")

    def test_api(self):
        reg = Registry(node("e" * 16, "yoga7"))
        reg.observe(MSF0["address"], {k: v for k, v in MSF0.items() if k != "address"})
        panel = Panel(reg, None, None, "127.0.0.1", 0, profiler=Profiler())
        panel.start()
        try:
            base = "http://127.0.0.1:%d" % panel.port
            req = urllib.request.Request(base + "/api/action", method="POST",
                                         data=json.dumps({"address": MSF0["address"], "action": "vibrate"}).encode())
            cid = json.load(urllib.request.urlopen(req, timeout=5))["id"]
            self.assertEqual(json.load(urllib.request.urlopen(base + "/api/action?id=" + cid))["status"], "pending")
            state = json.load(urllib.request.urlopen(base + "/api/state"))
            self.assertEqual(state["devices"][0]["profile"]["id"], "id115-plus")
        finally:
            panel.stop()


if __name__ == "__main__":
    unittest.main()
