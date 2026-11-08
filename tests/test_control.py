import base64
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request

from btedge import config, trust
from btedge.auth import Login, LoginError
from btedge.registry import Registry

from meshkit import owner
from btedge.registry import Registry
from btedge.web import Panel


class ControlConfigTest(unittest.TestCase):
    def test_control_role_generates_and_keeps_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"BTEDGE_ROLE": "control", "BTEDGE_STATE_DIR": tmp, "BTEDGE_LAN": "false",
                   "BTEDGE_MESH_OWNER": owner()[0] + " comment",
                   "BTEDGE_PEERS": "http://a:1, http://b:2"}
            cfg = config.load(None, environ=env)
            self.assertEqual(cfg["scanner"]["backend"], "none")
            self.assertFalse(cfg["mesh"]["lan"]["enabled"])
            self.assertEqual(cfg["mesh"]["peers"], ["http://a:1", "http://b:2"])
            self.assertEqual(cfg["mesh"]["owner"], owner()[0])
            self.assertTrue(cfg["admin"]["password"])
            self.assertEqual(config.load(None, environ=dict(env, BTEDGE_ADMIN_USER="fra"))["admin"]["user"], "fra")
            again = config.load(None, environ=env)
            self.assertEqual(again["admin"]["password"], cfg["admin"]["password"])

    def test_owner_key_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                config.load(None, environ={"BTEDGE_STATE_DIR": tmp})

    def test_rejects_unknown_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                config.load(None, environ={"BTEDGE_ROLE": "bogus", "BTEDGE_STATE_DIR": tmp,
                                           "BTEDGE_MESH_OWNER": owner()[0]})


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

    def change(self, current, new, login="pw"):
        body = json.dumps({"current": current, "new": new}).encode()
        req = urllib.request.Request(self.base + "/api/password", data=body, method="POST")
        req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:" + login.encode()).decode())
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

    def test_change_password_requires_the_current_one(self):
        self.assertEqual(self.change("wrong", "new-password"), 403)
        self.assertEqual(self.change("pw", "short"), 400)
        self.assertEqual(self.get("/api/state", "pw"), 200)
        self.assertEqual(self.change("pw", "new-password"), 200)
        self.assertEqual(self.get("/api/state", "pw"), 401)
        self.assertEqual(self.get("/api/state", "new-password"), 200)


class LoginTest(unittest.TestCase):
    def test_changed_password_survives_a_restart_and_beats_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "admin_password.hash")
            Login("admin", "from-env", path).change("from-env", "from-panel")
            with open(path) as fh:
                self.assertNotIn("from-panel", fh.read())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            again = Login("admin", "from-env", path)
            self.assertTrue(again.check("from-panel"))
            self.assertTrue(again.check("from-panel"))  # second time from the fast path
            self.assertFalse(again.check("from-env"))

    def test_corrupt_file_falls_back_to_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "admin_password.hash")
            with open(path, "w") as fh:
                fh.write("garbage")
            self.assertTrue(Login("admin", "from-env", path).check("from-env"))

    def test_node_without_login_has_nothing_to_change(self):
        login = Login("admin", None)
        self.assertTrue(login.authorized(None))
        with self.assertRaises(LoginError) as caught:
            login.change("x", "new-password")
        self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
