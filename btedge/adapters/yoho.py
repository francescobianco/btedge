"""Yoho Sports / uThink bands (e.g. ID115 Plus, advertised as "MS…").

Service cc00; commands are written with response to cc06. Reverse engineered in
github.com/francescobianco/id115 (see its README for the full command table).
"""

CMD = "cc06"


class Yoho:
    name = "yoho"
    actions = {"vibrate": "Fai vibrare"}

    def run(self, gatt, action):
        if action == "vibrate":
            gatt.write(CMD, bytes([0x08, 0x01]))  # "find band": vibrates
        return None
