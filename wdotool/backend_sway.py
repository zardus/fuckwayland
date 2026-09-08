"""sway/i3 window backend over raw i3-ipc (no external tools).

Framing: b"i3-ipc" + u32 payload length + u32 message type + JSON payload. Window ids are sway node ids;
commands address nodes with [con_id=N]. Desktops are workspaces: xdotool desktop N == workspace number N+1."""

import json
import select
import socket
import struct
import time

from fwcommon import session
from fwcommon.errors import CmdError
from wdotool.backend import Window, WindowBackend, Workspace, warn
from wdotool.ctx import SoftCmdError

_MAGIC = b"i3-ipc"
RUN_COMMAND = 0
GET_WORKSPACES = 1
SUBSCRIBE = 2
GET_OUTPUTS = 3
GET_TREE = 4
_EVENT_BIT = 0x80000000
EVENT_WORKSPACE = _EVENT_BIT | 0
EVENT_WINDOW = _EVENT_BIT | 3

SCRATCHPAD_WS = "__i3_scratch"

# Deadline for the command socket only (see __init__). Generous: every reply is built in the compositor's own
# event loop, and GET_TREE on a busy desktop is the slow one.
IPC_TIMEOUT = 10.0


def _lost(e) -> CmdError:
    """Any wire-level failure of the i3/sway IPC socket, as one clear line."""
    return CmdError("sway backend: lost the connection to the compositor (%s)" % e)


def _wedged() -> CmdError:
    """Connected, and not answering: a compositor stuck in its own event loop
    accepts on the IPC socket (the kernel does that) and then says nothing."""
    return CmdError("sway backend: no answer from the compositor within %gs "
                    "(it is not responding)" % IPC_TIMEOUT)


