"""Device profiling: who is this device and what can we do with it.

The address book (profiles.json, plus <state_dir>/profiles.json for local
additions, checked first) is an ordered list of rules. A rule matches when every
criterion it lists matches (within a criterion, any value is enough):

    name_prefix  ["MS"]            advertised name starts with one of these
    name_regex   "^Mi Band"        regular expression on the name
    services     ["cc00", "1802"]  GATT service UUIDs (16-bit short or full 128-bit)
    manufacturer [548]             Bluetooth SIG company identifiers
    icon         ["phone"]         BlueZ icon (from the device class / appearance)

The first matching rule with a class gives the model (label) and class; the first
matching rule that names an adapter gives the actions. So generic rules (e.g. Find
Me, which only brings an adapter) never hide a model recognised by a later rule.
"""
import json
import logging
import os
import re

from . import adapters
from .gatt import uuid128

log = logging.getLogger(__name__)
BUILTIN = os.path.join(os.path.dirname(__file__), "profiles.json")


def _load(path):
    with open(path) as fh:
        data = json.load(fh)
    return data.get("classes", {}), data.get("profiles", [])


class Profiler:
    def __init__(self, extra_path=None):
        self.classes, builtin = _load(BUILTIN)
        local = []
        if extra_path and os.path.exists(extra_path):
            try:
                classes, local = _load(extra_path)
                self.classes = dict(self.classes, **classes)
                log.info("loaded %d local profiles from %s", len(local), extra_path)
            except (OSError, ValueError) as exc:
                log.error("ignoring %s: %s", extra_path, exc)
        self.rules = [self._compile(r) for r in local + builtin]

    @staticmethod
    def _compile(rule):
        match = dict(rule.get("match") or {})
        if "services" in match:
            match["services"] = {uuid128(u) for u in match["services"]}
        if "name_regex" in match:
            match["name_regex"] = re.compile(match["name_regex"], re.I)
        return dict(rule, match=match)

    @staticmethod
    def _matches(match, dev):
        name = dev.get("name") or ""
        if "name_prefix" in match and not any(name.startswith(p) for p in match["name_prefix"]):
            return False
        if "name_regex" in match and not match["name_regex"].search(name):
            return False
        if "services" in match and not match["services"] & {str(u).lower() for u in dev.get("uuids") or []}:
            return False
        if "manufacturer" in match and not set(match["manufacturer"]) & set(dev.get("manufacturer") or []):
            return False
        if "icon" in match and dev.get("icon") not in match["icon"]:
            return False
        return bool(match)

    def profile(self, dev):
        """{"id", "label", "class", "class_label", "adapter", "actions"} or None."""
        found, first, acts, adapter = None, None, [], None
        for rule in self.rules:
            if not self._matches(rule["match"], dev):
                continue
            first = first or rule
            if found is None and rule.get("class"):
                found = rule
            if rule.get("adapter") and adapter is None:
                adapter = rule["adapter"]
                acts = adapters.actions(adapter)
        found = found or first
        if found is None:
            return None
        cls = found.get("class")
        return {"id": found["id"], "label": found.get("label"), "class": cls,
                "class_label": self.classes.get(cls) if cls else None,
                "adapter": adapter, "actions": acts}
