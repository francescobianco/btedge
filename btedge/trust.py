"""Mesh membership rooted in the owner's SSH key.

The mesh is identified by the owner's SSH *public* key. Its secret is the owner's
SSH signature (sshsig, namespace "btedge-mesh") over a fixed message: only the
holder of the private key can produce it, and since RSA and Ed25519 signatures
are deterministic, signing again always yields the same credential.

    owner's machine:  python3 -m btedge credential   (ssh-keygen -Y sign, via ssh-agent;
                                                     the private key never leaves)
    every node:       mesh.owner = public key, mesh.credential = that signature

At startup a node checks the credential against the owner key (ssh-keygen -Y
verify) and refuses to run if it is not the owner's signature. The HMAC key that
signs every mesh packet is derived from the credential, so a node without it can
neither read-and-forge nor join. Keep the credential secret like a password.
"""
import base64
import hashlib
import os
import shutil
import subprocess
import tempfile

NAMESPACE = "btedge-mesh"
MESSAGE = b"btedge mesh v1"
PREFIX = "btedge1:"
ARMOR = ("-----BEGIN SSH SIGNATURE-----", "-----END SSH SIGNATURE-----")


class TrustError(ValueError):
    pass


def _keygen():
    path = shutil.which("ssh-keygen")
    if not path:
        raise TrustError("ssh-keygen not found (install openssh-client)")
    return path


def owner_key(text):
    """Normalise an OpenSSH public key line to "type base64" (comment dropped)."""
    parts = (text or "").strip().split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise TrustError("mesh.owner must be an OpenSSH public key (e.g. the content of ~/.ssh/id_ed25519.pub)")
    if parts[0].startswith("ecdsa-") or parts[0].startswith("sk-ecdsa"):
        raise TrustError("ECDSA signatures are not deterministic: use an Ed25519 or RSA key")
    try:
        base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise TrustError("mesh.owner: invalid key data") from None
    return parts[0] + " " + parts[1]


def fingerprint(owner):
    blob = base64.b64decode(owner_key(owner).split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _sign(signing):
    proc = subprocess.run([_keygen(), "-Y", "sign", "-f", signing, "-n", NAMESPACE],
                          input=MESSAGE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise TrustError("ssh-keygen sign failed: %s" % proc.stderr.decode().strip())
    return PREFIX + "".join(l for l in proc.stdout.decode().splitlines() if l and not l.startswith("-----"))


def issue(pubkey_path):
    """Sign the mesh message with the SSH key at pubkey_path (through its agent or private key)."""
    signing = os.path.expanduser(pubkey_path)
    with open(signing) as fh:
        owner = owner_key(fh.read())
    credential = _sign(signing)
    verify(owner, credential)
    if _sign(signing) != credential:
        raise TrustError("this key does not sign deterministically: use an Ed25519 or RSA key")
    return owner, credential


def verify(owner, credential):
    """Check the credential is the owner's signature; return the derived mesh key."""
    owner = owner_key(owner)
    credential = (credential or "").strip()
    if not credential.startswith(PREFIX):
        raise TrustError("mesh.credential missing or malformed: run `python3 -m btedge credential` "
                         "on the machine holding the owner's SSH key")
    body = credential[len(PREFIX):]
    try:
        blob = base64.b64decode(body, validate=True)
    except ValueError:
        raise TrustError("mesh.credential: invalid base64") from None
    armored = "\n".join([ARMOR[0]] + [body[i:i + 70] for i in range(0, len(body), 70)] + [ARMOR[1], ""])
    with tempfile.TemporaryDirectory() as tmp:
        signers, sig = os.path.join(tmp, "allowed"), os.path.join(tmp, "sig")
        with open(signers, "w") as fh:
            fh.write("owner %s\n" % owner)
        with open(sig, "w") as fh:
            fh.write(armored)
        proc = subprocess.run([_keygen(), "-Y", "verify", "-f", signers, "-I", "owner", "-n", NAMESPACE, "-s", sig],
                              input=MESSAGE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise TrustError("mesh.credential is not a signature of the mesh owner (%s)" % fingerprint(owner))
    return hashlib.sha256(b"btedge-mesh-key\0" + blob).hexdigest()
