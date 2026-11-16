import json
import os
import tempfile
import time
import unittest

from btedge import script
from btedge.automation import Automation, check
from btedge.profiler import Profiler
from btedge.registry import Registry
from btedge.web import Panel

from meshkit import registry

BAND = "BA:03:5C:0B:21:E1"


def run(code, **globals_):
    out = []
    interp = script.Interpreter(dict(globals_, out=script.Native(lambda args: out.append(args[0]))))
    interp.run(script.parse(code))
    return out


class LanguageTest(unittest.TestCase):
    def test_expressions(self):
        self.assertEqual(run('out(1 + 2 * 3); out("a" + 1); out(7 % 4 == 3 && !false); out(null || "x")'),
                         [7, "a1", True, "x"])
        self.assertEqual(run("out(-(2 - 5)); out(10 / 4); out([1, 2].length); out([1, 'b'].includes('b'))"),
                         [3, 2.5, 2, True])
        self.assertEqual(run('out("Mi Band".startsWith("Mi")); out("ABC".toLowerCase().length); out("abc"[1])'),
                         [True, 3, "b"])

    def test_missing_values_compare_as_false_instead_of_failing(self):
        self.assertEqual(run("out(null > -70); out(null == null); out(1 == true); out('1' == 1)"),
                         [False, True, False, False])

    def test_variables_if_and_functions(self):
        code = """
        // closures and both function forms
        let near = -70; const twice = x => x * 2
        function pick(rssi) { if (rssi > near) { return "near" } else return "far" }
        var f = function (a, b) { return a + b }
        out(pick(-50)); out(pick(-90)); out(twice(f(1, 2)))
        near = -40 /* reassigned */
        out(pick(-50))
        """
        self.assertEqual(run(code), ["near", "far", 6, "far"])

    def test_syntax_errors_name_the_line(self):
        self.assertEqual(check("let a = 1\nhost.onReady(() => {\n  log(a\n})"), "riga 4: atteso ')', trovato '}'")
        self.assertIn("riga 1", check("let = 3"))
        self.assertIn("riga 2", check("log(1)\n#"))
        self.assertIsNone(check("host.onDevice(d => { if (d.rssi > -60) d.connect() })"))

    def test_runtime_errors(self):
        for code, text in (("nope()", "non è definito"), ("let a = 1; a.b", "impossibile leggere"),
                           ("x = 1", "non dichiarata"), ("out = 1", "non dichiarata"), ("1 / 0", "divisione"),
                           ("let a = 3; a()", "non è una funzione")):
            with self.assertRaises(script.ScriptError, msg=code) as caught:
                run(code)
            self.assertIn(text, str(caught.exception))

    def test_runaway_scripts_are_stopped(self):
        with self.assertRaises(script.ScriptError) as caught:
            run("let f = n => f(n + 1); f(0)")
        self.assertIn("annidate", str(caught.exception))
        with self.assertRaises(script.ScriptError) as caught:
            run("let f = n => { if (n > 0) { f(n - 1); f(n - 1) } }; f(30)")
        self.assertIn("troppi passi", str(caught.exception))
        with self.assertRaises(script.ScriptError):
            run('let d = s => s + s; out(d(d(d(d(d(d(d(d(d(d(d(d(d(d("xxxxxxxxxx")))))))))))))))')
        self.assertIn("annidato", check("(" * 3000))

    def test_nothing_of_python_is_reachable(self):
        for code in ("'a'.__class__", "[].append", "log.call", "host.__dict__"):
            with self.assertRaises(script.ScriptError, msg=code):
                run(code, log=script.Native(lambda args: None), host=script.Object("host", {}))


