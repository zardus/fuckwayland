"""Pure-stdlib D-Bus client for the user session bus (and any `unix:` address,
e.g. QEMU's `-display dbus` bus). No glib, no gdbus/busctl spawns, signals
included. One `Bus` per thread; not thread-safe.

Wire facts (dbus-specification, "Message Format" / "Authentication Protocol"):

- Address: `unix:path=/run/user/1000/bus`, `unix:abstract=NAME` or
  `unix:runtime=yes` ($XDG_RUNTIME_DIR/bus); several alternatives joined by
  `;`, key=value pairs joined by `,`, values percent-escaped.
- Auth (text lines, `\\r\\n`): client sends a NUL byte, then
  `AUTH EXTERNAL <hex of the ascii uid>` -> `OK <guid>` (or `REJECTED mechs`
  / `ERROR`); `NEGOTIATE_UNIX_FD` -> `AGREE_UNIX_FD` or `ERROR`; `BEGIN`; from
  then on binary messages. The bus learns our uid from SO_PEERCRED, fixed
  when the socket was connected -- so a socket connected by a forked child
  running as another uid keeps that identity after SCM_RIGHTS hands it back.
- Message = 12-byte fixed header `y endian('l'|'B') y type y flags y version=1
  u body_len u serial` + header fields `a(yv)` + pad to 8 + body. Types:
  1 METHOD_CALL 2 METHOD_RETURN 3 ERROR 4 SIGNAL. Field codes: 1 PATH o,
  2 INTERFACE s, 3 MEMBER s, 4 ERROR_NAME s, 5 REPLY_SERIAL u, 6 DESTINATION s,
  7 SENDER s, 8 SIGNATURE g, 9 UNIX_FDS u. Flags: 1 NO_REPLY_EXPECTED,
  2 NO_AUTO_START. Max message 2**27 bytes.
- Alignment (relative to message start; the body starts 8-aligned so its own
  offset 0 works): y1 b4 n2 q2 i4 u4 x8 t8 d8 h4 s4 o4 g1 a4 (4-byte length,
  then pad to the element alignment -- NOT counted in the length, present even
  for empty arrays) struct/dict-entry 8, v1 (signature then value).
- Strings: u32 byte length + UTF-8 + NUL; signatures: u8 length + ascii + NUL.
  `b` is a u32 0/1. `h` is an index into the fds carried by SCM_RIGHTS on the
  same sendmsg; UNIX_FDS in the header says how many.
- Header fields have fixed signatures (PATH is `o`, REPLY_SERIAL is `u`, ...)
  and a call needs PATH+MEMBER, a signal PATH+INTERFACE+MEMBER, a reply
  REPLY_SERIAL, an error ERROR_NAME as well: anything else is a malformed
  frame (ValueError). Unknown field codes and unknown message types (5 and
  up; 0 is INVALID) must be ignored -- such frames are dropped, fds closed.
- Fds that arrive with a message belong to whoever takes it (`call()` returns
  them, `messages()`/`wait_signal()` yield them); they are CLOEXEC. Fds on
  frames the client discards (late replies, auto-answered calls, unknown
  types, anything still queued at `close()`) are closed here.
- Timeouts: `Bus(timeout=)` bounds connect, auth, Hello and every later
  send -- the socket stays in timeout mode, and a send that times out leaves
  a half-written frame, so the connection is closed (Disconnected).
  `call(timeout=)` bounds the wait for the reply (NoReply). AF_UNIX connect()
  waits in the kernel while the listener's backlog is full (a wedged
  dbus-daemon), which SO_SNDTIMEO bounds.

API sketch:

    with Bus() as bus:                        # session bus via session.find_user_bus()
        names = bus.list_names()
        (serial, mons, logical, props) = bus.call(
            "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
            "org.gnome.Mutter.DisplayConfig", "GetCurrentState")
        bus.add_match("type='signal',interface='org.gnome.Mutter.DisplayConfig'")
        sig = bus.wait_signal("org.gnome.Mutter.DisplayConfig", "MonitorsChanged", 5)
        bus.set_property(dest, path, iface, "PowerSaveMode", Variant("i", 3))
    Bus(as_uid=1000)                          # connect as that uid via a forked child
    Bus("unix:path=/srv/vm/display-bus")      # any unix: address
"""

import json
import os
import re
import select
import signal
import socket
import struct
import sys
import time
import warnings
from collections import deque

METHOD_CALL, METHOD_RETURN, ERROR, SIGNAL = 1, 2, 3, 4
NO_REPLY_EXPECTED, NO_AUTO_START = 1, 2
(F_PATH, F_INTERFACE, F_MEMBER, F_ERROR_NAME, F_REPLY_SERIAL, F_DESTINATION,
 F_SENDER, F_SIGNATURE, F_UNIX_FDS) = range(1, 10)
_FIELD_SIG = {F_PATH: "o", F_INTERFACE: "s", F_MEMBER: "s", F_ERROR_NAME: "s",
              F_REPLY_SERIAL: "u", F_DESTINATION: "s", F_SENDER: "s",
              F_SIGNATURE: "g", F_UNIX_FDS: "u"}
MAX_MESSAGE = 1 << 27
_FORK_GRACE = 5.0  # extra seconds the parent gives the uid-switching child
_REQUIRED_FIELDS = {METHOD_CALL: ("path", "member"),
                    SIGNAL: ("path", "interface", "member"),
                    METHOD_RETURN: ("reply_serial",),
                    ERROR: ("error_name", "reply_serial")}
DBUS_NAME, DBUS_PATH, DBUS_IFACE = ("org.freedesktop.DBus",
                                    "/org/freedesktop/DBus",
                                    "org.freedesktop.DBus")
PROPS_IFACE = "org.freedesktop.DBus.Properties"
ERR = "org.freedesktop.DBus.Error."

# RequestName flags / replies
NAME_FLAG_ALLOW_REPLACEMENT, NAME_FLAG_REPLACE_EXISTING, NAME_FLAG_DO_NOT_QUEUE = 1, 2, 4
REQUEST_NAME_REPLY_PRIMARY_OWNER = 1


class DBusError(Exception):
    """An ERROR reply (`name` like org.freedesktop.DBus.Error.UnknownMethod) or a local failure reported with
    the matching standard name: NoServer (connect), AuthFailed, NoReply (timeout), Disconnected."""

    def __init__(self, name: str, message: str = ""):
        super().__init__(f"{name}: {message}" if message else name)
        self.name = name
        self.message = message


def no_bus_text(e: DBusError) -> str:
    """Why the session bus could not be reached, in one line. "There is no bus here" and "the bus refused us"
    are different problems -- the first is a login-session question, the second a permissions one -- and every
    backend that opens a bus has to tell the user which it hit."""
    if e.name == ERR + "NoServer":
        return ("no session D-Bus found (set DBUS_SESSION_BUS_ADDRESS or run "
                "inside the graphical session / under sudo)")
    return "cannot connect to the session D-Bus: %s" % e


