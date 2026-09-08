#!/usr/bin/env python3
"""U28: the COSMIC window backend, on the wire.

cosmic-comp publishes no `zwlr_foreign_toplevel_manager_v1` at all, so before this backend existed every
window command on a COSMIC session answered `no Wayland session found ... the compositor does not offer
wlr-foreign-toplevel` -- rc 2, naming a protocol COSMIC is never going to have [M recon2/cosmic.md §2, §3].
What it does have is `ext_foreign_toplevel_list_v1` for arrival and identity and the two `zcosmic_toplevel_*`
protocols for state and control, and all of it was driven by hand against a live cosmic-comp first
(`recon2/cosmic/cosmic_probe.py`, `cosmic_act.py`, output in `toplevels.txt`) before a line of the backend
was written.

Everything below runs against `wl_fake.CosmicCompositor`, which replays that session: the three toplevels
with their 32-character identifiers, the manager's `capabilities [1,2,3,4,6]` (no 5, no 7), the two
workspaces named `1` and `2` with coordinates `[1]` and `[2]`, and the recorded answers to the three
requests that were actually sent -- `set_maximized` -> `state [0]`, `unset_maximized` -> `[]`, `activate` ->
`[2]`. It deliberately advertises no wlr manager and no virtual pointer, because those absences are half of
what makes a COSMIC session what it is.
"""

import io
import os
import struct
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

# The suite never hands a tool over to the real X11 one: see tests/conftest.py (which covers pytest) and
# tests/test_passthrough.py. This line is what covers `python3 tests/<file>.py`, and it goes before the
# first tool import so that no module can read the variable's absence on the way in.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wl_fake
from fwcommon import session
from fwcommon.errors import CmdError
from support import env
from test_backend_wlr import FakeXPlane, net_wm_state
from wdotool.backend import ID_BASE, mint_id
from wdotool.backend_cosmic import CosmicBackend

#: the head the nested cosmic-comp published [M recon2/cosmic.md §3: `outputs: WINIT-0 1920x1080+0+0`]
OUT_W, OUT_H = 1920, 1080

#: the recorded manager capability array, and the two entries it does NOT carry
CAPS = wl_fake.COSMIC_CAPABILITIES
CAP_FULLSCREEN, CAP_STICKY = 5, 7


class Cosmic(wl_fake.CosmicCompositor):
    """The shared COSMIC fake with its output answering a mode event.

    The recorded registry has `wl_output` in it and the shared fake binds it without saying anything, which
    is right for a registry replay. The rectangle a *geometry-less* listing falls back to is this backend's
    business, so the mode that names it is sent here."""

    PREFIX = "wdotool-cosmic-"

    def on_bind(self, conn, state, name, iface, version, new_id):
        if iface == "wl_output":
            # mode(flags=current, width, height, refresh), then done
            self._send(conn, new_id, 1, struct.pack("<Iiii", 1, OUT_W, OUT_H, 60000))
            self._send(conn, new_id, 2)
            return
        super().on_bind(conn, state, name, iface, version, new_id)


class NoSeat(Cosmic):
    """A COSMIC session with no `wl_seat`. `activate` takes one and the protocol does not allow a null."""

    def advertise(self):
        return [g for g in super().advertise() if g[0] != "wl_seat"]


class NoWorkspaceProtocol(Cosmic):
    """A COSMIC session with no `ext_workspace_manager_v1`. Not a shape cosmic-comp has ever had -- it is
    in all 53 of the recorded globals -- but the backend must not assume a global it did not find."""

    def advertise(self):
        return [g for g in super().advertise() if g[0] != wl_fake.WS_MANAGER]


