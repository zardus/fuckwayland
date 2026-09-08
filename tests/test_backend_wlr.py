#!/usr/bin/env python3
"""wdotool's generic wlroots window backend, on the wire.

`WlrBackend` is the capability floor: the backend a wlroots compositor with
no i3-ipc socket falls back to, speaking
zwlr_foreign_toplevel_management_unstable_v1 and nothing else. It had no
test of its own -- only test_wire_hardening, which proves that a compositor
that goes silent during the *constructor* produces one line instead of a
traceback, and never gets as far as a window.

So everything below the constructor was unproven: that arrival order really
is what window ids are made of, that a `closed` toplevel leaves the listing,
that the four state names map onto a protocol with only all-or-nothing
maximize, that `activate` refuses without a seat rather than sending a null
one, and that every capability this backend does not have says so as an
`unsupported` CmdError rather than crashing. Each of those is a wire fact,
so the peer here is a real socket speaking the real bytes.
"""

import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wl_fake
from fwcommon.errors import CmdError
from support import env
from wdotool.backend_wlr import BASE_ID, WlrBackend

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

MAXIMIZED, MINIMIZED, ACTIVATED, FULLSCREEN = 0, 1, 2, 3

# handle requests, by the opcode the protocol gives them
SET_MAXIMIZED, UNSET_MAXIMIZED = 0, 1
SET_MINIMIZED, UNSET_MINIMIZED = 2, 3
ACTIVATE, CLOSE = 4, 5
SET_FULLSCREEN, UNSET_FULLSCREEN = 8, 9


def top(title, app_id, *states):
    return {"title": title, "app_id": app_id, "states": states}


class ToplevelCompositor(wl_fake.Server):
    """A wlroots compositor with zwlr_foreign_toplevel_management_v1.

    Announces `toplevels` -- (title, app_id, states) -- the moment the
    manager is bound, each with its own server-side object id out of the
    0xFF000000 range a real compositor allocates from, and records every
    request made on one. `outputs` are (width, height) mode events; without
    a wl_seat the manager still works but `activate` has no seat to pass."""

    MANAGER = "zwlr_foreign_toplevel_manager_v1"
    PREFIX = "wdotool-wlr-"

    HANDLE_BASE = 0xFF000000

    def __init__(self, toplevels=(), manager_version=3, with_seat=True,
                 outputs=((1920, 1080),)):
        self.toplevels = [dict(t) for t in toplevels]
        self.with_seat = with_seat
        self.outputs = list(outputs)
        self.requests = []       # (toplevel index, opcode, body)
        self._handles = {}       # object id -> toplevel index
        super().__init__(manager_version)

    def advertise(self):
        return (([("wl_seat", 7)] if self.with_seat else [])
                + [("wl_output", 4)] * len(self.outputs))

    def new_state(self):
        return {"mgr": None}

    def on_bind(self, conn, state, name, iface, version, new_id):
        if iface == "wl_output":
            w, h = self.outputs[self.names[name][1]]
            # mode(flags=current, width, height, refresh), then done
            self._send(conn, new_id, 1, struct.pack("<Iiii", 1, w, h, 60000))
            self._send(conn, new_id, 2)
        elif iface == self.MANAGER:
            state["mgr"] = new_id
            self._announce(conn, new_id)

    def _announce(self, conn, mgr):
        for i, t in enumerate(self.toplevels):
            oid = self.HANDLE_BASE + i
            self._handles[oid] = i
            self._send(conn, mgr, 0, struct.pack("<I", oid))      # toplevel
            self._send(conn, oid, 0, wl_fake.wstr(t["title"]))    # title
            self._send(conn, oid, 1, wl_fake.wstr(t["app_id"]))   # app_id
            self._send(conn, oid, 4, self._states(t["states"]))   # state
            self._send(conn, oid, 5)                              # done

    @staticmethod
    def _states(states):
        arr = struct.pack("<%dI" % len(states), *states)
        return struct.pack("<I", len(arr)) + arr + b"\0" * wl_fake.pad(len(arr))

    def on_request(self, conn, state, oid, opcode, body, fds):
        if oid in self._handles:
            self.requests.append((self._handles[oid], opcode, body))

    # -- what a compositor does between two of the client's roundtrips
    def closed(self, index):
        """`closed` on one handle: the window went away."""
        self._to_all(self.HANDLE_BASE + index, 6)

    def restate(self, index, *states):
        """A new `state` event, then `done` -- a window that changed."""
        self._to_all(self.HANDLE_BASE + index, 4, self._states(states))
        self._to_all(self.HANDLE_BASE + index, 5)

    def _to_all(self, oid, opcode, body=b""):
        with self._lock:
            conns = list(self._clients)
        for c in conns:
            self._send(c, oid, opcode, body)

    # -- readers used by the tests
    def of(self, index):
        return [(op, body) for i, op, body in self.requests if i == index]

    def opcodes(self, index):
        return [op for op, _b in self.of(index)]