class Variant:
    """Explicitly typed value for `v` slots on the write side (and what the
    reader returns for `v` when wrap_variants=True)."""

    __slots__ = ("sig", "value")

    def __init__(self, sig: str, value):
        if not _is_single(sig):
            raise ValueError(f"variant signature {sig!r} is not one complete type")
        self.sig = sig
        self.value = value

    def __repr__(self):
        return f"Variant({self.sig!r}, {self.value!r})"

    def __eq__(self, other):
        return (isinstance(other, Variant) and other.sig == self.sig
                and other.value == self.value)

    __hash__ = None


# ---------------------------------------------------------------- signatures

_ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8,
          "d": 8, "h": 4, "s": 4, "o": 4, "g": 1, "a": 4, "(": 8, "{": 8, "v": 1}
_FMT = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q",
        "t": "Q", "d": "d", "h": "I"}
_BASIC = "ybnqiuxtdhsog"
_PATH_RE = re.compile(r"^(/|(/[A-Za-z0-9_]+)+)$")


def _type_end(sig: str, i: int) -> int:
    """Index just past the complete type starting at sig[i]."""
    if i >= len(sig):
        raise ValueError(f"signature {sig!r}: truncated")
    c = sig[i]
    if c in _BASIC or c == "v":
        return i + 1
    if c == "a":
        if i + 1 >= len(sig):
            raise ValueError(f"signature {sig!r}: array without element type")
        return _type_end(sig, i + 1)
    if c == "(":
        j = i + 1
        while j < len(sig) and sig[j] != ")":
            j = _type_end(sig, j)
        if j >= len(sig):
            raise ValueError(f"signature {sig!r}: unterminated struct")
        if j == i + 1:
            raise ValueError(f"signature {sig!r}: empty struct")
        return j + 1
    if c == "{":
        if i == 0 or sig[i - 1] != "a":
            raise ValueError(f"signature {sig!r}: dict entry outside array")
        if i + 1 >= len(sig) or sig[i + 1] not in _BASIC:
            raise ValueError(f"signature {sig!r}: dict key must be basic")
        j = _type_end(sig, i + 2)
        if j >= len(sig) or sig[j] != "}":
            raise ValueError(f"signature {sig!r}: dict entry must have exactly one value")
        return j + 1
    raise ValueError(f"signature {sig!r}: bad type code {c!r}")


def split_signature(sig: str) -> list[str]:
    """'ua{sv}(ii)' -> ['u', 'a{sv}', '(ii)']."""
    out, i = [], 0
    while i < len(sig):
        j = _type_end(sig, i)
        out.append(sig[i:j])
        i = j
    return out


def _is_single(sig: str) -> bool:
    try:
        return bool(sig) and _type_end(sig, 0) == len(sig)
    except ValueError:
        return False


def _align_of(t: str) -> int:
    return _ALIGN[t[0]]


# ---------------------------------------------------------------- marshalling

class _Writer:
    def __init__(self, endian: str = "<"):
        if endian not in ("<", ">"):
            raise ValueError(f"endian must be '<' or '>', not {endian!r}")
        self.e = endian
        self.buf = bytearray()
        self.fds: list[int] = []

    def pad(self, n: int):
        self.buf += b"\0" * (-len(self.buf) % n)

    def _pack(self, fmt: str, v, t: str):
        try:
            self.buf += struct.pack(self.e + fmt, v)
        except (struct.error, TypeError) as e:
            raise ValueError(f"cannot marshal {v!r} as {t!r}: {e}") from None

    def string(self, v, t: str):
        if not isinstance(v, str):
            raise ValueError(f"cannot marshal {v!r} as {t!r}: not a str")
        b = v.encode("utf-8")
        if b"\0" in b:
            raise ValueError(f"{t!r} value contains NUL")
        if t == "o" and not _PATH_RE.match(v):
            raise ValueError(f"invalid object path {v!r}")
        if t == "g":
            if len(b) > 255:
                raise ValueError("signature longer than 255 bytes")
            split_signature(v)
            self.buf += struct.pack("B", len(b)) + b + b"\0"
        else:
            self.pad(4)
            self.buf += struct.pack(self.e + "I", len(b)) + b + b"\0"

    def write(self, sig: str, values):
        """Marshal a sequence of values against a full signature."""
        types = split_signature(sig)
        if not isinstance(values, (list, tuple)):
            raise ValueError("args must be a list/tuple")
        if len(types) != len(values):
            raise ValueError(f"signature {sig!r} has {len(types)} types, got {len(values)} values")
        for t, v in zip(types, values):
            self.one(t, v)

    def one(self, t: str, v):
        c = t[0]
        if c in _FMT:
            self.pad(_ALIGN[c])
            if c == "b":
                if not isinstance(v, (bool, int)):
                    raise ValueError(f"cannot marshal {v!r} as 'b'")
                v = 1 if v else 0
            elif c == "h":
                if not isinstance(v, int) or v < 0:
                    raise ValueError(f"cannot marshal {v!r} as 'h': need an fd")
                self.fds.append(v)
                v = len(self.fds) - 1
            elif c == "d":
                v = float(v)
            elif isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"cannot marshal {v!r} as {t!r}")
            self._pack(_FMT[c], v, t)
        elif c in "sog":
            self.string(v, c)
        elif c == "a":
            self.array(t, v)
        elif c == "(":
            self.pad(8)
            fields = split_signature(t[1:-1])
            if not isinstance(v, (list, tuple)) or len(v) != len(fields):
                raise ValueError(f"cannot marshal {v!r} as struct {t!r}")
            for ft, fv in zip(fields, v):
                self.one(ft, fv)
        elif c == "v":
            if not isinstance(v, Variant):
                v = guess_variant(v)
            self.string(v.sig, "g")
            self.one(v.sig, v.value)
        else:
            raise ValueError(f"bad type {t!r}")

    def array(self, t: str, v):
        elem = t[1:]
        self.pad(4)
        slot = len(self.buf)
        self.buf += b"\0\0\0\0"
        self.pad(_align_of(elem))
        start = len(self.buf)
        if elem[0] == "{":
            kt, vt = split_signature(elem[1:-1])
            items = v.items() if isinstance(v, dict) else v
            for k, val in items:
                self.pad(8)
                self.one(kt, k)
                self.one(vt, val)
        elif elem == "y" and isinstance(v, (bytes, bytearray, memoryview)):
            self.buf += bytes(v)
        else:
            if isinstance(v, (str, bytes, dict)):
                raise ValueError(f"cannot marshal {v!r} as array {t!r}")
            for item in v:
                self.one(elem, item)
        n = len(self.buf) - start
        if n > (1 << 26):
            raise ValueError("array longer than 2**26 bytes")
        struct.pack_into(self.e + "I", self.buf, slot, n)