class ExtWorkspaceEnter(Cosmic):
    """cosmic-comp's own shape for a v3 client: `ext_workspace_enter` (opcode 10), one workspace per window.

    The shared fake sends the deprecated `workspace_enter` (opcode 6) with the first workspace for every
    toplevel, which is what a v<3 client -- one holding `zcosmic_workspace_handle_v1`s -- would get.
    cosmic-comp branches on the bound version and sends the ext pair to a v3 client, because that is the only
    handle such a client holds [R recon2/cosmic/tlinfo.rs:639-640, `raw_ext_workspace_handles` ->
    `instance.ext_workspace_enter`]. A backend that read only opcode 6 would pass every other test in this
    file and report no desktop at all on a real COSMIC.

    `WHERE` is one workspace index per toplevel row; an index in `LEAVE` also gets the matching
    `ext_workspace_leave` straight after, which is what a window being dragged off a workspace looks like."""

    WHERE = (0, 1, 1)
    LEAVE = ()

    EXT_ENTER, EXT_LEAVE = 10, 11

    def _send_cosmic_state(self, conn, rec):
        ids, self.ws_ids = self.ws_ids, []   # no deprecated workspace_enter from the base
        try:
            super()._send_cosmic_state(conn, rec)
            i = list(self.tops).index(rec.ext)
            oid = ids[self.WHERE[i]]
            self._send(conn, rec.cosmic, self.EXT_ENTER, struct.pack("<I", oid))
            if i in self.LEAVE:
                self._send(conn, rec.cosmic, self.EXT_LEAVE, struct.pack("<I", oid))
            self._send(conn, rec.cosmic, 1)   # done
        finally:
            self.ws_ids = ids


class CosmicTest(unittest.TestCase):
    def setUp(self):
        # `xid_match` warns on a tie it cannot break, which is XWaylandIds's business and noise in the
        # other thirty tests: capture stderr here and let the tests that care read it back. Geometry asserts
        # this buffer is EMPTY, which is the point -- the geometry fallback used to print on every listing.
        self.err = io.StringIO()
        ctx = redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def compositor(self, cls=Cosmic, **kw):
        comp = cls(**kw)
        self.addCleanup(comp.close)
        ctx = env(XDG_RUNTIME_DIR=comp.dir,
                  WAYLAND_DISPLAY=os.path.basename(comp.path),
                  SWAYSOCK=None, I3SOCK=None)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return comp

    def backend(self, cls=Cosmic, **kw):
        comp = self.compositor(cls, **kw)
        return comp, self.connect()

    def connect(self):
        b = CosmicBackend()
        self.addCleanup(b.c.close)
        self.addCleanup(lambda: b._x.close() if b._x not in (None, "unset") else None)
        return b

    def wid(self, b, title):
        for w in b.list():
            if w.title == title:
                return w.id
        raise AssertionError("no window titled %r" % title)


