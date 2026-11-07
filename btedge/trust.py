"""Mesh membership: per-node keys certified by the owner's SSH key.

    owner   the mesh is identified by the owner's SSH public key (mesh.owner)
    node    every node generates an Ed25519 key on first start; the private key never
            leaves the machine and the node id is derived from the public key
    enroll  the owner signs a certificate for that node with their SSH key
            (`python3 -m btedge enroll root@node`, through ssh-agent: the owner's
            private key never leaves their machine either). The certificate covers
            the node id, its public key and a fingerprint of the machine
            (/etc/machine-id), so it is useless anywhere else: without the node's
            private key it cannot sign, and on another machine the node refuses it.

Every node signs its own state and every packet it sends with its node key; the
others accept them only with a valid certificate from the owner. There is no
shared secret: only the owner can create members. Payloads are signed, not
encrypted (use HTTPS or a VPN across the internet).
"""
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

log = logging.getLogger(__name__)

NAMESPACE = "btedge-node"
CERT_PREFIX = "btedge-cert1:"
ARMOR = ("-----BEGIN SSH SIGNATURE-----", "-----END SSH SIGNATURE-----")
MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")
KEY_FILE, CERT_FILE, REQUEST_FILE = "node.key", "node.cert", "enroll-request.json"


class TrustError(ValueError):
    pass


def b64(data):
    return base64.b64encode(data).decode()


def unb64(text):
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        raise TrustError("invalid base64") from None


def _keygen():
    path = shutil.which("ssh-keygen")
    if not path:
        raise TrustError("ssh-keygen not found (install openssh-client)")
    return path


# -- owner ----------------------------------------------------------------

def owner_key(text):
    """Normalise an OpenSSH public key line to "type base64" (comment dropped)."""
    parts = (text or "").strip().split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise TrustError("mesh.owner must be an OpenSSH public key (e.g. the content of ~/.ssh/id_ed25519.pub)")
    unb64(parts[1])
    return parts[0] + " " + parts[1]


def fingerprint(owner):
    blob = unb64(owner_key(owner).split()[1])
    return "SHA256:" + b64(hashlib.sha256(blob).digest()).rstrip("=")


# -- node identity --------------------------------------------------------

def node_id_of(public_raw):
    return hashlib.sha256(b"btedge-node\0" + public_raw).hexdigest()[:16]


def machine_fingerprint():
    for path in MACHINE_ID_PATHS:
        try:
            with open(path) as fh:
                mid = fh.read().strip()
            if mid:
                return hashlib.sha256(b"btedge-machine\0" + mid.encode()).hexdigest()[:16]
        except OSError:
            continue
    return "-"


def cert_message(node_id, public_b64, machine):
    return ("btedge node v1\nnode=%s\nkey=%s\nmachine=%s\n" % (node_id, public_b64, machine)).encode()


class Identity:
    """This node's key pair and (once enrolled) its certificate."""

    def __init__(self, state_dir, owner, name="", role="edge"):
        self.state_dir = state_dir
        self.owner = owner_key(owner)
        self.name, self.role = name, role
        self._key = self._load_key(os.path.join(state_dir, KEY_FILE))
        raw = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public = b64(raw)
        self.node_id = node_id_of(raw)
        self.machine = machine_fingerprint()
        self.cert = None
        self.problem = None
        self._cert_mtime = None
        self.reload()

    @staticmethod
    def _load_key(path):
        try:
            with open(path, "rb") as fh:
                return serialization.load_pem_private_key(fh.read(), password=None)
        except FileNotFoundError:
            pass
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
        log.warning("generated node key %s", path)
        return key

    @property
    def enrolled(self):
        return self.cert is not None

    def request(self):
        return {"node_id": self.node_id, "name": self.name, "role": self.role,
                "key": self.public, "machine": self.machine, "owner": fingerprint(self.owner)}

    def reload(self):
        """(Re)read node.cert; cheap to call often. Returns True when enrolment changed."""
        path = os.path.join(self.state_dir, CERT_FILE)
        try:
            mtime = os.stat(path).st_mtime
        except FileNotFoundError:
            mtime = None
        if mtime == self._cert_mtime and (self.cert or self.problem):
            return False
        self._cert_mtime = mtime
        was = self.cert
        self.cert, self.problem = None, None
        if mtime is None:
            self.problem = "not enrolled yet"
        else:
            with open(path) as fh:
                cert = fh.read().strip()
            try:
                verify_cert(self.owner, self.node_id, self.public, self.machine, cert)
                self.cert = cert
            except TrustError as exc:
                self.problem = str(exc)
        if not self.cert:
            with open(os.path.join(self.state_dir, REQUEST_FILE), "w") as fh:
                json.dump(self.request(), fh, indent=2)
            log.warning("node %s not in the mesh: %s. On the owner's machine run: "
                        "python3 -m btedge enroll <this host> (request in %s)",
                        self.node_id, self.problem, os.path.join(self.state_dir, REQUEST_FILE))
        elif was != self.cert:
            log.info("node %s enrolled in mesh %s", self.node_id, fingerprint(self.owner))
        return was != self.cert

    def sign(self, data):
        return self._key.sign(data)


