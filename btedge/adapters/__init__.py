"""Device adapters: the code that speaks each device family's protocol.

An adapter is a class with a name, the actions it offers ({id: label}) and
run(gatt, action, settings) doing the work over a Gatt session (see gatt.py); settings
is what the mesh wants on the device ({"alarms": ...}) and run returns the names of the
settings it wrote. An adapter with `alarms = N` gets the alarm grid in the panel, which
grows a column at a time up to the N alarm times its devices hold (3 on an ID115 Plus,
10 on other bands; the mesh keeps up to registry.ALARM_SLOTS). The profiler
(profiles.json) decides which adapter a device gets. To support a new family, add
a module here, register it in ADAPTERS and point a profile at it.
"""
from .immediate_alert import ImmediateAlert
from .yoho import Yoho

ADAPTERS = {cls.name: cls() for cls in (Yoho, ImmediateAlert)}


class UnknownAction(ValueError):
    pass


def actions(adapter_name):
    adapter = ADAPTERS.get(adapter_name)
    return [{"id": k, "label": v} for k, v in adapter.actions.items()] if adapter else []


def alarms(adapter_name):
    """How many alarm times the adapter's devices hold; None if they have no alarms."""
    return getattr(ADAPTERS.get(adapter_name), "alarms", None)


def run(adapter_name, action, gatt, settings=None):
    adapter = ADAPTERS.get(adapter_name)
    if adapter is None or action not in adapter.actions:
        raise UnknownAction("%s: action %r not supported" % (adapter_name, action))
    return adapter.run(gatt, action, settings) or ()