class Listing(CosmicTest):
    def test_ids_are_minted_from_the_identifier(self):
        """`ext_foreign_toplevel_handle_v1.identifier` is 32 base62 characters and is the only handle the
        protocol carries -- no pid, no X id, no number [M recon2/cosmic.md §4]. So the id is minted from it:
        30 bits of blake2b under 0x40000000, which keeps it out of the range Xwayland gives its clients
        ((client << 21) | serial) and 32-bit clean for everything downstream."""
        _comp, b = self.backend()
        wins = b.list()
        want = [mint_id(ident) for ident, _t, _a in wl_fake.COSMIC_TOPLEVELS]
        self.assertEqual([w.id for w in wins], want)
        for w in wins:
            self.assertGreaterEqual(w.id, ID_BASE)

    def test_the_ids_are_the_same_in_a_second_process(self):
        """The property the wlr floor's arrival-order ids do not have: there, a window's id changed when a
        neighbour closed [M recon2/hyprland.md §3]. Two backends over one compositor stand in for two
        processes reading one session."""
        _comp, b1 = self.backend()
        b2 = self.connect()
        self.assertEqual([w.id for w in b1.list()], [w.id for w in b2.list()])

    def test_title_class_and_the_pid_that_does_not_exist(self):
        _comp, b = self.backend()
        wins = b.list()
        self.assertEqual([w.title for w in wins], ["cosmicterm", "typedtest", "cosmicxterm"])
        self.assertEqual([w.class_ for w in wins], ["foot", "foot", "XTerm"])
        self.assertEqual([w.pid for w in wins], [0, 0, 0],
                         "neither toplevel protocol carries a pid")

    def test_kill_says_there_is_no_pid(self):
        _comp, b = self.backend()
        wid = self.wid(b, "cosmicterm")
        with self.assertRaises(CmdError) as cm:
            b.kill(wid)
        self.assertEqual(str(cm.exception), "no pid for window %d" % wid)

    def test_a_closed_toplevel_leaves_the_listing(self):
        comp, b = self.backend()
        wid = self.wid(b, "typedtest")
        b.close(wid)
        self.assertEqual([w.title for w in b.list()], ["cosmicterm", "cosmicxterm"])
        self.assertEqual(comp.calls[-1][0], "close")
        with self.assertRaises(CmdError) as cm:
            b.close(wid)
        self.assertEqual(str(cm.exception), "window %d not found" % wid)

    def test_the_compositor_without_the_protocols_says_which_ones(self):
        """A labwc registry has neither, and the refusal has to name what is missing rather than repeat the
        wlr backend's sentence."""
        self.compositor(cls=wl_fake.RegistryServer, fixture="labwc")
        with self.assertRaises(CmdError) as cm:
            CosmicBackend()
        self.assertIn("ext_foreign_toplevel_list_v1", str(cm.exception))
        self.assertIn("zcosmic_toplevel_info_v1", str(cm.exception))

    def test_no_socket_at_all(self):
        with env(WAYLAND_DISPLAY="/nonexistent/wdotool-no-wayland",
                 XDG_RUNTIME_DIR="/nonexistent/wdotool-no-runtime",
                 SWAYSOCK=None, I3SOCK=None):
            with self.assertRaises(CmdError) as cm:
                CosmicBackend()
        self.assertIn("no Wayland socket found", str(cm.exception))


class TheStateArray(CosmicTest):
    def test_the_recorded_transcript_replays_step_by_step(self):
        """`cosmic_act.py`'s three requests and the three answers it got: before `{'state': []}`, after
        set_maximized `[0]`, after unset_maximized `[]`, after activate `[2]` [M cosmic.md §4].

        Read back off the backend and not off the compositor's own bookkeeping: what is under test is that
        this client re-reads the `state` array the compositor sends after each request, which is the only
        answer either protocol has. `Window` has no maximize field, so the two maximize steps are asked for
        as *toggles*: the second one can only come out as `unset_maximized` if the backend read `[0]` back
        off the wire. The states a listing does show -- ACTIVATED here, MINIMIZED next door -- are read from
        the backend, and the flags a listing cannot show are ViewFlags's half."""
        comp, b = self.backend()
        wid = self.wid(b, "cosmicterm")
        self.assertEqual([w.focused for w in b.list()], [False, False, False])
        b.set_state(wid, "MAXIMIZED_VERT", 2)
        b.set_state(wid, "MAXIMIZED_VERT", 2)
        b.activate(wid)
        self.assertEqual([(w.title, w.focused) for w in b.list()],
                         [("cosmicterm", True), ("typedtest", False), ("cosmicxterm", False)])
        self.assertEqual([c[0] for c in comp.calls],
                         ["set_maximized", "unset_maximized", "activate"])

    def test_minimized_is_not_visible(self):
        _comp, b = self.backend()
        wid = self.wid(b, "typedtest")
        b.minimize(wid)
        self.assertEqual([(w.title, w.visible) for w in b.list()],
                         [("cosmicterm", True), ("typedtest", False), ("cosmicxterm", True)])
        b.map(wid)
        self.assertTrue(all(w.visible for w in b.list()))

    def test_both_maximize_axes_are_the_one_maximize_this_protocol_has(self):
        comp, b = self.backend()
        wid = self.wid(b, "cosmicterm")
        b.set_state(wid, "MAXIMIZED_VERT", 1)
        b.set_state(wid, "MAXIMIZED_HORZ", 1)
        b.set_state(wid, "MAXIMIZED_VERT", 0)
        self.assertEqual([c[0] for c in comp.calls],
                         ["set_maximized", "set_maximized", "unset_maximized"])

    def test_activate_passes_the_seat_and_refuses_without_one(self):
        comp, b = self.backend()
        b.activate(self.wid(b, "cosmicterm"))
        name, ids = comp.calls[-1]
        self.assertEqual(name, "activate")
        self.assertEqual(ids[1], b.seat, "activate(toplevel, seat)")

        comp2, b2 = self.backend(cls=NoSeat)
        self.assertIsNone(b2.seat)
        with self.assertRaises(CmdError) as cm:
            b2.activate(self.wid(b2, "cosmicterm"))
        self.assertIn("offers no wl_seat", str(cm.exception))
        self.assertEqual(comp2.calls, [], "no null seat may go on the wire")