# -- certificates (owner's SSH signature) -------------------------------------

def issue_cert(pubkey_path, request):
    """Sign an enrolment request with the owner's SSH key (agent or private key file)."""
    for field in ("node_id", "key", "machine"):
        if not isinstance(request.get(field), str):
            raise TrustError("enrolment request without %s" % field)
    if node_id_of(unb64(request["key"])) != request["node_id"]:
        raise TrustError("enrolment request: node id does not match its key")
    path = os.path.expanduser(pubkey_path)
    proc = subprocess.run([_keygen(), "-Y", "sign", "-f", path, "-n", NAMESPACE],
                          input=cert_message(request["node_id"], request["key"], request["machine"]),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise TrustError("ssh-keygen sign failed: %s" % proc.stderr.decode().strip())
    return CERT_PREFIX + "".join(l for l in proc.stdout.decode().splitlines() if l and not l.startswith("-----"))


_verified = {}


def verify_cert(owner, node_id, public_b64, machine, cert):
    """Raise TrustError unless cert is the owner's signature for exactly this node."""
    owner = owner_key(owner)
    if not isinstance(cert, str) or not cert.startswith(CERT_PREFIX):
        raise TrustError("malformed node certificate")
    if node_id_of(unb64(public_b64)) != node_id:
        raise TrustError("node id does not match its key")
    cache_key = (owner, node_id, public_b64, machine, cert)
    if cache_key in _verified:
        return
    body = cert[len(CERT_PREFIX):]
    unb64(body)
    armored = "\n".join([ARMOR[0]] + [body[i:i + 70] for i in range(0, len(body), 70)] + [ARMOR[1], ""])
    with tempfile.TemporaryDirectory() as tmp:
        signers, sig = os.path.join(tmp, "allowed"), os.path.join(tmp, "sig")
        with open(signers, "w") as fh:
            fh.write("owner %s\n" % owner)
        with open(sig, "w") as fh:
            fh.write(armored)
        proc = subprocess.run([_keygen(), "-Y", "verify", "-f", signers, "-I", "owner", "-n", NAMESPACE, "-s", sig],
                              input=cert_message(node_id, public_b64, machine),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise TrustError("certificate not signed by the mesh owner for this node and machine")
    if len(_verified) > 1024:
        _verified.clear()
    _verified[cache_key] = True


# -- signed states ----------------------------------------------------------

class Verifier:
    """Checks states and packets signed by enrolled nodes."""

    def __init__(self, owner, cert_check=verify_cert):
        self.owner = owner_key(owner)
        self.cert_check = cert_check
        self._cache = {}

    def state(self, wire):
        """Verify a wire state {"body", "sig", ...}; returns the parsed body."""
        try:
            body_text, sig = wire["body"], unb64(wire["sig"])
            if not isinstance(body_text, str) or len(body_text) > 1 << 20:
                raise TrustError("bad state body")
        except (KeyError, TypeError):
            raise TrustError("unsigned state") from None
        cached = self._cache.get(sig)
        if cached is not None and cached[0] == body_text:
            return cached[1]
        try:
            body = json.loads(body_text)
            node_id, key, cert, machine = body["id"], body["key"], body["cert"], body["machine"]
        except (ValueError, KeyError, TypeError):
            raise TrustError("malformed state body") from None
        self.cert_check(self.owner, node_id, key, machine, cert)
        self.signature(key, sig, body_text.encode())
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[sig] = (body_text, body)
        return body

    @staticmethod
    def signature(public_b64, sig, data):
        try:
            Ed25519PublicKey.from_public_bytes(unb64(public_b64)).verify(sig, data)
        except (InvalidSignature, ValueError):
            raise TrustError("bad signature") from None