def guess_variant(v) -> Variant:
    """Variant for a plain Python value where the type is unambiguous: bool->b, int->i (x when out of range),
    float->d, str->s, bytes->ay, Variant passthrough. Lists/dicts/tuples need an explicit Variant."""
    if isinstance(v, Variant):
        return v
    if isinstance(v, bool):
        return Variant("b", v)
    if isinstance(v, int):
        return Variant("i" if -(1 << 31) <= v < (1 << 31) else "x", v)
    if isinstance(v, float):
        return Variant("d", v)
    if isinstance(v, str):
        return Variant("s", v)
    if isinstance(v, (bytes, bytearray)):
        return Variant("ay", bytes(v))
    raise ValueError(f"cannot guess a variant signature for {v!r}; wrap it in Variant(sig, value)")


#: The specification's container limit: 32 levels of array nesting and 32 of
#: variant nesting. A body of 1000 nested variants is only 3 KiB on the wire
#: and is legal type nesting, so without this it reaches Python's recursion
#: limit instead of the peer's error.
MAX_NESTING = 64


class _Reader:
    def __init__(self, data, endian: str = "<", fds=(), wrap_variants: bool = False,
                 pos: int = 0):
        if endian not in ("<", ">"):
            raise ValueError(f"endian must be '<' or '>', not {endian!r}")
        self.d = data
        self.e = endian
        self.fds = list(fds)
        self.wrap = wrap_variants
        self.i = pos
        self.depth = 0

    def _enter(self):
        self.depth += 1
        if self.depth > MAX_NESTING:
            raise ValueError("container nesting deeper than %d" % MAX_NESTING)

    def pad(self, n: int):
        self.i += -self.i % n
        if self.i > len(self.d):
            raise ValueError("message truncated (padding)")

    def _need(self, n: int):
        if self.i + n > len(self.d):
            raise ValueError(f"message truncated at offset {self.i}")

    def read(self, sig: str) -> tuple:
        return tuple(self.one(t) for t in split_signature(sig))

    def string(self, t: str) -> str:
        if t == "g":
            self._need(1)
            n = self.d[self.i]
            self.i += 1
        else:
            self.pad(4)
            self._need(4)
            (n,) = struct.unpack_from(self.e + "I", self.d, self.i)
            self.i += 4
        self._need(n + 1)
        raw = bytes(self.d[self.i:self.i + n])
        if self.d[self.i + n] != 0:
            raise ValueError("string not NUL-terminated")
        self.i += n + 1
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"invalid UTF-8 in {t!r}") from None
        if "\0" in s:
            raise ValueError("embedded NUL in string")
        if t == "o" and not _PATH_RE.match(s):
            raise ValueError(f"invalid object path {s!r}")
        return s

    def one(self, t: str):
        c = t[0]
        if c in _FMT:
            self.pad(_ALIGN[c])
            n = struct.calcsize(_FMT[c])
            self._need(n)
            (v,) = struct.unpack_from(self.e + _FMT[c], self.d, self.i)
            self.i += n
            if c == "b":
                if v > 1:
                    raise ValueError(f"invalid boolean {v}")
                return bool(v)
            if c == "h":
                if v >= len(self.fds):
                    raise ValueError(f"unix fd index {v} out of range")
                return self.fds[v]
            return v
        if c in "sog":
            return self.string(c)
        if c == "a":
            self._enter()
            try:
                return self.array(t)
            finally:
                self.depth -= 1
        if c == "(":
            self.pad(8)
            self._enter()
            try:
                return tuple(self.one(ft) for ft in split_signature(t[1:-1]))
            finally:
                self.depth -= 1
        if c == "v":
            sig = self.string("g")
            if not _is_single(sig):
                raise ValueError(f"variant signature {sig!r} is not one complete type")
            self._enter()
            try:
                v = self.one(sig)
            finally:
                self.depth -= 1
            return Variant(sig, v) if self.wrap else v
        raise ValueError(f"bad type {t!r}")

    def array(self, t: str):
        elem = t[1:]
        self.pad(4)
        self._need(4)
        (n,) = struct.unpack_from(self.e + "I", self.d, self.i)
        self.i += 4
        if n > (1 << 26):
            raise ValueError("array longer than 2**26 bytes")
        self.pad(_align_of(elem))
        end = self.i + n
        if end > len(self.d):
            raise ValueError("array runs past end of message")
        if elem[0] == "{":
            kt, vt = split_signature(elem[1:-1])
            out = {}
            while self.i < end:
                self.pad(8)
                k = self.one(kt)
                out[k] = self.one(vt)
        elif elem == "y":
            out = bytes(self.d[self.i:end])
            self.i = end
        else:
            out = []
            while self.i < end:
                out.append(self.one(elem))
        if self.i != end:
            raise ValueError("array length does not match its contents")
        return out


def marshal(sig: str, args, endian: str = "<") -> tuple[bytes, list[int]]:
    """(body bytes, fds to send) for `args` against `sig`; body offset 0 is
    8-aligned by construction."""
    w = _Writer(endian)
    w.write(sig, args)
    return bytes(w.buf), w.fds


def unmarshal(sig: str, data: bytes, endian: str = "<", fds=(),
              wrap_variants: bool = False) -> tuple:
    """Values for `sig` from a body; raises ValueError on malformed data
    (trailing bytes included)."""
    r = _Reader(data, endian, fds, wrap_variants)
    out = r.read(sig)
    if r.i != len(data):
        raise ValueError(f"{len(data) - r.i} trailing bytes after body")
    return out


# ---------------------------------------------------------------- messages

