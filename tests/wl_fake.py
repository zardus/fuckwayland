"""Wayland wire helpers for the compositor fakes, and the server base the
two that boot alike share.

Deliberately NOT built on `fwcommon/wayland_mini.py`. These fakes are the
oracle for that client: a fake that marshalled with the code under test
would agree with it by construction, and the one thing these tests exist to
prove -- that the bytes on the wire are the bytes the protocol asks for --
would prove nothing. Everything here packs by hand.

`Server` is the accept loop, the message framing and the two wl_display
requests, for the fakes whose `wl_display.sync` reply is the plain
callback.done + delete_id pair (test_vkbd, test_vptr). The other five
compositor fakes answer sync differently -- some never, one only after a
mode event, one out of a subprocess -- so they take the marshallers only
and keep their own loops.
"""

import os
import socket
import struct
import tempfile
import threading


# -- marshalling --------------------------------------------------------------

def pad(n: int) -> int:
    """Zero bytes of padding after an `n`-byte string or array payload."""
    return -n % 4


def wstr(s) -> bytes:
    """A wire string: length including the NUL, the bytes, then padding."""
    b = (s.encode() if isinstance(s, str) else s) + b"\0"
    return struct.pack("<I", len(b)) + b + b"\0" * pad(len(b))


def msg(oid: int, op: int, body: bytes = b"") -> bytes:
    """One wire message: object id, (size << 16) | opcode, body."""
    return struct.pack("<II", oid, ((8 + len(body)) << 16) | op) + body


def marshal(args) -> bytes:
    """Typed arguments to a message body, over wayland_mini's own type set:
    u (uint), i (int), f (wl_fixed), s (string)."""
    out = b""
    for kind, v in args:
        if kind == "u":
            out += struct.pack("<I", v & 0xFFFFFFFF)
        elif kind == "i":
            out += struct.pack("<i", v)
        elif kind == "f":
            out += struct.pack("<i", int(v * 256))
        elif kind == "s":
            out += wstr(v)
        else:
            raise ValueError("bad arg type %r" % (kind,))
    return out


def read_str(payload: bytes, off: int) -> tuple[str, int]:
    """The string at `off`, and the offset just past its padding."""
    (n,) = struct.unpack_from("<I", payload, off)
    return payload[off + 4:off + 4 + n - 1].decode(), off + 4 + n + pad(n)


def unpack_bind(body: bytes):
    """wl_registry.bind: (name, interface, version, new_id)."""
    (name,) = struct.unpack_from("<I", body)
    iface, off = read_str(body, 4)
    ver, new_id = struct.unpack_from("<II", body, off)
    return name, iface, ver, new_id


# -- the server ---------------------------------------------------------------

