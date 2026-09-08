"""dbus_mini tests: signature grammar, (un)marshalling byte facts, message
framing, DisplayConfig/QEMU fixtures, and the client against an in-process
mock bus (SASL EXTERNAL, Hello, names, echo, errors, signals, timeouts, unix
fds, uid-switching fork hand-off). No real bus needed.

With DBUS_SESSION_BUS_ADDRESS set (e.g. `dbus-run-session -- python3
tests/test_dbus_mini.py`) the RealBus class additionally exercises a real
dbus-daemon: ListNames, NameHasOwner, Peer.Ping, properties, and
NameOwnerChanged from a second connection."""

import contextlib
import io
import json
import os
import select
import shutil
import socket
import subprocess
import struct
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fwcommon import dbus_mini
from fwcommon.dbus_mini import (Bus, DBusError, Message, Variant,
                                marshal, split_signature, unmarshal)

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ERR = dbus_mini.ERR


# ---------------------------------------------------------------- fixtures

GET_CURRENT_STATE_SIG = "ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv}"
APPLY_MONITORS_CONFIG_SIG = "uua(iiduba(ssa{sv}))a{sv}"
EDP = ("eDP-1", "BOE", "0x0a1b", "0x00000000")
DP = ("DP-1", "DEL", "DELL U2723QE", "7PJ2XM3")
SCALES = [1.0, 1.25, 1.5, 1.75, 2.0]


def get_current_state_fixture(wrap=True):
    """A 2-monitor GNOME 46 GetCurrentState reply, captured off a real session
    (built-in panel + rotated 4K Dell).
    wrap=True yields Variant-wrapped a{sv} values (the write-side shape and
    what wrap_variants=True reads); wrap=False the plain-value read shape."""
    def V(sig, value):
        return Variant(sig, value) if wrap else value

    modes_edp = [
        ("1920x1080@60.020", 1920, 1080, 60.02, 1.0, SCALES,
         {"is-current": V("b", True), "is-preferred": V("b", True)}),
        ("1920x1080@48.000", 1920, 1080, 48.0, 1.0, SCALES, {}),
        ("1680x1050@59.954", 1680, 1050, 59.954, 1.0, SCALES,
         # synthetic keys so the fixture pins every basic shape mutter
         # could put in a mode dict: b / d / ad / s
         {"x-test-d": V("d", 0.5), "x-test-ad": V("ad", [1.0, 1.5]),
          "x-test-s": V("s", "aux")}),
        ("1280x720@59.860", 1280, 720, 59.86, 1.0, [1.0], {}),
    ]
    modes_dp = [
        ("3840x2160@59.997", 3840, 2160, 59.997, 2.0,
         SCALES + [2.25, 2.5, 2.75, 3.0], {"is-preferred": V("b", True)}),
        ("2560x1440@59.951", 2560, 1440, 59.951, 1.0, SCALES,
         {"is-current": V("b", True)}),
        ("1920x1080i@60.000", 1920, 1080, 60.0, 1.0, SCALES,
         {"is-interlaced": V("b", True)}),
        ("2560x1440@144.000+vrr", 2560, 1440, 144.0, 1.0, SCALES,
         {"refresh-rate-mode": V("s", "variable")}),
    ]
    monitors = [
        (EDP, modes_edp, {"width-mm": V("i", 344), "height-mm": V("i", 194),
                          "is-builtin": V("b", True),
                          "display-name": V("s", "Built-in display"),
                          "privacy-screen-state": V("(bb)", (False, False))}),
        (DP, modes_dp, {"width-mm": V("i", 597), "height-mm": V("i", 336),
                        "is-builtin": V("b", False),
                        "display-name": V("s", 'Dell Inc. 27"'),
                        "is-underscanning": V("b", False),
                        "min-refresh-rate": V("i", 48)}),
    ]
    logical = [
        (0, 0, 1.0, 0, True, [EDP], {}),
        (1920, 0, 1.0, 1, False, [DP], {}),
    ]
    props = {"layout-mode": V("u", 1),
             "supports-changing-layout-mode": V("b", True),
             "legacy-ui-scaling-factor": V("i", 1)}
    return (42, monitors, logical, props)


def apply_monitors_config_fixture(wrap=True):
    def V(sig, value):
        return Variant(sig, value) if wrap else value
    return (42, 1,
            [(0, 0, 1.0, 0, True,
              [("eDP-1", "1920x1080@60.020", {"underscanning": V("b", False)})]),
             (1920, 0, 2.0, 1, False,
              [("DP-1", "3840x2160@59.997", {})])],
            {"layout-mode": V("u", 1)})


# The canonical Hello (D-Bus tutorial / libdbus), serial 1, 128 bytes:
# fixed header, then PATH, DESTINATION, INTERFACE, MEMBER fields (0x6e bytes).
HELLO_BYTES = bytes.fromhex(
    "6c01000100000000010000006e000000"
    "01016f00150000002f6f72672f667265656465736b746f702f44427573000000"
    "06017300140000006f72672e667265656465736b746f702e4442757300000000"
    "02017300140000006f72672e667265656465736b746f702e4442757300000000"
    "030173000500000048656c6c6f000000")


# ---------------------------------------------------------------- mock bus

INTROSPECT_XML = ('<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object '
                  'Introspection 1.0//EN" "http://www.freedesktop.org/standards/'
                  'dbus/1.0/introspect.dtd">\n<node><interface name="test.Echo">'
                  '<method name="Echo"/></interface></node>\n')


