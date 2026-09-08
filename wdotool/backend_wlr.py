"""wlr-foreign-toplevel window backend (zwlr_foreign_toplevel_management_v1) via wayland_mini.

The floor every wlroots compositor without an IPC socket falls back to: labwc, river, Wayfire's sessions
before the Wayfire backend, Budgie 10.10 and Xfce 4.20 on Wayland (both of which run labwc), LXQt on Wayland.
The protocol carries a title, an app id and four state bits and nothing else -- no geometry, no pid, no
stacking -- so the refusals here name that protocol rather than a compositor's layout policy.

Three things this backend does on top of the bare protocol, each for a measured defect:

* **Verify after act.** river 0.4 registers no listener for any handle request: `windowclose`,
  `windowactivate`, `windowminimize` and `windowstate --add FULLSCREEN` all returned 0 and changed nothing,
  while river-classic 0.3.17 really closed and really fullscreened [M recon2/river.md §2a, R `Window.zig`
  v0.4.8: `setTitle`/`setAppId`/`setActivated`/`destroy` are pushed at the handle and no `request_*` listener
  exists]. There is no version bit to gate on -- river advertises manager v3 exactly like sway -- so every
  mutating request waits up to VERIFY_TIMEOUT for the handle to say it happened and reports the silence.
* **Desktops over `ext_workspace_manager_v1`.** labwc, Budgie and Xfce-on-Wayland publish workspaces their
  own panels show while `wwmctl -d` refused [M recon2/labwc.md §6a, budgie.md, xfce-wayland.md]. Bound only
  when the global is there, so sway 1.11 and Wayfire 0.10 keep the old refusal [M wayfire.md §1.1].
* **X ids for XWayland windows.** An xterm whose real X id is `0x40000c` was listed as `0x000f4240` with a
  synthesized `xterm.xterm` for `xterm.XTerm`, on labwc, Budgie, Xfce-Wayland, Wayfire and Hyprland alike; a
  same-compositor control (one sway, native backend vs `WDOTOOL_BACKEND=wlr`) proved it is this backend and
  not the compositor [M labwc.md §4]. `views()` joins the toplevels to `_NET_CLIENT_LIST`.

Window ids are 1000000 + arrival order and are only stable within one wdotool process; unlike COSMIC's
`identifier` and Hyprland's `address` there is no handle to mint from [backend.mint_id]."""

import struct
import time

from fwcommon import session
from fwcommon.errors import CmdError
from fwcommon.wayland_mini import WlConn
from wdotool import ext_workspace, xid_match
from wdotool.backend import View, Window, WindowBackend, warn

BASE_ID = 1000000

#: How long a mutating request waits for the compositor to say it happened. Half a second: the state event
#: arrives inside the same roundtrip on every compositor that honours the request at all (sway, labwc,
#: river-classic), and a compositor that is going to ignore it is never going to answer.
VERIFY_TIMEOUT = 0.5

#: What "accepted and not applied" is called. The `WindowBackend.set_state` contract already carries this
#: shape for KWin's window rules. The first clause is fixed; the cause after it is per request, because one
#: sentence naming every cause is a false attribution on whichever compositor is not the one at fault.
READ_ONLY_REASON = ("the compositor accepted %s and did not apply it "
                    "(river 0.4's wlr-foreign-toplevel is read-only)")

#: The same, for the two requests that also go nowhere on compositors that are not read-only at all:
#: `WDOTOOL_BACKEND=wlr wdotool windowminimize` on a headless sway 1.11 waited the full VERIFY_TIMEOUT and
#: warned, correctly -- sway has no minimized state (measured on this guest, 2026-09-08) -- and
#: river-classic 0.3.17 does the same [M recon2/river.md §2a].
NO_MINIMIZE_REASON = ("the compositor accepted %s and did not apply it "
                      "(sway and river-classic have no minimized state; river 0.4's "
                      "wlr-foreign-toplevel is read-only)")

#: `close` has a third cause the other requests do not: the client itself. A window that answers close with
#: an unsaved-changes dialog changes nothing on its handle, and blaming river or sway for that on labwc --
#: where close was measured to work [M labwc.md §3] -- would be a lie about the compositor.
CLOSE_REASON = ("the window did not close within %.1f s: the client may be asking to save, or the "
                "compositor ignored the request (river 0.4)")


def read_only_reason(name: str) -> str:
    """The reason line for one request name, naming only the causes that can produce this silence."""
    if "minimized" in name:
        return NO_MINIMIZE_REASON % name
    return READ_ONLY_REASON % name

