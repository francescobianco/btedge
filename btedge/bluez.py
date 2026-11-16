"""Bluetooth backends.

BluezBackend talks to BlueZ over D-Bus: on every radio of the host (or the ones
named in scanner.adapter) it keeps discovery running, reports every advertisement
to the registry and broadcasts a small BLE beacon carrying the node id, so btedge
nodes can also detect each other over the air. It connects the devices the mesh
flagged autoconnect as soon as one of its radios hears them.

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
ADV_PATH = "/org/btedge/advertisement"  # one per radio: ADV_PATH_hci0 ...

# Service data UUID carried by btedge beacons; the payload is the 8-byte node id.
BTEDGE_UUID = "62746564-6765-4e4f-8000-6d6573680001"


class Unsupported(RuntimeError):
    """A backend cannot be put in the Bluetooth mode it was asked for."""


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


STATES = {"on": "scanning", "released": "paused", "off": "off"}  # Bluetooth mode -> status state


class Radio:
    """One Bluetooth adapter of the host (hci0, a second dongle...) and what btedge runs on it."""

    def __init__(self, path, address, adv_supported):
        self.path = path
        self.name = path.rsplit("/", 1)[-1]
        self.address = address
        self.adv_supported = adv_supported
        self.adv = None
        self.advertising = False
        self.adv_pending = False
        self.discovery_started = 0.0
        self.state = "starting"
        self.error = None
        self.beacon_error = None

    def info(self):
        return {"name": self.name, "address": self.address, "state": self.state,
                "advertising": self.advertising, "error": self.error}


class BluezBackend:
    def __init__(self, registry, node_id, adapter=None, advertise=True, cache_ttl=300, rescan_interval=20,
                 keep_connected=None):
        self.registry = registry
        self.node_id = node_id
        # the radios to use: every adapter BlueZ has, unless the config names some
        self.adapter_names = [adapter] if isinstance(adapter, str) else list(adapter or [])
        self.advertise = advertise
        self.cache_ttl = cache_ttl
        self.rescan_interval = rescan_interval
        self.status = {"backend": "bluez", "state": "starting", "adapter": None, "radios": [],
                       "advertising": False, "beacon_error": None, "error": None}
        self._radios = {}       # adapter object path -> Radio
        self._stale = True      # adapters came or went: look at BlueZ's objects again
        self._props = {}        # device object path -> Device1 properties
        self._seen = {}
        self.keep_connected = [a.upper() for a in keep_connected or []]
        self._connecting = set()
        self.mode = "on"        # see actions.py: "on", "released" or "off"
        self._applied = None    # the released/off mode already carried out on the adapters
        self._powered_off = set()  # adapters we switched off ourselves
        self._glib = None
        # dbus-glib must be told about threads before any second thread exists: the panel,
        # mesh and action threads otherwise race the GLib loop inside libdbus and corrupt
        # the heap. The backend is built in the main thread before anything starts.
        try:
            import dbus.mainloop.glib
            dbus.mainloop.glib.threads_init()
        except ImportError:
            pass  # reported when the backend starts

    def start(self):
        threading.Thread(target=self._run, name="bluez", daemon=True).start()

    # -- called from other threads ------------------------------------------

    def gatt(self, address):
        """GATT session with a device, through the radio that is connected to it or hears it best."""
        from .gatt import Gatt
        found = self._paths(address)
        radio = found[0][0].split("/")[3] if found else next((r.name for r in list(self._radios.values())), "hci0")
        return Gatt(address, adapter=radio)

    def set_mode(self, mode):
        """Scan ("on"), leave the adapters to the host ("released") or power them off."""
        self.mode = mode
        self._later(self._tick)

    def connect(self, address):
        self._later(lambda: self._connect(address, asked=True))

    def disconnect(self, address):
        self._later(lambda: self._disconnect(address))

    def _later(self, work):
        if self._glib:  # D-Bus only from the GLib thread
            self._glib.idle_add(lambda: work() and False)

    def _set(self, **kwargs):
        for key in ("error", "beacon_error"):
            if kwargs.get(key) and kwargs[key] != self.status.get(key):
                log.warning("bluetooth %s: %s", key, kwargs[key])
        self.status.update(kwargs)
        if self.status["state"] in ("error", "no_adapter") and not self._radios:
            # no radio to report on: tell the mesh why, so any panel shows what is wrong here
            self.registry.set_radios([{"name": "bluetooth", "address": "", "state": self.status["state"],
                                       "error": self.status["error"]}])

    def _run(self):
        try:
            import dbus
            import dbus.mainloop.glib
            from gi.repository import GLib
        except ImportError as exc:
            self._set(state="error", error="missing python3-dbus / python3-gi: %s" % exc)
            return
        self._dbus = dbus
        self._glib = GLib
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
        GLib.idle_add(lambda: self._tick() and False)  # run once now, then on the timer
        GLib.timeout_add_seconds(self.rescan_interval, self._tick)
        GLib.MainLoop().run()

    # -- radio supervision --------------------------------------------------

    def _tick(self):
        dbus = self._dbus
        try:
            if self.mode == "on":
                self._applied = None
                if self._stale:
                    self._sync(self._om.GetManagedObjects())
                for radio in self._radios.values():
                    try:
                        self._ensure(radio)
                    except dbus.DBusException as exc:  # one broken dongle must not stop the others
                        radio.state, radio.error = "error", exc.get_dbus_message() or str(exc)
                        self._stale = True
                self._reconnect()
                self._publish()
            elif self._applied != self.mode:
                self._release(self.mode)
        except dbus.DBusException as exc:
            self._stale = True
            self._set(state="error", error=exc.get_dbus_message() or str(exc))
        return True

    def _adapters(self, objects):
        return sorted(p for p, i in objects.items() if ADAPTER in i
                      and (not self.adapter_names or p.rsplit("/", 1)[-1] in self.adapter_names))

    def _sync(self, objects):
        """Match our radios, and the devices we know, with what BlueZ has now."""
        paths = self._adapters(objects)
        for path in [p for p in self._radios if p not in paths]:
            log.info("adapter %s gone", path)
            del self._radios[path]
        for path in paths:
            if path not in self._radios:
                self._radios[path] = Radio(path, str(objects[path][ADAPTER].get("Address", "")),
                                           ADV_MANAGER in objects[path])
                log.info("using adapter %s", path)
        for path in [p for p in self._props if not self._owns(p)]:
            self._props.pop(path)
            self._seen.pop(path, None)
        now = time.monotonic()
        for path, ifaces in objects.items():
            if DEVICE in ifaces and self._owns(path) and path not in self._props:
                self._props[str(path)] = dict(ifaces[DEVICE])
                self._seen[str(path)] = now
                if ifaces[DEVICE].get("Connected"):
                    self._report(str(path))
        self._stale = False

    def _publish(self):
        """Sum the radios up for the panel and tell the mesh which radios this node has."""
        radios = list(self._radios.values())
        if not radios:
            self._set(state="no_adapter", adapter=None, radios=[], advertising=False,
                      error="no Bluetooth adapter found")
            return
        else:
            many = len(radios) > 1
            self._set(state="scanning" if any(r.state == "scanning" for r in radios) else "error",
                      adapter=", ".join(r.name for r in radios), radios=[r.info() for r in radios],
                      advertising=any(r.advertising for r in radios),
                      error=next(((r.name + ": " if many else "") + r.error for r in radios if r.error), None),
                      beacon_error=next((r.beacon_error for r in radios if r.beacon_error), None))
        self.registry.set_radios([{"name": r.name, "address": r.address, "state": r.state, "error": r.error}
                                  for r in radios])

    def _release(self, mode):
        """Stop everything btedge does on the radios, then leave them to the host or power them off.

        "released" takes away only our own discovery sessions and beacons: the adapters stay
        powered and connected devices stay connected, since whoever needs the radio may be
        using them. An adapter is powered back on only if we were the ones to switch it off.
        """
        dbus = self._dbus
        objects = self._om.GetManagedObjects()
        for radio in self._radios.values():
            obj = self._bus.get_object(BLUEZ, radio.path)
            try:
                dbus.Interface(obj, ADAPTER).StopDiscovery()
            except dbus.DBusException:
                pass  # no session of ours
            self._unregister_advertisement(radio)
        self._radios.clear()  # radios and devices are enumerated again when we take them back
        self._props.clear()
        self._seen.clear()
        self._stale = True
        paths = self._adapters(objects)
        if not paths and mode == "off":
            self._set(state="no_adapter", adapter=None, radios=[], advertising=False,
                      error="no Bluetooth adapter found")
            return  # tried again at the next tick
        for path in paths:
            props = dbus.Interface(self._bus.get_object(BLUEZ, path), PROPS)
            if mode == "off":
                props.Set(ADAPTER, "Powered", dbus.Boolean(False))
                self._powered_off.add(path)
            elif path in self._powered_off:
                props.Set(ADAPTER, "Powered", dbus.Boolean(True))
                self._powered_off.discard(path)
            log.info("adapter %s %s", path, "powered off" if mode == "off" else "released to the host")
        self._applied = mode
        radios = [{"name": p.rsplit("/", 1)[-1], "address": str(objects[p][ADAPTER].get("Address", "")),
                   "state": STATES[mode]} for p in paths]
        self._set(state=STATES[mode], adapter=", ".join(r["name"] for r in radios) or None, radios=radios,
                  advertising=False, error=None, beacon_error=None)
        self.registry.set_radios(radios)

    def _ensure(self, radio):
        dbus = self._dbus
        obj = self._bus.get_object(BLUEZ, radio.path)
        props = dbus.Interface(obj, PROPS)
        if not props.Get(ADAPTER, "Powered"):
            props.Set(ADAPTER, "Powered", dbus.Boolean(True))
        self._powered_off.discard(radio.path)
        # BlueZ filters duplicate adverts during a discovery session (reporting every
        # advert floods D-Bus and costs a whole core on small boards), so the session is
        # restarted periodically: each device still in range is then reported again.
        adapter = dbus.Interface(obj, ADAPTER)
        if props.Get(ADAPTER, "Discovering"):
            if time.monotonic() - radio.discovery_started >= self.rescan_interval:
                try:
                    adapter.StopDiscovery()
                except dbus.DBusException:
                    pass
                adapter.StartDiscovery()
                radio.discovery_started = time.monotonic()
        else:
            try:
                adapter.SetDiscoveryFilter({"DuplicateData": dbus.Boolean(False)})
            except dbus.DBusException:
                pass
            adapter.StartDiscovery()
            radio.discovery_started = time.monotonic()
            log.info("discovery started on %s", radio.name)
        if self.advertise and radio.adv_supported and not radio.advertising and not radio.adv_pending:
            self._register_advertisement(radio, obj)
        self._prune_cache(radio, adapter)
        radio.state, radio.error = "scanning", None

    def _prune_cache(self, radio, adapter):
        """Drop long-gone unpaired devices so BlueZ's cache stays small on low-memory boards."""
        now = time.monotonic()
        for path, seen in list(self._seen.items()):
            if not path.startswith(radio.path + "/"):
                continue
            props = self._props.get(path, {})
            if now - seen > self.cache_ttl and not props.get("Paired") and not props.get("Connected"):
                try:
                    adapter.RemoveDevice(path)
                except self._dbus.DBusException:
                    pass
                self._seen.pop(path, None)
                self._props.pop(path, None)

    # -- connections --------------------------------------------------------

    def _paths(self, address):
        """[(object path, properties)] of a device on our radios: connected first, then loudest."""
        suffix = "/dev_" + address.upper().replace(":", "_")
        found = [(p, props) for p, props in list(self._props.items()) if p.endswith(suffix)]
        found.sort(key=lambda item: (not item[1].get("Connected"), -int(item[1].get("RSSI", -999))))
        return found

    def _reconnect(self):
        """Connect what must stay connected: scanner.keep_connected and the mesh's autoconnect devices."""
        for address in set(self.keep_connected) | self.registry.autoconnect:
            self._connect(address)

    def _connect(self, address, asked=False):
        """Connect a device through the radio that hears it best, unless it is connected already.

        A device flagged autoconnect in the mesh is left alone while another node holds it.
        """
        found = self._paths(address)
        if self.mode != "on" or address in self._connecting or not found or found[0][1].get("Connected"):
            return  # unknown to BlueZ yet: wait for an advert (or pair it once)
        if not asked and address.upper() not in self.keep_connected and self.registry.connected_elsewhere(address):
            return
        self._connecting.add(address)
        device = self._dbus.Interface(self._bus.get_object(BLUEZ, found[0][0]), DEVICE)

        def done():
            self._connecting.discard(address)
            log.info("connected %s on %s", address, found[0][0].split("/")[3])

        def failed(exc):
            self._connecting.discard(address)
            log.debug("connect %s: %s", address, exc.get_dbus_message() or exc)

        device.Connect(reply_handler=done, error_handler=failed, timeout=30)

    def _disconnect(self, address):
        for path, props in self._paths(address):
            if props.get("Connected"):
                device = self._dbus.Interface(self._bus.get_object(BLUEZ, path), DEVICE)
                device.Disconnect(reply_handler=lambda: None, error_handler=lambda exc: None)

    # -- BLE beacon ---------------------------------------------------------

    def _register_advertisement(self, radio, adapter_obj):
        if radio.adv is None:
            def released():
                radio.advertising = False

            radio.adv = _make_advertisement(self._dbus, self._bus, bytes.fromhex(self.node_id),
                                            ADV_PATH + "_" + radio.name, released)
        manager = self._dbus.Interface(adapter_obj, ADV_MANAGER)
        radio.adv_pending = True

        def ok():
            radio.adv_pending = False
            if self.mode != "on":  # released while the registration was in flight
                self._unregister_advertisement(radio)
                return
            radio.advertising, radio.beacon_error = True, None
            log.info("BLE beacon registered on %s", radio.name)
            self._publish()

        def failed(exc):
            radio.adv_pending = False
            radio.advertising, radio.beacon_error = False, exc.get_dbus_message() or str(exc)
            if self.mode == "on":
                self._publish()

        manager.RegisterAdvertisement(ADV_PATH + "_" + radio.name, {}, reply_handler=ok, error_handler=failed)

    def _unregister_advertisement(self, radio):
        radio.advertising = False
        try:
            manager = self._dbus.Interface(self._bus.get_object(BLUEZ, radio.path), ADV_MANAGER)
            manager.UnregisterAdvertisement(ADV_PATH + "_" + radio.name)
        except self._dbus.DBusException:
            pass  # not registered

    # -- D-Bus signals ------------------------------------------------------

    def _owns(self, path):
        return str(path).rsplit("/", 1)[0] in self._radios

    def _on_added(self, path, ifaces):
        if ADAPTER in ifaces:  # a dongle was plugged in
            self._stale = True
            self._tick()
        if DEVICE in ifaces and self._owns(path):
            self._props[str(path)] = dict(ifaces[DEVICE])
            if "RSSI" in ifaces[DEVICE] or ifaces[DEVICE].get("Connected"):
                self._report(str(path))

    def _on_removed(self, path, ifaces):
        path = str(path)
        if ADAPTER in ifaces and path in self._radios:
            log.info("adapter %s removed", path)
            del self._radios[path]
            self._stale = True
            if self.mode == "on":
                self._publish()
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
        address = str(props["Address"])
        self.registry.observe(address, device_fields(props), radio=path.split("/")[3])
        if address in self.registry.autoconnect and not props.get("Connected"):
            self._connect(address)  # heard: connect now rather than at the next tick