class _Conn:
    """One client of the MockBus: SASL server side + message loop."""

    def __init__(self, bus, sock):
        self.bus = bus
        self.sock = sock
        self.unique = None
        self.buf = bytearray()
        self.fds = []
        self.serial = 0
        self.lock = threading.Lock()
        self.matches = []
        self.pending_pings = {}
        self.fds_ok = False
        self.auth_lines = []
        self.closed = False

    # -- I/O

    def _readline(self):
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.buf += chunk
        line, _, rest = bytes(self.buf).partition(b"\r\n")
        self.buf = bytearray(rest)
        return line.decode()

    def send_raw(self, data, fds=()):
        with self.lock:
            try:
                if fds:
                    socket.send_fds(self.sock, [data], list(fds))
                else:
                    self.sock.sendall(data)
            except OSError:
                pass

    def send(self, msg):
        """Bus-originated message: sender org.freedesktop.DBus, own serial."""
        msg.sender = "org.freedesktop.DBus"
        with self.lock:
            self.serial += 1
            s = self.serial
        self.send_raw(msg.to_bytes(s), msg.fds)

    def forward(self, msg):
        """Message from another client: keep its serial."""
        self.send_raw(msg.to_bytes(), msg.fds)

    def signal(self, member, sig, args):
        self.send(Message.signal(dbus_mini.DBUS_PATH, dbus_mini.DBUS_IFACE,
                                 member, sig, args, destination=self.unique))

    # -- auth

    def _auth(self):
        first = self.sock.recv(1)
        if first != b"\0":
            return False
        while True:
            line = self._readline()
            if line is None:
                return False
            self.auth_lines.append(line)
            if line.startswith("AUTH EXTERNAL"):
                if self.bus.reject_auth:
                    self.sock.sendall(b"REJECTED EXTERNAL\r\n")
                else:
                    self.sock.sendall(b"OK deadbeef0000000000000000cafe0001\r\n")
            elif line == "NEGOTIATE_UNIX_FD":
                if self.bus.agree_fds:
                    self.fds_ok = True
                    self.sock.sendall(b"AGREE_UNIX_FD\r\n")
                else:
                    self.sock.sendall(b"ERROR Unix fd passing not supported\r\n")
            elif line == "BEGIN":
                return True
            else:
                self.sock.sendall(b"ERROR\r\n")

    # -- messages

    def serve(self):
        try:
            if self._auth():
                self._loop()
        except OSError:
            pass
        finally:
            self.bus.drop(self)
            self.sock.close()

    def _loop(self):
        while True:
            n = Message.frame_length(self.buf)
            if n is not None and len(self.buf) >= n:
                frame = bytes(self.buf[:n])
                del self.buf[:n]
                m = Message.from_bytes(frame)
                if m.unix_fds:
                    m.fds = self.fds[:m.unix_fds]
                    del self.fds[:m.unix_fds]
                self.dispatch(m)
                for fd in m.fds:  # sendmsg dup'd whatever was forwarded/echoed
                    os.close(fd)
                continue
            if self.fds_ok:
                data, fds, _, _ = socket.recv_fds(self.sock, 65536, 16)
                self.fds.extend(fds)
            else:
                data = self.sock.recv(65536)
            if not data:
                return
            self.buf += data

    def dispatch(self, m):
        m.sender = self.unique
        if self.unique is None:
            if m.member != "Hello":
                self.send(Message.error(m, ERR + "AccessDenied", "Hello first"))
                return
            self.unique = self.bus.hello(self)
            m.sender = self.unique
            self.send(Message.method_return(m, "s", (self.unique,)))
            self.signal("NameAcquired", "s", (self.unique,))
            return
        if m.type in (dbus_mini.METHOD_RETURN, dbus_mini.ERROR):
            if m.destination == "org.freedesktop.DBus":
                orig = self.pending_pings.pop(m.reply_serial, None)
                if orig is not None:
                    ok = m.type == dbus_mini.METHOD_RETURN
                    self.send(Message.method_return(orig, "b", (ok,)))
                return
            target = self.bus.lookup(m.destination)
            if target is not None:
                target.forward(m)
            return
        if m.type == dbus_mini.SIGNAL:
            for c in self.bus.connections():
                if c is not self and any("type='signal'" in r for r in c.matches):
                    c.forward(m)
            return
        # METHOD_CALL
        if m.interface == "org.freedesktop.DBus.Peer" and m.member == "Ping":
            self.send(Message.method_return(m))
            return
        if m.destination in ("org.freedesktop.DBus", None):
            self._bus_method(m)
        elif m.destination in ("test.Echo", "org.gnome.Mutter.DisplayConfig"):
            self._test_method(m)
        else:
            target = self.bus.lookup(m.destination)
            if target is None:
                self.send(Message.error(m, ERR + "ServiceUnknown", f"The name {m.destination} "
                                        "was not provided by any .service files"))
            else:
                target.forward(m)

    def _bus_method(self, m):
        a = m.args()
        mem = m.member
        if mem == "ListNames":
            self.send(Message.method_return(m, "as", (self.bus.list_names(),)))
        elif mem == "NameHasOwner":
            owned = a[0] == "org.freedesktop.DBus" or self.bus.lookup(a[0]) is not None
            self.send(Message.method_return(m, "b", (owned,)))
        elif mem == "GetNameOwner":
            c = self.bus.lookup(a[0])
            if c is None:
                self.send(Message.error(m, ERR + "NameHasNoOwner",
                                        f"Could not get owner of name '{a[0]}': no such name"))
            else:
                self.send(Message.method_return(m, "s", (c.unique,)))
        elif mem == "RequestName":
            code = self.bus.request_name(self, a[0])
            self.send(Message.method_return(m, "u", (code,)))
        elif mem == "ReleaseName":
            code = self.bus.release_name(self, a[0])
            self.send(Message.method_return(m, "u", (code,)))
        elif mem == "AddMatch":
            self.matches.append(a[0])
            self.send(Message.method_return(m))
        elif mem == "RemoveMatch":
            self.send(Message.method_return(m))
        elif mem == "GetId":
            self.send(Message.method_return(m, "s", ("deadbeef0000000000000000cafe0001",)))
        else:
            self.send(Message.error(m, ERR + "UnknownMethod",
                                    f"Method \"{mem}\" with signature \"{m.signature}\" on "
                                    f"interface \"{m.interface}\" doesn't exist\n"))

    def _test_method(self, m):
        mem = m.member
        if m.interface == "org.freedesktop.DBus.Introspectable" and mem == "Introspect":
            self.send(Message.method_return(m, "s", (INTROSPECT_XML,)))
        elif m.interface == dbus_mini.PROPS_IFACE:
            a = m.args(wrap_variants=True)
            props = self.bus.props
            if mem == "Get":
                if a[1] not in props:
                    self.send(Message.error(m, ERR + "InvalidArgs", f"No such property '{a[1]}'"))
                else:
                    self.send(Message.method_return(m, "v", (props[a[1]],)))
            elif mem == "GetAll":
                self.send(Message.method_return(m, "a{sv}", (dict(props),)))
            elif mem == "Set":
                props[a[1]] = a[2]
                self.send(Message.method_return(m))
        elif mem == "GetCurrentState":
            self.send(Message.method_return(m, GET_CURRENT_STATE_SIG,
                                            get_current_state_fixture(True)))
        elif mem == "ApplyMonitorsConfig":
            a = m.args(wrap_variants=True)
            if a[0] != 42:
                self.send(Message.error(m, ERR + "AccessDenied", "The requested configuration "
                                        "is based on stale information"))
                return
            self.bus.last_apply = (m.signature, a)
            self.send(Message.method_return(m))
        elif mem == "Echo":
            self.send(Message.method_return(m, m.signature, m.args(wrap_variants=True)))
        elif mem == "Fail":
            self.send(Message.error(m, "test.Echo.Error.Boom", "kaboom"))
        elif mem == "Fire":
            (text,) = m.args()
            self.send(Message.signal("/test", "test.Echo", "Fired", "s", (text,),
                                     destination=self.unique))
            self.send(Message.method_return(m))
        elif mem == "FireLater":
            delay, text = m.args()

            def later():
                time.sleep(delay)
                self.send(Message.signal("/test", "test.Echo", "Fired", "s", (text,),
                                         destination=self.unique))
            threading.Thread(target=later, daemon=True).start()
            self.send(Message.method_return(m))
        elif mem == "Slow":
            (delay,) = m.args()
            time.sleep(delay)
            self.send(Message.method_return(m, "s", ("late",)))
        elif mem == "GetFd":
            r, w = os.pipe()
            os.write(w, b"hello fd")
            os.close(w)
            self.send(Message.method_return(m, "hs", (r, "pipe")))
            os.close(r)
        elif mem == "PingMe":
            ping = Message.call(self.unique, "/", "org.freedesktop.DBus.Peer", "Ping")
            with self.lock:
                self.serial += 1
                s = self.serial
            self.pending_pings[s] = m
            ping.sender = "org.freedesktop.DBus"
            self.send_raw(ping.to_bytes(s))
        elif mem == "Silent":
            pass  # NO_REPLY_EXPECTED callers do not want a reply
        elif mem == "Odd":
            # a type-5 frame carrying an fd (spec: unknown types must be
            # ignored), then the real reply
            r, w = os.pipe()
            os.close(w)
            body, fds = marshal("h", (r,))
            odd = Message(5, path="/", member="X", destination=self.unique,
                          signature="h", body=body, fds=fds)
            with self.lock:
                self.serial += 1
                s = self.serial
            self.send_raw(odd.to_bytes(s), fds)
            os.close(r)
            self.send(Message.method_return(m, "s", ("after odd",)))
        elif mem == "FireFd":
            r, w = os.pipe()
            os.close(w)
            self.send(Message.signal("/test", "test.Echo", "FiredFd", "h", (r,),
                                     destination=self.unique))
            os.close(r)
            self.send(Message.method_return(m))
        elif mem == "Stall":
            # reply, then stop reading this connection until the test says so
            self.send(Message.method_return(m))
            self.bus.stall.wait(10)
        else:
            self.send(Message.error(m, ERR + "UnknownMethod", f"no {mem}"))


class MockBus:
    """dbus-daemon stand-in on a unix socket (a directory of its own so
    `unix:path=` parsing, percent-escapes and abstract sockets are testable)."""

    def __init__(self, agree_fds=True, reject_auth=False, abstract=False, subdir=None,
                 die_mid_auth=0):
        self.agree_fds, self.reject_auth = agree_fds, reject_auth
        #: how many of the next connections die mid-auth: accepted, our AUTH
        #: line left sitting unread, then closed -- which is ECONNRESET on the
        #: client's read (and EPIPE on its next write)
        self.die_mid_auth = die_mid_auth
        self.dir = tempfile.mkdtemp(prefix="dbus-mini-")
        d = os.path.join(self.dir, subdir) if subdir else self.dir
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, "bus")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if abstract:
            name = "dbus-mini-test-%d-%d" % (os.getpid(), id(self))
            self.srv.bind("\0" + name)
            self.address = "unix:abstract=" + name
        else:
            self.srv.bind(self.path)
            self.address = "unix:path=" + self.path
        self.srv.listen(8)
        self.lock = threading.Lock()
        #: notified by drop(), so a test that has just closed a client can
        #: wait for the bus to finish letting go of it -- see wait_dropped()
        self.gone = threading.Condition(self.lock)
        self.conns = []
        self.names = {}
        self.next_id = 0
        self.props = {"Count": Variant("i", 7), "Name": Variant("s", "mock"),
                      "Rate": Variant("d", 59.94), "Flags": Variant("au", [1, 2])}
        self.last_apply = None
        self.stall = threading.Event()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while True:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            if self.die_mid_auth > 0:
                self.die_mid_auth -= 1
                select.select([s], [], [], 5.0)   # let the AUTH line land...
                s.close()                         # ...and never read it
                continue
            c = _Conn(self, s)
            with self.lock:
                self.conns.append(c)
            threading.Thread(target=c.serve, daemon=True).start()

    def close(self):
        self.srv.close()
        with self.lock:
            conns = list(self.conns)
        for c in conns:
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- name registry

    def hello(self, conn):
        with self.lock:
            self.next_id += 1
            return ":1.%d" % self.next_id

    def connections(self):
        with self.lock:
            return [c for c in self.conns if c.unique]

    def lookup(self, name):
        with self.lock:
            if name in self.names:
                return self.names[name]
            for c in self.conns:
                if c.unique == name:
                    return c
        return None

    def list_names(self):
        with self.lock:
            return (["org.freedesktop.DBus"] + [c.unique for c in self.conns if c.unique]
                    + list(self.names))

    def request_name(self, conn, name):
        with self.lock:
            cur = self.names.get(name)
            if cur is conn:
                return 4
            if cur is not None:
                return 3
            self.names[name] = conn
            others = [c for c in self.conns if c.unique]
        conn.signal("NameAcquired", "s", (name,))
        for c in others:
            c.signal("NameOwnerChanged", "sss", (name, "", conn.unique))
        return 1

    def release_name(self, conn, name):
        with self.lock:
            if self.names.get(name) is not conn:
                return 2 if name in self.names else 3
            del self.names[name]
            others = [c for c in self.conns if c.unique]
        for c in others:
            c.signal("NameOwnerChanged", "sss", (name, conn.unique, ""))
        return 1

    def wait_dropped(self, unique, timeout=5.0):
        """Block until the connection called `unique` is off the bus -- and
        with it every name it owned. True, or False on timeout.

        A client closing its socket and the bus noticing are two events in two
        threads: drop() runs in that connection's own serving thread, once its
        recv() finally returns nothing. A test that tears one mock service
        down and stands its replacement up races between the two, and loses
        under load -- the new RequestName is answered 3 (the name has an
        owner) because the owner in the table is the connection that just
        went away."""
        deadline = time.monotonic() + timeout
        with self.gone:
            while any(c.unique == unique for c in self.conns):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.gone.wait(left)
        return True

    def drop(self, conn):
        with self.gone:
            if conn in self.conns:
                self.conns.remove(conn)
            gone = [n for n, c in self.names.items() if c is conn]
            for n in gone:
                del self.names[n]
            others = [c for c in self.conns if c.unique]
            self.gone.notify_all()
        for n in gone:
            for c in others:
                c.signal("NameOwnerChanged", "sss", (n, conn.unique, ""))

    def conn_of(self, unique):
        return self.lookup(unique)


