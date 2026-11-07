import json
import os
import shutil
import unittest
import urllib.request

from btedge import protocol, trust
from btedge.bluez import BTEDGE_UUID, device_fields
from btedge.mesh import Mesh
from btedge.web import Panel

from meshkit import identity, owner, registry


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


MESH_CFG = {"gossip_interval": 5, "node_ttl": 60, "max_hops": 8,
            "lan": {"enabled": False, "group": "239.255.66.77", "port": 47474}, "peers": []}


def mesh(reg, **cfg):
    return Mesh(reg, dict(MESH_CFG, **cfg), reg.identity, reg.verifier)


class RegistryTest(unittest.TestCase):
    def test_local_device_expires_unless_connected(self):
        clock = Clock()
        reg = registry("a", device_ttl=10, clock=clock)
        reg.observe("AA", {"rssi": -50})
        reg.observe("BB", {"rssi": -70, "connected": True})
        clock.t += 11
        self.assertEqual([d["address"] for d in reg.view()["devices"]], ["BB"])

    def test_merge_aggregates_devices_across_nodes(self):
        a, b = registry("alpha"), registry("beta")
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
        beta = [n for n in view["nodes"] if n["id"] == b.node["id"]][0]
        self.assertEqual(beta["link"]["via"], "lan")
        self.assertEqual(beta["hops"], 1)

    def test_states_forward_across_hops_signed_bodies_untouched(self):
        a, b, c = registry("a"), registry("b"), registry("c")
        c.observe("CC", {"rssi": -50})
        b.merge(c.node["id"], c.snapshot(), via="ip")
        a.merge(b.node["id"], b.snapshot(), via="lan")
        nodes = {n["id"]: n for n in a.view()["nodes"]}
        self.assertEqual(nodes[c.node["id"]]["hops"], 2)
        self.assertIsNone(nodes[c.node["id"]]["link"])
        # an older copy is ignored; a tampered body is rejected
        old = [s for s in c.snapshot()]
        a.merge(c.node["id"], c.snapshot(), via="ip")
        self.assertEqual(a.merge(c.node["id"], old, via="ip"), 0)
        forged = dict(c.snapshot()[0])
        forged["body"] = forged["body"].replace('"CC"', '"XX"')
        self.assertEqual(a.merge(c.node["id"], [forged], via="ip"), 0)

    def test_unenrolled_and_foreign_nodes_are_rejected(self):
        a = registry("a")
        outsider = registry("mallory", owner_name="someone-else")
        self.assertEqual(a.merge(outsider.node["id"], outsider.snapshot(), via="lan"), 0)

    def test_own_state_is_never_accepted_back(self):
        a = registry("a")
        self.assertEqual(a.merge("x" * 16, a.snapshot(), via="lan"), 0)

    def test_beacons_of_known_nodes_become_bt_links(self):
        a, b = registry("a"), registry("b")
        a.observe("BE:AC", {"rssi": -55, "btedge_node": b.node["id"]})
        a.merge(b.node["id"], b.snapshot(), via="lan")
        view = a.view()
        self.assertEqual(view["devices"], [])
        self.assertEqual([n["bt"] for n in view["nodes"] if n["id"] == b.node["id"]][0]["rssi"], -55)


class TrustTest(unittest.TestCase):
    def test_certificate_is_bound_to_node_and_machine(self):
        ident = identity("n1")
        self.assertTrue(ident.enrolled)
        pub = owner()[0]
        with open(os.path.join(ident.state_dir, trust.CERT_FILE)) as fh:
            cert = fh.read().strip()
        other = identity("n2", enroll=False)
        with self.assertRaises(trust.TrustError):  # someone else's certificate
            trust.verify_cert(pub, other.node_id, other.public, other.machine, cert)
        with self.assertRaises(trust.TrustError):  # same node, another machine
            trust.verify_cert(pub, ident.node_id, ident.public, "0123456789abcdef", cert)
        self.assertFalse(other.enrolled)
        self.assertTrue(os.path.exists(os.path.join(other.state_dir, trust.REQUEST_FILE)))

    def test_copied_state_dir_on_another_machine_is_refused(self):
        ident = identity("n3")
        clone = ident.state_dir + "-clone"
        shutil.copytree(ident.state_dir, clone)
        orig = trust.machine_fingerprint
        trust.machine_fingerprint = lambda: "fedcba9876543210"
        try:
            copy = trust.Identity(clone, owner()[0], name="n3")
        finally:
            trust.machine_fingerprint = orig
        self.assertEqual(copy.node_id, ident.node_id)
        self.assertFalse(copy.enrolled)


class ProtocolTest(unittest.TestCase):
    def test_roundtrip_and_rejections(self):
        a, b = registry("a"), registry("b")
        pkt = protocol.encode(a.identity, a.snapshot())
        self.assertEqual(protocol.decode(pkt, b.verifier)[0], a.node["id"])
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(pkt[:-1] + b"x", b.verifier)
        outsider = registry("m", owner_name="someone-else")
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(protocol.encode(outsider.identity, outsider.snapshot()), b.verifier)
        # a member's state relayed by an outsider is not enough to talk to the mesh
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(protocol.encode(outsider.identity, a.snapshot()), b.verifier)

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
        server_reg, client_reg = registry("server"), registry("client")
        server_reg.observe("SS", {"rssi": -40})
        client_reg.observe("CC", {"rssi": -70})
        panel = Panel(server_reg, mesh(server_reg), None, "127.0.0.1", 0)
        panel.start()
        try:
            url = "http://127.0.0.1:%d" % panel.port
            client_mesh = mesh(client_reg, peers=[url])
            client_mesh.sync_peer(url)
            self.assertTrue(client_mesh.peer_status[url]["ok"], client_mesh.peer_status[url])
            self.assertEqual({d["address"] for d in client_reg.view()["devices"]}, {"SS", "CC"})
            self.assertEqual({d["address"] for d in server_reg.view()["devices"]}, {"SS", "CC"})

            with urllib.request.urlopen(url + "/api/state") as resp:
                self.assertEqual(len(json.load(resp)["nodes"]), 2)

            for intruder in (registry("intruder", owner_name="someone-else"), registry("pending", enroll=False)):
                m = mesh(intruder, peers=[url])
                m.sync_peer(url)
                self.assertFalse(m.peer_status[url]["ok"])
        finally:
            panel.stop()


if __name__ == "__main__":
    unittest.main()