class Message:
    """One D-Bus message. `args(wrap_variants=False)` unmarshals the body."""

    __slots__ = ("type", "flags", "serial", "path", "interface", "member",
                 "error_name", "reply_serial", "destination", "sender",
                 "signature", "unix_fds", "endian", "body", "fds", "_args")

    def __init__(self, type: int, path=None, interface=None, member=None,
                 error_name=None, reply_serial=None, destination=None,
                 sender=None, signature="", body=b"", fds=(), flags=0,
                 serial=0, endian="<"):
        self.type, self.flags, self.serial = type, flags, serial
        self.path, self.interface, self.member = path, interface, member
        self.error_name, self.reply_serial = error_name, reply_serial
        self.destination, self.sender = destination, sender
        self.signature, self.body, self.fds = signature, body, list(fds)
        self.unix_fds, self.endian, self._args = len(self.fds), endian, {}

    def __repr__(self):
        kind = {1: "call", 2: "return", 3: "error", 4: "signal"}.get(self.type, self.type)
        return (f"<Message {kind} serial={self.serial} path={self.path} "
                f"iface={self.interface} member={self.member} "
                f"error={self.error_name} reply_to={self.reply_serial} "
                f"sender={self.sender} sig={self.signature!r}>")

    def args(self, wrap_variants: bool = False) -> tuple:
        if wrap_variants not in self._args:
            self._args[wrap_variants] = unmarshal(self.signature, self.body,
                                                  self.endian, self.fds,
                                                  wrap_variants)
        return self._args[wrap_variants]

    @classmethod
    def call(cls, dest, path, iface, member, sig="", args=(), flags=0):
        body, fds = marshal(sig, args)
        return cls(METHOD_CALL, path=path, interface=iface, member=member,
                   destination=dest, signature=sig, body=body, fds=fds,
                   flags=flags)

    @classmethod
    def method_return(cls, to: "Message", sig="", args=()):
        body, fds = marshal(sig, args)
        return cls(METHOD_RETURN, reply_serial=to.serial, destination=to.sender,
                   signature=sig, body=body, fds=fds)

    @classmethod
    def error(cls, to: "Message", name: str, text: str = ""):
        body, _ = marshal("s", (text,))
        return cls(ERROR, reply_serial=to.serial, destination=to.sender,
                   error_name=name, signature="s", body=body)

    @classmethod
    def signal(cls, path, iface, member, sig="", args=(), destination=None):
        body, fds = marshal(sig, args)
        return cls(SIGNAL, path=path, interface=iface, member=member,
                   destination=destination, signature=sig, body=body, fds=fds)

    def to_bytes(self, serial: int | None = None) -> bytes:
        """Serialize (little-endian). Header fields in libdbus order: PATH, DESTINATION, INTERFACE, MEMBER,
        ERROR_NAME, REPLY_SERIAL, SENDER, SIGNATURE, UNIX_FDS -- byte-identical to gdbus/libdbus for the common
        call shape."""
        if serial is not None:
            self.serial = serial
        if self.type == METHOD_CALL and not (self.path and self.member):
            raise ValueError("method call needs path and member")
        if self.type == SIGNAL and not (self.path and self.interface and self.member):
            raise ValueError("signal needs path, interface and member")
        if self.type in (METHOD_RETURN, ERROR) and self.reply_serial is None:
            raise ValueError("reply needs reply_serial")
        if self.type == ERROR and not self.error_name:
            raise ValueError("error needs error_name")
        fields = []
        for code, val in ((F_PATH, self.path), (F_DESTINATION, self.destination),
                          (F_INTERFACE, self.interface), (F_MEMBER, self.member),
                          (F_ERROR_NAME, self.error_name),
                          (F_REPLY_SERIAL, self.reply_serial),
                          (F_SENDER, self.sender),
                          (F_SIGNATURE, self.signature or None),
                          (F_UNIX_FDS, len(self.fds) or None)):
            if val is not None:
                fields.append((code, Variant(_FIELD_SIG[code], val)))
        w = _Writer("<")
        w.buf += struct.pack("<BBBBII", ord("l"), self.type, self.flags, 1,
                             len(self.body), self.serial)
        w.one("a(yv)", fields)
        w.pad(8)
        out = bytes(w.buf) + self.body
        if len(out) > MAX_MESSAGE:
            raise ValueError("message longer than 2**27 bytes")
        return out

    @staticmethod
    def frame_length(buf) -> int | None:
        """Total length of the message at buf[0:], or None if <16 bytes seen."""
        if len(buf) < 16:
            return None
        e = _endian_of(buf[0])
        body_len, _serial, fields_len = struct.unpack_from(e + "III", buf, 4)
        total = 16 + fields_len
        total += -total % 8
        total += body_len
        if total > MAX_MESSAGE:
            raise ValueError("message longer than 2**27 bytes")
        return total

    @classmethod
    def from_bytes(cls, data, fds=()) -> "Message":
        """Parse one complete message (header validated, body kept for `args()`); `fds` are the SCM_RIGHTS fds
        that came with it. Unknown message types (5+) parse -- the spec says ignore them, which is the
        receiver's job; type 0 (INVALID) and malformed headers raise."""
        e = _endian_of(data[0])
        r = _Reader(data, e, wrap_variants=True)
        _endian, mtype, flags, version, body_len, serial, fields = r.read("yyyyuua(yv)")
        if version != 1:
            raise ValueError(f"unsupported protocol version {version}")
        if mtype == 0:
            raise ValueError("message type 0 (INVALID)")
        r.pad(8)
        if r.i + body_len != len(data):
            raise ValueError("body length does not match frame")
        m = cls(mtype, flags=flags, serial=serial, endian=e)
        seen = set()
        for code, val in fields:
            if code in seen:
                raise ValueError(f"duplicate header field {code}")
            seen.add(code)
            if code not in _FIELD_SIG:
                continue  # unknown fields must be ignored
            if val.sig != _FIELD_SIG[code]:
                raise ValueError(f"header field {code} has signature {val.sig!r}, "
                                 f"expected {_FIELD_SIG[code]!r}")
            attr = {F_PATH: "path", F_INTERFACE: "interface", F_MEMBER: "member",
                    F_ERROR_NAME: "error_name", F_REPLY_SERIAL: "reply_serial",
                    F_DESTINATION: "destination", F_SENDER: "sender",
                    F_SIGNATURE: "signature", F_UNIX_FDS: "unix_fds"}[code]
            setattr(m, attr, val.value)
        for attr in _REQUIRED_FIELDS.get(mtype, ()):
            if getattr(m, attr) is None:
                raise ValueError(f"message type {mtype} without {attr.upper()} field")
        m.body = bytes(data[r.i:])
        m.fds = list(fds)
        if m.signature:
            split_signature(m.signature)
        elif body_len:
            raise ValueError("body without SIGNATURE field")
        return m


def _endian_of(b: int) -> str:
    if b == ord("l"):
        return "<"
    if b == ord("B"):
        return ">"
    raise ValueError(f"bad endianness byte {b!r}")


# ---------------------------------------------------------------- addresses

def parse_address(addr: str) -> list[dict[str, str]]:
    """'unix:path=/a%20b,guid=..;unix:abstract=x' ->
    [{'transport': 'unix', 'path': '/a b', 'guid': ...}, {...}]."""
    out = []
    for item in addr.split(";"):
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"bad D-Bus address {item!r}")
        transport, rest = item.split(":", 1)
        kv = {"transport": transport}
        for pair in rest.split(","):
            if not pair:
                continue
            if "=" not in pair:
                raise ValueError(f"bad D-Bus address element {pair!r}")
            k, v = pair.split("=", 1)
            kv[k] = re.sub(rb"%([0-9A-Fa-f]{2})",
                           lambda m: bytes([int(m.group(1), 16)]),
                           v.encode("utf-8")).decode("utf-8")
        out.append(kv)
    if not out:
        raise ValueError("empty D-Bus address")
    return out


def _unix_path(kv: dict[str, str]) -> str | None:
    """Filesystem path of a parsed unix: element (path= or runtime=yes)."""
    if "path" in kv:
        return kv["path"]
    if kv.get("runtime") == "yes" and os.environ.get("XDG_RUNTIME_DIR"):
        return os.path.join(os.environ["XDG_RUNTIME_DIR"], "bus")
    return None


