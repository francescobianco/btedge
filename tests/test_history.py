import os
import tempfile
import unittest

from btedge.history import History, Tracker
from btedge.registry import Registry


def node(ch, name):
    return {"id": ch * 16, "name": name, "http": 4646}


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(os.path.join(self.tmp.name, "t.db"))
        self.home = Registry(node("a", "pc"))
        self.other = Registry(node("b", "orangepi"))
        self.tracker = Tracker(self.home, self.history, margin=6)
        self.t = 1_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, home_rssi=None, other_rssi=None):
        if home_rssi is not None:
            self.home.observe("PH", {"rssi": home_rssi, "name": "phone"})
        if other_rssi is not None:
            self.other.observe("PH", {"rssi": other_rssi, "name": "phone"})
        self.home.merge(self.other.node["id"], self.other.snapshot(), via="lan")
        self.t += 10
        self.tracker.step(now=self.t)

    def rooms(self):
        return [(m["from_node"], m["to_node"]) for m in reversed(self.history.moves(0, address="PH"))]

    def test_walk_pc_to_orangepi_and_back(self):
        a, b = "a" * 16, "b" * 16
        self.tick(home_rssi=-45)
        self.tick(home_rssi=-60, other_rssi=-62)   # within margin: stays at the pc
        self.tick(home_rssi=-80, other_rssi=-50)   # clearly closer to the orangepi
        self.tick(home_rssi=-48, other_rssi=-85)   # back at the pc
        self.assertEqual(self.rooms(), [(None, a), (a, b), (b, a)])
        stays = self.history.stays("PH", 0)
        self.assertEqual([s["node_id"] for s in stays], [a, b, a])
        flows = {(f["from_node"], f["to_node"]): (f["count"], f["named"]) for f in self.history.flows(0)}
        self.assertEqual(flows, {("_out", a): (1, 1), (a, b): (1, 1), (b, a): (1, 1)})
        self.assertEqual(self.tracker.current()["PH"]["node"], a)
        self.assertEqual(self.history.device("PH")["name"], "phone")

    def test_leaving_the_mesh_moves_to_outside(self):
        self.tick(home_rssi=-45)
        self.t += 120
        self.home._local.clear()  # nobody hears it anymore
        self.tracker.step(now=self.t)
        self.assertEqual(self.rooms()[-1], ("a" * 16, None))
        self.assertEqual([d["address"] for d in self.history.departed(0)], ["PH"])
        self.assertNotIn("PH", self.tracker.current())

    def test_restart_resumes_recent_stays(self):
        self.tick(home_rssi=-45)
        tracker = Tracker(self.home, self.history)
        tracker.start(resume_age=10 ** 9)
        tracker.stop()
        self.assertEqual(tracker.current()["PH"]["node"], "a" * 16)
        self.t += 10
        tracker.step(now=self.t)
        self.assertEqual(len(self.history.moves(0, address="PH")), 1)

    def test_labels_are_persisted_and_reloaded(self):
        self.home.set_label("PH", "Telefono", ts=5.0)
        self.tick(home_rssi=-50)
        self.assertEqual(self.history.labels(), {"PH": ["Telefono", 5.0]})
        fresh = Registry(node("c", "new"))
        Tracker(fresh, self.history).start()
        fresh.observe("PH", {"rssi": -40})
        self.assertEqual(fresh.view()["devices"][0]["label"], "Telefono")

    def test_labels_travel_through_the_mesh(self):
        self.other.set_label("PH", "Telefono", ts=10.0)
        self.home.set_label("PH", "vecchio", ts=5.0)
        self.tick(home_rssi=-50)
        self.assertEqual(self.home.view()["devices"][0]["label"], "Telefono")


if __name__ == "__main__":
    unittest.main()
