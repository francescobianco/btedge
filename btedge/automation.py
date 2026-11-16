"""Runs the mesh's snippets on this node.

Snippets (see script.py for the language) are written in a panel and replicated to
every node with the shared settings (registry.py); each node with Bluetooth runs all
the enabled ones against what it sees itself. A snippet registers handlers on `host`:

    host.onReady(() => ...)            the snippet was (re)loaded on this node
    host.onDevice(device => ...)       a device came in range of this node
    host.onDeviceLost(device => ...)   it has not been heard for a while
    host.onConnect(device => ...)      it connected to this node
    host.onDisconnect(device => ...)
    host.every(60, () => ...)          every N seconds (at least 5)

    host.name  host.id  host.role  host.radios
    device.address  .name  .label  .rssi  .connected  .paired  .radio  .manufacturer
          .uuids  .class  .model  .autoconnect
    device.connect()  device.disconnect()  device.action("vibrate")
    log("text", value, ...)

Device methods only queue the work: it runs afterwards, in order, on a worker thread,
so a handler never blocks. Errors and the last log lines of each snippet are published
in the node's state and shown in the panel next to the snippet.
"""
import collections
import logging
import queue
import threading
import time

from . import script
from .script import Native, Object, ScriptError

log = logging.getLogger(__name__)

EVENTS = {"onReady": "ready", "onDevice": "device", "onDeviceLost": "lost",
          "onConnect": "connect", "onDisconnect": "disconnect"}
MIN_EVERY = 5
LOG_LINES = 5
QUEUE = 32


def check(code):
    """Syntax error of a snippet as text, or None: the panel refuses to save broken ones."""
    try:
        script.parse(code)
    except ScriptError as exc:
        return str(exc)
    return None


class Instance:
    """One loaded snippet: its handlers, what it already knows about, its status."""

    def __init__(self, sid, name, code, engine):
        self.sid, self.name, self.source = sid, name, code
        self.engine = engine
        self.handlers = {event: [] for event in EVENTS.values()}
        self.timers = []                    # [interval, function, next run]
        self.seen, self.connected, self.last = set(), set(), {}
        self.error = None
        self.log = collections.deque(maxlen=LOG_LINES)
        self.jobs = None                    # queue of device operations, with its worker
        self.dirty = True
        node = engine.registry.node
        self.host = Object("host", dict(
            {name: Native(lambda args, event=event: self.on(event, args)) for name, event in EVENTS.items()},
            name=node["name"], id=node["id"], role=node.get("role", "edge"), radios=engine.radios(),
            every=Native(self.every)), assign=self.assign)
        self.interpreter = script.Interpreter({"host": self.host, "log": Native(self.say)})
        try:
            self.interpreter.run(script.parse(code))
        except ScriptError as exc:
            self.fail(exc)
        else:
            self.fire("ready")

    # -- what the snippet calls ---------------------------------------------

    def on(self, event, args):
        if not args or not isinstance(args[0], script.Function):
            raise ScriptError("serve una funzione")
        self.handlers[event].append(args[0])

    def assign(self, name, value, line):
        if name not in EVENTS:
            raise ScriptError("host non ha l'evento %r" % name, line)
        self.on(EVENTS[name], [value])

    def every(self, args):
        if len(args) != 2 or isinstance(args[0], bool) or not isinstance(args[0], (int, float)) \
                or not isinstance(args[1], script.Function):
            raise ScriptError("host.every(secondi, funzione)")
        interval = max(MIN_EVERY, args[0])
        self.timers.append([interval, args[1], self.engine.clock() + interval])

    def say(self, args):
        line = " ".join(script.text(a) for a in args)[:200]
        self.log.append(time.strftime("%H:%M:%S ") + line)
        self.dirty = True
        log.info("snippet %s: %s", self.name, line)

    def fail(self, exc):
        self.error = str(exc)[:300]
        self.dirty = True
        log.warning("snippet %s: %s", self.name, exc)

    def device(self, dev):
        address = dev["address"]
        profile = self.engine.profiler.profile(dev) if self.engine.profiler else None

        def op(name, *args):
            self.submit(name, address, *args)

        def action(args):
            if not args or not isinstance(args[0], str):
                raise ScriptError('device.action("nome")')
            op("action", args[0])

        return Object("device", {
            "address": address, "name": dev.get("name"), "label": self.engine.registry.label(address),
            "rssi": dev.get("rssi"), "connected": bool(dev.get("connected")), "paired": bool(dev.get("paired")),
            "radio": dev.get("radio"), "manufacturer": list(dev.get("manufacturer") or []),
            "uuids": list(dev.get("uuids") or []),
            "class": profile["class"] if profile else None, "model": profile["id"] if profile else None,
            "autoconnect": address in self.engine.registry.autoconnect,
            "connect": Native(lambda args: op("connect")), "disconnect": Native(lambda args: op("disconnect")),
            "action": Native(action),
        })

    # -- events -------------------------------------------------------------

    def fire(self, event, dev=None):
        for handler in self.handlers[event]:
            self.call(handler, [self.device(dev)] if dev is not None else [])

    def call(self, function, args=()):
        try:
            self.interpreter.call(function, args)
        except ScriptError as exc:
            self.fail(exc)

    def feed(self, devices, now):
        """Turn the node's current devices into events, then run the timers that are due."""
        for address, dev in devices.items():
            if address not in self.seen:
                self.seen.add(address)
                self.fire("device", dev)
            connected = bool(dev.get("connected"))
            if connected and address not in self.connected:
                self.connected.add(address)
                self.fire("connect", dev)
            elif not connected and address in self.connected:
                self.connected.discard(address)
                self.fire("disconnect", dev)
        for address in self.seen - devices.keys():
            self.seen.discard(address)
            gone = dict(self.last.get(address) or {"address": address}, connected=False)
            if address in self.connected:
                self.connected.discard(address)
                self.fire("disconnect", gone)
            self.fire("lost", gone)
        self.last = devices
        for timer in self.timers:
            if now >= timer[2]:
                timer[2] = now + timer[0]
                self.call(timer[1])

    # -- device operations --------------------------------------------------

    def submit(self, name, address, *args):
        if self.jobs is None:
            self.jobs = queue.Queue(QUEUE)
            threading.Thread(target=self.work, args=(self.jobs,), name="snippet-" + self.sid, daemon=True).start()
        try:
            self.jobs.put_nowait((name, address, args))
        except queue.Full:
            raise ScriptError("troppe operazioni in coda") from None

    def work(self, jobs):
        while True:
            job = jobs.get()
            if job is None:
                return
            name, address, args = job
            try:
                self.engine.operations[name](address, *args)
            except Exception as exc:  # real hardware behind: report, keep the worker alive
                self.fail(ScriptError("%s %s: %s" % (name, address, exc)))

    def close(self):
        if self.jobs is not None:
            try:
                self.jobs.put_nowait(None)
            except queue.Full:
                pass

    def status(self):
        return {"error": self.error, "log": list(self.log)}