def _nfds():
    return len(os.listdir("/proc/self/fd"))


def _wait_nfds(n, timeout=2.0):
    """The mock's threads close their fd copies a moment after forwarding."""
    deadline = time.monotonic() + timeout
    while _nfds() != n and time.monotonic() < deadline:
        time.sleep(0.01)
    return _nfds()


def _frame(mtype, fields, body=b"", flags=0):
    w = dbus_mini._Writer()
    w.buf += struct.pack("<BBBBII", ord("l"), mtype, flags, 1, len(body), 1)
    w.one("a(yv)", fields)
    w.pad(8)
    return bytes(w.buf) + body


# ---------------------------------------------------------------- unit tests

class Signatures(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_signature(""), [])
        self.assertEqual(split_signature("ybnqiuxtdhsogv"), list("ybnqiuxtdhsogv"))
        self.assertEqual(split_signature("ua{sv}(ii)aa(ss)a{s(ii)}"),
                         ["u", "a{sv}", "(ii)", "aa(ss)", "a{s(ii)}"])
        self.assertEqual(split_signature(GET_CURRENT_STATE_SIG),
                         ["u", "a((ssss)a(siiddada{sv})a{sv})", "a(iiduba(ssss)a{sv})", "a{sv}"])

    def test_invalid(self):
        for bad in ("a", "(", "()", "(i", "{sv}", "a{vs}", "a{(i)s}", "a{sii}", "z", "a{s", "(i}"):
            with self.assertRaises(ValueError, msg=bad):
                split_signature(bad)
        with self.assertRaises(ValueError):
            Variant("ii", (1, 2))
        with self.assertRaises(ValueError):
            Variant("", 1)


class Marshal(unittest.TestCase):
    def rt(self, sig, args, expect=None, endian="<"):
        body, fds = marshal(sig, args, endian)
        self.assertEqual(fds, [])
        got = unmarshal(sig, body, endian)
        self.assertEqual(got, tuple(args) if expect is None else expect)
        return body

    def test_basic_types(self):
        self.rt("y", (255,))
        self.rt("b", (True,))
        self.rt("b", (0,), (False,))
        self.rt("n", (-32768,))
        self.rt("q", (65535,))
        self.rt("i", (-2147483648,))
        self.rt("u", (4294967295,))
        self.rt("x", (-(1 << 63),))
        self.rt("t", ((1 << 64) - 1,))
        self.rt("d", (59.94,))
        self.rt("d", (2,), (2.0,))
        self.rt("s", ("héllo wörld ✓",))
        self.rt("s", ("",))
        self.rt("o", ("/",))
        self.rt("o", ("/org/gnome/Mutter/DisplayConfig",))
        self.rt("g", ("a{sv}",))
        self.rt("g", ("",))
        self.rt("ybnqiuxtdsog", (1, False, -2, 3, -4, 5, -6, 7, 8.5, "s", "/o", "g"))

    def test_basic_bytes(self):
        self.assertEqual(marshal("yqui", (1, 2, 3, -4))[0],
                         b"\x01\0" + struct.pack("<HIi", 2, 3, -4))
        self.assertEqual(marshal("s", ("hi",))[0], b"\x02\0\0\0hi\0")
        self.assertEqual(marshal("g", ("ii",))[0], b"\x02ii\0")
        self.assertEqual(marshal("b", (True,))[0], b"\x01\0\0\0")
        self.assertEqual(marshal("ys", (9, "ab"))[0], b"\x09\0\0\0\x02\0\0\0ab\0")

    def test_range_and_type_errors(self):
        for sig, val in (("y", 256), ("n", 40000), ("u", -1), ("i", 1 << 31),
                         ("t", -1), ("s", 5), ("s", "a\0b"), ("o", "bad"),
                         ("o", "/trailing/"), ("g", "zz"), ("ai", "str"),
                         ("(ii)", (1,)), ("a{sv}", {"k": [1]}), ("i", True),
                         ("v", [1, 2]), ("b", "yes")):
            with self.assertRaises(ValueError, msg=f"{sig}={val!r}"):
                marshal(sig, (val,))
        with self.assertRaises(ValueError):
            marshal("ii", (1,))

    def test_containers(self):
        self.rt("ai", ([1, -2, 3],))
        self.rt("ai", ([],))
        self.rt("as", (["a", "", "ccc"],))
        self.rt("ay", (b"\x00\xff\x10",))
        self.rt("ay", ([1, 2],), (b"\x01\x02",))
        self.rt("(ii)", ((1, 2),))
        self.rt("(s(ii)as)", (("x", (1, 2), ["y"]),))
        self.rt("a(ii)", ([(1, 2), (3, 4)],))
        self.rt("aai", ([[1], [], [2, 3]],))
        self.rt("a{ss}", ({"a": "b", "c": "d"},))
        self.rt("a{ss}", ({},))
        self.rt("a{ii}", ({1: 2},))
        self.rt("a{s(ii)}", ({"p": (1, 2)},))
        self.rt("a{ss}", ([("k", "v")],), ({"k": "v"},))  # pair list accepted
        self.rt("aa{sv}", ([{}, {"a": Variant("i", 1)}],), ([{}, {"a": 1}],))
        self.rt("a{sa{sv}}", ({"o": {"k": Variant("s", "v")}},), ({"o": {"k": "v"}},))
        # ao / ag / ax / at / ad
        self.rt("aoagaxatad", (["/a", "/b"], ["i", "a{sv}"], [-1, 1], [1 << 40], [1.5, -0.0]))

    def test_variants(self):
        body, _ = marshal("v", (Variant("s", "x"),))
        self.assertEqual(body, b"\x01s\0\0\x01\0\0\0x\0")
        self.assertEqual(unmarshal("v", body), ("x",))
        self.assertEqual(unmarshal("v", body, wrap_variants=True), (Variant("s", "x"),))
        nested = Variant("v", Variant("(ib)", (5, True)))
        body, _ = marshal("v", (nested,))
        self.assertEqual(unmarshal("v", body), ((5, True),))
        self.assertEqual(unmarshal("v", body, wrap_variants=True), (nested,))
        # guessing plain values in v slots
        body, _ = marshal("a{sv}", ({"b": True, "i": -1, "x": 1 << 40, "d": 1.5,
                                     "s": "str", "ay": b"\x01"},))
        self.assertEqual(unmarshal("a{sv}", body, wrap_variants=True)[0],
                         {"b": Variant("b", True), "i": Variant("i", -1),
                          "x": Variant("x", 1 << 40), "d": Variant("d", 1.5),
                          "s": Variant("s", "str"), "ay": Variant("ay", b"\x01")})
        self.assertEqual(dbus_mini.guess_variant(Variant("u", 1)), Variant("u", 1))
        with self.assertRaises(ValueError):
            marshal("v", ({"a": 1},))

    def test_nested_asv_with_empty_containers(self):
        val = {"empty-array": Variant("ai", []),
               "empty-dict": Variant("a{sv}", {}),
               "empty-structs": Variant("a(ssss)", []),
               "nested": Variant("a{sv}", {"in": Variant("v", Variant("au", [1, 2]))}),
               "pair": Variant("(bb)", (True, False)),
               "specs": Variant("a(ssss)", [EDP, DP]),
               "bytes": Variant("ay", b"")}
        body, _ = marshal("a{sv}", (val,))
        self.assertEqual(unmarshal("a{sv}", body, wrap_variants=True), (val,))
        self.assertEqual(unmarshal("a{sv}", body), ({
            "empty-array": [], "empty-dict": {}, "empty-structs": [],
            "nested": {"in": [1, 2]}, "pair": (True, False), "specs": [EDP, DP],
            "bytes": b""},))

    # -- alignment facts

    def test_struct_after_byte(self):
        body = self.rt("y(ii)", (7, (-1, 2)))
        self.assertEqual(body, b"\x07" + b"\0" * 7 + struct.pack("<ii", -1, 2))
        self.assertEqual(len(body), 16)

    def test_array_of_t_after_u(self):
        body = self.rt("uuat", (1, 2, [3, 4]))
        # length field at 8 counts only element bytes; 4 pad bytes to reach 8-alignment
        self.assertEqual(body, struct.pack("<III", 1, 2, 16) + b"\0\0\0\0"
                         + struct.pack("<QQ", 3, 4))
        body = self.rt("yat", (1, []))
        self.assertEqual(body, b"\x01\0\0\0" + struct.pack("<I", 0))  # already 8-aligned

    def test_empty_array_of_structs_keeps_padding(self):
        body = self.rt("a(ii)", ([],))
        self.assertEqual(body, b"\0\0\0\0" + b"\0\0\0\0")
        body = self.rt("a(ii)y", ([], 9))
        self.assertEqual(body, b"\0" * 8 + b"\x09")
        body = self.rt("a{sv}", ({},))
        self.assertEqual(body, b"\0" * 8)
        body = self.rt("ai", ([],))
        self.assertEqual(body, b"\0\0\0\0")  # 4-aligned elements: no pad

    def test_dict_entries_align_8(self):
        body = self.rt("ya{sv}", (1, {"k": Variant("b", True)}), (1, {"k": True}))
        self.assertEqual(body, b"\x01\0\0\0" + struct.pack("<I", 16) + struct.pack("<I", 1)
                         + b"k\0" + b"\x01b\0" + b"\0\0\0" + struct.pack("<I", 1))
        body = self.rt("a{ss}", ({"a": "b", "cc": "d"},))
        # entry 1 at 8: I(1) "a" NUL pad2 | I(1) "b" NUL pad2 -> entry 2 at 24; 38 bytes total
        self.assertEqual(body, struct.pack("<I", 30) + b"\0\0\0\0"
                         + struct.pack("<I", 1) + b"a\0\0\0" + struct.pack("<I", 1) + b"b\0\0\0"
                         + struct.pack("<I", 2) + b"cc\0\0" + struct.pack("<I", 1) + b"d\0")

    def test_variant_after_byte(self):
        body = self.rt("yv", (5, Variant("u", 9)), (5, 9))
        self.assertEqual(body, b"\x05\x01u\0" + struct.pack("<I", 9))

    def test_x_after_y_in_struct(self):
        body = self.rt("(yx)", ((1, -1),))
        self.assertEqual(body, b"\x01" + b"\0" * 7 + struct.pack("<q", -1))

    # -- reading

    def test_big_endian_read(self):
        body = struct.pack(">Ii", 1, -2) + struct.pack(">I", 2) + b"hi\0" + b"\0"
        body += struct.pack(">I", 8) + b"\0\0\0\0" + struct.pack(">Q", 1 << 40)
        self.assertEqual(unmarshal("uisat", body, ">"), (1, -2, "hi", [1 << 40]))
        # our writer in '>' mode agrees byte for byte
        self.assertEqual(marshal("uisat", (1, -2, "hi", [1 << 40]), ">")[0], body)
        # and the fixture survives a big-endian round trip
        body, _ = marshal(GET_CURRENT_STATE_SIG, get_current_state_fixture(True), ">")
        self.assertEqual(unmarshal(GET_CURRENT_STATE_SIG, body, ">"),
                         get_current_state_fixture(False))

    def test_read_errors(self):
        for sig, data in (("u", b"\0\0\0"), ("s", b"\x02\0\0\0hi"), ("s", b"\x01\0\0\0hX"),
                          ("b", b"\x02\0\0\0"), ("s", b"\x01\0\0\0\xff\0"),
                          ("ai", b"\x08\0\0\0\0\0\0\0"), ("v", b"\x02ii\0"),
                          ("o", b"\x01\0\0\0x\0"), ("h", b"\0\0\0\0"),
                          ("a(ii)", b"\x04\0\0\0\0\0\0\0\0\0\0\0")):
            with self.assertRaises(ValueError, msg=f"{sig}:{data!r}"):
                unmarshal(sig, data)
        with self.assertRaises(ValueError):
            unmarshal("u", b"\0\0\0\0\0")  # trailing bytes
        with self.assertRaises(ValueError):
            unmarshal("u", b"\0\0\0\0", "x")

    def test_unix_fd_index(self):
        body, fds = marshal("hsh", (7, "x", 9))
        self.assertEqual(fds, [7, 9])
        self.assertEqual(body[:4], b"\0\0\0\0")
        self.assertEqual(unmarshal("hsh", body, fds=[100, 200]), (100, "x", 200))