class SwayBackend(WindowBackend):
    name = "sway"

    def __init__(self, sockpath: str | None = None):
        self.sockpath = sockpath or session.find_sway_socket()
        if not self.sockpath:
            raise CmdError(
                "sway backend: no sway/i3 IPC socket found (SWAYSOCK unset and "
                "no sway-ipc.* socket in any runtime dir)"
            )
        # Only the command socket gets a deadline: everything it ever waits for is the answer to a request we
        # have just sent, so silence means the compositor is wedged -- which used to hang every tool for ever.
        # The events socket is left blocking (_connect()'s default); select_window() and wxprop's -spy wait on
        # it for an event that may be minutes away.
        self.sock = self._connect(IPC_TIMEOUT)

    # -- wire ---------------------------------------------------------------

    def _connect(self, timeout: float | None = None) -> socket.socket:
        """A fresh IPC socket, carrying `timeout` once it is up.

        The connect() itself always gets IPC_TIMEOUT, whatever the caller wants afterwards. A compositor wedged
        inside its own event loop still has a *listening* socket -- the kernel queues connections for it without
        waking it -- so connecting looks fine right up to the moment the backlog fills. Measured here against a
        raw AF_UNIX socket with listen(3) that never accepts: four connects succeed, and the fifth blocks in the
        kernel for ever with no deadline of its own. With one, the fifth is EAGAIN (Linux answers a full
        AF_UNIX backlog that way rather than making us wait), retried until the deadline, and the tool says the
        compositor is not responding instead of hanging."""
        deadline = time.monotonic() + IPC_TIMEOUT
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                s.connect(self.sockpath)
                break
            except (TimeoutError, BlockingIOError):
                # both are OSError subclasses: this arm has to come first
                s.close()
                if time.monotonic() >= deadline:
                    raise _wedged() from None
                time.sleep(0.01)
            except OSError as e:
                s.close()
                raise CmdError("sway backend: cannot connect to %s: %s" % (self.sockpath, e)) from None
        s.settimeout(timeout)
        return s

    @staticmethod
    def _read_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise CmdError("sway backend: IPC connection closed")
            buf += chunk
        return buf

    @classmethod
    def _send(cls, sock, mtype: int, payload=b""):
        if isinstance(payload, str):
            payload = payload.encode()
        try:
            sock.sendall(_MAGIC + struct.pack("<II", len(payload), mtype) + payload)
        except TimeoutError:
            raise _wedged() from None
        except OSError as e:
            raise _lost(e) from None

    @classmethod
    def _recv(cls, sock):
        # A clean EOF is only the tidiest way for the compositor to go away: a session that ends mid-chain gives
        # ECONNRESET on the read and EPIPE on the next write, and a compositor that answers with something that
        # is not JSON gives ValueError. All three are the same event to the user -- one line, not a traceback
        # (B5). A compositor that is still there but not answering is a different event, and says so.
        try:
            hdr = cls._read_exact(sock, 14)
            if hdr[:6] != _MAGIC:
                raise CmdError("sway backend: bad IPC framing")
            length, mtype = struct.unpack("<II", hdr[6:])
            payload = cls._read_exact(sock, length) if length else b"null"
            return mtype, json.loads(payload.decode("utf-8", "replace"))
        except TimeoutError:
            # TimeoutError is an OSError: this arm has to come first, or a
            # compositor that is merely wedged reads as one that has gone.
            raise _wedged() from None
        except (OSError, struct.error, ValueError) as e:
            raise _lost(e) from None

    def _msg(self, mtype: int, payload=b""):
        self._send(self.sock, mtype, payload)
        while True:
            t, data = self._recv(self.sock)
            if not t & _EVENT_BIT:  # we never subscribe on this socket
                return data

    def run(self, command: str):
        results = self._msg(RUN_COMMAND, command)
        for r in results or []:
            if not r.get("success"):
                raise CmdError("sway: %s" % (r.get("error") or "command failed: %s" % command))

    # -- tree ---------------------------------------------------------------

    def _nodes(self):
        """[(node, Window, floating, ws_name)] for every view in the tree."""
        # wwmctl.core.windows() unpacks this exact tuple shape; keep in sync.
        tree = self._msg(GET_TREE)
        out = []

        def walk(node, ws_num, ws_name, floating):
            ntype = node.get("type")
            if ntype == "workspace":
                ws_num = node.get("num", -1)
                ws_name = node.get("name") or ""
            is_view = ntype in ("con", "floating_con") and (
                node.get("app_id") is not None
                or node.get("window_properties") is not None
                or (node.get("pid") and not node.get("nodes"))
            )
            if is_view:
                wp = node.get("window_properties") or {}
                rect = node.get("rect") or {}
                num = ws_num if isinstance(ws_num, int) else -1
                out.append((
                    node,
                    Window(
                        id=node["id"],
                        title=node.get("name") or "",
                        class_=node.get("app_id") or wp.get("class") or "",
                        # WM_CLASS instance of an X11 client under Xwayland; native Wayland views have none
                        # (search --classname then falls back to class_ = app_id).
                        instance=wp.get("instance") or "",
                        pid=node.get("pid") or 0,
                        x=rect.get("x", 0),
                        y=rect.get("y", 0),
                        w=rect.get("width", 0),
                        h=rect.get("height", 0),
                        focused=bool(node.get("focused")),
                        visible=bool(node.get("visible")),
                        desktop=num - 1 if num > 0 else -1,
                    ),
                    floating,
                    ws_name,
                ))
            for ch in node.get("nodes") or []:
                walk(ch, ws_num, ws_name, False)
            for ch in node.get("floating_nodes") or []:
                walk(ch, ws_num, ws_name, True)

        walk(tree, -1, "", False)
        return out

    def _node(self, wid: int):
        for node, win, floating, ws_name in self._nodes():
            if win.id == wid:
                return node, win, floating, ws_name
        raise CmdError("window %d not found" % wid)

    # -- WindowBackend ------------------------------------------------------

    def list(self) -> list[Window]:
        return [win for _n, win, _f, _w in self._nodes()]

    def activate(self, wid: int):
        self._node(wid)
        self.run("[con_id=%d] focus" % wid)

    def close(self, wid: int):
        self._node(wid)
        self.run("[con_id=%d] kill" % wid)

    @staticmethod
    def _deco_h(node) -> int:
        """Titlebar height: sway's move/resize address the whole container
        (decoration included) while we report/take the content rect."""
        return (node.get("deco_rect") or {}).get("height", 0)

    @staticmethod
    def _refuse_fullscreen(node, what: str):
        """sway silently ignores move/resize of fullscreen containers, which
        would make --sync spin forever; refuse up front instead."""
        if node.get("fullscreen_mode"):
            raise CmdError(
                "sway: cannot %s a fullscreen window "
                "(windowstate --remove FULLSCREEN first)" % what
            )

    def move_window(self, wid: int, x: int, y: int):
        node, _w, floating, _ws = self._node(wid)
        if not floating:
            raise SoftCmdError(
                "sway: cannot move a tiled window to an absolute position "
                "(floating enable it first)"
            )
        self._refuse_fullscreen(node, "move")
        self.run("[con_id=%d] move absolute position %d %d" % (wid, x, y - self._deco_h(node)))

    def resize(self, wid: int, w: int, h: int):
        node, win, floating, _ws = self._node(wid)
        if not floating:
            # `resize set` on a tiled container moves the split ratio: the window ends up some other size on the
            # axis the layout owns and unchanged on the other, which read as a silent partial success. Refuse it
            # the way a tiled move is refused.
            raise SoftCmdError(
                "sway: cannot resize a tiled window to an absolute size "
                "(floating enable it first)"
            )
        self._refuse_fullscreen(node, "resize")
        self.run("[con_id=%d] resize set %d px %d px" % (wid, w, h + self._deco_h(node)))
        # sway resizes floating windows around their center; xdotool (X11)
        # keeps the top-left corner fixed. Move it back where it was.
        node2 = self._node(wid)[0]
        self.run("[con_id=%d] move absolute position %d %d" % (wid, win.x, win.y - self._deco_h(node2)))

    def minimize(self, wid: int):
        self.unmap(wid)

    def map(self, wid: int):
        _n, _win, _f, ws_name = self._node(wid)
        if ws_name == SCRATCHPAD_WS:
            self.run("[con_id=%d] scratchpad show" % wid)

    def unmap(self, wid: int):
        _n, _win, _f, ws_name = self._node(wid)
        if ws_name != SCRATCHPAD_WS:
            self.run("[con_id=%d] move scratchpad" % wid)

    def is_mapped(self, wid: int) -> bool:
        """Mapped = not stashed in the scratchpad. A window on an unfocused workspace (or a background tab) is
        mapped but not visible; X11's map state is what windowmap/windowunmap --sync must wait on."""
        return self._node(wid)[3] != SCRATCHPAD_WS

    def raise_(self, wid: int):
        _n, _win, floating, _ws = self._node(wid)
        if floating:
            self.run("[con_id=%d] focus" % wid)
        else:
            warn("windowraise: tiled sway windows have no stacking order; ignoring")

    def lower(self, wid: int):
        self._node(wid)
        warn("windowlower: sway cannot lower windows; ignoring")

    def set_state(self, wid: int, state: str, action: int):
        node, _win, _f, ws_name = self._node(wid)
        word = {0: "disable", 1: "enable", 2: "toggle"}[action]
        if state == "FULLSCREEN":
            self.run("[con_id=%d] fullscreen %s" % (wid, word))
        elif state == "STICKY":
            self.run("[con_id=%d] sticky %s" % (wid, word))
        elif state == "DEMANDS_ATTENTION":
            if action == 2:
                word = "disable" if node.get("urgent") else "enable"
            self.run("[con_id=%d] urgent %s" % (wid, word))
        elif state == "HIDDEN":
            hidden = ws_name == SCRATCHPAD_WS
            if action == 2:
                action = 0 if hidden else 1
            if action == 1 and not hidden:
                self.run("[con_id=%d] move scratchpad" % wid)
            elif action == 0 and hidden:
                self.run("[con_id=%d] scratchpad show" % wid)
        else:
            raise CmdError("windowstate %s is not supported by the sway backend" % state)

    def window_desktop(self, wid: int) -> int:
        return self._node(wid)[1].desktop

    def set_window_desktop(self, wid: int, n: int):
        self._node(wid)
        self.run("[con_id=%d] move container to workspace number %d" % (wid, n + 1))

    def workspaces(self) -> "list[Workspace]":
        """One record per workspace, ascending. sway answers GET_WORKSPACES in creation order and numbers only
        the numbered ones, so the sort is on the raw `num` with the nameless-number workspaces last; the index
        is the desktop number the tools use (workspace number N+1 is desktop N), and a named workspace with no
        number is -1."""
        rows = sorted(self._msg(GET_WORKSPACES) or [],
                      key=lambda ws: (ws.get("num", -1) < 0,
                                      ws.get("num", -1),
                                      ws.get("name") or ""))
        out = []
        for ws in rows:
            num = ws.get("num", -1)
            rect = ws.get("rect") or {}
            out.append(Workspace(
                index=num - 1 if num > 0 else -1,
                name=ws.get("name") or "",
                active=bool(ws.get("focused")),
                work_area=(rect.get("x", 0), rect.get("y", 0),
                           rect.get("width", 0), rect.get("height", 0)),
            ))
        return out

    def move_to_current_desktop(self, wid: int) -> bool:
        self.window_desktop(wid)  # gone -> CmdError, exit 1
        self.run("[con_id=%d] move container to workspace current" % wid)
        return True

    def get_desktop(self) -> int:
        for ws in self._msg(GET_WORKSPACES):
            if ws.get("focused"):
                num = ws.get("num", -1)
                return num - 1 if num > 0 else -1
        return -1

    def set_desktop(self, n: int):
        self.run("workspace number %d" % (n + 1))

    def num_desktops(self) -> int:
        return len(self._msg(GET_WORKSPACES))

    def events(self, timeout: float | None = None, workspaces: bool = False):
        """(id, change) for every sway window event, in sway's own vocabulary (new, close, focus, title,
        fullscreen_mode, move, floating, urgent), on a connection of its own: a subscription and the command
        socket cannot share one, since every reply we wait for would arrive behind an unbounded queue of events.

        With `workspaces` the workspace stream is folded in as (0, "workspace") -- no view has node id 0 -- the
        way the GNOME and KWin backends fold theirs, so a root-level watcher (wxprop -root -spy) needs one
        stream only. Stops after `timeout` seconds of silence; None waits for ever, which is what both callers
        do."""
        s = self._connect()
        try:
            self._send(s, SUBSCRIBE,
                       b'["window","workspace"]' if workspaces
                       else b'["window"]')
            t, reply = self._recv(s)
            if t & _EVENT_BIT or not (isinstance(reply, dict)
                                      and reply.get("success")):
                raise CmdError("sway backend: subscribe to window events failed")
            while True:
                if timeout is not None and not select.select([s], (), (),
                                                             timeout)[0]:
                    return
                t, data = self._recv(s)
                if not isinstance(data, dict):
                    continue
                if t == EVENT_WINDOW:
                    wid = (data.get("container") or {}).get("id")
                    if wid is not None:
                        yield int(wid), str(data.get("change") or "")
                elif workspaces and t == EVENT_WORKSPACE:
                    yield 0, "workspace"
        finally:
            s.close()

    select_window_hint = "focus the target window to select it"

    def select_window(self) -> int:
        """sway/i3: wait for the next window-focus event.

        Not xdotool's semantics (the window under the pointer at the next button press) and knowingly so: sway's
        IPC has no interactive picker, no pointer position and no way to grab input from outside the compositor,
        so there is nothing to click *with*. Clicking the window that already has focus therefore does not end
        this wait -- focus it from another window, or use another selector. Fixing it properly needs a sway-side
        feature, not a client-side workaround."""
        for wid, change in self.events():
            if change == "focus":
                return wid
        raise CmdError("sway backend: the window event stream ended")

    def display_size(self) -> tuple[int, int]:
        boxes = []
        for o in self._msg(GET_OUTPUTS):
            if not o.get("active"):
                continue
            rect = o.get("rect") or {}
            boxes.append((rect.get("x", 0), rect.get("y", 0), rect.get("width", 0), rect.get("height", 0)))
        if not boxes:
            raise CmdError("sway backend: no active outputs")
        # Bounding box of the full layout; origins can be non-zero/negative.
        minx = min(x for x, _y, _w, _h in boxes)
        miny = min(y for _x, y, _w, _h in boxes)
        w = max(x + w for x, _y, w, _h in boxes) - minx
        h = max(y + h for _x, y, _w, h in boxes) - miny
        if not w or not h:
            raise CmdError("sway backend: no active outputs")
        return w, h
