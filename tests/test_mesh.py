import json
import unittest
import urllib.request

from btedge import protocol
from btedge.bluez import BTEDGE_UUID, device_fields
from btedge.mesh import Mesh
from btedge.registry import Registry
from btedge.web import Panel


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def node(node_id, name):
    return {"id": node_id, "name": name, "http": 8080}


MESH_CFG = {"key": "secret", "gossip_interval": 5, "node_ttl": 60, "max_hops": 8,
            "lan": {"enabled": False, "group": "239.255.66.77", "port": 47474}, "peers": []}


class RegistryTest(unittest.TestCase):
    def test_local_device_expires_unless_connected(self):
        clock = Clock()
        reg = Registry(node("a" * 16, "a"), device_ttl=10, clock=clock)
        reg.observe("AA", {"rssi": -50})
        reg.observe("BB", {"rssi": -70, "connected": True})
        clock.t += 11
        self.assertEqual([d["address"] for d in reg.view()["devices"]], ["BB"])

    def test_merge_aggregates_devices_across_nodes(self):
        a = Registry(node("a" * 16, "alpha"))
        b = Registry(node("b" * 16, "beta"))
        a.observe("11:22", {"rssi": -80})
        b.observe("11:22", {"rssi": -45, "name": "phone"})
        b.observe("33:44", {"rssi": -60})
        a.merge(b.node["id"], b.snapshot(), via="lan", source="10.0.0.2")
        view = a.view()
        dev = {d["address"]: d for d in view["devices"]}
        self.assertEqual(dev["11:22"]["name"], "phone")
        self.assertEqual(dev["11:22"]["rssi"], -45)
        self.assertEqual([s["name"] for s in dev["11:22"]["seen_by"]], ["beta", "alpha"])
        self.assertFalse(dev["33:44"]["local"])
        beta = [n for n in view["nodes"] if n["id"] == "b" * 16][0]
        self.assertEqual(beta["link"]["via"], "lan")
        self.assertEqual(beta["hops"], 1)

    def test_states_forward_across_hops_and_old_seq_is_ignored(self):
        a, b, c = (Registry(node(ch * 16, ch)) for ch in "abc")
        c.observe("CC", {"rssi": -50})
        b.merge(c.node["id"], c.snapshot(), via="ip")
        a.merge(b.node["id"], b.snapshot(), via="lan")
        nodes = {n["id"]: n for n in a.view()["nodes"]}
        self.assertEqual(nodes["c" * 16]["hops"], 2)
        self.assertIsNone(nodes["c" * 16]["link"])
        stale = [s for s in b.snapshot() if s["id"] == "c" * 16]
        stale[0]["seq"] -= 1
        self.assertEqual(a.merge(b.node["id"], stale, via="lan"), 0)

    def test_own_state_is_never_accepted_back(self):
        a = Registry(node("a" * 16, "a"))
        self.assertEqual(a.merge("x" * 16, a.snapshot(), via="lan"), 0)

    def test_beacons_of_known_nodes_become_bt_links(self):
        a = Registry(node("a" * 16, "a"))
        b = Registry(node("b" * 16, "b"))
        a.observe("BE:AC", {"rssi": -55, "btedge_node": "b" * 16})
        a.merge(b.node["id"], b.snapshot(), via="lan")
        view = a.view()
        self.assertEqual(view["devices"], [])
        self.assertEqual([n["bt"] for n in view["nodes"] if n["id"] == "b" * 16][0]["rssi"], -55)


class ProtocolTest(unittest.TestCase):
    def test_roundtrip_and_key_mismatch(self):
        pkt = protocol.encode("a" * 16, [{"id": "a" * 16}], key="k1")
        self.assertEqual(protocol.decode(pkt, key="k1")["from"], "a" * 16)
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(pkt, key="k2")
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(pkt[:-1] + b"x", key="k1")

    def test_device_fields_from_bluez(self):
        fields = device_fields({
            "Address": "AA:BB:CC:DD:EE:FF", "Alias": "AA-BB-CC-DD-EE-FF", "RSSI": -61,
            "ManufacturerData": {0x004C: b"\x01"}, "ServiceData": {BTEDGE_UUID: b"\x01" * 8},
        })
        self.assertIsNone(fields["name"])
        self.assertEqual(fields["manufacturer"], [0x004C])
        self.assertEqual(fields["btedge_node"], "01" * 8)


class HttpSyncTest(unittest.TestCase):
    def test_peer_sync_over_http_exchanges_both_ways(self):
        server_reg = Registry(node("5" * 16, "server"))
        client_reg = Registry(node("c" * 16, "client"))
        server_reg.observe("SS", {"rssi": -40})
        client_reg.observe("CC", {"rssi": -70})
        server_mesh = Mesh(server_reg, MESH_CFG)
        panel = Panel(server_reg, server_mesh, None, "127.0.0.1", 0)
        panel.start()
        try:
            url = "http://127.0.0.1:%d" % panel.port
            client_mesh = Mesh(client_reg, dict(MESH_CFG, peers=[url]))
            client_mesh.sync_peer(url)
            self.assertTrue(client_mesh.peer_status[url]["ok"], client_mesh.peer_status[url])
            self.assertEqual({d["address"] for d in client_reg.view()["devices"]}, {"SS", "CC"})
            self.assertEqual({d["address"] for d in server_reg.view()["devices"]}, {"SS", "CC"})

            with urllib.request.urlopen(url + "/api/state") as resp:
                state = json.load(resp)
            self.assertEqual(len(state["nodes"]), 2)

            wrong = Mesh(Registry(node("d" * 16, "intruder")), dict(MESH_CFG, key="other"))
            wrong.sync_peer(url)
            self.assertFalse(wrong.peer_status[url]["ok"])
        finally:
            panel.stop()


if __name__ == "__main__":
    unittest.main()
