"""A throwaway mesh for tests: a temporary owner SSH key and enrolled nodes."""
import atexit
import os
import shutil
import subprocess
import tempfile

from btedge import trust
from btedge.registry import Registry

_ROOT = tempfile.mkdtemp(prefix="btedge-test-")
atexit.register(shutil.rmtree, _ROOT, True)


def owner(name="owner"):
    """(public key line, path of the .pub) of a fresh Ed25519 owner key."""
    path = os.path.join(_ROOT, name)
    if not os.path.exists(path):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", path], check=True)
    with open(path + ".pub") as fh:
        return trust.owner_key(fh.read()), path + ".pub"


def identity(name, owner_name="owner", enroll=True, role="edge"):
    pub, path = owner(owner_name)
    state = tempfile.mkdtemp(prefix=name + "-", dir=_ROOT)
    ident = trust.Identity(state, pub, name=name, role=role)
    if enroll:
        with open(os.path.join(state, trust.CERT_FILE), "w") as fh:
            fh.write(trust.issue_cert(path, ident.request()))
        ident.reload()
    return ident


def registry(name, role="edge", owner_name="owner", enroll=True, **kw):
    ident = identity(name, owner_name, enroll, role)
    reg = Registry({"id": ident.node_id, "name": name, "http": 4646, "role": role},
                   identity=ident, verifier=trust.Verifier(owner()[0]), **kw)
    return reg