def socket_path_of(addr: str) -> str | None:
    """Filesystem path of the first unix:path= (or unix:runtime=yes) element,
    else None."""
    for kv in parse_address(addr):
        if kv["transport"] == "unix" and _unix_path(kv):
            return _unix_path(kv)
    return None


def _connect_socket(addr: str, timeout: float | None = 10.0) -> socket.socket:
    """Connect to the first reachable element of `addr` within `timeout` seconds; the returned socket stays in
    timeout mode so every later send is bounded too."""
    last = None
    for kv in parse_address(addr):
        if kv["transport"] != "unix":
            last = ValueError(f"unsupported transport {kv['transport']!r}")
            continue
        if _unix_path(kv):
            target = _unix_path(kv)
        elif "abstract" in kv:
            target = "\0" + kv["abstract"]
        elif kv.get("runtime") == "yes":
            last = ValueError("unix:runtime=yes needs XDG_RUNTIME_DIR")
            continue
        else:
            last = ValueError("unix: address needs path=, abstract= or runtime=yes")
            continue
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if timeout is not None:
            # AF_UNIX connect() sleeps in the kernel (unix_wait_for_peer) while the listener's backlog is full
            # and returns EAGAIN once SO_SNDTIMEO expires; O_NONBLOCK would make it fail at once.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                         struct.pack("ll", int(timeout), int(timeout % 1 * 1e6)))
        try:
            s.connect(target)
        except BlockingIOError:
            s.close()
            last = TimeoutError(f"connect timed out after {timeout}s (listener not accepting)")
            continue
        except OSError as e:
            s.close()
            last = e
            continue
        s.settimeout(timeout)
        return s
    raise DBusError(ERR + "NoServer", f"cannot connect to {addr}: {last}")


# ---------------------------------------------------------------- auth

