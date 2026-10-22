"""Bluetooth backends.

BluezBackend talks to BlueZ over D-Bus: it keeps discovery running, reports
every advertisement to the registry and broadcasts a small BLE beacon carrying
the node id, so btedge nodes can also detect each other over the air.

FakeBackend simulates devices for development on machines without Bluetooth.
"""
import logging
import random
import threading
import time

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
ADV = "org.bluez.LEAdvertisement1"
ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
PROPS = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
ADV_PATH = "/org/btedge/advertisement0"

# Service data UUID carried by btedge beacons; the payload is the 8-byte node id.
BTEDGE_UUID = "62746564-6765-4e4f-8000-6d6573680001"

ADVERT_KEYS = ("RSSI", "ManufacturerData", "ServiceData", "TxPower", "Connected")


def device_fields(props):
    """Convert BlueZ Device1 properties into registry fields."""
    address = str(props.get("Address", ""))
    name = props.get("Name")
    alias = props.get("Alias")
    if name is None and alias is not None and str(alias) != address.replace(":", "-"):
        name = alias
    fields = {
        "name": str(name) if name is not None else None,
        "rssi": int(props["RSSI"]) if "RSSI" in props else None,
        "connected": bool(props["Connected"]) if "Connected" in props else None,
        "paired": bool(props["Paired"]) if "Paired" in props else None,
        "address_type": str(props["AddressType"]) if "AddressType" in props else None,
        "icon": str(props["Icon"]) if "Icon" in props else None,
        "tx_power": int(props["TxPower"]) if "TxPower" in props else None,
        "manufacturer": sorted(int(k) for k in props["ManufacturerData"]) if props.get("ManufacturerData") else None,
        "uuids": [str(u) for u in props["UUIDs"]][:8] if props.get("UUIDs") else None,
    }
    service_data = props.get("ServiceData") or {}
    for uuid, value in service_data.items():
        if str(uuid).lower() == BTEDGE_UUID:
            fields["btedge_node"] = bytes(bytearray(value)).hex()
    return fields