class Fixtures(unittest.TestCase):
    def test_get_current_state(self):
        body, _ = marshal(GET_CURRENT_STATE_SIG, get_current_state_fixture(True))
        self.assertEqual(len(body) % 4, 0)
        self.assertEqual(unmarshal(GET_CURRENT_STATE_SIG, body), get_current_state_fixture(False))
        self.assertEqual(unmarshal(GET_CURRENT_STATE_SIG, body, wrap_variants=True),
                         get_current_state_fixture(True))
        serial, monitors, logical, props = unmarshal(GET_CURRENT_STATE_SIG, body)
        self.assertEqual(serial, 42)
        self.assertEqual(monitors[0][0], EDP)
        self.assertEqual(monitors[1][1][2][0], "1920x1080i@60.000")
        self.assertEqual(monitors[0][1][0][5], SCALES)
        self.assertIs(monitors[0][2]["is-builtin"], True)
        self.assertEqual(logical[1][:5], (1920, 0, 1.0, 1, False))
        self.assertEqual(props["layout-mode"], 1)

    def test_apply_monitors_config(self):
        args = apply_monitors_config_fixture(True)
        body, _ = marshal(APPLY_MONITORS_CONFIG_SIG, args)
        self.assertEqual(unmarshal(APPLY_MONITORS_CONFIG_SIG, body),
                         apply_monitors_config_fixture(False))
        self.assertEqual(unmarshal(APPLY_MONITORS_CONFIG_SIG, body, wrap_variants=True), args)

    def test_qemu_set_ui_info(self):
        body, _ = marshal("qqiiuu", (0, 0, 0, 0, 1280, 800))
        self.assertEqual(body, struct.pack("<HHiiII", 0, 0, 0, 0, 1280, 800))
        self.assertEqual(unmarshal("qqiiuu", body), (0, 0, 0, 0, 1280, 800))


