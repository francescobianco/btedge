"""`python3 -m btedge enroll TARGET`: admit a node into the mesh, as its owner.

Run on the machine holding the owner's SSH key. It reads the node's enrolment
request (node id, public key, machine fingerprint), signs a certificate for it
with ssh-keygen (through ssh-agent: the private key never leaves this machine)
and writes it back as <state_dir>/node.cert. The node picks it up within seconds.

TARGET is an ssh destination (root@orangepi) or a local state directory.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys

from . import trust


def _default_key():
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        if os.path.exists(os.path.expanduser("~/.ssh/" + name)):
            return "~/.ssh/" + name
    return "~/.ssh/id_ed25519.pub"


def _read(target, path, local):
    if local:
        try:
            with open(path) as fh:
                return fh.read()
        except PermissionError:
            return subprocess.run(["sudo", "cat", path], check=True, stdout=subprocess.PIPE).stdout.decode()
    return subprocess.run(["ssh", target, "cat " + shlex.quote(path)], check=True, stdout=subprocess.PIPE).stdout.decode()


def _write(target, path, text, local):
    data = (text + "\n").encode()
    if local:
        try:
            with open(path, "wb") as fh:
                fh.write(data)
            return
        except PermissionError:
            subprocess.run(["sudo", "tee", path], check=True, input=data, stdout=subprocess.DEVNULL)
            return
    subprocess.run(["ssh", target, "umask 022 && cat > " + shlex.quote(path)], check=True, input=data)


def main(argv):
    parser = argparse.ArgumentParser(prog="btedge enroll", description=__doc__.split("\n\n")[0])
    parser.add_argument("target", help="ssh destination of the node, or its local state directory")
    parser.add_argument("--state-dir", default="/var/lib/btedge", help="node state directory (default: %(default)s)")
    parser.add_argument("-f", "--key", default=_default_key(), help="owner's SSH public key (default: %(default)s)")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)

    local = os.path.isdir(args.target)
    state_dir = args.target if local else args.state_dir
    try:
        with open(os.path.expanduser(args.key)) as fh:
            owner = trust.owner_key(fh.read())
        request = json.loads(_read(args.target, os.path.join(state_dir, trust.REQUEST_FILE), local))
        print("node     %s (%s, %s)" % (request.get("node_id"), request.get("name") or "?", request.get("role") or "edge"))
        print("machine  %s" % request.get("machine"))
        print("mesh     %s" % trust.fingerprint(owner))
        if request.get("owner") and request["owner"] != trust.fingerprint(owner):
            raise SystemExit("error: the node belongs to mesh %s, not to this key" % request["owner"])
        if not args.yes and input("Admit this node into the mesh? [y/N] ").strip().lower() not in ("y", "yes"):
            raise SystemExit("aborted")
        cert = trust.issue_cert(args.key, request)
        trust.verify_cert(owner, request["node_id"], request["key"], request["machine"], cert)
        _write(args.target, os.path.join(state_dir, trust.CERT_FILE), cert, local)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit("error: %s" % exc)
    print("enrolled: the node joins the mesh within a few seconds", file=sys.stderr)