class Capabilities(CosmicTest):
    def test_fullscreen_is_gated_on_the_advertised_array(self):
        """Decision C5.15. `set_fullscreen` did work on the live compositor even though 5 is not in
        `[1,2,3,4,6]`, but a client that ignores the capability array is a client that will be wrong the
        first time the array is right -- so the array decides, and the refusal names the capability."""
        comp, b = self.backend()
        self.assertNotIn(CAP_FULLSCREEN, CAPS)
        with self.assertRaises(CmdError) as cm:
            b.set_state(self.wid(b, "cosmicterm"), "FULLSCREEN", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertEqual(str(cm.exception),
                         "windowstate FULLSCREEN is not supported by the cosmic backend: "
                         "cosmic-comp does not advertise the fullscreen capability")
        self.assertEqual(comp.calls, [])

    def test_a_compositor_that_does_advertise_it_gets_the_request(self):
        comp, b = self.backend(capabilities=CAPS + (CAP_FULLSCREEN,))
        wid = self.wid(b, "cosmicterm")
        self.assertIsNone(b.set_state(wid, "FULLSCREEN", 1))
        name, ids = comp.calls[-1]
        self.assertEqual(name, "set_fullscreen")
        self.assertEqual(ids[1], 0, "a null output means wherever the window is")
        b.set_state(wid, "FULLSCREEN", 2)
        self.assertEqual(comp.calls[-1][0], "unset_fullscreen",
                         "the toggle reads the state the compositor sent back")

    def test_sticky_is_gated_the_same_way(self):
        _comp, b = self.backend()
        self.assertNotIn(CAP_STICKY, CAPS)
        with self.assertRaises(CmdError) as cm:
            b.set_state(self.wid(b, "cosmicterm"), "STICKY", 1)
        self.assertIn("sticky capability", str(cm.exception))

    def test_close_is_gated_too(self):
        comp, b = self.backend(capabilities=())
        with self.assertRaises(CmdError) as cm:
            b.close(self.wid(b, "cosmicterm"))
        self.assertIn("close capability", str(cm.exception))
        self.assertEqual(comp.calls, [])

    def test_move_resize_raise_and_lower_name_the_protocol(self):
        """There is no request behind any of them: the manager has `set_rectangle`, a minimise-animation
        hint, and nothing else [M cosmic.md §5.2]."""
        _comp, b = self.backend()
        wid = self.wid(b, "cosmicterm")
        for call, args, op in ((b.move_window, (wid, 1, 2), "windowmove"),
                               (b.resize, (wid, 3, 4), "windowsize"),
                               (b.raise_, (wid,), "windowraise"),
                               (b.lower, (wid,), "windowlower")):
            with self.assertRaises(CmdError) as cm:
                call(*args)
            self.assertTrue(getattr(cm.exception, "unsupported", False), op)
            self.assertEqual(str(cm.exception),
                             "%s is not supported by the cosmic backend: the COSMIC toplevel protocol has "
                             "no move, resize, raise or lower" % op)

    def test_a_state_this_protocol_has_no_word_for(self):
        _comp, b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.set_state(self.wid(b, "cosmicterm"), "SHADED", 1)
        self.assertEqual(str(cm.exception),
                         "windowstate SHADED is not supported by the cosmic backend")


class Geometry(CosmicTest):
    def test_a_geometry_event_fills_the_rectangle(self):
        _comp, b = self.backend(geometry=(40, 50, 640, 480))
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in b.list()],
                         [(40, 50, 640, 480)] * 3)

    def test_without_one_the_output_rectangle_is_reported_and_nothing_is_printed(self):
        """No `geometry` event ever arrived in the nested rig -- not in 4 s after `get_cosmic_toplevel` and
        not after a maximize -- because cosmic-comp sends it only alongside `output_enter` or on a change
        [M cosmic.md §4, R toplevel_info.rs]. So the fallback is the normal case on the only COSMIC ever
        measured, and `list()` is what every window command runs: a line on stderr there is a line on every
        `wdotool search`, every `getactivewindow` and every `wwmctl -l`. The flag is how a caller that wants
        to say so finds out, the way the wlr floor reports the same rectangle silently."""
        _comp, b = self.backend()
        self.assertFalse(b.geometry_is_floor, "nothing has been listed yet")
        first = b.list()
        b.list()
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in first], [(0, 0, OUT_W, OUT_H)] * 3)
        self.assertTrue(b.geometry_is_floor)
        self.assertEqual(self.err.getvalue(), "")

    def test_a_geometry_event_leaves_the_flag_alone(self):
        _comp, b = self.backend(geometry=(40, 50, 640, 480))
        b.list()
        self.assertFalse(b.geometry_is_floor)

    def test_display_size_is_the_output_mode(self):
        _comp, b = self.backend()
        self.assertEqual(b.display_size(), (OUT_W, OUT_H))