class WlrTest(unittest.TestCase):
    """One compositor per test, with the environment pointed at it."""

    TOPLEVELS = (top("Alpha One", "alpha", ACTIVATED),
                 top("Beta Two", "beta"),
                 top("Hidden", "gamma", MINIMIZED))
    comp_kw: dict = {}

    def compositor(self, **kw):
        opts = dict(self.comp_kw, **kw)
        opts.setdefault("toplevels", self.TOPLEVELS)
        comp = ToplevelCompositor(**opts)
        self.addCleanup(comp.close)
        ctx = env(XDG_RUNTIME_DIR=comp.dir,
                  WAYLAND_DISPLAY=os.path.basename(comp.path),
                  SWAYSOCK=None, I3SOCK=None)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return comp

    def backend(self, **kw):
        comp = self.compositor(**kw)
        b = WlrBackend()
        self.addCleanup(b.c.close)
        return comp, b


class Listing(WlrTest):
    def test_ids_are_arrival_order_from_the_base(self):
        _comp, b = self.backend()
        wins = b.list()
        self.assertEqual([w.id for w in wins],
                         [BASE_ID, BASE_ID + 1, BASE_ID + 2])
        self.assertEqual([w.title for w in wins],
                         ["Alpha One", "Beta Two", "Hidden"])
        self.assertEqual([w.class_ for w in wins],
                         ["alpha", "beta", "gamma"])

    def test_the_states_that_this_protocol_does_carry(self):
        _comp, b = self.backend()
        wins = b.list()
        self.assertEqual([w.focused for w in wins], [True, False, False])
        self.assertEqual([w.visible for w in wins], [True, True, False])
        # no pids and no workspaces exist in this protocol: it says so in
        # the listing rather than guessing
        self.assertEqual([w.pid for w in wins], [0, 0, 0])
        self.assertEqual([w.desktop for w in wins], [-1, -1, -1])

    def test_geometry_is_the_output_box_because_none_is_carried(self):
        _comp, b = self.backend()
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in b.list()],
                         [(0, 0, 1920, 1080)] * 3)

    def test_the_widest_mode_wins_over_several_outputs(self):
        _comp, b = self.backend(outputs=((1280, 720), (1920, 1200)))
        self.assertEqual(b.display_size(), (1920, 1200))

    def test_no_mode_event_is_a_clean_refusal(self):
        _comp, b = self.backend(outputs=())
        with self.assertRaises(CmdError) as cm:
            b.display_size()
        self.assertIn("no wl_output mode seen", str(cm.exception))

    def test_a_closed_toplevel_leaves_the_listing(self):
        comp, b = self.backend()
        comp.closed(1)
        self.assertEqual([w.title for w in b.list()], ["Alpha One", "Hidden"])
        # the survivors keep the ids they had: arrival order counts the
        # closed window too, so a window the user already found by id does
        # not silently become a different one when a neighbour goes away
        self.assertEqual([w.id for w in b.list()], [BASE_ID, BASE_ID + 2])

    def test_addressing_a_closed_window_is_one_line(self):
        comp, b = self.backend()
        comp.closed(2)
        b.list()
        with self.assertRaises(CmdError) as cm:
            b.close(BASE_ID + 2)
        self.assertEqual(str(cm.exception), "window %d not found" % (BASE_ID + 2))


class Actions(WlrTest):
    def test_close_minimize_map_unmap(self):
        comp, b = self.backend()
        b.close(BASE_ID)
        b.minimize(BASE_ID + 1)
        b.map(BASE_ID + 1)
        b.unmap(BASE_ID + 2)
        self.assertEqual(comp.opcodes(0), [CLOSE])
        self.assertEqual(comp.opcodes(1), [SET_MINIMIZED, UNSET_MINIMIZED])
        self.assertEqual(comp.opcodes(2), [SET_MINIMIZED])

    def test_activate_passes_the_seat_it_bound(self):
        comp, b = self.backend()
        b.activate(BASE_ID + 1)
        (op, body), = comp.of(1)
        self.assertEqual(op, ACTIVATE)
        (seat,) = struct.unpack("<I", body)
        self.assertEqual(seat, b.seat)
        self.assertIn(("wl_seat", 2), comp.binds)

    def test_activate_without_a_seat_refuses_rather_than_sending_null(self):
        comp, b = self.backend(with_seat=False)
        self.assertIsNone(b.seat)
        with self.assertRaises(CmdError) as cm:
            b.activate(BASE_ID)
        self.assertIn("compositor offers no wl_seat", str(cm.exception))
        self.assertEqual(comp.requests, [])


