#!/usr/bin/env python3
"""The generic wlroots apply, against a compositor that rearranges the layout it just accepted.

CI run 34308982263 (commit 4d6fd40) failed the same two display checks on all four labwc flavors --
resolute-labwc, resolute-xfce-wayland, resolute-budgie, resolute-lxqt-wayland -- with numbers that grew by
one head's width per apply:

    FAIL display: --right-of puts Virtual-3 to the right of Virtual-2 [Virtual-2 at 5760,0, Virtual-3 at 3840,0]
    FAIL display: --below puts Virtual-3 under Virtual-2   [Virtual-2 at 7680,1080, Virtual-3 at 5760,1080]
    FAIL mirror:  ... the region 800x600+100+100 is not inside Virtual-3, which is 1920x1080+9600+1080

Run down on the rig on 2026-09-09 (resolute-labwc golden, `vmctl start --heads 3 --mem 3G`, labwc 0.9.3 on
wlroots 0.19.2).  The whole cascade starts at `--output Virtual-3 --off` followed by `--output Virtual-3
--auto`: xrandr brings an output back at 0,0 -- X is the oracle and this repo does what X did -- so the
configuration asks for Virtual-3 on top of Virtual-1, and labwc answers `succeeded` and then lays the three
heads out itself (Virtual-3 0,0 / Virtual-1 1920,0 / Virtual-2 3840,0).  From then on every configuration of
that session comes back with one head where labwc chose:

    asked  V-1 0,0     V-2 1920,0    V-3 3840,0     read back  V-1 0,0  V-3 3840,0  V-2 5760,0

The three set_position requests were read off our own wire in the guest (a probe of WlrOutputs.send) and they
were exactly those numbers, and `wlr-randr` 0.4.1 -- the reference client, and the flavor's own oracle --
produces the identical layout from the identical starting state.  Re-sending the same configuration lands it
exactly, first retry, every time it was tried.  So `WlrOutputs.apply` reads the layout back and sends the same
configuration once more when a head is not where it was put; this file is that behaviour, at the wire, against
wl_fake.OutputManagerServer with the rearrangement armed.
"""

import os
import sys
import tempfile
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py (which covers pytest) and
# tests/test_passthrough.py.  This line is what covers `python3 tests/<file>.py`, where conftest is not
# loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wl_fake
from w11common.wayland_mini import WlConn
from wxrandr import core
from wxrandr.core import Fatal, State, WlrOutputs, build_targets, snapshot_wlr


class Live(unittest.TestCase):
    """One fake compositor and one connected WlrOutputs per test."""

    def outputs(self, **kw):
        self.server = wl_fake.OutputManagerServer(**kw)
        self.addCleanup(self.server.close)
        conn = WlConn(self.server.path)
        self.addCleanup(conn.close)
        wlr = WlrOutputs(conn)
        self.addCleanup(wlr.close)
        return wlr

    def state(self):
        """A State on a file that is never written: nothing here reads a saved mode or a primary."""
        fh = tempfile.NamedTemporaryFile(prefix="wxrandr-wlr-state-", suffix=".json", delete=False)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return State("wlroots", path=fh.name)


