import base64
import os
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request

from btedge import config, trust


def make_owner(tmp):
    key = os.path.join(tmp, "id_ed25519")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], check=True)
    return trust.issue(key + ".pub")
from btedge.registry import Registry
from btedge.web import Panel


class ControlConfigTest(unittest.TestCase):
    def test_control_role_generates_and_keeps_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner, cred = make_owner(tmp)
            env = {"BTEDGE_ROLE": "control", "BTEDGE_STATE_DIR": tmp, "BTEDGE_LAN": "false",
                   "BTEDGE_MESH_OWNER": owner, "BTEDGE_MESH_CREDENTIAL": cred,
                   "BTEDGE_PEERS": "http://a:1, http://b:2"}
            cfg = config.load(None, environ=env)
            self.assertEqual(cfg["scanner"]["backend"], "none")
            self.assertFalse(cfg["mesh"]["lan"]["enabled"])
            self.assertEqual(cfg["mesh"]["peers"], ["http://a:1", "http://b:2"])
            self.assertTrue(cfg["mesh"]["key"] and cfg["admin"]["password"])
            self.assertEqual(config.load(None, environ=dict(env, BTEDGE_ADMIN_USER="fra"))["admin"]["user"], "fra")
            again = config.load(None, environ=env)
            self.assertEqual((again["mesh"]["key"], again["admin"]["password"]),
                             (cfg["mesh"]["key"], cfg["admin"]["password"]))

    def test_membership_needs_the_owners_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner, cred = make_owner(tmp)
            base = {"BTEDGE_STATE_DIR": tmp, "BTEDGE_MESH_OWNER": owner}
            key = config.load(None, environ=dict(base, BTEDGE_MESH_CREDENTIAL=cred))["mesh"]["key"]
            self.assertEqual(key, trust.verify(owner, trust.issue(os.path.join(tmp, "id_ed25519.pub"))[1]))
            with self.assertRaises(ValueError):
                config.load(None, environ=base)  # no credential
            os.makedirs(os.path.join(tmp, "other"))
            _, foreign = make_owner(os.path.join(tmp, "other"))  # signed by someone else
            with self.assertRaises(trust.TrustError):
                config.load(None, environ=dict(base, BTEDGE_MESH_CREDENTIAL=foreign))
            with self.assertRaises(trust.TrustError):
                config.load(None, environ=dict(base, BTEDGE_MESH_CREDENTIAL=cred[:-8] + "AAAAAAAA"))

    def test_rejects_unknown_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                owner, cred = make_owner(tmp)
                config.load(None, environ={"BTEDGE_ROLE": "bogus", "BTEDGE_STATE_DIR": tmp,
                                           "BTEDGE_MESH_OWNER": owner, "BTEDGE_MESH_CREDENTIAL": cred})


class AuthTest(unittest.TestCase):
    def setUp(self):
        reg = Registry({"id": "c" * 16, "name": "ctl", "http": 0, "role": "control"})
        self.panel = Panel(reg, None, None, "127.0.0.1", 0, admin_password="pw")
        self.panel.start()
        self.base = "http://127.0.0.1:%d" % self.panel.port

    def tearDown(self):
        self.panel.stop()

    def get(self, path, password=None):
        req = urllib.request.Request(self.base + path)
        if password:
            req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:" + password.encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_panel_requires_login_but_health_does_not(self):
        self.assertEqual(self.get("/api/health"), 200)
        self.assertEqual(self.get("/api/state"), 401)
        self.assertEqual(self.get("/api/state", "wrong"), 401)
        self.assertEqual(self.get("/api/state", "pw"), 200)


if __name__ == "__main__":
    unittest.main()