class AutomationTest(unittest.TestCase):
    def setUp(self):
        self.reg = Registry({"id": "a" * 16, "name": "yoga7", "role": "edge"})
        self.now = 0.0
        self.done = []
        ops = {name: (lambda *args, name=name: self.done.append((name,) + args))
               for name in ("connect", "disconnect", "action")}
        self.auto = Automation(self.reg, ops, Profiler(), clock=lambda: self.now)

    def load(self, code, sid="s1", enabled=True):
        self.assertTrue(self.reg.set_shared("snippet:" + sid, {"name": sid, "code": code, "enabled": enabled}))
        self.auto.step()

    def status(self, sid="s1"):
        return self.reg._automation[sid]

    def logs(self, sid="s1"):
        return [line.split(" ", 1)[1] for line in self.status(sid)["log"]]

    def wait(self, count):
        for _ in range(200):
            if len(self.done) >= count:
                return
            time.sleep(0.01)
        self.fail("operations not run: %r" % self.done)

    def test_events_follow_what_the_node_sees(self):
        self.load("""
            host.onReady(() => log("ready", host.name, host.role))
            host.onDevice(d => log("in", d.name, d.rssi > -70, d.class))
            host.onConnect = d => log("up", d.address)
            host.onDisconnect(d => log("down"))
            host.onDeviceLost(d => log("out", d.name))
        """)
        self.assertEqual(self.logs(), ["ready yoga7 edge"])
        self.reg.observe(BAND, {"name": "MSF0", "rssi": -60, "uuids": ["0000cc00-0000-1000-8000-00805f9b34fb"]})
        self.auto.step()
        self.auto.step()  # no news, no events
        self.reg.observe(BAND, {"connected": True})
        self.auto.step()
        self.reg.set_bluetooth("released")  # the node stops seeing it
        self.auto.step()
        self.assertEqual(self.logs(), ["ready yoga7 edge", "in MSF0 true wristband", "up " + BAND, "down", "out MSF0"])
        self.assertIsNone(self.status()["error"])

    def test_device_operations_run_in_order_off_the_event_thread(self):
        self.load("""host.onDevice(d => { if (d.name == "MSF0") { d.connect(); d.action("vibrate"); d.disconnect() } })""")
        self.reg.observe("11:22", {"name": "other"})
        self.reg.observe(BAND, {"name": "MSF0"})
        self.auto.step()
        self.wait(3)
        self.assertEqual(self.done, [("connect", BAND), ("action", BAND, "vibrate"), ("disconnect", BAND)])

    def test_failed_operation_is_reported_on_the_snippet(self):
        def boom(address, action):
            raise LookupError("no adapter for this device")
        self.auto.operations["action"] = boom
        self.load("host.onDevice(d => d.action('vibrate'))")
        self.reg.observe(BAND, {"name": "MSF0"})
        self.auto.step()
        for _ in range(200):
            if self.auto.instances["s1"].error:
                break
            time.sleep(0.01)
        self.auto.step()
        self.assertIn("no adapter", self.status()["error"])

    def test_timers(self):
        self.load("let n = 0; host.every(60, () => { n = n + 1; log(n) })")
        for self.now in (30, 61, 90, 125):
            self.auto.step()
        self.assertEqual(self.logs(), ["1", "2"])

    def test_broken_snippet_reports_its_error_and_spares_the_others(self):
        self.load("host.onDevice(d => log(d.nope))", "bad")
        self.load("host.onDevice(d => log('fine'))", "good")
        self.load("host.onBoh(() => 1)", "worse")
        self.reg.observe(BAND, {"name": "MSF0"})
        self.auto.step()
        self.assertIn("device non ha 'nope'", self.status("bad")["error"])
        self.assertIn("host non ha 'onBoh'", self.status("worse")["error"])
        self.assertEqual(self.logs("good"), ["fine"])

    def test_edit_disable_and_delete(self):
        self.load("host.onReady(() => log('v1'))")
        self.load("host.onReady(() => log('v2'))")
        self.assertEqual(self.logs(), ["v2"])
        self.load("host.onReady(() => log('v2'))", enabled=False)
        self.assertEqual(self.reg._automation, {})
        self.load("host.onReady(() => log('v3'))")
        self.reg.set_shared("snippet:s1", None)
        self.auto.step()
        self.assertEqual(self.reg._automation, {})

    def test_other_nodes_beacons_are_not_devices(self):
        self.load("host.onDevice(d => log(d.address))")
        self.reg.observe("AA:AA", {"btedge_node": "b" * 16})
        self.auto.step()
        self.assertEqual(self.logs(), [])