class Server:
    """A compositor fake on a real Wayland socket, one thread per client.

    A subclass says which globals it advertises (`advertise`), what a bind
    means (`on_bind`) and what to do with a request on one of its own
    objects (`on_request`); the manager interface named by `MANAGER` is
    advertised last, and only when `manager_version` is not None -- None is
    the compositor that does not implement the protocol at all.

    Subclasses set their own attributes *before* calling super().__init__():
    the accept loop starts inside it.
    """

    #: the interface `manager_version` gates, advertised after `advertise()`
    MANAGER = None
    PREFIX = "wdotool-wl-"
    BACKLOG = 4

    def __init__(self, manager_version=1):
        self.manager_version = manager_version
        self.dir = tempfile.mkdtemp(prefix=self.PREFIX)
        self.path = os.path.join(self.dir, "wayland-fake")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(self.BACKLOG)
        # A blocking accept() is not woken by close() on Linux, so poll
        # instead: the suite creates one of these per test and must not pay
        # a join timeout for each.
        self.srv.settimeout(0.05)
        self.connections = 0
        self.mgr_name = None     # registry name the manager was advertised as
        self.names = {}          # registry name -> (interface, nth of that)
        self.binds = []          # (interface, version)
        self.events = []         # every request on the object under test
        self.destroyed = 0
        self._registries = []    # (conn, registry id) per live client
        self._clients = []
        self._stop = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- what a subclass fills in
    def advertise(self):
        """[(interface, version)] to advertise before the manager."""
        return []

    def new_state(self) -> dict:
        """Extra per-connection state, on top of the registry id."""
        return {}

    def on_bind(self, conn, state, name, iface, version, new_id):
        """A wl_registry.bind for something other than the manager."""

    def on_request(self, conn, state, oid, opcode, body, fds):
        """A request on an object this server handed out."""

    # -- lifecycle
    def close(self):
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass
        self._thread.join(timeout=5)
        self.drop_clients()
        try:
            os.unlink(self.path)
        except OSError:
            pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass

    def withdraw_manager(self):
        """wl_registry.global_remove for the manager, on every live
        connection -- a compositor withdrawing the protocol mid-session."""
        with self._lock:
            regs = list(self._registries)
        for conn, reg in regs:
            self._send(conn, reg, 1, struct.pack("<I", self.mgr_name or 0))

    def drop_clients(self):
        """Hang up on everyone -- what a compositor restart looks like to a
        client that was holding one of our objects."""
        with self._lock:
            socks = list(self._clients)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    # -- the accept loop and the framing
    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self._clients.append(conn)
            self.connections += 1
            threading.Thread(target=self._client, args=(conn,),
                             daemon=True).start()

    def _client(self, conn):
        buf = b""
        fds: list[int] = []
        state = dict(self.new_state(), registry=None)
        try:
            while not self._stop:
                data, anc, _flags, _addr = conn.recvmsg(65536, 4096)
                if not data:
                    return
                for level, typ, fddata in anc:
                    if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                        n = len(fddata) // 4
                        fds.extend(struct.unpack("%di" % n, fddata[:n * 4]))
                buf += data
                while len(buf) >= 8:
                    oid, sizeop = struct.unpack_from("<II", buf)
                    size, opcode = sizeop >> 16, sizeop & 0xFFFF
                    if size < 8 or len(buf) < size:
                        break
                    body, buf = buf[8:size], buf[size:]
                    self._request(conn, state, fds, oid, opcode, body)
        except OSError:
            return
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass

    def _request(self, conn, state, fds, oid, opcode, body):
        if oid == 1 and opcode == 0:            # wl_display.sync(callback)
            (cb,) = struct.unpack_from("<I", body)
            self._send(conn, cb, 0, struct.pack("<I", 0))   # callback.done
            self._send(conn, 1, 1, struct.pack("<I", cb))   # delete_id
            return
        if oid == 1 and opcode == 1:            # wl_display.get_registry
            (reg,) = struct.unpack_from("<I", body)
            state["registry"] = reg
            self._advertise(conn, reg)
            with self._lock:
                self._registries.append((conn, reg))
            return
        if oid == state["registry"] and opcode == 0:   # wl_registry.bind
            name, iface, ver, new_id = unpack_bind(body)
            self.binds.append((iface, ver))
            self.on_bind(conn, state, name, iface, ver, new_id)
            return
        self.on_request(conn, state, oid, opcode, body, fds)

    def _advertise(self, conn, reg):
        name = 1
        seen: dict[str, int] = {}
        for iface, ver in self.advertise():
            self.names[name] = (iface, seen.get(iface, 0))
            seen[iface] = seen.get(iface, 0) + 1
            self._global(conn, reg, name, iface, ver)
            name += 1
        if self.manager_version is not None:
            self.mgr_name = name
            self.names[name] = (self.MANAGER, 0)
            self._global(conn, reg, name, self.MANAGER, self.manager_version)

    # -- wire helpers
    def _send(self, conn, oid, opcode, body=b""):
        try:
            conn.sendall(msg(oid, opcode, body))
        except OSError:
            pass

    def _global(self, conn, reg, name, iface, version):
        self._send(conn, reg, 0,
                   struct.pack("<I", name) + wstr(iface)
                   + struct.pack("<I", version))

    def _error(self, conn, oid, code, text):
        self._send(conn, 1, 0,
                   struct.pack("<II", oid, code) + wstr(text))