class Automation:
    """Keeps the loaded snippets in step with the shared settings and feeds them events.

    operations: {"connect": f(address), "disconnect": f(address), "action": f(address, name)}.
    """

    def __init__(self, registry, operations, profiler=None, interval=1.0, clock=time.monotonic):
        self.registry = registry
        self.operations = operations
        self.profiler = profiler
        self.interval = interval
        self.clock = clock
        self.instances = {}
        self._version = None
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, name="automation", daemon=True).start()

    def stop(self):
        self._stop.set()

    def radios(self):
        return [r["name"] for r in self.registry.radios]

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.step()
            except Exception:  # a bug here must not end automation for good
                log.exception("automation")

    def step(self):
        version = self.registry.shared_version
        if version != self._version:
            self._version = version
            self.reload(self.registry.shared("snippet:"))
        devices = {a: d for a, d in self.registry.local_devices().items() if not d.get("btedge_node")}
        now = self.clock()
        for instance in self.instances.values():
            instance.feed(devices, now)
        if any(i.dirty for i in self.instances.values()):
            self.publish()

    def reload(self, snippets):
        wanted = {sid: s for sid, s in snippets.items() if s.get("enabled")}
        for sid in [s for s in self.instances if s not in wanted or self.instances[s].source != wanted[s]["code"]]:
            self.instances.pop(sid).close()
        for sid, snippet in wanted.items():
            if sid not in self.instances:
                self.instances[sid] = Instance(sid, snippet["name"], snippet["code"], self)
                log.info("snippet %s loaded", snippet["name"])
            self.instances[sid].name = snippet["name"]
        self.publish()

    def publish(self):
        for instance in self.instances.values():
            instance.dirty = False
        self.registry.set_automation({sid: i.status() for sid, i in self.instances.items()})