class States(WlrTest):
    def test_fullscreen_add_and_remove(self):
        comp, b = self.backend()
        b.set_state(BASE_ID, "FULLSCREEN", 1)
        b.set_state(BASE_ID, "FULLSCREEN", 0)
        self.assertEqual(comp.opcodes(0), [SET_FULLSCREEN, UNSET_FULLSCREEN])
        # set_fullscreen takes an output, and a null one means "wherever"
        self.assertEqual(comp.of(0)[0][1], struct.pack("<I", 0))

    def test_fullscreen_toggle_reads_the_state_the_compositor_sent(self):
        comp, b = self.backend()
        b.set_state(BASE_ID, "FULLSCREEN", 2)
        self.assertEqual(comp.opcodes(0), [SET_FULLSCREEN])
        comp.restate(0, ACTIVATED, FULLSCREEN)
        b.set_state(BASE_ID, "FULLSCREEN", 2)
        self.assertEqual(comp.opcodes(0), [SET_FULLSCREEN, UNSET_FULLSCREEN])

    def test_fullscreen_needs_version_two(self):
        _comp, b = self.backend(manager_version=1)
        with self.assertRaises(CmdError) as cm:
            b.set_state(BASE_ID, "FULLSCREEN", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertIn("windowstate FULLSCREEN", str(cm.exception))

    def test_both_maximize_axes_are_the_one_maximize_this_protocol_has(self):
        comp, b = self.backend()
        b.set_state(BASE_ID + 1, "MAXIMIZED_VERT", 1)
        b.set_state(BASE_ID + 1, "MAXIMIZED_HORZ", 1)
        b.set_state(BASE_ID + 1, "MAXIMIZED_VERT", 0)
        self.assertEqual(comp.opcodes(1),
                         [SET_MAXIMIZED, SET_MAXIMIZED, UNSET_MAXIMIZED])

    def test_hidden_is_minimize(self):
        comp, b = self.backend()
        b.set_state(BASE_ID, "HIDDEN", 1)
        b.set_state(BASE_ID + 2, "HIDDEN", 2)   # already minimized: toggle off
        self.assertEqual(comp.opcodes(0), [SET_MINIMIZED])
        self.assertEqual(comp.opcodes(2), [UNSET_MINIMIZED])

    def test_a_state_this_protocol_has_no_word_for(self):
        _comp, b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.set_state(BASE_ID, "SHADED", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertEqual(str(cm.exception),
                         "windowstate SHADED is not supported by the wlr backend")


class Capabilities(WlrTest):
    def test_there_are_no_desktops(self):
        _comp, b = self.backend()
        for call in (b.get_desktop, b.num_desktops):
            with self.assertRaises(CmdError) as cm:
                call()
            self.assertTrue(getattr(cm.exception, "unsupported", False))
        with self.assertRaises(CmdError) as cm:
            b.set_desktop(1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))

    def test_a_compositor_without_the_protocol_says_which_one(self):
        self.compositor(manager_version=None)
        with self.assertRaises(CmdError) as cm:
            WlrBackend()
        self.assertIn("zwlr_foreign_toplevel_management_unstable_v1",
                      str(cm.exception))

    def test_no_socket_at_all(self):
        with env(WAYLAND_DISPLAY="/nonexistent/wdotool-no-wayland",
                 XDG_RUNTIME_DIR="/nonexistent/wdotool-no-runtime",
                 SWAYSOCK=None, I3SOCK=None):
            with self.assertRaises(CmdError) as cm:
                WlrBackend()
        self.assertIn("no Wayland socket found", str(cm.exception))

    def test_it_binds_the_manager_at_the_version_it_supports(self):
        """A compositor may offer a version newer than the events this
        client knows how to read; binding it would promise to understand
        them."""
        comp, b = self.backend(manager_version=9)
        self.assertEqual(b.mgr_ver, 3)
        self.assertIn((ToplevelCompositor.MANAGER, 3), comp.binds)


if __name__ == "__main__":
    unittest.main()
