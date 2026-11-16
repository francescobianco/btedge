"""Yoho Sports / uThink bands (e.g. ID115 Plus, advertised as "MS…").

Service cc00; commands are written with response to cc06. Reverse engineered in
github.com/francescobianco/id115 (see its README for the full command table).

The band cannot be asked what it holds, so the mesh keeps the wanted state (the alarms
set in the panel) and "sync" writes it: the clock first, then all the alarms in one
packet of at most 3 times, each with its days.
"""
import datetime

CMD = "cc06"
SLOTS = 3


def clock_packet(now):
    """04 00 <year-2000> <month> <day> <hour> <min> <sec>"""
    return bytes([0x04, 0x00, now.year - 2000, now.month, now.day, now.hour, now.minute, now.second])


def alarms_packet(slots):
    """0C <any> then 3 slots of <on> <hour> <min> <days>; days: bit 0 = Sunday ... bit 6 = Saturday.

    slots: [{"time": "HH:MM", "days": [0..6]}] with 0 = Monday, as the mesh keeps them.
    """
    masks = {}
    for slot in slots:
        for day in slot["days"]:
            masks[slot["time"]] = masks.get(slot["time"], 0) | 1 << (day + 1) % 7
    if len(masks) > SLOTS:
        raise ValueError("this band holds %d alarm times, %d set" % (SLOTS, len(masks)))
    packet = [0x0C, 1 if masks else 0]
    for time in sorted(masks):
        hour, minute = time.split(":")
        packet += [1, int(hour), int(minute), masks[time]]
    return bytes(packet + [0] * (2 + 4 * SLOTS - len(packet)))


def local_now(tz):
    """The time to put on the band: in the time zone the alarms were set in, else the node's."""
    if tz:
        try:
            import zoneinfo
            return datetime.datetime.now(zoneinfo.ZoneInfo(tz))
        except Exception:  # unknown zone, or no tz database on this node
            pass
    return datetime.datetime.now()


class Yoho:
    name = "yoho"
    actions = {"vibrate": "Fai vibrare", "sync": "Applica ora e sveglie"}
    alarms = SLOTS  # how many alarm times the device holds

    def run(self, gatt, action, settings=None):
        """Returns the settings it wrote on the device, if any."""
        if action == "vibrate":
            gatt.write(CMD, bytes([0x08, 0x01]))  # "find band": vibrates
        if action == "sync":
            alarms = (settings or {}).get("alarms")
            gatt.write(CMD, clock_packet(local_now(alarms.get("tz") if alarms else None)))
            if alarms is None:
                return ()  # never set in the mesh: leave the alarms the band has
            gatt.write(CMD, alarms_packet(alarms["slots"]))
            return ("alarms",)
        return ()