class TheLayoutIsReadBackAndReSent(Live):
    """The measured labwc 0.9.3 sequence, and what the backend does with it."""

    def stanza(self, name, **kw):
        return core.Stanza(name=name, **kw)

    def plan(self, wlr, stanzas):
        st = self.state()
        return st, build_targets(snapshot_wlr(wlr, st), stanzas, st)

    def test_one_apply_when_the_compositor_keeps_the_layout(self):
        """The retry costs nothing on a compositor that does what it was asked: one configuration, no more."""
        wlr = self.outputs()
        st, targets = self.plan(wlr, [self.stanza("Virtual-3", relation=("below", "Virtual-2"))])
        wlr.apply(st, targets)
        self.assertEqual(len(self.server.applies), 1)
        self.assertEqual(self.server.layout(),
                         {"Virtual-1": (0, 0), "Virtual-2": (1920, 0), "Virtual-3": (1920, 1080)})

    def test_a_rearranged_head_is_re_sent_and_lands(self):
        """The CI failure itself: --right-of asks V-1 0,0 / V-2 1920,0 / V-3 3840,0, labwc puts Virtual-2 at
        5760,0 beside Virtual-3, and the second identical configuration lands all three."""
        wlr = self.outputs()
        self.server.stray_once("Virtual-2")
        st, targets = self.plan(wlr, [self.stanza("Virtual-3", relation=("right-of", "Virtual-2"))])
        fresh = wlr.apply(st, targets)
        self.assertEqual(len(self.server.applies), 2, "the rearranged layout was not re-sent")
        self.assertEqual(self.server.applies[0], self.server.applies[1],
                         "the retry asked for a different layout than the apply it is retrying")
        self.assertEqual(self.server.layout(),
                         {"Virtual-1": (0, 0), "Virtual-2": (1920, 0), "Virtual-3": (3840, 0)})
        by = {o.name: (o.x, o.y) for o in fresh}
        self.assertEqual(by["Virtual-2"], (1920, 0), "apply returned the layout it did not get")
        self.assertEqual(by["Virtual-3"], (3840, 0))

    def test_the_5760_of_the_ci_run_is_what_the_fake_produces(self):
        """The fake is not free to be wrong about the number: with the retry taken out of the picture (one
        raw send), Virtual-2 lands on 5760,0 -- the byte CI printed."""
        wlr = self.outputs()
        self.server.stray_once("Virtual-2")
        st, targets = self.plan(wlr, [self.stanza("Virtual-3", relation=("right-of", "Virtual-2"))])
        dims = {t.name: core.predicted_dims(t, st, wire="fixed") for t in targets if t.enabled}
        pos = core.resolve_positions(targets, dims)
        self.assertEqual(pos, {"Virtual-1": (0, 0), "Virtual-2": (1920, 0), "Virtual-3": (3840, 0)})
        wlr.send({t.name: t for t in targets}, pos)
        wlr.conn.roundtrip()
        self.assertEqual(self.server.layout(),
                         {"Virtual-1": (0, 0), "Virtual-2": (5760, 0), "Virtual-3": (3840, 0)})

    def test_a_compositor_that_ignores_the_retry_too_gets_a_sentence(self):
        """Two applies and no third: the numbers reach the user instead of a layout nobody asked for."""
        wlr = self.outputs()
        original = wlr.send

        def send(targets, positions):
            self.server.stray_once("Virtual-2")
            return original(targets, positions)
        wlr.send = send
        st, targets = self.plan(wlr, [self.stanza("Virtual-3", relation=("right-of", "Virtual-2"))])
        with self.assertRaises(Fatal) as caught:
            wlr.apply(st, targets)
        self.assertEqual(str(caught.exception),
                         "the compositor accepted the position 1920,0 for Virtual-2 twice and put it "
                         "at 5760,0 both times\n")
        self.assertEqual(len(self.server.applies), 2)

    def test_the_off_then_auto_pair_asks_for_the_overlap_x_asks_for(self):
        """xrandr --off then --auto brings the output back at 0,0, on top of whatever is there. That is what
        X does and what the rig's display phase leans on, so the backend must ask for it -- and the head that
        is turned off must not be counted as a stray on the way."""
        wlr = self.outputs()
        st, targets = self.plan(wlr, [self.stanza("Virtual-3", off=True)])
        wlr.apply(st, targets)
        self.assertEqual(self.server.layout(), {"Virtual-1": (0, 0), "Virtual-2": (1920, 0)})
        self.assertEqual(len(self.server.applies), 1, "a disabled head was mistaken for a stray one")
        wlr2 = self.outputs_again()
        st, targets = self.plan(wlr2, [self.stanza("Virtual-3", auto=True)])
        wlr2.apply(st, targets)
        self.assertEqual(self.server.applies[-1]["Virtual-3"], (0, 0, True))
        self.assertEqual(self.server.layout()["Virtual-3"], (0, 0))

    def outputs_again(self):
        """A second client on the SAME fake: every wxrandr invocation is its own process and its own
        connection, and the layout it reads is whatever the previous one left."""
        conn = WlConn(self.server.path)
        self.addCleanup(conn.close)
        wlr = WlrOutputs(conn)
        self.addCleanup(wlr.close)
        return wlr


class StrayHead(unittest.TestCase):
    """`_stray_head` alone: which output a sentence names, and which mismatches are not one."""

    def out(self, name, x, y, active=True):
        o = core.OutputState(name=name, active=active, ident=1)
        o.x, o.y = x, y
        return o

    def test_snapshot_order_decides_which_output_is_named(self):
        """Two heads adrift: the one the compositor lists first is the one the message is about, so the
        sentence does not change between runs with the dict's iteration order."""
        fresh = [self.out("Virtual-3", 9, 9), self.out("Virtual-2", 8, 8)]
        pos = {"Virtual-2": (1920, 0), "Virtual-3": (3840, 0)}
        self.assertEqual(core._stray_head(pos, fresh), ("Virtual-3", (3840, 0), (9, 9)))

    def test_a_head_that_is_off_is_not_a_stray(self):
        fresh = [self.out("Virtual-3", 0, 0, active=False)]
        self.assertIsNone(core._stray_head({"Virtual-3": (3840, 0)}, fresh))

    def test_an_output_with_no_planned_position_is_not_a_stray(self):
        fresh = [self.out("Virtual-1", 4096, 0)]
        self.assertIsNone(core._stray_head({}, fresh))

    def test_the_layout_that_landed_is_no_stray_at_all(self):
        fresh = [self.out("Virtual-2", 1920, 0), self.out("Virtual-1", 0, 0)]
        self.assertIsNone(core._stray_head({"Virtual-1": (0, 0), "Virtual-2": (1920, 0)}, fresh))


if __name__ == "__main__":
    unittest.main()