def _make_advertisement(dbus, bus, payload, path, on_release):
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
            on_release()

    return Advertisement(bus, path)


class FakeBackend:
    """Simulated neighbourhood shared by every fake node of a test mesh.

    Rooms 0..rooms-1 sit on a ring. Devices are derived from a fixed seed, so all
    fake nodes agree on them; roaming devices walk around the ring on a clock-based
    schedule, the others stay home. Each node reports the devices near its room,
    with RSSI falling off with distance, so devices visibly move between nodes.
    A node may have several simulated radios, each hearing a slightly different signal.
    """

    NAMES = ["iPhone", "Galaxy S23", "Mi Band 7", "JBL Flip 5", "AirPods Pro", None, None,
             "Tile", "Fitbit Charge", "LE-Bose QC", "ESP32-sensor", None, "MSF0"]
    MAKERS = [0x004C, 0x0075, 0x038F, 0x0057, 0x004C, 0x0006, None, 0x00C7, 0x0224, 0x009E, 0x02E5, None, None]
    UUIDS = {"MSF0": ["0000cc00-0000-1000-8000-00805f9b34fb"]}  # an ID115 Plus: actions and alarms
    ROAMING = {0, 1, 2, 4, 7, 9}

    def __init__(self, registry, room=0, rooms=1, seed="btedge-fake", radios=1):
        self.registry = registry
        self.room = room
        self.rooms = max(1, rooms)
        self.mode = "on"
        self.radios = ["sim%d" % i for i in range(max(1, radios))]
        self.status = {"backend": "fake", "state": "scanning", "adapter": "simulated room %d/%d" % (room, self.rooms),
                       "advertising": False, "error": None}
        self._publish()
        rand = random.Random(seed)
        self._noise = random.Random()
        self._connected = set()  # asked with connect(); autoconnect devices connect by themselves
        self._devices = []
        for i, name in enumerate(self.NAMES):
            self._devices.append({
                "address": ":".join("%02X" % rand.randrange(256) for _ in range(6)),
                "name": name, "maker": self.MAKERS[i], "home": i % self.rooms,
                "period": rand.uniform(240, 900), "phase": rand.random(),
                "roaming": i in self.ROAMING,
            })

    def _publish(self):
        radios = [{"name": name, "address": "", "state": STATES[self.mode]} for name in self.radios]
        self.status["radios"] = radios
        self.registry.set_radios(radios)

    def gatt(self, address):
        from .gatt import FakeGatt
        return FakeGatt(address)

    def connect(self, address):
        self._connected.add(address)

    def disconnect(self, address):
        self._connected.discard(address)

    def start(self):
        threading.Thread(target=self._run, name="fake-bt", daemon=True).start()

    def set_mode(self, mode):
        self.mode = mode
        self.status["state"] = STATES[mode]
        self._publish()

    def _distance(self, dev, now):
        if dev["roaming"] and self.rooms > 1:
            # Dwell in each room for most of the cycle, then walk to the next one.
            t = ((now / dev["period"]) + dev["phase"]) % 1 * self.rooms
            step, frac = int(t), t % 1
            position = step + max(0.0, (frac - 0.7) / 0.3)
        else:
            position = dev["home"]
        delta = abs(position - self.room) % self.rooms
        return min(delta, self.rooms - delta)

    def _run(self):
        while True:
            now = time.time()
            for dev in self._devices if self.mode == "on" else ():
                distance = self._distance(dev, now)
                if distance > 1.2:
                    self._connected.discard(dev["address"])
                    continue
                wanted = dev["address"] in self._connected or dev["address"] in self.registry.autoconnect
                for i, radio in enumerate(self.radios):
                    self.registry.observe(dev["address"], {
                        "name": dev["name"], "rssi": int(-45 - 30 * distance - 6 * i + self._noise.randint(-3, 3)),
                        "address_type": "random" if dev["name"] is None else "public",
                        "manufacturer": [dev["maker"]] if dev["maker"] is not None else None,
                        "connected": wanted and i == 0, "paired": False, "uuids": self.UUIDS.get(dev["name"]),
                    }, radio=radio)
            time.sleep(2)
