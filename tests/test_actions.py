import json
import os
import tempfile
import contextlib
import datetime
import zoneinfo
import io
import unittest
import unittest.mock
import urllib.request

from btedge import switch, trust
from btedge.adapters import yoho
from btedge.actions import Dispatcher, read_mode
from btedge.bluez import Unsupported
from btedge.gatt import FakeGatt
from btedge.profiler import Profiler
from btedge.web import Panel

from meshkit import owner, registry

MSF0 = {"address": "BA:03:5C:0B:21:E1", "name": "MSF0", "connected": True,
        "uuids": ["00001800-0000-1000-8000-00805f9b34fb", "0000cc00-0000-1000-8000-00805f9b34fb",
                  "0000fee7-0000-1000-8000-00805f9b34fb"], "manufacturer": [954]}


class FakeBackend:
    mode = "on"
    can_power_off = True

    def gatt(self, address):
        return FakeGatt(address)

    def set_mode(self, mode):
        if mode == "off" and not self.can_power_off:
            raise Unsupported("no power switch")
        self.mode = mode


class ProfilerTest(unittest.TestCase):
    def setUp(self):
        self.p = Profiler()

    def test_id115_plus_is_a_wristband_that_vibrates(self):
        prof = self.p.profile(MSF0)
        self.assertEqual((prof["id"], prof["class"], prof["adapter"]), ("id115-plus", "wristband", "yoho"))
        self.assertEqual([a["id"] for a in prof["actions"]], ["vibrate", "sync"])
        self.assertEqual(prof["alarms"], 3)

    def test_find_me_service_gives_generic_vibration(self):
        prof = self.p.profile({"address": "X", "name": "Mi Smart Band 5", "uuids": ["00001802-0000-1000-8000-00805f9b34fb"]})
        self.assertEqual((prof["id"], prof["class"], prof["adapter"]), ("mi-band", "wristband", "immediate_alert"))

    def test_class_only_and_unknown(self):
        self.assertEqual(self.p.profile({"address": "X", "icon": "phone"})["actions"], [])
        self.assertIsNone(self.p.profile({"address": "X", "name": "boh"}))


class ActionFlowTest(unittest.TestCase):
    def test_command_travels_the_mesh_and_the_edge_vibrates_the_band(self):
        FakeGatt.writes.clear()
        control = registry("ctl", role="control")
        edge = registry("yoga7")
        edge.observe(MSF0["address"], {k: v for k, v in MSF0.items() if k != "address"})
        control.merge(edge.node["id"], edge.snapshot(), via="ip")

        panel = Panel(control, None, None, "127.0.0.1", 0, profiler=Profiler())
        sent = panel.act(MSF0["address"], "vibrate")
        self.assertEqual(sent["node"], edge.node["id"])
        with self.assertRaises(LookupError):
            panel.act(MSF0["address"], "explode")

        edge.merge(control.node["id"], control.snapshot(), via="ip")
        dispatcher = Dispatcher(edge, FakeBackend(), Profiler())
        (cmd,) = edge.pending()
        edge.set_result(cmd["id"], "running")
        self.assertEqual(edge.pending(), [])  # never run twice
        dispatcher.execute(cmd)
        self.assertEqual(FakeGatt.writes, [(MSF0["address"], "0000cc06-0000-1000-8000-00805f9b34fb", b"\x08\x01")])

        control.merge(edge.node["id"], edge.snapshot(), via="ip")
        self.assertEqual(control.result(sent["id"])["status"], "ok")

    def test_api(self):
        reg = registry("yoga7")
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