class Workspaces(CosmicTest):
    def test_the_two_recorded_workspaces_in_coordinate_order(self):
        """`ws {'name': '1', 'coords': [1], 'caps': 1}` and `'2'` with `[2]` [M cosmic.md §4]."""
        _comp, b = self.backend()
        self.assertEqual(b.num_desktops(), 2)
        self.assertEqual([w.name for w in b.workspaces()], ["1", "2"])

    def test_a_window_reports_the_workspace_it_entered(self):
        """`workspace_enter` names a workspace by object id, and the id is one of the handles the
        ext-workspace client already holds -- which is how the desktop column gets a number at all on a
        protocol family where the wlr floor leaves it -1."""
        _comp, b = self.backend()
        self.assertEqual([w.desktop for w in b.list()], [0, 0, 0])

    def test_the_workspace_a_window_entered_is_the_one_it_reports(self):
        """Not index 0 for everything: the oid the enter names is looked up in the same coordinate-ordered
        list `wwmctl -d` counts, so a window on the second workspace has to come back as desktop 1."""
        _comp, b = self.backend(cls=ExtWorkspaceEnter)
        self.assertEqual([w.desktop for w in b.list()], [0, 1, 1])

    def test_the_ext_spelling_of_the_enter_is_the_one_cosmic_comp_sends(self):
        """v3 renamed the pair. A client that binds `zcosmic_toplevel_info_v1` v3 holds no
        `zcosmic_workspace_handle_v1` at all, so cosmic-comp sends it `ext_workspace_enter` (opcode 10) and
        never the deprecated opcode 6 [R tlinfo.rs:639-640]. Both are read, and this is the one that runs on
        a real session."""
        comp, b = self.backend(cls=ExtWorkspaceEnter)
        self.assertEqual(b.info_ver, 3)
        self.assertEqual([w.desktop for w in b.list()][1], 1)
        self.assertEqual(comp.ws_ids[1], b._ws_rows()[1].oid,
                         "the handle the enter named is the ext-workspace client's own")

    def test_a_window_that_left_its_workspace_has_none(self):
        class Leaves(ExtWorkspaceEnter):
            LEAVE = (1,)

        _comp, b = self.backend(cls=Leaves)
        self.assertEqual([w.desktop for w in b.list()], [0, -1, 1])

    def test_an_empty_workspace_manager_is_not_a_missing_one(self):
        """The protocol is there and says there are no workspaces: that is an answer (-1, 0), not a
        capability gap, and a window with no `workspace_enter` has no desktop of its own."""
        _comp, b = self.backend(workspaces=())
        self.assertEqual([w.desktop for w in b.list()], [-1, -1, -1])
        self.assertEqual((b.get_desktop(), b.num_desktops()), (-1, 0))

    def test_no_workspace_protocol_at_all_is_the_capability_refusal(self):
        _comp, b = self.backend(cls=NoWorkspaceProtocol)
        self.assertIsNone(b.ws)
        self.assertIsNone(b.workspaces())
        for call in (b.get_desktop, b.num_desktops):
            with self.assertRaises(CmdError) as cm:
                call()
            self.assertTrue(getattr(cm.exception, "unsupported", False))
        with self.assertRaises(CmdError) as cm:
            b.set_window_desktop(self.wid(b, "cosmicterm"), 0)
        self.assertTrue(getattr(cm.exception, "unsupported", False))

    def test_set_desktop_is_activate_then_commit(self):
        comp, b = self.backend()
        b.set_desktop(1)
        self.assertEqual(comp.ws_calls, [("activate", 1), ("commit", None)])
        comp.set_active(1)
        self.assertEqual(b.get_desktop(), 1)

    def test_moving_a_window_uses_the_request_that_works(self):
        """cosmic-comp's handler for the deprecated `move_to_workspace` is an empty arm, while
        `MoveToExtWorkspace` resolves the handle and moves the window [R recon2/cosmic/tlmgmt.rs:251-263];
        its capability array still says 6 and not 8 [R state.rs:752-756]. So the gate is 6 and the request
        is opcode 13."""
        comp, b = self.backend()
        b.set_window_desktop(self.wid(b, "cosmicterm"), 1)
        name, ids = comp.calls[-1]
        self.assertEqual(name, "move_to_ext_workspace")
        self.assertEqual(ids[1], comp.ws_ids[1], "the second workspace's own handle")
        self.assertEqual(len(ids), 3, "toplevel, workspace, output")

    def test_a_workspace_that_is_not_there(self):
        comp, b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.set_window_desktop(self.wid(b, "cosmicterm"), 9)
        self.assertIn("no workspace 9", str(cm.exception))
        self.assertEqual(comp.calls, [])