class SharedSettingsTest(unittest.TestCase):
    def sync(self, *nodes):
        for a in nodes:
            for b in nodes:
                if a is not b:
                    b.merge(a.node["id"], a.snapshot(), via="ip")

    def test_settings_reach_every_node_and_outlive_their_author(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shared.json")
            control, far = registry("ctl", role="control"), registry("far")
            edge = registry("edge", shared_path=path)
            control.set_shared("autoconnect:" + BAND, True)
            control.set_shared("snippet:s1", {"name": "hi", "code": "log(1)", "enabled": True})
            self.sync(control, edge)   # far never talks to the control node
            self.sync(edge, far)
            self.assertEqual(far.autoconnect, {BAND})
            self.assertEqual(far.shared("snippet:")["s1"]["code"], "log(1)")
            with open(path) as fh:
                self.assertIn("autoconnect:" + BAND, json.load(fh))
            again = Registry({"id": "e" * 16, "name": "edge"}, shared_path=path)  # restart, alone
            self.assertEqual(again.autoconnect, {BAND})

            far.set_shared("autoconnect:" + BAND, None)  # removed elsewhere: the deletion wins
            self.sync(far, edge)
            self.sync(edge, control)
            self.assertEqual(control.autoconnect, frozenset())
            self.assertFalse(control.view()["autoconnect"])

    def test_bad_entries_are_not_adopted(self):
        reg = Registry({"id": "a" * 16, "name": "n"})
        self.assertFalse(reg.set_shared("anything:else", True))
        self.assertFalse(reg.set_shared("snippet:x", {"name": "n", "code": "x" * 5000, "enabled": True}))
        self.assertFalse(reg._adopt({"autoconnect:AA": [True, time.time() + 10 * 86400]}))  # clock gone wild
        self.assertEqual(reg.shared_version, 0)

    def test_panel_shows_snippets_with_what_each_node_says(self):
        control, edge = registry("ctl", role="control"), registry("edge")
        panel = Panel(control, None, None, "127.0.0.1", 0, profiler=Profiler())
        with self.assertRaises(ValueError) as caught:
            panel.save_snippet({"name": "x", "code": "host.onReady(() => {"})
        self.assertIn("riga 1", str(caught.exception))
        sid = panel.save_snippet({"name": "hello", "code": "host.onReady(() => log('hi'))"})["id"]
        self.sync(control, edge)
        Automation(edge, {}, Profiler()).step()
        self.sync(control, edge)
        (snippet,) = control.view()["snippets"]
        self.assertEqual((snippet["id"], snippet["name"], snippet["enabled"]), (sid, "hello", True))
        (host,) = snippet["hosts"]
        self.assertEqual((host["name"], host["error"], host["log"][0][9:]), ("edge", None, "hi"))
        panel.save_snippet({"id": sid, "delete": True})
        self.assertEqual(control.view()["snippets"], [])


class MultiRadioTest(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.reg = Registry({"id": "a" * 16, "name": "n"}, clock=lambda: self.now)

    def device(self):
        return self.reg.local_devices()[BAND]

    def test_device_is_reported_once_with_its_best_radio(self):
        self.reg.observe(BAND, {"name": "MSF0", "rssi": -80}, radio="hci0")
        self.assertEqual((self.device()["rssi"], self.device()["radio"]), (-80, "hci0"))
        self.reg.observe(BAND, {"rssi": -55}, radio="hci1")
        self.reg.observe(BAND, {"rssi": -82}, radio="hci0")
        self.assertEqual((self.device()["rssi"], self.device()["radio"]), (-55, "hci1"))
        self.assertEqual(len(self.reg.view()["devices"]), 1)
        self.assertEqual(self.reg.view()["devices"][0]["seen_by"][0]["radio"], "hci1")

    def test_connected_radio_wins_and_a_silent_one_is_forgotten(self):
        self.reg.observe(BAND, {"rssi": -50}, radio="hci1")
        self.reg.observe(BAND, {"rssi": -85, "connected": True}, radio="hci0")
        self.assertEqual((self.device()["radio"], self.device()["connected"]), ("hci0", True))
        self.reg.observe(BAND, {"connected": False}, radio="hci0")
        self.assertEqual((self.device()["radio"], self.device()["connected"]), ("hci1", False))
        self.now = 100.0  # hci1 has not heard it for longer than device_ttl
        self.reg.observe(BAND, {"rssi": -70}, radio="hci0")
        self.assertEqual((self.device()["radio"], self.device()["rssi"]), ("hci0", -70))

    def test_radios_travel_with_the_node(self):
        edge, control = registry("edge"), registry("ctl", role="control")
        edge.set_radios([{"name": "hci0", "address": "AA", "state": "scanning"},
                         {"name": "hci1", "address": "BB", "state": "scanning"}])
        control.merge(edge.node["id"], edge.snapshot(), via="ip")
        node = next(n for n in control.view()["nodes"] if n["name"] == "edge")
        self.assertEqual([r["name"] for r in node["radios"]], ["hci0", "hci1"])


if __name__ == "__main__":
    unittest.main()
