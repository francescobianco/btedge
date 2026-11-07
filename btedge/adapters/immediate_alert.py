"""Standard BLE Find Me profile: Immediate Alert service (1802), Alert Level (2A06).

Many bands, watches and trackers vibrate or beep when the alert level is set to
"high" (2). Written without response, as the specification requires.
"""

ALERT_LEVEL = "2a06"


class ImmediateAlert:
    name = "immediate_alert"
    actions = {"vibrate": "Fai vibrare"}

    def run(self, gatt, action):
        if action == "vibrate":
            gatt.write(ALERT_LEVEL, bytes([0x02]), response=False)
        return None