#: The refusal for the four commands the protocol has no request for. labwc is a *stacking* compositor and
#: still cannot move a window, so the reason names the protocol, not a tiling policy [M labwc.md §6c].
NO_GEOMETRY = "zwlr_foreign_toplevel_management_v1 carries no geometry and no stacking"

# zwlr_foreign_toplevel_handle_v1 state enum
_ST_MAXIMIZED = 0
_ST_MINIMIZED = 1
_ST_ACTIVATED = 2
_ST_FULLSCREEN = 3

# handle requests
_REQ_SET_MAXIMIZED = 0
_REQ_UNSET_MAXIMIZED = 1
_REQ_SET_MINIMIZED = 2
_REQ_UNSET_MINIMIZED = 3
_REQ_ACTIVATE = 4
_REQ_CLOSE = 5
_REQ_SET_FULLSCREEN = 8
_REQ_UNSET_FULLSCREEN = 9


class _Toplevel:
    __slots__ = ("oid", "title", "app_id", "states", "closed")

    def __init__(self, oid):
        self.oid = oid
        self.title = ""
        self.app_id = ""
        self.states: set[int] = set()
        self.closed = False


class XPlaneViews:
    """`views()` for the two foreign-toplevel backends: the Wayland listing joined to `_NET_CLIENT_LIST`.

    Both wlroots and cosmic-comp run an Xwayland whose windows the toplevel protocols report under a
    synthesized id, and neither protocol carries an X id, a pid or a rectangle -- so the join, its matcher
    and its failure rules are one piece of code with two users. `CosmicBackend` imports it from here rather
    than the other way round because the wlr floor is the older of the two.

    What a backend supplies: `self.uid` (the session's uid, for the X socket), `list()` and `_view_flags()`,
    a list of `View` keyword dicts parallel to the listing. The two calls are adjacent on purpose: both are
    built from the same records in arrival order, and no event can land between them because `wayland_mini`
    reads the socket only inside a roundtrip."""

    def views(self) -> "list[View] | None":
        """The listing with the X plane folded in, or None when there is no X plane to fold.

        The pairing has only the title and the app id against `WM_CLASS` -- no pid filter and no geometry
        score, which is why `match_xids` is called with `ratio=None` and with no `ix` key: the toplevel
        arrival order is not `_NET_CLIENT_LIST` order the way KWin's window list is, so using it as a
        tie-break would be a guess. The KWin rule stands unchanged: a pair must agree on something, and a tie
        nobody can break keeps xid 0 [M labwc.md §4, cosmic.md §5.4, xid_match.match_xids]."""
        wins = self.list()
        flags = self._view_flags()
        x = self._x11()
        if x is None:
            return None
        try:
            clients = self._x_clients(x)
        except Exception:   # any X failure: the floor listing, no crash
            self._drop_x(x)
            return None
        raw = [{"u": str(w.id), "c": w.class_, "n": "", "t": w.title} for w in wins]
        xids = xid_match.match_xids(raw, clients, None)
        by_xid = {c["xid"]: c for c in clients}
        out = []
        for w, fl in zip(wins, flags):
            xid = xids.get(str(w.id), 0)
            c = by_xid.get(xid)
            if c is None:
                # a native toplevel: `app_id` and no WM_CLASS pair, which is what makes wwmctl render it as
                # `foot.foot` rather than inventing an instance it never read
                out.append(View(window=w, app_id=w.class_, **fl))
                continue
            gx, gy, gw, gh = c["geo"]
            w.pid, w.x, w.y, w.w, w.h = int(c["pid"]), gx, gy, gw, gh
            w.instance = c["inst"]
            w.class_ = c["cls"] or w.class_
            out.append(View(window=w, xid=xid, instance=c["inst"], cls=c["cls"],
                            client_type="x11", **fl))
        return out

    def _x11(self):
        """The X connection, opened once, or None. Never starts an Xwayland: labwc Depends on xwayland and
        has one already, while sway and Wayfire spawn theirs on demand and a connect would be the spawn."""
        if self._x != "unset":
            return self._x
        self._x = None
        if not session.xwayland_running(self.uid):
            return None
        display = session.find_x_display(self.uid) or None
        xauth = session.find_xauthority(self.uid) or None
        try:
            from wdotool import x11_mini
            self._x = x11_mini.X11Conn(display, xauthority=xauth)
        except Exception:   # no X plane: every xid stays 0
            self._x = None
        return self._x

    def _drop_x(self, x):
        """Forget a connection that failed mid-read -- and close it first: wdotool's daemon and a wwmctl
        loop both outlive one views() call, and a dropped reference is a leaked fd there."""
        try:
            x.close()
        except OSError:
            pass
        self._x = None

    @staticmethod
    def _x_clients(x) -> "list[dict]":
        """The X clients as `xid_match` wants them, in `_NET_CLIENT_LIST` order."""
        out = []
        for xid in x.client_list():
            try:
                inst, cls = x.get_wm_class(xid)
                name = x.get_prop_string(xid, "_NET_WM_NAME") or x.get_prop_string(xid, "WM_NAME")
                geo = x.get_geometry(xid)
                pid = x.get_pid(xid)
            except Exception:   # a window that just died
                continue
            out.append({"xid": int(xid), "pid": int(pid), "inst": inst, "cls": cls,
                        "name": name, "geo": geo})
        return out


