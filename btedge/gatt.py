"""Blocking GATT access through BlueZ, used by device adapters.

Adapters run in worker threads, so this uses its own private system-bus connection
with plain blocking D-Bus calls instead of the backend's GLib main loop.
"""
import time

BLUEZ = "org.bluez"
DEVICE = "org.bluez.Device1"
CHAR = "org.bluez.GattCharacteristic1"
PROPS = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
BASE_UUID = "-0000-1000-8000-00805f9b34fb"


class GattError(RuntimeError):
    pass


def uuid128(uuid):
    uuid = uuid.lower()
    return "0000%s%s" % (uuid, BASE_UUID) if len(uuid) == 4 else uuid


class Gatt:
    """GATT session with one device: connects on demand and finds characteristics by UUID."""

    def __init__(self, address, adapter="hci0", retries=3):
        import dbus
        import dbus.mainloop
        self._dbus = dbus
        self.address = address.upper()
        self.retries = retries
        # Not attached to the backend's GLib loop: only blocking calls from this thread.
        self.bus = dbus.SystemBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        # libdbus kills the whole process with _exit(1) when a bus connection it owns is
        # closed while "exit on disconnect" is set (the default): turn it off first.
        self.bus.set_exit_on_disconnect(False)
        self.path = "/org/bluez/%s/dev_%s" % (adapter, self.address.replace(":", "_"))

    def close(self):
        self.bus.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _prop(self, name):
        try:
            obj = self.bus.get_object(BLUEZ, self.path)
            return self._dbus.Interface(obj, PROPS).Get(DEVICE, name)
        except self._dbus.DBusException as exc:
            raise GattError("device not known to BlueZ: %s" % exc.get_dbus_message()) from None

    def connect(self):
        if self._prop("ServicesResolved"):
            return
        device = self._dbus.Interface(self.bus.get_object(BLUEZ, self.path), DEVICE)
        last = None
        for _ in range(self.retries):
            try:
                device.Connect(timeout=30)
            except self._dbus.DBusException as exc:
                last = exc.get_dbus_message() or str(exc)
            for _ in range(20):
                if self._prop("ServicesResolved"):
                    return
                time.sleep(0.5)
        raise GattError("cannot connect: %s" % (last or "services not resolved"))

    def characteristic(self, uuid):
        uuid = uuid128(uuid)
        om = self._dbus.Interface(self.bus.get_object(BLUEZ, "/"), OBJECT_MANAGER)
        for path, ifaces in om.GetManagedObjects().items():
            if path.startswith(self.path + "/") and CHAR in ifaces and str(ifaces[CHAR]["UUID"]) == uuid:
                return self._dbus.Interface(self.bus.get_object(BLUEZ, path), CHAR)
        raise GattError("characteristic %s not found" % uuid)

    def write(self, uuid, data, response=True):
        self.connect()
        char = self.characteristic(uuid)
        opts = {"type": "request" if response else "command"}
        try:
            char.WriteValue(self._dbus.Array(bytes(data), signature="y"), opts, timeout=15)
        except self._dbus.DBusException as exc:
            raise GattError("write %s failed: %s" % (uuid, exc.get_dbus_message())) from None

    def read(self, uuid):
        self.connect()
        try:
            return bytes(self.characteristic(uuid).ReadValue({}, timeout=15))
        except self._dbus.DBusException as exc:
            raise GattError("read %s failed: %s" % (uuid, exc.get_dbus_message())) from None


class FakeGatt:
    """Stand-in for the fake backend: records writes, reads return zeros."""

    writes = []

    def __init__(self, address, **_):
        self.address = address

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def write(self, uuid, data, response=True):
        FakeGatt.writes.append((self.address, uuid128(uuid), bytes(data)))

    def read(self, uuid):
        return bytes(8)