class Messages(unittest.TestCase):
    def test_hello_bytes_are_canonical(self):
        m = Message.call(dbus_mini.DBUS_NAME, dbus_mini.DBUS_PATH, dbus_mini.DBUS_IFACE, "Hello")
        self.assertEqual(m.to_bytes(1), HELLO_BYTES)
        self.assertEqual(len(HELLO_BYTES), 128)
        back = Message.from_bytes(HELLO_BYTES)
        self.assertEqual((back.type, back.serial, back.path, back.destination,
                          back.interface, back.member, back.signature, back.body),
                         (1, 1, "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          "org.freedesktop.DBus", "Hello", "", b""))
        self.assertEqual(Message.frame_length(HELLO_BYTES[:16]), 128)
        self.assertIsNone(Message.frame_length(HELLO_BYTES[:15]))

    def test_call_with_args_and_roundtrip(self):
        m = Message.call("d.est", "/p", "i.face", "M", "sai", ("x", [1, 2]))
        data = m.to_bytes(77)
        back = Message.from_bytes(data)
        self.assertEqual(back.args(), ("x", [1, 2]))
        self.assertEqual(back.serial, 77)
        self.assertEqual(back.signature, "sai")
        self.assertEqual(len(data) - len(back.body), 8 * ((len(data) - len(back.body)) // 8))

    def test_return_error_signal(self):
        call = Message.from_bytes(Message.call("d", "/", "i", "m").to_bytes(5))
        call.sender = ":1.9"
        r = Message.from_bytes(Message.method_return(call, "i", (1,)).to_bytes(6))
        self.assertEqual((r.type, r.reply_serial, r.destination, r.args()), (2, 5, ":1.9", (1,)))
        e = Message.from_bytes(Message.error(call, "a.b.Err", "why").to_bytes(7))
        self.assertEqual((e.type, e.error_name, e.reply_serial, e.args()),
                         (3, "a.b.Err", 5, ("why",)))
        s = Message.from_bytes(Message.signal("/p", "i.f", "Sig", "b", (True,)).to_bytes(8))
        self.assertEqual((s.type, s.path, s.interface, s.member, s.args()),
                         (4, "/p", "i.f", "Sig", (True,)))
        with self.assertRaises(ValueError):
            Message(dbus_mini.SIGNAL, path="/p", member="m").to_bytes(1)
        with self.assertRaises(ValueError):
            Message(dbus_mini.METHOD_CALL, path="/p").to_bytes(1)

    def test_big_endian_message(self):
        fields = (b"\x05\x01u\0" + struct.pack(">I", 3)          # REPLY_SERIAL 3
                  + b"\x08\x01g\0" + b"\x01s\0")                   # SIGNATURE s
        body = struct.pack(">I", 2) + b"hi\0"
        data = (b"B\x02\x00\x01" + struct.pack(">II", len(body), 9)
                + struct.pack(">I", len(fields)) + fields + b"\0" + body)
        self.assertEqual(len(fields), 15)
        self.assertEqual(Message.frame_length(data), len(data))
        m = Message.from_bytes(data)
        self.assertEqual((m.endian, m.type, m.serial, m.reply_serial, m.args()),
                         (">", 2, 9, 3, ("hi",)))

    def test_unknown_header_field_ignored_and_fds_counted(self):
        m = Message.call("d", "/", "i", "m", "h", (5,))
        data = bytearray(m.to_bytes(1))
        back = Message.from_bytes(bytes(data), fds=[42])
        self.assertEqual(back.unix_fds, 1)
        self.assertEqual(back.args(), (42,))
        # splice in an unknown field code 200 with a 'y' value (before the pad)
        w = dbus_mini._Writer()
        w.buf += struct.pack("<BBBBII", ord("l"), 1, 0, 1, 0, 1)
        w.one("a(yv)", [(1, Variant("o", "/")), (3, Variant("s", "m")), (200, Variant("y", 1))])
        w.pad(8)
        hdr = bytes(w.buf)
        back = Message.from_bytes(hdr)
        self.assertEqual((back.path, back.member, back.signature), ("/", "m", ""))
        bad = bytearray(hdr)
        bad[3] = 2
        with self.assertRaises(ValueError):
            Message.from_bytes(bytes(bad))

    def test_header_field_types_and_required_fields(self):
        P, M, I, RS, EN = (Variant("o", "/"), Variant("s", "m"), Variant("s", "i.f"),
                           Variant("u", 3), Variant("s", "a.b.E"))
        call = Message.from_bytes(_frame(1, [(1, P), (3, M)]))
        self.assertEqual((call.path, call.member, call.interface), ("/", "m", None))
        self.assertEqual(Message.from_bytes(_frame(2, [(5, RS)])).reply_serial, 3)
        self.assertEqual(Message.from_bytes(_frame(3, [(4, EN), (5, RS)])).error_name, "a.b.E")
        self.assertEqual(Message.from_bytes(_frame(4, [(1, P), (2, I), (3, M)])).interface, "i.f")
        for what, mtype, fields in (
                ("PATH as u", 1, [(1, Variant("u", 5)), (3, M)]),
                ("REPLY_SERIAL as s", 2, [(5, Variant("s", "notanint"))]),
                ("SIGNATURE as s", 2, [(5, RS), (8, Variant("s", "i"))]),
                ("UNIX_FDS as i", 2, [(5, RS), (9, Variant("i", 1))]),
                ("call without MEMBER", 1, [(1, P)]),
                ("call without PATH", 1, [(3, M)]),
                ("return without REPLY_SERIAL", 2, []),
                ("error without ERROR_NAME", 3, [(5, RS)]),
                ("error without REPLY_SERIAL", 3, [(4, EN)]),
                ("signal without INTERFACE", 4, [(1, P), (3, M)]),
                ("type 0", 0, [(1, P), (3, M)])):
            with self.assertRaises(ValueError, msg=what):
                Message.from_bytes(_frame(mtype, fields))

    def test_unknown_message_type_parses(self):
        # "Unknown types must be ignored": that is the receiver's job (see the
        # mock-bus test); the parser keeps them (no required fields) and the
        # header validation still applies
        m = Message.from_bytes(_frame(5, [(7, Variant("s", ":1.2"))]))
        self.assertEqual((m.type, m.sender), (5, ":1.2"))
        with self.assertRaises(ValueError):
            Message.from_bytes(_frame(5, [(7, Variant("u", 1))]))

    def test_address_parsing(self):
        self.assertEqual(dbus_mini.parse_address("unix:path=/run/user/1000/bus"),
                         [{"transport": "unix", "path": "/run/user/1000/bus"}])
        self.assertEqual(dbus_mini.parse_address("unix:abstract=/tmp/dbus-X,guid=ab;"
                                                 "unix:path=/a%20b%2Cc"),
                         [{"transport": "unix", "abstract": "/tmp/dbus-X", "guid": "ab"},
                          {"transport": "unix", "path": "/a b,c"}])
        self.assertEqual(dbus_mini.socket_path_of("unix:abstract=x;unix:path=/p"), "/p")
        self.assertIsNone(dbus_mini.socket_path_of("unix:abstract=x"))
        saved = os.environ.get("XDG_RUNTIME_DIR")
        try:
            os.environ["XDG_RUNTIME_DIR"] = "/run/user/7"
            self.assertEqual(dbus_mini.socket_path_of("unix:runtime=yes"), "/run/user/7/bus")
            del os.environ["XDG_RUNTIME_DIR"]
            self.assertIsNone(dbus_mini.socket_path_of("unix:runtime=yes"))
        finally:
            if saved is not None:
                os.environ["XDG_RUNTIME_DIR"] = saved
        for bad in ("", "nocolon", "unix:path"):
            with self.assertRaises(ValueError):
                dbus_mini.parse_address(bad)


# ---------------------------------------------------------------- mock bus tests

class MockBusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockBus()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def setUp(self):
        self.bus = Bus(self.mock.address)

    def tearDown(self):
        self.bus.close()

    def echo(self, sig, *args, **kw):
        return self.bus.call("test.Echo", "/test", "test.Echo", "Echo", sig, args, **kw)

    def test_connect_auth_hello(self):
        self.assertRegex(self.bus.unique_name, r"^:1\.\d+$")
        self.assertEqual(self.bus.auth_path, "direct")
        self.assertTrue(self.bus.fds_ok)
        self.assertEqual(self.bus.guid, "deadbeef0000000000000000cafe0001")
        conn = self.mock.conn_of(self.bus.unique_name)
        self.assertEqual(conn.auth_lines[0], "AUTH EXTERNAL " + str(os.geteuid()).encode().hex())
        self.assertEqual(conn.auth_lines[1:], ["NEGOTIATE_UNIX_FD", "BEGIN"])
        # NameAcquired for our unique name is queued (not lost behind Hello)
        m = self.bus.wait_signal(dbus_mini.DBUS_IFACE, "NameAcquired", 2)
        self.assertEqual(m.args(), (self.bus.unique_name,))

    def test_names(self):
        names = self.bus.list_names()
        self.assertIn("org.freedesktop.DBus", names)
        self.assertIn(self.bus.unique_name, names)
        self.assertTrue(self.bus.name_has_owner(self.bus.unique_name))
        self.assertFalse(self.bus.name_has_owner("no.such.Name"))
        self.assertEqual(self.bus.request_name("test.Owned"), 1)
        self.assertEqual(self.bus.get_name_owner("test.Owned"), self.bus.unique_name)
        self.assertEqual(self.bus.release_name("test.Owned"), 1)
        with self.assertRaises(DBusError) as cm:
            self.bus.get_name_owner("test.Owned")
        self.assertEqual(cm.exception.name, ERR + "NameHasNoOwner")

    def test_echo_basic_types(self):
        for sig, args in (("y", (200,)), ("b", (False,)), ("n", (-5,)), ("q", (5,)),
                          ("i", (-7,)), ("u", (7,)), ("x", (-(1 << 40),)), ("t", (1 << 40,)),
                          ("d", (-1.25,)), ("s", ("ünïcode",)), ("o", ("/a/b",)),
                          ("g", ("a(ii)",)), ("", ())):
            self.assertEqual(self.echo(sig, *args), args, sig)

    def test_echo_containers_and_variants(self):
        val = get_current_state_fixture(True)
        self.assertEqual(self.echo(GET_CURRENT_STATE_SIG, *val), get_current_state_fixture(False))
        self.assertEqual(self.echo(GET_CURRENT_STATE_SIG, *val, wrap_variants=True), val)
        self.assertEqual(self.echo("a{sv}ai(ss)aay", {"k": Variant("v", Variant("as", ["x"]))},
                                   [], ("a", "b"), [b"\0\1", b""]),
                         ({"k": ["x"]}, [], ("a", "b"), [b"\0\1", b""]))

    def test_display_config_fixture_over_the_wire(self):
        state = self.bus.call("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                              "org.gnome.Mutter.DisplayConfig", "GetCurrentState")
        self.assertEqual(state, get_current_state_fixture(False))
        self.assertEqual(self.bus.call("org.gnome.Mutter.DisplayConfig",
                                       "/org/gnome/Mutter/DisplayConfig",
                                       "org.gnome.Mutter.DisplayConfig", "ApplyMonitorsConfig",
                                       APPLY_MONITORS_CONFIG_SIG, apply_monitors_config_fixture()),
                         ())
        self.assertEqual(self.mock.last_apply,
                         (APPLY_MONITORS_CONFIG_SIG, apply_monitors_config_fixture(True)))
        stale = (7,) + apply_monitors_config_fixture()[1:]
        with self.assertRaises(DBusError) as cm:
            self.bus.call("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                          "org.gnome.Mutter.DisplayConfig", "ApplyMonitorsConfig",
                          APPLY_MONITORS_CONFIG_SIG, stale)
        self.assertEqual(cm.exception.name, ERR + "AccessDenied")
        self.assertEqual(cm.exception.message,
                         "The requested configuration is based on stale information")

    def test_error_reply(self):
        with self.assertRaises(DBusError) as cm:
            self.bus.call("test.Echo", "/test", "test.Echo", "Fail")
        self.assertEqual((cm.exception.name, cm.exception.message),
                         ("test.Echo.Error.Boom", "kaboom"))
        self.assertEqual(str(cm.exception), "test.Echo.Error.Boom: kaboom")
        with self.assertRaises(DBusError) as cm:
            self.bus.call("no.such.Service", "/", "x", "y")
        self.assertEqual(cm.exception.name, ERR + "ServiceUnknown")
        with self.assertRaises(DBusError) as cm:
            self.bus._bus("Bogus")
        self.assertEqual(cm.exception.name, ERR + "UnknownMethod")

    def test_timeout_then_late_reply_is_discarded(self):
        t0 = time.monotonic()
        with self.assertRaises(DBusError) as cm:
            self.bus.call("test.Echo", "/test", "test.Echo", "Slow", "d", (0.5,), timeout=0.15)
        self.assertEqual(cm.exception.name, ERR + "NoReply")
        self.assertLess(time.monotonic() - t0, 0.45)
        time.sleep(0.5)  # late "late" reply arrives now and must not confuse the next call
        self.assertEqual(self.echo("s", "after"), ("after",))
        self.assertEqual(len(self.bus._replies), 0)
        self.assertEqual(self.bus.call("test.Echo", "/test", "test.Echo", "Slow", "d", (0.05,)),
                         ("late",))

    def test_no_reply_expected(self):
        out = self.bus.call("test.Echo", "/test", "test.Echo", "Silent",
                            flags=dbus_mini.NO_REPLY_EXPECTED)
        self.assertEqual(out, ())
        self.assertEqual(self.echo("i", 1), (1,))

    def test_signals_wait_and_queue(self):
        self.bus.wait_signal(None, "NameAcquired", 2)
        self.bus.call("test.Echo", "/test", "test.Echo", "Fire", "s", ("one",))
        # the signal arrived before the reply and was queued
        m = self.bus.wait_signal("test.Echo", "Fired", 0)
        self.assertEqual((m.path, m.sender, m.args()), ("/test", "org.freedesktop.DBus", ("one",)))
        self.assertIsNone(self.bus.wait_signal("test.Echo", "Fired", 0.05))
        self.bus.call("test.Echo", "/test", "test.Echo", "FireLater", "ds", (0.1, "two"))
        t0 = time.monotonic()
        m = self.bus.wait_signal("test.Echo", "Fired", 2, path="/test")
        self.assertEqual(m.args(), ("two",))
        self.assertGreater(time.monotonic() - t0, 0.05)
        # filters: a non-matching signal stays queued while we time out on another
        self.bus.call("test.Echo", "/test", "test.Echo", "Fire", "s", ("three",))
        self.assertIsNone(self.bus.wait_signal("test.Echo", "Fired", 0.05, path="/elsewhere"))
        self.assertIsNone(self.bus.wait_signal("test.Echo", "Other", 0))
        self.assertIsNone(self.bus.wait_signal("test.Echo", "Fired", 0, sender=":1.999"))
        self.assertEqual(self.bus.wait_signal(None, None, 0, sender="org.freedesktop.DBus").args(),
                         ("three",))

    def test_messages_generator(self):
        self.bus.wait_signal(None, "NameAcquired", 2)
        self.bus.call("test.Echo", "/test", "test.Echo", "FireLater", "ds", (0.05, "a"))
        self.bus.call("test.Echo", "/test", "test.Echo", "FireLater", "ds", (0.1, "b"))
        got = [m.args()[0] for m in self.bus.messages(0.5)]
        self.assertEqual(sorted(got), ["a", "b"])
        t0 = time.monotonic()
        self.assertEqual(list(self.bus.messages(0.1)), [])
        self.assertGreaterEqual(time.monotonic() - t0, 0.09)

    def test_name_owner_changed_from_second_connection(self):
        self.bus.add_match("type='signal',interface='org.freedesktop.DBus',"
                           "member='NameOwnerChanged'")
        other = Bus(self.mock.address)
        try:
            self.assertEqual(other.request_name("test.Second"), 1)
            m = self.bus.wait_signal(dbus_mini.DBUS_IFACE, "NameOwnerChanged", 2)
            self.assertEqual(m.args(), ("test.Second", "", other.unique_name))
            self.assertTrue(self.bus.name_has_owner("test.Second"))
        finally:
            other.close()
        m = self.bus.wait_signal(dbus_mini.DBUS_IFACE, "NameOwnerChanged", 2)
        self.assertEqual(m.args(), ("test.Second", other.unique_name, ""))

    def test_properties_and_introspect(self):
        self.assertEqual(self.bus.get_property("test.Echo", "/test", "test.Props", "Count"), 7)
        self.assertEqual(self.bus.get_property("test.Echo", "/test", "test.Props", "Rate",
                                               wrap_variants=True), Variant("d", 59.94))
        self.bus.set_property("test.Echo", "/test", "test.Props", "Count", Variant("i", 8))
        self.bus.set_property("test.Echo", "/test", "test.Props", "Name", "plain")
        allp = self.bus.get_all_properties("test.Echo", "/test", "test.Props")
        self.assertEqual(allp, {"Count": 8, "Name": "plain", "Rate": 59.94, "Flags": [1, 2]})
        wrapped = self.bus.get_all_properties("test.Echo", "/test", "test.Props",
                                              wrap_variants=True)
        self.assertEqual(wrapped["Name"], Variant("s", "plain"))
        with self.assertRaises(DBusError) as cm:
            self.bus.get_property("test.Echo", "/test", "test.Props", "Nope")
        self.assertEqual(cm.exception.name, ERR + "InvalidArgs")
        xml = self.bus.introspect("test.Echo", "/test")
        self.assertIn('<interface name="test.Echo">', xml)
        self.bus.ping("test.Echo")

    def test_unix_fd_read(self):
        fd, label = self.bus.call("test.Echo", "/test", "test.Echo", "GetFd")
        self.assertEqual(label, "pipe")
        try:
            self.assertEqual(os.read(fd, 100), b"hello fd")
        finally:
            os.close(fd)

    def test_unix_fd_write(self):
        r, w = os.pipe()
        try:
            (back,) = self.echo("h", w)
            self.assertNotEqual(back, w)  # a fresh fd from SCM_RIGHTS
            os.write(back, b"x")
            os.close(back)
            self.assertEqual(os.read(r, 1), b"x")
        finally:
            os.close(r)
            os.close(w)

    def test_incoming_ping_is_answered_while_blocked_in_call(self):
        (ok,) = self.bus.call("test.Echo", "/test", "test.Echo", "PingMe")
        self.assertIs(ok, True)

    def test_received_fds_are_not_inheritable(self):
        # recvmsg delivers SCM_RIGHTS fds without CLOEXEC; a bus fd must not
        # leak into swaymsg/xdotool children
        r, w = os.pipe()
        try:
            (fd,) = self.echo("h", r)
            self.assertFalse(os.get_inheritable(fd))
            os.close(fd)
        finally:
            os.close(r)
            os.close(w)
        self.assertFalse(self.bus.sock.get_inheritable())

    def test_auto_answered_calls_close_their_fds(self):
        self.bus.wait_signal(None, "NameAcquired", 2)
        other = Bus(self.mock.address)
        try:
            other.wait_signal(None, "NameAcquired", 2)
            r, w = os.pipe()
            base = _nfds()
            names = []

            def blast():
                for flags in (0, 0, 0, dbus_mini.NO_REPLY_EXPECTED, dbus_mini.NO_REPLY_EXPECTED):
                    try:
                        other.call(self.bus.unique_name, "/", "x.y", "Nope", "h", (r,),
                                   timeout=3, flags=flags)
                    except DBusError as e:
                        names.append(e.name)
            t = threading.Thread(target=blast)
            t.start()
            deadline = time.monotonic() + 5
            while t.is_alive() and time.monotonic() < deadline:
                self.bus._pump(0.1)
            t.join(1)
            self.assertEqual(names, [ERR + "UnknownMethod"] * 3)
            self.bus._pump(0.2)  # the two NO_REPLY_EXPECTED ones
            self.assertEqual(_wait_nfds(base), base)
            os.close(r)
            os.close(w)
        finally:
            other.close()

    def test_unknown_message_type_is_ignored(self):
        self.bus.wait_signal(None, "NameAcquired", 2)
        base = _nfds()
        self.assertEqual(self.bus.call("test.Echo", "/test", "test.Echo", "Odd"), ("after odd",))
        self.assertEqual(len(self.bus._queue), 0)
        self.assertEqual(_wait_nfds(base), base)  # the fd on the dropped frame was closed
        self.assertEqual(self.echo("s", "still fine"), ("still fine",))

    def test_close_releases_queued_fds(self):
        self.bus.wait_signal(None, "NameAcquired", 2)
        base = _nfds()
        self.bus.call("test.Echo", "/test", "test.Echo", "FireFd")
        self.assertEqual(len(self.bus._queue), 1)  # FiredFd, fd unclaimed
        self.assertEqual(_wait_nfds(base + 1), base + 1)
        self.bus.close()
        # the signal's fd and our socket go, and the in-process mock drops its
        # end of the connection a moment later
        self.assertEqual(_wait_nfds(base - 2), base - 2)
        self.assertEqual(self.bus._queue, deque())

    def test_fileno_after_close(self):
        self.assertEqual(self.bus.fileno(), self.bus.sock.fileno())
        self.bus.close()
        with self.assertRaises(DBusError) as cm:
            self.bus.fileno()
        self.assertEqual(cm.exception.name, ERR + "Disconnected")

    def test_serve_calls_between_two_clients(self):
        server = Bus(self.mock.address)
        server.serve_calls = True
        results = []

        def serve():
            for m in server.messages(3):
                if m.type == dbus_mini.METHOD_CALL and m.member == "Add":
                    a, b = m.args()
                    server.reply(m, "i", (a + b,))
                elif m.type == dbus_mini.METHOD_CALL:
                    server.error_reply(m, "test.Svc.NoSuch", "nope")
                    results.append(m.member)
                    return
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            self.assertEqual(self.bus.call(server.unique_name, "/x", "test.Svc", "Add",
                                           "ii", (2, 3)), (5,))
            with self.assertRaises(DBusError) as cm:
                self.bus.call(server.unique_name, "/x", "test.Svc", "Stop")
            self.assertEqual(cm.exception.name, "test.Svc.NoSuch")
            t.join(3)
            self.assertEqual(results, ["Stop"])
        finally:
            server.close()

    def test_close_and_context_manager(self):
        with Bus(self.mock.address) as b:
            self.assertTrue(b.list_names())
        self.assertIsNone(b.sock)
        with self.assertRaises(DBusError) as cm:
            b.list_names()
        self.assertEqual(cm.exception.name, ERR + "Disconnected")
        b.close()  # idempotent

    def test_bus_closing_connection(self):
        conn = self.mock.conn_of(self.bus.unique_name)
        conn.sock.shutdown(socket.SHUT_RDWR)
        with self.assertRaises(DBusError) as cm:
            self.bus.list_names()
        self.assertEqual(cm.exception.name, ERR + "Disconnected")

    def test_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = dbus_mini.main(["--address", self.mock.address, "--names"])
        self.assertEqual(rc, 0)
        self.assertIn("org.freedesktop.DBus", out.getvalue().split())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = dbus_mini.main(["--address", self.mock.address, "--call", "test.Echo", "/test",
                                 "test.Echo", "Echo", "sa{sv}(ib)ay",
                                 json.dumps(["x", {"k": 1.5, "b": True}, [1, False], [1, 2]])])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()),
                         ["x", {"k": 1.5, "b": True}, [1, False], [1, 2]])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = dbus_mini.main(["--address", self.mock.address, "--has-owner",
                                 "org.freedesktop.DBus"])
        self.assertEqual((rc, out.getvalue()), (0, "true\n"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = dbus_mini.main(["--address", self.mock.address, "--get", "test.Echo", "/test",
                                 "test.Props", "Flags"])
        self.assertEqual((rc, out.getvalue()), (0, "[1, 2]\n"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = dbus_mini.main(["--address", self.mock.address, "--call", "test.Echo", "/test",
                                 "test.Echo", "Fail"])
        self.assertEqual(rc, 1)
        self.assertIn("test.Echo.Error.Boom: kaboom", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dbus_mini.main([]), 2)

    def test_cli_bad_options(self):
        for argv in (["--seconds", "abc", "--names"], ["--names", "--seconds"],
                     ["--address"], ["--as-uid", "root", "--names"],
                     ["--as-uid"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = dbus_mini.main(["--address", self.mock.address] + argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("dbus_mini: --", err.getvalue())
        # Ctrl-C while --monitor blocks: quiet exit, no traceback
        orig = Bus.messages

        def interrupted(self, timeout=None):
            raise KeyboardInterrupt
        Bus.messages = interrupted
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = dbus_mini.main(["--address", self.mock.address, "--monitor"])
        finally:
            Bus.messages = orig
        self.assertEqual(rc, 1)


class MockBusVariants(unittest.TestCase):
    def test_fd_negotiation_refused(self):
        mock = MockBus(agree_fds=False)
        try:
            with Bus(mock.address) as bus:
                self.assertFalse(bus.fds_ok)
                self.assertTrue(bus.list_names())
                r, w = os.pipe()
                try:
                    with self.assertRaises(DBusError) as cm:
                        bus.call("test.Echo", "/test", "test.Echo", "Echo", "h", (w,))
                    self.assertEqual(cm.exception.name, ERR + "NotSupported")
                finally:
                    os.close(r)
                    os.close(w)
            with Bus(mock.address, want_fds=False) as bus:
                conn = mock.conn_of(bus.unique_name)
                self.assertEqual(conn.auth_lines[1], "BEGIN")
        finally:
            mock.close()

    def test_send_timeout_when_peer_stops_reading(self):
        mock = MockBus()
        try:
            bus = Bus(mock.address, timeout=0.5)
            bus.call("test.Echo", "/test", "test.Echo", "Stall")
            t0 = time.monotonic()
            with self.assertRaises(DBusError) as cm:
                bus.call("test.Echo", "/test", "test.Echo", "Echo", "ay", (bytes(4 << 20),),
                         timeout=5)
            self.assertEqual(cm.exception.name, ERR + "Disconnected")
            self.assertIn("timed out", cm.exception.message)
            self.assertLess(time.monotonic() - t0, 3)
            self.assertIsNone(bus.sock)  # half a frame went out: connection dropped
        finally:
            mock.stall.set()
            mock.close()

    def test_connect_timeout_when_backlog_full(self):
        d = tempfile.mkdtemp(prefix="dbus-mini-")
        path = os.path.join(d, "bus")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(0)
        filler = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        filler.connect(path)  # occupies the whole backlog; nobody accepts
        try:
            for kw in ({}, {"as_uid": os.getuid()}):
                t0 = time.monotonic()
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    with self.assertRaises(DBusError) as cm:
                        Bus("unix:path=" + path, timeout=0.3, **kw)
                self.assertEqual(cm.exception.name, ERR + "NoServer", kw)
                self.assertIn("timed out", cm.exception.message)
                self.assertLess(time.monotonic() - t0, 3, kw)
        finally:
            filler.close()
            srv.close()
            shutil.rmtree(d, ignore_errors=True)

    def test_runtime_address(self):
        mock = MockBus()
        saved = os.environ.get("XDG_RUNTIME_DIR")
        try:
            os.environ["XDG_RUNTIME_DIR"] = mock.dir
            with Bus("unix:runtime=yes") as bus:
                self.assertTrue(bus.list_names())
            del os.environ["XDG_RUNTIME_DIR"]
            with self.assertRaises(DBusError) as cm:
                Bus("unix:runtime=yes")
            self.assertIn("XDG_RUNTIME_DIR", cm.exception.message)
        finally:
            if saved is not None:
                os.environ["XDG_RUNTIME_DIR"] = saved
            mock.close()

    def test_auth_rejected(self):
        mock = MockBus(reject_auth=True)
        try:
            with self.assertRaises(DBusError) as cm:
                Bus(mock.address)
            self.assertEqual(cm.exception.name, ERR + "AuthFailed")
            self.assertIn("REJECTED EXTERNAL", cm.exception.message)
        finally:
            mock.close()

    def test_a_bus_that_dies_mid_auth_is_a_dbus_error(self):
        """authenticate() talks to the socket with raw sendall/recv, so the
        ConnectionResetError from a peer that never drains our AUTH line used
        to escape past Bus() -- and past backend_detect.session_bus(), which
        catches DBusError only -- as a traceback out of wdotool and wwmctl."""
        bus = MockBus(die_mid_auth=1)
        try:
            with self.assertRaises(DBusError) as cm:
                Bus(bus.address, timeout=5.0)
            self.assertEqual(cm.exception.name, ERR + "Disconnected")
            self.assertIn("during authentication", cm.exception.message)
            with Bus(bus.address) as ok:          # and the bus is still usable
                self.assertTrue(ok.list_names())
        finally:
            bus.close()

    def test_a_bus_that_dies_mid_auth_still_lets_root_retry_as_the_owner(self):
        """The documented euid-0 retry fires on AuthFailed, Disconnected and
        AccessDenied; a bare OSError defeated it, so a root caller never got
        to the fork path at all. Real privileges are not needed: geteuid and
        the setuid step are stubbed, the child authenticates as the owner uid
        the retry chose."""
        owner = os.getuid() or 424242            # never 0: root gets no retry
        bus = MockBus(die_mid_auth=1)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)  # fork()
                with mock.patch.object(dbus_mini.os, "geteuid", lambda: 0), \
                     mock.patch.object(dbus_mini, "_drop_privileges", lambda uid: None), \
                     mock.patch.object(Bus, "_owner_uid", lambda self: owner):
                    conn = Bus(bus.address, timeout=5.0)
            with conn:
                self.assertEqual(conn.auth_path, "fork")
                self.assertIn(conn.unique_name, conn.list_names())
                self.assertEqual(bus.conn_of(conn.unique_name).auth_lines[0],
                                 "AUTH EXTERNAL " + str(owner).encode().hex())
        finally:
            bus.close()

    def test_abstract_and_escaped_and_multi_address(self):
        mock = MockBus(abstract=True)
        try:
            with Bus(mock.address) as bus:
                self.assertTrue(bus.list_names())
        finally:
            mock.close()
        mock = MockBus(subdir="a b,c")
        try:
            esc = "unix:path=" + mock.path.replace(" ", "%20").replace(",", "%2c")
            with Bus(esc) as bus:
                self.assertTrue(bus.list_names())
            with Bus("unix:path=/nonexistent/bus;" + esc) as bus:
                self.assertTrue(bus.list_names())
        finally:
            mock.close()
        with self.assertRaises(DBusError) as cm:
            Bus("unix:path=/nonexistent/bus")
        self.assertEqual(cm.exception.name, ERR + "NoServer")
        with self.assertRaises(DBusError) as cm:
            Bus("tcp:host=localhost")
        self.assertIn("unsupported transport", cm.exception.message)


class ForkHandoff(unittest.TestCase):
    """connect_as_uid: auth + Hello in a forked child, socket back via
    SCM_RIGHTS. Run as our own uid (no privileges needed; the setuid step is
    skipped when uid == getuid())."""

    @classmethod
    def setUpClass(cls):
        cls.mock = MockBus()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def test_helper(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # fork() in a threaded process
            sock, unique, fds_ok, leftover = dbus_mini.connect_as_uid(self.mock.address,
                                                                      os.getuid())
        try:
            self.assertRegex(unique, r"^:1\.\d+$")
            self.assertTrue(fds_ok)
            self.assertFalse(sock.get_inheritable())  # would leak into every subprocess
            self.assertEqual(sock.gettimeout(), 10.0)  # O_NONBLOCK came along: state matches
            # NameAcquired follows the Hello reply: whatever the child had already
            # read comes back as leftover bytes, the rest is still on the socket
            buf = bytearray(leftover)
            sock.settimeout(3)
            while Message.frame_length(buf) is None or len(buf) < Message.frame_length(buf):
                buf += sock.recv(4096)
            m = Message.from_bytes(bytes(buf[:Message.frame_length(buf)]))
            self.assertEqual((m.member, m.args()), ("NameAcquired", (unique,)))
            conn = self.mock.conn_of(unique)
            self.assertEqual(conn.auth_lines[0],
                             "AUTH EXTERNAL " + str(os.getuid()).encode().hex())
        finally:
            sock.close()

    def test_bus_as_uid(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            bus = Bus(self.mock.address, as_uid=os.getuid())
        with bus:
            self.assertEqual(bus.auth_path, "fork")
            self.assertRegex(bus.unique_name, r"^:1\.\d+$")
            self.assertIn(bus.unique_name, bus.list_names())
            self.assertEqual(bus.wait_signal(None, "NameAcquired", 2).args(), (bus.unique_name,))
            self.assertEqual(bus.call("test.Echo", "/test", "test.Echo", "Echo", "s",
                                      ("via fork",)), ("via fork",))
            fd, _ = bus.call("test.Echo", "/test", "test.Echo", "GetFd")
            self.assertEqual(os.read(fd, 100), b"hello fd")
            self.assertFalse(os.get_inheritable(fd))
            os.close(fd)
            self.assertFalse(bus.sock.get_inheritable())

    def test_child_failure_is_reported(self):
        # the child's own error name and message come through, once
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with self.assertRaises(DBusError) as cm:
                Bus("unix:path=/nonexistent/bus", as_uid=os.getuid())
        self.assertEqual(cm.exception.name, ERR + "NoServer")
        self.assertIn("nonexistent", cm.exception.message)
        self.assertNotIn("DBusError", cm.exception.message)
        self.assertEqual(cm.exception.message.count("NoServer"), 0)
        mock = MockBus(reject_auth=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                with self.assertRaises(DBusError) as cm:
                    Bus(mock.address, as_uid=os.getuid())
            self.assertEqual(cm.exception.name, ERR + "AuthFailed")
            self.assertIn("REJECTED", cm.exception.message)
        finally:
            mock.close()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with self.assertRaises(DBusError) as cm:
                Bus(self.mock.address, as_uid=4242424)
        self.assertEqual(cm.exception.name, ERR + "Failed")
        self.assertIn("4242424 not found", cm.exception.message)
        if os.geteuid() != 0:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                with self.assertRaises(DBusError) as cm:
                    Bus(self.mock.address, as_uid=0)
            self.assertEqual(cm.exception.name, ERR + "AccessDenied")
            self.assertIn("needs root", cm.exception.message)

    def test_stuck_child_is_killed(self):
        # the parent's backstop must not turn into waitpid() on a hung child
        orig_connect, orig_grace = dbus_mini._connect_socket, dbus_mini._FORK_GRACE
        dbus_mini._connect_socket = lambda addr, timeout=None: time.sleep(60)
        dbus_mini._FORK_GRACE = 0.3
        try:
            t0 = time.monotonic()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                with self.assertRaises(DBusError) as cm:
                    dbus_mini.connect_as_uid(self.mock.address, os.getuid(), timeout=0.2)
            self.assertEqual(cm.exception.name, ERR + "NoServer")
            self.assertIn("killed", cm.exception.message)
            self.assertLess(time.monotonic() - t0, 2)
            with self.assertRaises(ChildProcessError):
                os.waitpid(-1, os.WNOHANG)  # killed and reaped: no child left
        finally:
            dbus_mini._connect_socket, dbus_mini._FORK_GRACE = orig_connect, orig_grace


# ---------------------------------------------------------------- real bus

def _real_bus():
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not addr:
        return None
    try:
        with Bus(addr):
            return addr
    except (DBusError, OSError, ValueError):
        return None


REAL_BUS = _real_bus()


@unittest.skipUnless(REAL_BUS, "no DBUS_SESSION_BUS_ADDRESS (run under dbus-run-session)")
class RealBus(unittest.TestCase):
    def setUp(self):
        self.bus = Bus(REAL_BUS)

    def tearDown(self):
        self.bus.close()

    def test_hello_and_names(self):
        self.assertRegex(self.bus.unique_name, r"^:\d+\.\d+$")
        names = self.bus.list_names()
        self.assertIn("org.freedesktop.DBus", names)
        self.assertIn(self.bus.unique_name, names)
        self.assertTrue(self.bus.name_has_owner("org.freedesktop.DBus"))
        self.assertFalse(self.bus.name_has_owner("org.fuckwayland.NoSuchName"))
        self.assertEqual(self.bus.get_name_owner(self.bus.unique_name), self.bus.unique_name)

    def test_ping_and_errors(self):
        self.bus.ping("org.freedesktop.DBus")
        with self.assertRaises(DBusError) as cm:
            self.bus._bus("NameHasOwner", "i", (1,))
        self.assertIn(cm.exception.name, (ERR + "InvalidArgs", ERR + "UnknownMethod"))
        with self.assertRaises(DBusError) as cm:
            self.bus.call("org.fuckwayland.NoSuchName", "/", "x.y", "Z")
        self.assertEqual(cm.exception.name, ERR + "ServiceUnknown")

    def test_properties_and_introspect(self):
        xml = self.bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
        self.assertIn("<node", xml)
        self.assertIn('name="ListNames"', xml)
        try:
            feats = self.bus.get_all_properties("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                                "org.freedesktop.DBus")
        except DBusError as e:  # dbus-daemon < 1.11.x has no properties
            self.skipTest(f"bus has no properties: {e}")
        self.assertIsInstance(feats.get("Interfaces"), list)

    def test_name_owner_changed_via_wait_signal(self):
        name = "org.fuckwayland.DbusMiniTest%d" % os.getpid()
        self.bus.add_match(f"type='signal',sender='org.freedesktop.DBus',"
                           f"interface='org.freedesktop.DBus',member='NameOwnerChanged',"
                           f"arg0='{name}'")
        seen = {}

        def owner():
            with Bus(REAL_BUS) as b:
                seen["unique"] = b.unique_name
                seen["code"] = b.request_name(name, dbus_mini.NAME_FLAG_DO_NOT_QUEUE)
                seen["acquired"] = b.wait_signal("org.freedesktop.DBus", "NameAcquired", 5,
                                                 sender="org.freedesktop.DBus")
                time.sleep(0.3)
        t = threading.Thread(target=owner)
        t.start()
        m = self.bus.wait_signal("org.freedesktop.DBus", "NameOwnerChanged", 5)
        t.join(5)
        self.assertIsNotNone(m)
        self.assertEqual(seen["code"], 1)
        self.assertEqual(m.args(), (name, "", seen["unique"]))
        self.assertEqual(m.sender, "org.freedesktop.DBus")
        gone = self.bus.wait_signal("org.freedesktop.DBus", "NameOwnerChanged", 5)
        self.assertEqual(gone.args(), (name, seen["unique"], ""))
        self.assertFalse(self.bus.name_has_owner(name))

    def test_messages_generator_and_cli(self):
        self.bus.add_match("type='signal',interface='org.freedesktop.DBus'")
        with Bus(REAL_BUS):
            pass
        kinds = {m.member for m in self.bus.messages(0.5)}
        self.assertIn("NameOwnerChanged", kinds)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = dbus_mini.main(["--address", REAL_BUS, "--names"])
        self.assertEqual(rc, 0)
        self.assertIn("org.freedesktop.DBus", out.getvalue().split())


class NoBusText(unittest.TestCase):
    """"There is no bus here" and "the bus refused us" are different
    problems, and every backend that opens one has to say which it hit.

    The first is a login-session question -- a cron job, an `ssh` with no
    graphical session, a `sudo` that dropped DBUS_SESSION_BUS_ADDRESS -- and
    the answer is to point at the variable. The second is a permissions one:
    dbus-daemon under Ubuntu's session.conf answers root's EXTERNAL auth with
    OK and then closes the socket when the policy check runs, so the failure
    arrives as AuthFailed/Disconnected on a bus that plainly exists. Telling
    a user to "run inside the graphical session" there is a wrong answer to a
    question they did not ask."""

    def test_the_two_failures_do_not_read_the_same(self):
        no_server = dbus_mini.no_bus_text(
            DBusError(ERR + "NoServer", "cannot connect to unix:path=/run/user/1000/bus: "
                                        "[Errno 2] No such file or directory"))
        refused = dbus_mini.no_bus_text(
            DBusError(ERR + "AuthFailed", "bus closed the connection during auth"))
        self.assertNotEqual(no_server, refused)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS", no_server)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", refused)
        self.assertIn("AuthFailed", refused)
        self.assertIn("during auth", refused)

    def test_a_connect_failure_names_the_address_it_tried(self):
        """Which socket, in the exception the backends re-raise: with two
        runtime directories in play (`sudo` keeps root's, the session's bus
        is the user's) "no session D-Bus found" alone does not say which one
        was looked at, and the address is the whole of the difference."""
        addr = "unix:path=%s" % os.path.join(tempfile.gettempdir(),
                                             "fuckwayland-no-such-bus-%d" % os.getpid())
        with self.assertRaises(DBusError) as cm:
            Bus(addr, timeout=2.0)
        self.assertEqual(cm.exception.name, ERR + "NoServer")
        self.assertIn(addr, cm.exception.message)


@unittest.skipIf(REAL_BUS, "already running under a session bus: RealBus ran for real")
@unittest.skipUnless(shutil.which("dbus-run-session"), "no dbus-run-session on this box")
class RealBusUnderItsOwnDaemon(unittest.TestCase):
    """The RealBus class above is skipped unless DBUS_SESSION_BUS_ADDRESS is
    set, and on this box nothing sets it -- so its five tests, the only ones
    in the suite that talk to a real dbus-daemon rather than to MockBus, had
    not run in any ordinary `python3 tests/test_dbus_mini.py`. MockBus is
    written from the same reading of the specification as dbus_mini itself,
    so a shared misreading (a field alignment, an auth reply, a serial rule)
    would pass both sides.

    `dbus-run-session` starts a private daemon, exports the address into the
    child, and takes the daemon down with it, so the five run here with no
    session and nothing left behind. Measured on this host: 0.8 s for the
    five, 1.0 s including the daemon."""

    def test_the_real_bus_tests_pass_under_dbus_run_session(self):
        p = subprocess.run(["dbus-run-session", "--", sys.executable, "-m",
                            "unittest", "-v", "tests.test_dbus_mini.RealBus"],
                           cwd=ROOT, capture_output=True, text=True, timeout=300,
                           env=dict(os.environ, FUCKWAYLAND_PASSTHROUGH="never"))
        self.assertEqual(p.returncode, 0, p.stderr[-3000:])
        self.assertIn("Ran 5 tests", p.stderr)
        self.assertNotIn("skipped", p.stderr)
        self.assertNotIn("no DBUS_SESSION_BUS_ADDRESS", p.stderr)


if __name__ == "__main__":
    unittest.main()