class AlarmsTest(unittest.TestCase):
    WORKDAYS = {"slots": [{"time": "07:30", "days": [0, 1, 2, 3, 4]}, {"time": "07:00", "days": [4, 3, 2, 1, 0]},
                          {"time": "07:15", "days": [0, 1, 2, 3, 4]}], "tz": "Europe/Rome"}

    def setUp(self):
        FakeGatt.writes.clear()
        self.reg = registry("yoga7")
        self.reg.observe(MSF0["address"], {k: v for k, v in MSF0.items() if k != "address"})
        self.dispatcher = Dispatcher(self.reg, FakeBackend(), Profiler())

    def device(self):
        return self.reg.view()["devices"][0]

    def test_packets_match_the_ones_documented_for_the_band(self):
        self.assertEqual(yoho.alarms_packet(self.WORKDAYS["slots"]).hex(), "0c01" "0107003e" "01070f3e" "01071e3e")
        self.assertEqual(yoho.alarms_packet([]).hex(), "0c00" + "00" * 12)
        weekend = [{"time": "09:05", "days": [5, 6]}, {"time": "23:59", "days": []}]  # no day: not an alarm
        self.assertEqual(yoho.alarms_packet(weekend).hex(), "0c01" "01090541" + "00" * 8)
        with self.assertRaises(ValueError):
            yoho.alarms_packet([{"time": "0%d:00" % i, "days": [0]} for i in range(4)])
        now = datetime.datetime(2026, 11, 16, 23, 5, 9)
        self.assertEqual(yoho.clock_packet(now).hex(), "04001a0b1017" "0509")

    def test_sync_writes_clock_and_wanted_alarms_and_remembers_it(self):
        self.assertTrue(self.reg.set_shared("alarms:" + MSF0["address"], self.WORKDAYS))
        self.assertTrue(self.device()["alarms"]["pending"])
        self.dispatcher.run(MSF0["address"], "sync")
        (_, _, clock), (_, _, alarms) = FakeGatt.writes
        self.assertEqual(clock[:2], b"\x04\x00")
        self.assertEqual(clock[5], datetime.datetime.now(zoneinfo.ZoneInfo("Europe/Rome")).hour)
        self.assertEqual(alarms.hex(), "0c01" "0107003e" "01070f3e" "01071e3e")
        self.assertFalse(self.device()["alarms"]["pending"])
        self.assertIsNotNone(self.device()["alarms"]["applied"])

        changed = dict(self.WORKDAYS, slots=self.WORKDAYS["slots"][:1])
        self.reg.set_shared("alarms:" + MSF0["address"], changed)  # edited: the band is behind again
        self.assertTrue(self.device()["alarms"]["pending"])

    def test_sync_leaves_the_alarms_of_a_band_never_configured_in_the_mesh(self):
        self.dispatcher.run(MSF0["address"], "sync")
        ((_, _, clock),) = FakeGatt.writes
        self.assertEqual(clock[:2], b"\x04\x00")
        self.assertIsNone(self.device()["alarms"])

    def test_too_many_times_for_the_band_is_an_error_not_a_crash(self):
        self.reg.set_shared("alarms:" + MSF0["address"],
                            {"slots": [{"time": "0%d:00" % i, "days": [0]} for i in range(4)]})
        self.dispatcher.execute({"id": "x", "address": MSF0["address"], "action": "sync"})
        result = self.reg.result("x")
        self.assertEqual(result["status"], "error")
        self.assertIn("3 alarm times", result["error"])

    def test_bad_alarms_are_refused(self):
        for bad in ({"slots": [{"time": "7:00", "days": [0]}]}, {"slots": [{"time": "07:00", "days": [7]}]},
                    {"slots": [{"time": "25:00", "days": []}]}, {"slots": "x"}, {"slots": [{"time": "07:00"}]}):
            self.assertFalse(self.reg.set_shared("alarms:" + MSF0["address"], bad), bad)


class BluetoothSwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, switch.MODE_FILE)
        self.control = registry("ctl", role="control")
        self.edge = registry("yoga7")
        self.backend = FakeBackend()
        self.dispatcher = Dispatcher(self.edge, self.backend, Profiler(), mode_file=self.file)
        self.panel = Panel(self.control, None, None, "127.0.0.1", 0, profiler=Profiler())
        self.edge.observe(MSF0["address"], {"name": "MSF0", "connected": True})
        self.sync()

    def sync(self):
        self.control.merge(self.edge.node["id"], self.edge.snapshot(), via="ip")
        self.edge.merge(self.control.node["id"], self.control.snapshot(), via="ip")

    def node(self):
        return next(n for n in self.control.view()["nodes"] if n["id"] == self.edge.node["id"])

    def switch(self, mode):
        sent = self.panel.switch(self.edge.node["id"], mode)
        self.sync()
        for cmd in self.edge.pending():
            self.dispatcher.switch(cmd)
        self.sync()
        return self.control.result(sent["id"])

    def test_panel_releases_the_adapter_of_an_edge_and_takes_it_back(self):
        self.assertEqual(self.node()["bluetooth"], "on")
        self.assertEqual(self.switch("released")["status"], "ok")
        self.assertEqual(self.backend.mode, "released")
        self.assertEqual(self.node()["bluetooth"], "released")
        self.assertEqual(self.control.view()["devices"], [])  # even the connected one is gone
        self.edge.observe("AA:BB", {"name": "late advert"})
        self.assertEqual(self.edge.view()["devices"], [])
        self.assertEqual(read_mode(self.file), "released")

        self.assertEqual(self.switch("off")["status"], "ok")
        self.assertEqual((self.backend.mode, self.node()["bluetooth"]), ("off", "off"))

        self.assertEqual(self.switch("on")["status"], "ok")
        self.assertEqual((self.backend.mode, self.node()["bluetooth"]), ("on", "on"))
        self.assertEqual(read_mode(self.file), "on")

    def test_mode_survives_a_restart(self):
        self.switch("off")
        backend, edge = FakeBackend(), registry("yoga7-again")
        Dispatcher(edge, backend, Profiler(), mode_file=self.file)
        self.assertEqual((backend.mode, edge.bluetooth), ("off", "off"))

    def test_a_mode_the_backend_cannot_do_is_an_error_and_changes_nothing(self):
        self.backend.can_power_off = False
        result = self.switch("off")
        self.assertEqual((result["status"], result["error"]), ("error", "no power switch"))
        self.assertEqual((self.node()["bluetooth"], read_mode(self.file)), ("on", "on"))

    def test_released_node_refuses_device_actions(self):
        self.switch("released")
        cmd = {"id": "x", "node": self.edge.node["id"], "address": MSF0["address"], "action": "vibrate"}
        self.dispatcher.execute(cmd)
        self.assertEqual(self.edge.result("x")["status"], "error")

    def test_nodes_without_bluetooth_have_no_switch(self):
        with self.assertRaises(LookupError):
            self.panel.switch(self.control.node["id"], "off")
        with self.assertRaises(LookupError):
            self.panel.switch("f" * 16, "off")

    def cli(self, *argv):
        state = self.edge.identity.state_dir
        self.file = os.path.join(state, switch.MODE_FILE)
        self.dispatcher.mode_file = self.file
        env = {"BTEDGE_STATE_DIR": state, "BTEDGE_MESH_OWNER": owner()[0], "BTEDGE_PORT": "1"}
        config = os.path.join(self.tmp.name, "config.json")
        with open(config, "w") as fh:
            fh.write("{}")
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            switch.main(list(argv) + ["-c", config])
        return out.getvalue()

    def test_host_command_drives_the_running_node(self):
        self.assertIn("released", self.cli("release"))
        self.dispatcher.follow_file()
        self.assertEqual((self.backend.mode, self.edge.bluetooth), ("released", "released"))
        self.assertIn("released", self.cli())
        self.assertIn("on:", self.cli("on"))
        self.dispatcher.follow_file()
        self.assertEqual(self.backend.mode, "on")

        self.backend.can_power_off = False  # refused: the file goes back to what is true
        self.cli("off")
        self.dispatcher.follow_file()
        self.assertEqual((self.backend.mode, read_mode(self.file)), ("on", "on"))


if __name__ == "__main__":
    unittest.main()