class BluezBackend:
    def __init__(self, registry, node_id, adapter=None, advertise=True, cache_ttl=300, rescan_interval=20):
        self.registry = registry
        self.node_id = node_id
        self.adapter_name = adapter
        self.advertise = advertise
        self.cache_ttl = cache_ttl
        self.rescan_interval = rescan_interval
        self._discovery_started = 0.0
        self.status = {"backend": "bluez", "state": "starting", "adapter": None,
                       "advertising": False, "beacon_error": None, "error": None}
        self._adapter = None
        self._props = {}
        self._seen = {}
        self._adv = None
        self._adv_pending = False

    def start(self):
        threading.Thread(target=self._run, name="bluez", daemon=True).start()

    def _set(self, **kwargs):
        for key in ("error", "beacon_error"):
            if kwargs.get(key) and kwargs[key] != self.status.get(key):
                log.warning("bluetooth %s: %s", key, kwargs[key])
        self.status.update(kwargs)

    def _run(self):
        try:
            import dbus
            import dbus.mainloop.glib
            from gi.repository import GLib
        except ImportError as exc:
            self._set(state="error", error="missing python3-dbus / python3-gi: %s" % exc)
            return
        self._dbus = dbus
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        try:
            self._bus = dbus.SystemBus()
            self._om = dbus.Interface(self._bus.get_object(BLUEZ, "/"), OBJECT_MANAGER)
        except dbus.DBusException as exc:
            self._set(state="error", error="BlueZ not reachable: %s" % exc.get_dbus_message())
            return
        self._bus.add_signal_receiver(self._on_added, dbus_interface=OBJECT_MANAGER,
                                      signal_name="InterfacesAdded")
        self._bus.add_signal_receiver(self._on_removed, dbus_interface=OBJECT_MANAGER,
                                      signal_name="InterfacesRemoved")
        self._bus.add_signal_receiver(self._on_changed, dbus_interface=PROPS,
                                      signal_name="PropertiesChanged", arg0=DEVICE,
                                      path_keyword="path")
        GLib.idle_add(self._tick)
        GLib.timeout_add_seconds(self.rescan_interval, self._tick)
        GLib.MainLoop().run()

    # -- adapter supervision ------------------------------------------------

    def _tick(self):
        try:
            self._ensure()
        except self._dbus.DBusException as exc:
            self._set(state="error", error=exc.get_dbus_message() or str(exc))
            self._adapter = None
        return True

    def _ensure(self):
        dbus = self._dbus
        if self._adapter is None:
            objects = self._om.GetManagedObjects()
            adapters = sorted(p for p, i in objects.items() if ADAPTER in i
                              and (not self.adapter_name or p.endswith("/" + self.adapter_name)))
            if not adapters:
                self._set(state="no_adapter", adapter=None, advertising=False,
                          error="no Bluetooth adapter found")
                return
            self._adapter = adapters[0]
            self._adv_supported = ADV_MANAGER in objects[self._adapter]
            self._props.clear()
            self._seen.clear()
            now = time.monotonic()
            for path, ifaces in objects.items():
                if DEVICE in ifaces and path.startswith(self._adapter + "/"):
                    self._props[path] = dict(ifaces[DEVICE])
                    self._seen[path] = now
                    if self._props[path].get("Connected"):
                        self._report(path)
            self._set(adapter=self._adapter.rsplit("/", 1)[-1], advertising=False)
            log.info("using adapter %s", self._adapter)

        obj = self._bus.get_object(BLUEZ, self._adapter)
        props = dbus.Interface(obj, PROPS)
        if not props.Get(ADAPTER, "Powered"):
            props.Set(ADAPTER, "Powered", dbus.Boolean(True))
        # BlueZ filters duplicate adverts during a discovery session (reporting every
        # advert floods D-Bus and costs a whole core on small boards), so the session is
        # restarted periodically: each device still in range is then reported again.
        adapter = dbus.Interface(obj, ADAPTER)
        if props.Get(ADAPTER, "Discovering"):
            if time.monotonic() - self._discovery_started >= self.rescan_interval:
                try:
                    adapter.StopDiscovery()
                except dbus.DBusException:
                    pass
                adapter.StartDiscovery()
                self._discovery_started = time.monotonic()
        else:
            try:
                adapter.SetDiscoveryFilter({"DuplicateData": dbus.Boolean(False)})
            except dbus.DBusException:
                pass
            adapter.StartDiscovery()
            self._discovery_started = time.monotonic()
            log.info("discovery started")
        if self.advertise and self._adv_supported and not self.status["advertising"] and not self._adv_pending:
            self._register_advertisement(obj)
        self._prune_cache(adapter)
        self._set(state="scanning", error=None)

    def _prune_cache(self, adapter):
        """Drop long-gone unpaired devices so BlueZ's cache stays small on low-memory boards."""
        now = time.monotonic()
        for path, seen in list(self._seen.items()):
            props = self._props.get(path, {})
            if now - seen > self.cache_ttl and not props.get("Paired") and not props.get("Connected"):
                try:
                    adapter.RemoveDevice(path)
                except self._dbus.DBusException:
                    pass
                self._seen.pop(path, None)
                self._props.pop(path, None)

    # -- BLE beacon ---------------------------------------------------------

    def _register_advertisement(self, adapter_obj):
        if self._adv is None:
            self._adv = _make_advertisement(self._dbus, self._bus, bytes.fromhex(self.node_id), self)
        manager = self._dbus.Interface(adapter_obj, ADV_MANAGER)
        self._adv_pending = True

        def ok():
            self._adv_pending = False
            self._set(advertising=True, beacon_error=None)
            log.info("BLE beacon registered")

        def failed(exc):
            self._adv_pending = False
            self._set(advertising=False, beacon_error=exc.get_dbus_message() or str(exc))

        manager.RegisterAdvertisement(ADV_PATH, {}, reply_handler=ok, error_handler=failed)

    # -- D-Bus signals ------------------------------------------------------

    def _owns(self, path):
        return self._adapter is not None and str(path).startswith(self._adapter + "/")

    def _on_added(self, path, ifaces):
        if ADAPTER in ifaces and self._adapter is None:
            self._tick()
        if DEVICE in ifaces and self._owns(path):
            self._props[str(path)] = dict(ifaces[DEVICE])
            if "RSSI" in ifaces[DEVICE] or ifaces[DEVICE].get("Connected"):
                self._report(str(path))

    def _on_removed(self, path, ifaces):
        path = str(path)
        if ADAPTER in ifaces and path == self._adapter:
            self._adapter = None
            self._set(state="no_adapter", adapter=None, advertising=False, error="adapter removed")
        if DEVICE in ifaces:
            self._props.pop(path, None)
            self._seen.pop(path, None)

    def _on_changed(self, interface, changed, invalidated, path=None):
        if not self._owns(path):
            return
        path = str(path)
        props = self._props.setdefault(path, {})
        props.update(changed)
        for key in invalidated:
            props.pop(key, None)
        if any(k in changed for k in ADVERT_KEYS):
            self._report(path)

    def _report(self, path):
        props = self._props[path]
        if "Address" not in props:
            props["Address"] = path.rsplit("/dev_", 1)[-1].replace("_", ":")
        self._seen[path] = time.monotonic()
        self.registry.observe(str(props["Address"]), device_fields(props))


