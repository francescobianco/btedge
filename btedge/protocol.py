"""Wire format shared by every mesh transport.

    packet = b"BTE2" + signature(64 bytes) + zlib(json({"v": 2, "from": id, "states": [...]}))

Each state is {"body": <json text signed by its origin node>, "sig": ..., "age": s,
"hops": n}: the body (devices, labels, commands, node key and certificate) is
forwarded byte for byte, while age and hops are updated by every forwarder.
The envelope is signed by the sending node, whose own state must be in the
packet, so only enrolled nodes can talk to the mesh (see trust.py).
"""
import json
import zlib

from .trust import TrustError

MAGIC = b"BTE2"
VERSION = 2
SIG = 64
MAX_PACKET = 1 << 20
MAX_PAYLOAD = 8 << 20


class ProtocolError(ValueError):
    pass


def encode(identity, states):
    body = json.dumps({"v": VERSION, "from": identity.node_id, "states": states}, separators=(",", ":"))
    payload = zlib.compress(body.encode())
    return MAGIC + identity.sign(payload) + payload


def decode(packet, verifier):
    """Check the envelope signature; returns (sender id, wire states). States are verified on merge."""
    if len(packet) > MAX_PACKET or len(packet) < 4 + SIG or not packet.startswith(MAGIC):
        raise ProtocolError("not a btedge packet")
    sig, payload = packet[4:4 + SIG], packet[4 + SIG:]
    inflater = zlib.decompressobj()
    try:
        body = inflater.decompress(payload, MAX_PAYLOAD)
        if inflater.unconsumed_tail:
            raise ProtocolError("payload too large")
        msg = json.loads(body)
    except (zlib.error, ValueError) as exc:
        raise ProtocolError("malformed payload: %s" % exc) from None
    if (not isinstance(msg, dict) or msg.get("v") != VERSION
            or not isinstance(msg.get("from"), str) or not isinstance(msg.get("states"), list)):
        raise ProtocolError("unsupported message")
    own = next((s for s in msg["states"] if isinstance(s, dict) and s.get("hops") == 0), None)
    try:
        if own is None:
            raise TrustError("sender state missing")
        sender = verifier.state(own)
        if sender["id"] != msg["from"]:
            raise TrustError("sender mismatch")
        verifier.signature(sender["key"], sig, payload)
    except TrustError as exc:
        raise ProtocolError("rejected: %s" % exc) from None
    return msg["from"], msg["states"]