def _readline(sock: socket.socket, buf: bytearray, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while b"\r\n" not in buf:
        if len(buf) > 16384:
            raise DBusError(ERR + "AuthFailed", "auth line too long")
        r, _, _ = select.select([sock], [], [], max(0.0, deadline - time.monotonic()))
        if not r:
            raise DBusError(ERR + "AuthFailed", "timeout during authentication")
        chunk = sock.recv(4096)
        if not chunk:
            raise DBusError(ERR + "AuthFailed", "bus closed the connection during auth")
        buf += chunk
    line, _, rest = bytes(buf).partition(b"\r\n")
    del buf[:len(line) + 2]
    return line.decode("ascii", "replace")


def authenticate(sock: socket.socket, uid: int | None = None, timeout: float = 10.0,
                 want_fds: bool = True) -> tuple[str, bool, bytearray]:
    """SASL EXTERNAL + optional fd negotiation + BEGIN on a fresh socket.
    Returns (server guid, unix fds negotiated, leftover bytes)."""
    if uid is None:
        uid = os.geteuid()
    buf = bytearray()
    sock.sendall(b"\0AUTH EXTERNAL " + str(uid).encode().hex().encode() + b"\r\n")
    line = _readline(sock, buf, timeout)
    if line.startswith("DATA"):  # some servers ask again before OK
        sock.sendall(b"DATA\r\n")
        line = _readline(sock, buf, timeout)
    if not line.startswith("OK"):
        raise DBusError(ERR + "AuthFailed",
                        f"EXTERNAL auth as uid {uid} refused: {line.strip()}")
    guid = line[2:].strip()
    fds_ok = False
    if want_fds:
        sock.sendall(b"NEGOTIATE_UNIX_FD\r\n")
        line = _readline(sock, buf, timeout)
        fds_ok = line.startswith("AGREE_UNIX_FD")
        # REJECTED / ERROR: fine, we just do not use 'h'
    sock.sendall(b"BEGIN\r\n")
    return guid, fds_ok, buf


def _drop_privileges(uid: int):
    import pwd
    if os.getuid() == uid and os.geteuid() == uid:
        return
    try:
        gid = pwd.getpwuid(uid).pw_gid
    except KeyError:
        raise DBusError(ERR + "Failed", f"uid {uid} not found in the password database") from None
    try:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    except PermissionError as e:
        raise DBusError(ERR + "AccessDenied",
                        f"cannot switch to uid {uid} from uid {os.geteuid()}: {e} "
                        "(as_uid needs root)") from None
    if os.getuid() != uid or os.geteuid() != uid:
        raise DBusError(ERR + "Failed", f"could not switch to uid {uid}")


def connect_as_uid(addr: str, uid: int, timeout: float = 10.0):
    """Connect + authenticate + Hello as `uid` in a forked child and pass the live socket back over a socketpair
    with SCM_RIGHTS. The bus pinned the child's SO_PEERCRED at connect(), so the parent (typically root) now
    holds a connection the bus attributes to `uid`. Returns (socket, unique_name, fds_negotiated,
    leftover_bytes). A child failure comes back as a DBusError with the child's own error name (NoServer,
    AuthFailed, AccessDenied when not root, ...); a child that is still silent `timeout + _FORK_GRACE` seconds
    in is killed (it holds every inherited fd) and reported as NoServer."""
    parent, child = socket.socketpair()
    with warnings.catch_warnings():
        # 3.12 warns about fork() in threaded processes; the child only does
        # socket I/O and _exit()s, it never touches the interpreter's locks.
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:  # child
        status = b""
        fds = []
        try:
            parent.close()
            _drop_privileges(uid)
            s = _connect_socket(addr, timeout)
            guid, fds_ok, leftover = authenticate(s, uid, timeout)
            unique, leftover = _hello_raw(s, leftover, timeout)
            status = json.dumps({"ok": True, "unique": unique, "fds": fds_ok,
                                 "guid": guid, "leftover": leftover.hex()}).encode()
            fds = [s.fileno()]
        except DBusError as e:
            status = json.dumps({"ok": False, "name": e.name, "error": e.message}).encode()
        except BaseException as e:  # report everything to the parent
            status = json.dumps({"ok": False, "name": ERR + "Failed",
                                 "error": f"{type(e).__name__}: {e}"}).encode()
        try:
            socket.send_fds(child, [status], fds)
        finally:
            os._exit(0)
    child.close()
    data, fds, r = b"", [], []
    try:
        r, _, _ = select.select([parent], [], [], timeout + _FORK_GRACE)
        if r:
            data, fds, _flags, _addr = socket.recv_fds(parent, 65536, 1)
        else:
            os.kill(pid, signal.SIGKILL)
    finally:
        parent.close()
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    for fd in fds:
        os.set_inheritable(fd, False)  # recvmsg hands them over without CLOEXEC
    if not r:
        raise DBusError(ERR + "NoServer", f"uid-switching child did not answer within "
                                          f"{timeout + _FORK_GRACE:g}s (killed)")
    try:
        info = json.loads(data.decode() or "{}")
    except ValueError:
        info = {}
    if not info.get("ok") or not fds:
        _close_fds(fds)
        raise DBusError(info.get("name") or ERR + "Failed",
                        f"connecting as uid {uid}: {info.get('error') or 'child died'}")
    sock = socket.socket(fileno=fds[0])
    # O_NONBLOCK travelled with the open file description but socket(fileno=)
    # assumes a blocking fd: make the Python-side state match.
    sock.settimeout(timeout)
    return sock, info["unique"], info["fds"], bytearray.fromhex(info["leftover"])


def _close_fds(fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _hello_raw(sock: socket.socket, buf: bytearray, timeout: float) -> tuple[str, bytearray]:
    """Send Hello on a freshly authenticated socket and read the reply
    without a Bus object; returns (unique name, unread bytes)."""
    sock.sendall(Message.call(DBUS_NAME, DBUS_PATH, DBUS_IFACE, "Hello").to_bytes(1))
    deadline = time.monotonic() + timeout
    while True:
        n = Message.frame_length(buf)
        if n is not None and len(buf) >= n:
            m = Message.from_bytes(bytes(buf[:n]))
            del buf[:n]
            if m.type == ERROR and m.reply_serial == 1:
                raise DBusError(m.error_name, m.args()[0] if m.signature.startswith("s") else "")
            if m.type == METHOD_RETURN and m.reply_serial == 1:
                return m.args()[0], buf
            continue
        r, _, _ = select.select([sock], [], [], max(0.0, deadline - time.monotonic()))
        if not r:
            raise DBusError(ERR + "NoReply", "no reply to Hello")
        chunk = sock.recv(65536)
        if not chunk:
            raise DBusError(ERR + "Disconnected", "bus closed the connection after auth")
        buf += chunk


# ---------------------------------------------------------------- the client

class Bus:
    """A connection to one message bus.

    Bus(addr=None, as_uid=None, timeout=10.0): `addr` is a D-Bus address string; None means the graphical
    session's bus from `session.find_user_bus()`. `as_uid` forces the connection to be made by a forked child
    running as that uid (see `connect_as_uid`); with as_uid=None a root caller that the bus turns away retries
    the same way as the socket's owner. dbus-daemon (Ubuntu 24.04 session.conf, no `<allow user="*"/>`) answers
    root's EXTERNAL auth with OK and then silently closes the socket when the policy check runs, so the Hello
    dies with EPIPE/EOF -- AuthFailed, Disconnected and AccessDenied before Hello completes all trigger the
    retry. `auth_path` records which happened: 'direct' or 'fork'.

    `timeout` bounds connect, auth, Hello and every later send (see the module docstring); a send that times out
    closes the connection.

    Incoming messages that are not replies to our calls (signals; method calls aimed at our unique name) are
    queued for `messages()` / `wait_signal()`; fds they carry belong to the caller that takes them. With
    `serve_calls=False` (default) method calls are answered with UnknownMethod immediately (Peer.Ping with an
    empty return) so callers never wait 25 s on us; set it to True to reply yourself via `reply()` /
    `error_reply()`."""

    def __init__(self, addr: str | None = None, as_uid: int | None = None,
                 timeout: float = 10.0, want_fds: bool = True):
        if addr is None:
            from w11common import session
            hit = session.find_user_bus()
            if not hit:
                raise DBusError(ERR + "NoServer", "no session D-Bus found "
                                "(set DBUS_SESSION_BUS_ADDRESS or run under the "
                                "graphical session / sudo)")
            _owner, addr = hit
        self.address = addr
        self.timeout = timeout
        self.unique_name: str | None = None
        self.guid = ""
        self.fds_ok = False
        self.auth_path = "direct"
        self.serve_calls = False
        self._serial = 0
        self._buf = bytearray()
        self._pending_fds: list[int] = []
        self._waiting: set[int] = set()
        self._replies: dict[int, Message] = {}
        self._queue: deque[Message] = deque()
        self.sock: socket.socket | None = None
        if as_uid is not None:
            self._connect_fork(as_uid, timeout)
            return
        try:
            self._connect_direct(timeout, want_fds)
            (self.unique_name,) = self.call(DBUS_NAME, DBUS_PATH, DBUS_IFACE, "Hello",
                                            timeout=timeout)
        except DBusError as e:
            self.close()
            owner = self._owner_uid()
            retry = (ERR + "AuthFailed", ERR + "Disconnected", ERR + "AccessDenied")
            if e.name in retry and os.geteuid() == 0 and owner not in (None, 0):
                self._buf = bytearray()
                self._connect_fork(owner, timeout)
            else:
                raise

    @classmethod
    def connect(cls, addr: str | None = None, **kw) -> "Bus":
        return cls(addr, **kw)

    def _owner_uid(self) -> int | None:
        p = socket_path_of(self.address)
        try:
            return os.stat(p).st_uid if p else None
        except OSError:
            return None

    def _connect_direct(self, timeout: float, want_fds: bool):
        s = _connect_socket(self.address, timeout)
        try:
            self.guid, self.fds_ok, self._buf = authenticate(s, None, timeout, want_fds)
        except OSError as e:
            # authenticate() speaks to the socket with plain sendall/recv, so a bus that goes away mid-handshake
            # used to escape as a bare OSError -- past Bus() and past backend_detect.session_bus(), which
            # catches DBusError only, and out of wwmctl/wdotool as a traceback. Closing without draining our
            # AUTH line gives ECONNRESET on the read and EPIPE on the next write; a peer that stops reading
            # gives TimeoutError (also an OSError). All of them are the same event as a hangup after OK, and get
            # its name -- which is also one of the three the euid-0 retry acts on.
            s.close()
            raise DBusError(ERR + "Disconnected",
                            f"lost the connection during authentication: {e}") from None
        except BaseException:
            s.close()
            raise
        self.sock = s

    def _connect_fork(self, uid: int, timeout: float):
        self.sock, self.unique_name, self.fds_ok, self._buf = connect_as_uid(
            self.address, uid, timeout)
        self.auth_path = "fork"
        self._serial = 1  # the child used serial 1 for Hello

    # -- lifecycle

    def close(self):
        """Close the socket and every fd still held by unconsumed messages
        (queued signals/calls, unclaimed replies, fds not yet framed)."""
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None
        for m in list(self._queue) + list(self._replies.values()):
            _close_fds(m.fds)
            m.fds = []
        self._queue.clear()
        self._replies.clear()
        _close_fds(self._pending_fds)
        self._pending_fds = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def fileno(self) -> int:
        if self.sock is None:
            raise DBusError(ERR + "Disconnected", "bus connection is closed")
        return self.sock.fileno()

    # -- raw send / receive

    def send(self, msg: Message) -> int:
        """Serialize with the next serial and write it; returns the serial."""
        if self.sock is None:
            raise DBusError(ERR + "Disconnected", "bus connection is closed")
        self._serial += 1
        if self._serial >= (1 << 32):
            self._serial = 1
        data = msg.to_bytes(self._serial)
        try:
            if msg.fds:
                if not self.fds_ok:
                    raise DBusError(ERR + "NotSupported", "bus did not agree to unix fd passing")
                # the fds ride on the first bytes of the frame; sendmsg on a
                # socket in timeout mode may stop short on a big body
                n = socket.send_fds(self.sock, [data], msg.fds)
                if n < len(data):
                    self.sock.sendall(data[n:])
            else:
                self.sock.sendall(data)
        except TimeoutError:
            self.close()  # a partial frame is on the wire: nothing after it would parse
            raise DBusError(ERR + "Disconnected",
                            f"send of {msg.member} timed out after {self.timeout}s "
                            "(peer not reading); connection closed") from None
        except OSError as e:
            raise DBusError(ERR + "Disconnected", f"send failed: {e}") from None
        return self._serial

    def _recv_chunk(self):
        try:
            if self.fds_ok:
                data, fds, _flags, _addr = socket.recv_fds(self.sock, 65536, 64)
                for fd in fds:
                    os.set_inheritable(fd, False)  # recvmsg gives them without CLOEXEC
                self._pending_fds.extend(fds)
            else:
                data = self.sock.recv(65536)
        except OSError as e:
            raise DBusError(ERR + "Disconnected", f"recv failed: {e}") from None
        if not data:
            raise DBusError(ERR + "Disconnected", "bus closed the connection")
        self._buf += data

    def _parse_one(self) -> Message | None:
        # A frame we cannot even measure stays in the buffer for ever, and a frame we cannot parse leaves the
        # fds that came with it attached to the next message. Neither is recoverable -- the byte stream has no
        # resynchronisation point -- so the connection goes, as one clear DBusError rather than a ValueError out
        # of the marshaller.
        try:
            n = Message.frame_length(self._buf)
        except (ValueError, RecursionError) as e:
            self.close()
            raise DBusError(ERR + "Disconnected",
                            "malformed message from the bus: %s" % e) from None
        if n is None or len(self._buf) < n:
            return None
        frame = bytes(self._buf[:n])
        del self._buf[:n]
        try:
            m = Message.from_bytes(frame)
        except (ValueError, RecursionError, struct.error) as e:
            self.close()
            raise DBusError(ERR + "Disconnected",
                            "malformed message from the bus: %s" % e) from None
        if m.unix_fds:  # SCM_RIGHTS fds arrive with the recv that carried the frame
            m.fds = self._pending_fds[:m.unix_fds]
            del self._pending_fds[:m.unix_fds]
        return m

    def _dispatch(self, m: Message):
        if m.type in (METHOD_RETURN, ERROR):
            if m.reply_serial in self._waiting:
                self._waiting.discard(m.reply_serial)
                self._replies[m.reply_serial] = m
            else:
                _close_fds(m.fds)  # late reply after a timeout
            return
        if m.type not in (METHOD_CALL, SIGNAL):
            _close_fds(m.fds)  # unknown message types must be ignored
            return
        if m.type == METHOD_CALL and not self.serve_calls:
            _close_fds(m.fds)
            if not m.flags & NO_REPLY_EXPECTED:
                if m.interface == "org.freedesktop.DBus.Peer" and m.member == "Ping":
                    self.send(Message.method_return(m))
                else:
                    self.send(Message.error(m, ERR + "UnknownMethod",
                                            f"No such method {m.member!r}"))
            return
        self._queue.append(m)

    def _pump(self, timeout: float | None) -> bool:
        """Read and dispatch until at least one message arrived (True) or
        `timeout` seconds passed (False)."""
        if self.sock is None:
            raise DBusError(ERR + "Disconnected", "bus connection is closed")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            got = False
            while True:
                m = self._parse_one()
                if m is None:
                    break
                self._dispatch(m)
                got = True
            if got:
                return True
            wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            r, _, _ = select.select([self.sock], [], [], wait)
            if not r:
                return False
            self._recv_chunk()

    # -- calls

    def call_message(self, msg: Message, timeout: float | None = 25.0) -> Message:
        """Send a METHOD_CALL, return the reply Message (raises DBusError for
        an ERROR reply or NoReply on timeout)."""
        if msg.flags & NO_REPLY_EXPECTED:
            self.send(msg)
            return Message(METHOD_RETURN, reply_serial=msg.serial)
        serial = self.send(msg)
        self._waiting.add(serial)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while serial not in self._replies:
                wait = None if deadline is None else deadline - time.monotonic()
                if wait is not None and wait <= 0:
                    raise DBusError(ERR + "NoReply", f"no reply to {msg.member} within {timeout}s")
                self._pump(wait)
        finally:
            self._waiting.discard(serial)
        reply = self._replies.pop(serial)
        if reply.type == ERROR:
            text = ""
            if reply.signature.startswith("s"):
                try:
                    text = reply.args()[0]
                except ValueError:
                    pass
            raise DBusError(reply.error_name or ERR + "Failed", text)
        return reply

    def call(self, dest: str, path: str, iface: str | None, member: str,
             sig: str = "", args=(), timeout: float | None = 25.0,
             wrap_variants: bool = False, flags: int = 0) -> tuple:
        """Synchronous method call; returns the reply args as a tuple."""
        reply = self.call_message(Message.call(dest, path, iface, member, sig, args, flags),
                                  timeout)
        try:
            return reply.args(wrap_variants)
        except (ValueError, RecursionError, struct.error) as e:
            _close_fds(reply.fds)
            raise DBusError(ERR + "InvalidArgs",
                            "%s.%s replied with a body its own signature %r "
                            "does not describe: %s"
                            % (iface or "", member, reply.signature, e)) from None

    def reply(self, to: Message, sig: str = "", args=()) -> int:
        return self.send(Message.method_return(to, sig, args))

    def error_reply(self, to: Message, name: str, text: str = "") -> int:
        return self.send(Message.error(to, name, text))

    def emit_signal(self, path: str, iface: str, member: str, sig: str = "",
                    args=(), destination: str | None = None) -> int:
        return self.send(Message.signal(path, iface, member, sig, args, destination))

    # -- org.freedesktop.DBus helpers

    def _bus(self, member, sig="", args=(), timeout=25.0):
        return self.call(DBUS_NAME, DBUS_PATH, DBUS_IFACE, member, sig, args, timeout)

    def list_names(self) -> list[str]:
        return self._bus("ListNames")[0]

    def name_has_owner(self, name: str) -> bool:
        return self._bus("NameHasOwner", "s", (name,))[0]

    def get_name_owner(self, name: str) -> str:
        return self._bus("GetNameOwner", "s", (name,))[0]

    def request_name(self, name: str, flags: int = 0) -> int:
        """RequestName reply code: 1 primary owner, 2 in queue, 3 exists,
        4 already owner."""
        return self._bus("RequestName", "su", (name, flags))[0]

    def release_name(self, name: str) -> int:
        return self._bus("ReleaseName", "s", (name,))[0]

    def add_match(self, rule: str):
        self._bus("AddMatch", "s", (rule,))

    def remove_match(self, rule: str):
        self._bus("RemoveMatch", "s", (rule,))

    def ping(self, dest: str, timeout: float = 25.0):
        self.call(dest, "/", "org.freedesktop.DBus.Peer", "Ping", timeout=timeout)

    # -- properties / introspection

    def get_property(self, dest, path, iface, name, timeout=25.0, wrap_variants=False):
        return self.call(dest, path, PROPS_IFACE, "Get", "ss", (iface, name),
                         timeout, wrap_variants)[0]

    def set_property(self, dest, path, iface, name, value, timeout=25.0):
        """`value` is a Variant (or a plain bool/int/float/str/bytes)."""
        self.call(dest, path, PROPS_IFACE, "Set", "ssv",
                  (iface, name, guess_variant(value)), timeout)

    def get_all_properties(self, dest, path, iface, timeout=25.0, wrap_variants=False) -> dict:
        return self.call(dest, path, PROPS_IFACE, "GetAll", "s", (iface,),
                         timeout, wrap_variants)[0]

    def introspect(self, dest, path, timeout=25.0) -> str:
        return self.call(dest, path, "org.freedesktop.DBus.Introspectable",
                         "Introspect", timeout=timeout)[0]

    # -- signals / queue

    def messages(self, timeout: float | None = None):
        """Yield queued messages (signals, and calls when serve_calls) as they arrive; stops after `timeout`
        seconds of silence (None = forever). Fds in a yielded message (`m.args()`) are the caller's to close."""
        while True:
            while self._queue:
                yield self._queue.popleft()
            if not self._pump(timeout):
                return

    def wait_signal(self, iface: str | None, member: str | None,
                    timeout: float | None = 25.0, path: str | None = None,
                    sender: str | None = None) -> Message | None:
        """Next queued/incoming signal matching the given fields (None = any); other messages stay queued. None
        on timeout. Remember to `add_match` first -- the bus only routes subscribed signals. A signal's `sender`
        is always a unique name (`:1.42`) or `org.freedesktop.DBus`, never the well-known name."""
        def match(m):
            return (m.type == SIGNAL
                    and (iface is None or m.interface == iface)
                    and (member is None or m.member == member)
                    and (path is None or m.path == path)
                    and (sender is None or m.sender == sender))

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            for i, m in enumerate(self._queue):
                if match(m):
                    del self._queue[i]
                    return m
            wait = None if deadline is None else deadline - time.monotonic()
            if wait is not None and wait <= 0:
                return None
            if not self._pump(wait):
                return None


# ---------------------------------------------------------------- CLI

def _jsonable(v):
    if isinstance(v, Variant):
        return _jsonable(v.value)
    if isinstance(v, (bytes, bytearray)):
        return list(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return v


def _usage(out):
    out.write(
        "usage: python3 -m w11common.dbus_mini [--address ADDR] [--as-uid UID|owner] CMD\n"
        "  --names                                   ListNames\n"
        "  --has-owner NAME                          NameHasOwner\n"
        "  --call DEST PATH IFACE MEMBER [SIG JSON]  method call, args as a JSON list\n"
        "  --get DEST PATH IFACE PROP                Properties.Get\n"
        "  --get-all DEST PATH IFACE                 Properties.GetAll\n"
        "  --introspect DEST PATH                    Introspectable.Introspect\n"
        "  --monitor [RULE...] [--seconds N]         print signals (AddMatch each RULE;\n"
        "                                            default: all signals)\n")


def main(argv=None) -> int:
    """`_run` plus quiet exits: Ctrl-C (how --monitor ends) and a closed
    stdout pipe return 1 without a traceback, like wwmctl/cli.py."""
    try:
        rc = _run(argv)
        sys.stdout.flush()
        return rc
    except KeyboardInterrupt:
        return 1
    except BrokenPipeError:
        try:  # keep the interpreter's exit-time flush from raising again
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:
            pass
        return 1


def _run(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    addr, as_uid, seconds = None, None, None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--address", "--as-uid", "--seconds"):
            if i + 1 >= len(argv):
                print(f"dbus_mini: {a} needs a value", file=sys.stderr)
                return 2
            val = argv[i + 1]
            i += 2
            try:
                if a == "--address":
                    addr = val
                elif a == "--as-uid":
                    as_uid = val if val == "owner" else int(val)
                else:
                    seconds = float(val)
            except ValueError:
                print(f"dbus_mini: {a} wants a number, not {val!r}", file=sys.stderr)
                return 2
        elif a in ("-h", "--help"):
            _usage(sys.stdout)
            return 0
        else:
            rest.append(a)
            i += 1
    if not rest:
        _usage(sys.stderr)
        return 2
    try:
        if as_uid == "owner":
            if addr is None:
                from w11common import session
                hit = session.find_user_bus()
                if not hit:
                    raise DBusError(ERR + "NoServer", "no session D-Bus found")
                as_uid, addr = hit
            else:
                p = socket_path_of(addr)
                as_uid = os.stat(p).st_uid if p else None
        cmd, args = rest[0], rest[1:]
        with Bus(addr, as_uid=as_uid) as bus:
            if cmd == "--names":
                for n in sorted(bus.list_names()):
                    print(n)
            elif cmd == "--has-owner" and len(args) == 1:
                print("true" if bus.name_has_owner(args[0]) else "false")
            elif cmd == "--call" and len(args) in (4, 6):
                sig = args[4] if len(args) == 6 else ""
                cargs = json.loads(args[5]) if len(args) == 6 else []
                out = bus.call(args[0], args[1], args[2] or None, args[3], sig, cargs)
                print(json.dumps(_jsonable(out)))
            elif cmd == "--get" and len(args) == 4:
                print(json.dumps(_jsonable(bus.get_property(*args))))
            elif cmd == "--get-all" and len(args) == 3:
                print(json.dumps(_jsonable(bus.get_all_properties(*args)), indent=1))
            elif cmd == "--introspect" and len(args) == 2:
                print(bus.introspect(*args))
            elif cmd == "--monitor":
                for rule in args or ["type='signal'"]:
                    bus.add_match(rule)
                for m in bus.messages(seconds):
                    try:
                        body = json.dumps(_jsonable(m.args()))
                    except ValueError as e:
                        body = f"<unparseable: {e}>"
                    print(f"{m.sender} {m.path} {m.interface}.{m.member} {m.signature} {body}",
                          flush=True)
            else:
                _usage(sys.stderr)
                return 2
    except DBusError as e:
        print(f"dbus_mini: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        raise  # stdout went away: main() exits quietly
    except (ValueError, OSError) as e:
        print(f"dbus_mini: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