class CosmicXPlane(FakeXPlane):
    """The shared X rig on its own display number, so this file and test_backend_wlr.py never race for one.

    `views()` and its matcher are one piece of code shared by the two backends (`backend_wlr.XPlaneViews`),
    so the rig that feeds it is shared too."""

    DISPLAY_NUM = 4
    SOCK_PREFIX = "cosmic-x11-"
    CLIENTS = ((0x600012, "xterm", "XTerm", "cosmicxterm", 77, (0, 0, 484, 316)),)


class ViewFlags(CosmicXPlane, CosmicTest):
    """The state bits the X join must not drop.

    `views()` replaces the listing wxprop reads, and wxprop's fallback for a backend without one was
    `{"visible": w.visible}`: a row handed over with the flags at their defaults loses
    `_NET_WM_STATE_HIDDEN` on a minimized window -- which the wlr floor used to print -- and never gains the
    fullscreen, maximize or sticky bits this protocol's five-entry state array does carry."""

    def flagged(self, **kw):
        """The views of a session where cosmicterm is minimized, typedtest fullscreen and sticky.

        Driven through the manager rather than planted: the states come back off the compositor's own
        `state` array, which is the only place this backend reads them from."""
        self.x_server()
        _comp, b = self.backend(capabilities=CAPS + (CAP_FULLSCREEN, CAP_STICKY), **kw)
        b.set_state(self.wid(b, "cosmicterm"), "HIDDEN", 1)
        b.set_state(self.wid(b, "typedtest"), "FULLSCREEN", 1)
        b.set_state(self.wid(b, "typedtest"), "STICKY", 1)
        return {v.window.title: v for v in b.views()}

    def test_the_minimized_window_comes_back_minimized(self):
        v = self.flagged()["cosmicterm"]
        self.assertTrue(v.minimized)
        self.assertFalse(v.window.visible)

    def test_fullscreen_and_sticky_reach_the_view(self):
        v = self.flagged()["typedtest"]
        self.assertEqual((v.fullscreen, v.sticky, v.minimized), (True, True, False))

    def test_the_xwayland_row_carries_them_too(self):
        """The row that goes through the X join keeps the Wayland state: the two halves are one View."""
        self.x_server()
        _comp, b = self.backend()
        b.set_state(self.wid(b, "cosmicxterm"), "MAXIMIZED_VERT", 1)
        v = {r.window.title: r for r in b.views()}["cosmicxterm"]
        self.assertEqual(v.xid, 0x600012)
        self.assertEqual((v.maximized_h, v.maximized_v), (True, True))

    def test_wxprop_prints_the_states_the_protocol_carries(self):
        """The contract that made this a bug: `_node_from_view` reads `minimized`/`hidden` for `visible`,
        `fullscreen` for `fullscreen_mode` and `sticky` for STICKY, and `_NET_WM_STATE_HIDDEN` comes off that
        node [wxprop/core.py:171-176, 545]."""
        views = self.flagged()
        self.assertEqual(net_wm_state(views["cosmicterm"]), ["_NET_WM_STATE_HIDDEN"])
        self.assertEqual(net_wm_state(views["typedtest"]),
                         ["_NET_WM_STATE_FULLSCREEN", "_NET_WM_STATE_STICKY"])
        self.assertEqual(net_wm_state(views["cosmicxterm"]), [],
                         "and nothing is invented for a window with an empty state array")