def _make_advertisement(dbus, bus, payload, backend):
    import dbus.service

    class Advertisement(dbus.service.Object):
        @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            if interface != ADV:
                raise dbus.exceptions.DBusException("org.freedesktop.DBus.Error.InvalidArgs")
            return {
                "Type": "broadcast",
                "ServiceData": dbus.Dictionary(
                    {BTEDGE_UUID: dbus.Array(payload, signature="y")}, signature="sv"),
            }

        @dbus.service.method(ADV, in_signature="", out_signature="")
        def Release(self):
            backend._set(advertising=False)

    return Advertisement(bus, ADV_PATH)


class FakeBackend:
    """Simulated neighbourhood: devices drift in signal strength, come and go."""

    NAMES = ["iPhone", "Galaxy S23", "Mi Band 7", "JBL Flip 5", "AirPods Pro", None, None,
             "Tile", "Fitbit Charge", "LE-Bose QC", "ESP32-sensor", None]
    MAKERS = [0x004C, 0x0075, 0x038F, 0x0057, 0x004C, 0x0006, None, 0x00C7, 0x0224, 0x009E, 0x02E5, None]

    def __init__(self, registry, seed=None):
        self.registry = registry
        self.status = {"backend": "fake", "state": "scanning", "adapter": "simulated",
                       "advertising": False, "error": None}
        self._rand = random.Random(seed)
        self._devices = []
        for i, name in enumerate(self.NAMES):
            address = ":".join("%02X" % self._rand.randrange(256) for _ in range(6))
            self._devices.append({"address": address, "name": name, "maker": self.MAKERS[i],
                                  "rssi": self._rand.randint(-95, -40), "present": True})

    def start(self):
        threading.Thread(target=self._run, name="fake-bt", daemon=True).start()

    def _run(self):
        while True:
            for dev in self._devices:
                if self._rand.random() < 0.03:
                    dev["present"] = not dev["present"]
                if not dev["present"]:
                    continue
                dev["rssi"] = max(-100, min(-30, dev["rssi"] + self._rand.randint(-4, 4)))
                self.registry.observe(dev["address"], {
                    "name": dev["name"], "rssi": dev["rssi"],
                    "address_type": "random" if dev["name"] is None else "public",
                    "manufacturer": [dev["maker"]] if dev["maker"] is not None else None,
                    "connected": False, "paired": False,
                })
            time.sleep(2)
