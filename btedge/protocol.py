"""Wire format shared by every mesh transport.

    packet = b"BTE1" + mac(32 bytes) + zlib(json({"v": 1, "from": id, "states": [...]}))

The mac is HMAC-SHA256 with the mesh key, or plain SHA-256 when no key is set,
so nodes with different keys silently ignore each other. Payloads are signed,
not encrypted.
"""
import hashlib
import hmac
import json
import zlib

MAGIC = b"BTE1"
VERSION = 1
MAX_PACKET = 1 << 20
MAX_PAYLOAD = 8 << 20


class ProtocolError(ValueError):
    pass


def _mac(key, payload):
    if key:
        return hmac.new(key.encode(), payload, hashlib.sha256).digest()
    return hashlib.sha256(payload).digest()


def encode(sender, states, key=""):
    body = json.dumps({"v": VERSION, "from": sender, "states": states}, separators=(",", ":"))
    payload = zlib.compress(body.encode())
    return MAGIC + _mac(key, payload) + payload


def decode(packet, key=""):
    if len(packet) > MAX_PACKET or len(packet) < 36 or not packet.startswith(MAGIC):
        raise ProtocolError("not a btedge packet")
    mac, payload = packet[4:36], packet[36:]
    if not hmac.compare_digest(mac, _mac(key, payload)):
        raise ProtocolError("bad signature (mesh key mismatch?)")
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
    return msg