# -- registries, replayed from a recorded global list -------------------------

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
REGISTRY_DIR = os.path.join(FIXTURES, "registries")


def registry_fixture(name: str) -> "list[tuple[str, int]]":
    """The `(interface, version)` list recorded in tests/fixtures/registries/<name>.txt.

    One `<interface> <version>` per line, `#` comments carrying the provenance -- which compositor, which
    version, which recon report the dump came out of. Detection is a question about the registry and nothing
    else, so the fixtures are the whole answer: a test that asks "does this session look like COSMIC" replays
    cosmic-comp's own 53 globals rather than a hand-written pair."""
    rows = []
    with open(os.path.join(REGISTRY_DIR, name + ".txt"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            iface, _sp, ver = line.partition(" ")
            rows.append((iface, int(ver)))
    return rows


class RegistryServer(Server):
    """A compositor that advertises exactly one recorded registry and binds nothing.

    `manager_version=None` is the base's "this server implements no manager interface at all" path, which is
    what makes it usable for the detection tests: the whole point is that a COSMIC registry has no
    zwlr_foreign_toplevel_manager_v1 to bind, and a fake that quietly added one would answer the question the
    test is asking."""

    MANAGER = None
    PREFIX = "wdotool-wl-reg-"

    def __init__(self, fixture):
        self.fixture = fixture
        self.globals = registry_fixture(fixture)
        super().__init__(manager_version=None)

    def advertise(self):
        return self.globals


def registry_server(fixture: str) -> RegistryServer:
    """A `RegistryServer` over tests/fixtures/registries/<fixture>.txt."""
    return RegistryServer(fixture)


# -- ext_workspace_manager_v1 -------------------------------------------------

WS_MANAGER = "ext_workspace_manager_v1"

#: The three recorded workspace sets, as (name, state, capabilities, coordinates).
#: state bit 0 is `active`, capability 1 is `activate`.
#: labwc 0.9.3 with three configured names, no `coordinates` event at all
#: [M recon2/labwc.md §6a].
LABWC_WORKSPACES = (("one", 1, 1, None), ("two", 0, 1, None), ("three", 0, 1, None))
#: Budgie 10.10 over labwc with <desktops number="4"/> [M recon2/budgie.md].
BUDGIE_WORKSPACES = tuple(("Workspace %d" % i, 1 if i == 1 else 0, 1, None)
                          for i in range(1, 5))
#: COSMIC's two, which do carry coordinates [M recon2/cosmic.md §4].
COSMIC_WORKSPACES = (("1", 0, 1, (1,)), ("2", 0, 1, (2,)))

#: manager events / requests
_WSM_EV_GROUP, _WSM_EV_WORKSPACE, _WSM_EV_DONE = 0, 1, 2
_WSM_REQ_COMMIT, _WSM_REQ_STOP = 0, 1
#: group events
_WSG_EV_CAPABILITIES, _WSG_EV_WORKSPACE_ENTER = 0, 3
#: workspace events / requests
_WS_EV_ID, _WS_EV_NAME, _WS_EV_COORDINATES = 0, 1, 2
_WS_EV_STATE, _WS_EV_CAPABILITIES, _WS_EV_REMOVED = 3, 4, 5
_WS_REQ_ACTIVATE, _WS_REQ_DEACTIVATE = 1, 2


class WorkspaceServer:
    """Mixin: `ext_workspace_manager_v1` v1 over any of the servers here.

    The event order is the recorded one -- group, then each workspace with name, (coordinates,) state and
    capabilities, then the manager's `done`; labwc sends no `coordinates` event and COSMIC does, which is the
    whole reason a client may not order desktops by arrival alone.

    Server-allocated object ids start at 0xFF000000, which is where the live dump found them
    (`workspace_group 4278190080`). Every request lands in `ws_calls` as `(what, index-or-None)`, so a test can
    say that `set_desktop 2` sent `activate` on the third handle and then `commit`, in that order.

    Set `workspaces` before super().__init__(); the accept loop starts inside it."""

    workspaces = LABWC_WORKSPACES

    def __init__(self, *a, **kw):
        self.ws_calls = []
        self.ws_ids = []            # object id per workspace, in advertised order
        self.ws_group = None
        self.ws_mgr = None
        super().__init__(*a, **kw)

    def advertise(self):
        return list(super().advertise()) + [(WS_MANAGER, 1)]

    def on_bind(self, conn, state, name, iface, version, new_id):
        if iface == WS_MANAGER:
            self._ws_announce(conn, new_id)
            return
        super().on_bind(conn, state, name, iface, version, new_id)

    def on_request(self, conn, state, oid, opcode, body, fds):
        if oid == self.ws_mgr:
            if opcode == _WSM_REQ_COMMIT:
                self.ws_calls.append(("commit", None))
            elif opcode == _WSM_REQ_STOP:
                self.ws_calls.append(("stop", None))
            return
        if oid in self.ws_ids:
            idx = self.ws_ids.index(oid)
            if opcode == _WS_REQ_ACTIVATE:
                self.ws_calls.append(("activate", idx))
            elif opcode == _WS_REQ_DEACTIVATE:
                self.ws_calls.append(("deactivate", idx))
            else:
                self.ws_calls.append(("ws-%d" % opcode, idx))
            return
        super().on_request(conn, state, oid, opcode, body, fds)

    def _ws_announce(self, conn, mgr_id):
        self.ws_mgr = mgr_id
        oid = 0xFF000000
        self.ws_group = oid
        self._send(conn, mgr_id, _WSM_EV_GROUP, struct.pack("<I", oid))
        self._send(conn, oid, _WSG_EV_CAPABILITIES, struct.pack("<I", 0))
        self.ws_ids = []
        for name, ws_state, caps, coords in self.workspaces:
            oid += 1
            self.ws_ids.append(oid)
            self._send(conn, mgr_id, _WSM_EV_WORKSPACE, struct.pack("<I", oid))
            self._send(conn, self.ws_group, _WSG_EV_WORKSPACE_ENTER, struct.pack("<I", oid))
            self._send(conn, oid, _WS_EV_NAME, wstr(name))
            if coords is not None:
                arr = struct.pack("<%dI" % len(coords), *coords)
                self._send(conn, oid, _WS_EV_COORDINATES,
                           struct.pack("<I", len(arr)) + arr + b"\0" * pad(len(arr)))
            self._send(conn, oid, _WS_EV_STATE, struct.pack("<I", ws_state))
            self._send(conn, oid, _WS_EV_CAPABILITIES, struct.pack("<I", caps))
        self._send(conn, mgr_id, _WSM_EV_DONE)

    def set_active(self, index: int):
        """Move the active bit to `index` and re-send the two `state` events, the way a compositor answers an
        activate. A test that only recorded the request cannot tell an accepted one from an ignored one."""
        rows = []
        for i, (name, _st, caps, coords) in enumerate(self.workspaces):
            rows.append((name, 1 if i == index else 0, caps, coords))
        self.workspaces = tuple(rows)
        with self._lock:
            conns = list(self._clients)
        for conn in conns:
            for oid, row in zip(self.ws_ids, self.workspaces):
                self._send(conn, oid, _WS_EV_STATE, struct.pack("<I", row[1]))
            if self.ws_mgr:
                self._send(conn, self.ws_mgr, _WSM_EV_DONE)


    def remove(self, index: int):
        """Destroy one workspace the way a compositor does: `ext_workspace_handle_v1.removed` on that
        handle's own object, then the manager's `done`.  `self.ws_ids` is left alone, so it stays the
        oracle a test compares the client's `handles()` against -- a test that reached into the client and
        flipped its own row would be asserting against the reader it is testing."""
        with self._lock:
            conns = list(self._clients)
        oid = self.ws_ids[index]
        for conn in conns:
            self._send(conn, oid, _WS_EV_REMOVED)
            if self.ws_mgr:
                self._send(conn, self.ws_mgr, _WSM_EV_DONE)


class WorkspaceCompositor(WorkspaceServer, Server):
    """`ext_workspace_manager_v1` and nothing else -- labwc's and Budgie's shape for a desktops test."""

    MANAGER = None
    PREFIX = "wdotool-wl-ws-"

    def __init__(self, workspaces=LABWC_WORKSPACES, extra=()):
        self.workspaces = tuple(workspaces)
        self.extra = list(extra)
        super().__init__(manager_version=None)

    def advertise(self):
        return self.extra + super().advertise()


# -- COSMIC --------------------------------------------------------------------

EXT_TOPLEVEL_LIST = "ext_foreign_toplevel_list_v1"
COSMIC_INFO = "zcosmic_toplevel_info_v1"
COSMIC_MGR = "zcosmic_toplevel_manager_v1"
COSMIC_KEYBOARD = "zcosmic_keyboard_layout_manager_v1"

#: The three toplevels the live cosmic-comp published, identifier first
#: [M recon2/cosmic.md §4, cosmic_probe.py]. The identifiers are 32 base62
#: characters and are the only handle the protocol carries -- no pid, no X id,
#: no numeric id -- which is what `backend.mint_id` exists for.
COSMIC_TOPLEVELS = (
    ("LsUebsS7Qe8NowEoh7IP065Bbw8xd69L", "cosmicterm", "foot"),
    ("2eoqv7wMaz7wrTrUQORF0E7otL6j0vF9", "typedtest", "foot"),
    ("1S3D08Rq060fWxCEvHUpNhnRKQf31iaC", "cosmicxterm", "XTerm"),
)
#: `zcosmic_toplevel_manager_v1.capabilities`, exactly as it arrived: close,
#: activate, maximize, minimize, move_to_workspace. No 5 (fullscreen) and no 7
#: (sticky) [M cosmic.md §4].
COSMIC_CAPABILITIES = (1, 2, 3, 4, 6)

#: zcosmic_toplevel_handle_v1.state
CST_MAXIMIZED, CST_MINIMIZED, CST_ACTIVATED, CST_FULLSCREEN, CST_STICKY = 0, 1, 2, 3, 4

#: ext_foreign_toplevel_handle_v1 events
_EXT_EV_CLOSED, _EXT_EV_DONE, _EXT_EV_TITLE, _EXT_EV_APP_ID, _EXT_EV_IDENTIFIER = 0, 1, 2, 3, 4
#: zcosmic_toplevel_info_v1
_CI_REQ_STOP, _CI_REQ_GET = 0, 1
#: zcosmic_toplevel_handle_v1 events
_CH_EV_CLOSED, _CH_EV_DONE, _CH_EV_STATE, _CH_EV_GEOMETRY = 0, 1, 8, 9
#: `workspace_enter` (6) takes a `zcosmic_workspace_handle_v1`, which is the handle a client below info v3
#: holds; a v3 client holds `ext_workspace_handle_v1`s only, so cosmic-comp sends it `ext_workspace_enter`
#: (10) and never opcode 6 [R recon2/cosmic/tlinfo.rs:639-640: `raw_ext_workspace_handles` ->
#: `instance.ext_workspace_enter`].  The fake branches on the version the client actually bound, so a fake
#: binding v3 and sending 6 -- which is what it did until 2026-09-09 -- cannot happen again.
_CH_EV_WORKSPACE_ENTER = 6
_CH_EV_EXT_WORKSPACE_ENTER = 10
#: zcosmic_toplevel_manager_v1 events / requests
_CM_EV_CAPABILITIES = 0
_CM_REQUESTS = {
    1: "close", 2: "activate", 3: "set_maximized", 4: "unset_maximized",
    5: "set_minimized", 6: "unset_minimized", 7: "set_fullscreen",
    8: "unset_fullscreen", 9: "set_rectangle", 10: "move_to_workspace",
    11: "set_sticky", 12: "unset_sticky", 13: "move_to_ext_workspace",
}
#: which state a request sets (True) or clears (False)
_CM_STATE_EFFECT = {
    "set_maximized": (CST_MAXIMIZED, True), "unset_maximized": (CST_MAXIMIZED, False),
    "set_minimized": (CST_MINIMIZED, True), "unset_minimized": (CST_MINIMIZED, False),
    "set_fullscreen": (CST_FULLSCREEN, True), "unset_fullscreen": (CST_FULLSCREEN, False),
    "set_sticky": (CST_STICKY, True), "unset_sticky": (CST_STICKY, False),
    "activate": (CST_ACTIVATED, True),
}


class _CosmicTop:
    __slots__ = ("ext", "cosmic", "identifier", "title", "app_id", "states", "geometry", "closed")

    def __init__(self, ext, identifier, title, app_id):
        self.ext = ext
        self.cosmic = None
        self.identifier = identifier
        self.title = title
        self.app_id = app_id
        self.states: list[int] = []
        self.geometry = None
        self.closed = False


class CosmicCompositor(WorkspaceServer, Server):
    """cosmic-comp on the wire: the two toplevel protocols it has and the wlr one it does not.

    Everything here was driven by hand against a live cosmic-comp first (`recon2/cosmic/cosmic_probe.py`,
    `cosmic_act.py`, output in `toplevels.txt`), which is what makes it an oracle rather than a mirror of the
    backend: `set_maximized` came back as `state [0]`, `unset_maximized` as `[]` and `activate` as `[2]`, the
    manager announced `capabilities [1,2,3,4,6]` -- no fullscreen (5), no sticky (7) -- and the two workspaces
    were named `1` and `2` with coordinates `[1]` and `[2]` [M recon2/cosmic.md §4].

    What it deliberately does NOT advertise is half the point: no `zwlr_foreign_toplevel_manager_v1` (which is
    why every window command answered `no Wayland session found` on COSMIC) and no
    `zwlr_virtual_pointer_manager_v1` (which is why the pointer half needs uinput there).

    `geometry=None` is the recorded case: no `geometry` event was ever delivered in the nested rig, not in 4 s
    and not after a maximize, because cosmic-comp sends it only alongside `output_enter` or on a change. Pass
    `geometry=(x, y, w, h)` for the compositor that does send one."""

    MANAGER = None
    PREFIX = "wdotool-wl-cosmic-"
    workspaces = COSMIC_WORKSPACES

    #: what a COSMIC session's registry looks like, in the order the live one
    #: announced the four that matter [M cosmic.md §4 globals line]
    GLOBALS = ((EXT_TOPLEVEL_LIST, 1), (COSMIC_INFO, 3), (COSMIC_MGR, 4),
               (COSMIC_KEYBOARD, 1), ("zwp_virtual_keyboard_manager_v1", 1),
               ("wl_compositor", 6), ("wl_shm", 1), ("wl_output", 4), ("wl_seat", 9))

    def __init__(self, toplevels=COSMIC_TOPLEVELS, capabilities=COSMIC_CAPABILITIES,
                 geometry=None, layout_group=None, workspaces=COSMIC_WORKSPACES):
        self.rows = tuple(toplevels)
        self.capabilities = tuple(capabilities)
        self.geometry = geometry
        self.layout_group = layout_group
        self.workspaces = tuple(workspaces)
        self.tops: dict[int, _CosmicTop] = {}     # ext handle oid -> record
        self.by_cosmic: dict[int, _CosmicTop] = {}
        self.calls = []           # (request name, [object ids after the toplevel])
        self.list_oid = None
        self.info_oid = None
        self.info_bound = None       # the zcosmic_toplevel_info_v1 version the client bound
        self.mgr_oid = None
        self.seat_oid = None
        self.kbd_mgr = None
        self.kbd_layouts = []     # every zcosmic_keyboard_layout_v1 handed out
        self._next = 0xFE000000
        super().__init__(manager_version=None)

    def advertise(self):
        return list(self.GLOBALS) + super().advertise()

    def _alloc(self) -> int:
        self._next += 1
        return self._next

    # -- binds
    def on_bind(self, conn, state, name, iface, version, new_id):
        if iface == EXT_TOPLEVEL_LIST:
            self.list_oid = new_id
            self._announce(conn, new_id)
            return
        if iface == COSMIC_INFO:
            self.info_oid = new_id
            self.info_bound = version
            return
        if iface == COSMIC_MGR:
            self.mgr_oid = new_id
            arr = struct.pack("<%dI" % len(self.capabilities), *self.capabilities)
            self._send(conn, new_id, _CM_EV_CAPABILITIES,
                       struct.pack("<I", len(arr)) + arr + b"\0" * pad(len(arr)))
            return
        if iface == "wl_seat":
            self.seat_oid = new_id
            return
        if iface == COSMIC_KEYBOARD:
            self.kbd_mgr = new_id
            return
        super().on_bind(conn, state, name, iface, version, new_id)

    def _announce(self, conn, list_oid):
        """toplevel, then title / app_id / identifier / done -- the recorded order."""
        for identifier, title, app_id in self.rows:
            oid = self._alloc()
            rec = _CosmicTop(oid, identifier, title, app_id)
            self.tops[oid] = rec
            self._send(conn, list_oid, 0, struct.pack("<I", oid))
            self._send(conn, oid, _EXT_EV_TITLE, wstr(title))
            self._send(conn, oid, _EXT_EV_APP_ID, wstr(app_id))
            self._send(conn, oid, _EXT_EV_IDENTIFIER, wstr(identifier))
            self._send(conn, oid, _EXT_EV_DONE)

    # -- requests
    def on_request(self, conn, state, oid, opcode, body, fds):
        if oid == self.info_oid and opcode == _CI_REQ_GET:
            new_id, ext = struct.unpack_from("<II", body)
            rec = self.tops.get(ext)
            if rec is not None:
                rec.cosmic = new_id
                self.by_cosmic[new_id] = rec
                self._send_cosmic_state(conn, rec)
            return
        if oid == self.mgr_oid:
            self._manager_request(conn, opcode, body)
            return
        if self.kbd_mgr is not None and oid == self.kbd_mgr and opcode == 0:
            new_id = struct.unpack_from("<I", body)[0]
            self.kbd_layouts.append(new_id)
            if self.layout_group is not None:
                self._send(conn, new_id, 0, struct.pack("<I", self.layout_group))
            return
        super().on_request(conn, state, oid, opcode, body, fds)

    def _send_cosmic_state(self, conn, rec):
        arr = struct.pack("<%dI" % len(rec.states), *rec.states)
        if self.geometry is not None and rec.geometry is None:
            rec.geometry = self.geometry
        if rec.geometry is not None:
            x, y, w, h = rec.geometry
            self._send(conn, rec.cosmic, _CH_EV_GEOMETRY,
                       struct.pack("<Iiiii", 0, x, y, w, h))
        if self.ws_ids:
            op = (_CH_EV_EXT_WORKSPACE_ENTER if (self.info_bound or 0) >= 3
                  else _CH_EV_WORKSPACE_ENTER)
            self._send(conn, rec.cosmic, op, struct.pack("<I", self.ws_ids[0]))
        self._send(conn, rec.cosmic, _CH_EV_STATE,
                   struct.pack("<I", len(arr)) + arr + b"\0" * pad(len(arr)))
        self._send(conn, rec.cosmic, _CH_EV_DONE)

    def _manager_request(self, conn, opcode, body):
        name = _CM_REQUESTS.get(opcode, "op%d" % opcode)
        ids = list(struct.unpack_from("<%dI" % (len(body) // 4), body)) if body else []
        self.calls.append((name, ids))
        rec = self.by_cosmic.get(ids[0]) if ids else None
        if rec is None:
            return
        if name == "close":
            rec.closed = True
            self._send(conn, rec.cosmic, _CH_EV_CLOSED)
            self._send(conn, rec.ext, _EXT_EV_CLOSED)
            return
        effect = _CM_STATE_EFFECT.get(name)
        if effect is None:
            return
        bit, on = effect
        if on and bit not in rec.states:
            rec.states.append(bit)
        elif not on and bit in rec.states:
            rec.states.remove(bit)
        self._send_cosmic_state(conn, rec)