class WlrBackend(XPlaneViews, WindowBackend):
    name = "wlr"
    #: What every wlroots xwm puts on its `_NET_SUPPORTING_WM_CHECK` window -- read off the X plane on labwc,
    #: Wayfire and river, byte-identical to sway's [M labwc.md §3 wwmctl, wayfire.md §2.4, river.md §3]. So
    #: `wwmctl -m` from a root shell, or on a session with no Xwayland, says what the in-session run says
    #: instead of falling back to the backend token `wlr`.
    wm_name = "wlroots wm"

    def __init__(self, conn=None):
        """`conn` is a live `WlConn` whose registry has been read -- detection's, so a session opens one
        connection and not two. Without it this opens its own and owns it; with it the caller keeps it, and
        keeps it after a constructor failure too."""
        self._own_conn = conn is None
        self.uid = None
        if conn is None:
            hit = session.find_wayland_socket()
            if not hit:
                raise CmdError("wlr backend: no Wayland socket found")
            self.uid, _rd, sockpath = hit
            try:
                self.c = WlConn(sockpath)
            except OSError as e:
                raise CmdError("wlr backend: cannot connect to %s: %s" % (sockpath, e)) from None
        else:
            self.c = conn
        try:
            reg = self.c.get_registry()
            g = self.c.find_global("zwlr_foreign_toplevel_manager_v1")
        except (OSError, RuntimeError, struct.error) as e:
            self._close_own()
            raise CmdError("wlr backend: %s" % e) from None
        if not g:
            self._close_own()
            raise CmdError(
                "wlr backend: compositor does not offer "
                "zwlr_foreign_toplevel_management_unstable_v1"
            )
        self.tops: dict[int, _Toplevel] = {}  # handle oid -> record
        self.order: list[int] = []            # handle oids, arrival order
        self.mgr_ver = min(g[1], 3)
        self.mgr = self.c.bind(g[0], "zwlr_foreign_toplevel_manager_v1", self.mgr_ver)
        self.c.on(self.mgr, self._on_mgr)

        self.seat = None
        sg = self.c.find_global("wl_seat")
        if sg:
            self.seat = self.c.bind(sg[0], "wl_seat", min(sg[1], 2))
            self.c.on(self.seat, lambda op, cur, fds: None)

        self.out_w = self.out_h = 0
        for name, (iface, ver) in list(reg.items()):
            if iface == "wl_output":
                oid = self.c.bind(name, "wl_output", min(ver, 2))
                self.c.on(oid, self._on_output)

        # The workspace protocol is a separate global, and most of the family does not have it: None here is
        # what keeps sway 1.11's and Wayfire 0.10's desktop refusals exactly as they were.
        self.ws = ext_workspace.WorkspaceClient.bind(self.c)

        self._x = "unset"   # lazy X11Conn for views(); None once it is known there is none
        self._pump()  # toplevel announcements
        self._pump()  # each handle's initial title/app_id/state/done

    def _close_own(self):
        """Drop the connection only if this constructor opened it: a caller that handed one in still holds
        it, and closing it here would take detection's registry away from whoever asks next."""
        if self._own_conn:
            try:
                self.c.close()
            except OSError:
                pass

    # -- events -------------------------------------------------------------

    def _on_mgr(self, op, cur, fds):
        if op == 0:  # toplevel(new_id)
            oid = cur.u32()
            t = _Toplevel(oid)
            self.tops[oid] = t
            self.order.append(oid)
            self.c.on(oid, lambda o, c, f, t=t: self._on_top(t, o, c))
        # op 1 = finished

    def _on_top(self, t: _Toplevel, op, cur):
        if op == 0:
            t.title = cur.string()
        elif op == 1:
            t.app_id = cur.string()
        elif op == 4:
            arr = cur.array()
            t.states = set(struct.unpack("<%dI" % (len(arr) // 4), arr))
        elif op == 6:
            t.closed = True
        # 2/3 output enter/leave, 5 done, 7 parent: ignored

    def _on_output(self, op, cur, fds):
        if op == 1:  # mode(flags, width, height, refresh)
            flags, w, h = cur.u32(), cur.i32(), cur.i32()
            if flags & 1:  # current mode
                self.out_w = max(self.out_w, w)
                self.out_h = max(self.out_h, h)

    def _pump(self):
        """One roundtrip, with the wire's failures turned into one clear line.

        A compositor that goes away mid-session (RuntimeError), or answers with an event whose payload is
        shorter than the interface says (struct.error), or whose socket errors or times out (OSError), is a
        routine thing for a session that is restarting -- not a traceback."""
        try:
            self.c.roundtrip()
        except CmdError:
            raise
        except (OSError, RuntimeError, struct.error) as e:
            raise CmdError("wlr backend: %s" % e) from None

    def _by_wid(self, wid: int) -> _Toplevel:
        self._pump()
        idx = wid - BASE_ID
        if 0 <= idx < len(self.order):
            t = self.tops[self.order[idx]]
            if not t.closed:
                return t
        raise CmdError("window %d not found" % wid)

    def _request(self, t: _Toplevel, opcode: int, args=(), name=None, check=None, reason=None):
        """Send one handle request and say whether the compositor acted on it.

        Returns None when it did (or when there is nothing to check), and a reason line when VERIFY_TIMEOUT
        passed with the handle unchanged -- `reason` where the caller has one of its own, else the line
        `read_only_reason` picks for this request. `check(t)` reads the record the compositor's own events
        fill in, so this is the wire's answer and not a guess: the protocol has no reply for any of these
        requests, and river 0.4 returns success for all four of them [M river.md §2a]."""
        try:
            self.c.send(t.oid, opcode, args)
        except OSError as e:
            raise CmdError("wlr backend: %s" % e) from None
        self._pump()
        if check is None:
            return None
        deadline = time.monotonic() + VERIFY_TIMEOUT
        while not check(t):
            left = deadline - time.monotonic()
            if left <= 0:
                return reason or read_only_reason(name or "the request")
            try:
                self.c.dispatch(left)
            except (OSError, RuntimeError, struct.error) as e:
                raise CmdError("wlr backend: %s" % e) from None
        return None

    def _act(self, t: _Toplevel, opcode: int, name: str, args=(), check=None, reason=None):
        """`_request` for the commands that have nowhere to return a reason: warn and carry on, which is the
        idiom wdotool already uses for KWin's accepted-and-ignored operations (decision C5.17)."""
        why = self._request(t, opcode, args, name=name, check=check, reason=reason)
        if why:
            warn(why)

    @staticmethod
    def _has(bit: int, on: bool):
        return lambda t: (bit in t.states) == on

    # -- WindowBackend ------------------------------------------------------

    def list(self) -> list[Window]:
        self._pump()
        wins = []
        for i, oid in enumerate(self.order):
            t = self.tops[oid]
            if t.closed:
                continue
            wins.append(Window(
                id=BASE_ID + i,
                title=t.title,
                class_=t.app_id,
                pid=0,
                x=0, y=0, w=self.out_w, h=self.out_h,  # geometry unknown
                focused=_ST_ACTIVATED in t.states,
                visible=_ST_MINIMIZED not in t.states,
                desktop=-1,
            ))
        return wins

    def activate(self, wid: int):
        t = self._by_wid(wid)
        if self.seat is None:
            raise CmdError("wlr backend: compositor offers no wl_seat; cannot activate windows")
        self._act(t, _REQ_ACTIVATE, "activate", [("u", self.seat)],
                  check=self._has(_ST_ACTIVATED, True))

    def close(self, wid: int):
        t = self._by_wid(wid)
        before = set(t.states)
        # A client may take a moment to go, or refuse outright, and its own state may change on the way out:
        # anything at all having happened is enough to say the request was heard [M river.md §2a, where
        # nothing happened at all, twice, five seconds apart].
        self._act(t, _REQ_CLOSE, "close", reason=CLOSE_REASON % VERIFY_TIMEOUT,
                  check=lambda tt: tt.closed or tt.states != before)

    def minimize(self, wid: int):
        self._act(self._by_wid(wid), _REQ_SET_MINIMIZED, "set_minimized",
                  check=self._has(_ST_MINIMIZED, True))

    def map(self, wid: int):
        self._act(self._by_wid(wid), _REQ_UNSET_MINIMIZED, "unset_minimized",
                  check=self._has(_ST_MINIMIZED, False))

    def unmap(self, wid: int):
        self._act(self._by_wid(wid), _REQ_SET_MINIMIZED, "set_minimized",
                  check=self._has(_ST_MINIMIZED, True))

    def set_state(self, wid: int, state: str, action: int):
        t = self._by_wid(wid)
        if state == "FULLSCREEN":
            if self.mgr_ver < 2:
                self._unsupported("windowstate FULLSCREEN")
            on = action == 1 or (action == 2 and _ST_FULLSCREEN not in t.states)
            if on:
                return self._request(t, _REQ_SET_FULLSCREEN, [("u", 0)], name="set_fullscreen",
                                     check=self._has(_ST_FULLSCREEN, True))
            return self._request(t, _REQ_UNSET_FULLSCREEN, name="unset_fullscreen",
                                 check=self._has(_ST_FULLSCREEN, False))
        if state in ("MAXIMIZED_VERT", "MAXIMIZED_HORZ"):
            # the protocol only has all-or-nothing maximize
            on = action == 1 or (action == 2 and _ST_MAXIMIZED not in t.states)
            return self._request(t, _REQ_SET_MAXIMIZED if on else _REQ_UNSET_MAXIMIZED,
                                 name="set_maximized" if on else "unset_maximized",
                                 check=self._has(_ST_MAXIMIZED, on))
        if state == "HIDDEN":
            on = action == 1 or (action == 2 and _ST_MINIMIZED not in t.states)
            return self._request(t, _REQ_SET_MINIMIZED if on else _REQ_UNSET_MINIMIZED,
                                 name="set_minimized" if on else "unset_minimized",
                                 check=self._has(_ST_MINIMIZED, on))
        self._unsupported("windowstate %s" % state)

    def _no_geometry(self, op: str):
        """`_unsupported` with the cause appended. The prefix is left exactly as it was -- callers print it
        as `wwmctl: windowsize is not supported by the wlr backend; ignoring` -- and the sentence after the
        colon replaces README note (c)'s tiling explanation, which is wrong on labwc: it is a stacking
        compositor and still cannot move a window, because the protocol has no request for it
        [M recon2/labwc.md §6c]."""
        err = CmdError("%s is not supported by the %s backend: %s" % (op, self.name, NO_GEOMETRY))
        err.unsupported = True
        raise err

    def move_window(self, wid: int, x: int, y: int):
        self._no_geometry("windowmove")

    def resize(self, wid: int, w: int, h: int):
        self._no_geometry("windowsize")

    def raise_(self, wid: int):
        self._no_geometry("windowraise")

    def lower(self, wid: int):
        self._no_geometry("windowlower")

    # -- desktops -----------------------------------------------------------

    def get_desktop(self) -> int:
        if self.ws is None:
            self._unsupported("get_desktop")
        self._pump()
        return self.ws.active_index()

    def set_desktop(self, n: int):
        if self.ws is None:
            self._unsupported("set_desktop")
        self._pump()
        if not self.ws.activate(n):
            raise CmdError("wlr backend: cannot activate workspace %d" % n)
        self._pump()

    def num_desktops(self) -> int:
        if self.ws is None:
            self._unsupported("get_num_desktops")
        self._pump()
        return self.ws.count()

    def workspaces(self):
        if self.ws is None:
            return None
        self._pump()
        return self.ws.workspace_list()

    def display_size(self) -> tuple[int, int]:
        if not self.out_w or not self.out_h:
            raise CmdError("wlr backend: no wl_output mode seen")
        return self.out_w, self.out_h

    # -- XWayland ids -------------------------------------------------------
    # views(), the X connection and the matcher live in XPlaneViews above; only the state bits are the
    # backend's own.

    def _view_flags(self) -> "list[dict]":
        """The `View` state flags for each row of `list()`, in the same order -- the `XPlaneViews` hook.

        The four bits this protocol carries are the four `View` fields wxprop reads: `_node_from_view` builds
        `visible` from `minimized`/`hidden` and `fullscreen_mode` from `fullscreen`, and
        `_NET_WM_STATE_HIDDEN` comes off that node [wxprop/core.py:171-176, 545]. Leaving them at their
        defaults would drop `_NET_WM_STATE_HIDDEN` from a minimized native window -- which the listing
        fallback `{"visible": w.visible}` used to print, before this backend had a views() at all."""
        out = []
        for oid in self.order:
            t = self.tops[oid]
            if t.closed:
                continue
            mx = _ST_MAXIMIZED in t.states
            out.append({"minimized": _ST_MINIMIZED in t.states,
                        "fullscreen": _ST_FULLSCREEN in t.states,
                        "maximized_h": mx, "maximized_v": mx})
        return out