class XWaylandIds(CosmicXPlane, CosmicTest):
    """The weakest matcher in the tree, and deliberately so (decision C5.16): a COSMIC toplevel has neither
    a pid nor -- usually -- a geometry, so `WM_CLASS` and the title are all there is [M cosmic.md §5.4]."""

    XTERM = 0x600012

    def test_the_xterm_gets_its_real_id_and_wm_class(self):
        """cosmic-comp runs its own Xwayland and `wxprop -id 0x600012` was byte-identical to `xprop` there
        [M cosmic.md §3]; what was missing is the join from the toplevel list to that id."""
        self.x_server()
        _comp, b = self.backend()
        views = b.views()
        by_title = {v.window.title: v for v in views}
        xt = by_title["cosmicxterm"]
        self.assertEqual(xt.xid, self.XTERM)
        self.assertEqual((xt.instance, xt.cls), ("xterm", "XTerm"))
        self.assertEqual(xt.client_type, "x11")
        self.assertEqual(xt.window.pid, 77)
        self.assertEqual((xt.window.x, xt.window.y, xt.window.w, xt.window.h), (0, 0, 484, 316))
        self.assertEqual([v.xid for v in views if v.window.title != "cosmicxterm"], [0, 0])

    def test_two_identical_windows_keep_xid_zero(self):
        self.x_server([(0x600012, "foot", "foot", "cosmicterm", 1, (0, 0, 10, 10)),
                       (0x600013, "foot", "foot", "cosmicterm", 2, (0, 0, 10, 10))])
        rows = (("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "cosmicterm", "foot"),
                ("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "cosmicterm", "foot"))
        _comp, b = self.backend(toplevels=rows)
        views = b.views()
        self.assertEqual([v.xid for v in views], [0, 0])
        self.assertIn("could not be told apart", self.err.getvalue())

    def test_nothing_is_opened_when_no_xwayland_runs(self):
        self.x_server()
        patch = mock.patch.object(session, "xwayland_running", lambda uid=None: False)
        patch.start()
        self.addCleanup(patch.stop)
        _comp, b = self.backend()
        self.assertIsNone(b.views())


if __name__ == "__main__":
    unittest.main()
