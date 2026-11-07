"""Device adapters: the code that speaks each device family's protocol.

An adapter is a class with a name, the actions it offers ({id: label}) and
run(gatt, action) doing the work over a Gatt session (see gatt.py). The profiler
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


def run(adapter_name, action, gatt):
    adapter = ADAPTERS.get(adapter_name)
    if adapter is None or action not in adapter.actions:
        raise UnknownAction("%s: action %r not supported" % (adapter_name, action))
    return adapter.run(gatt, action)
