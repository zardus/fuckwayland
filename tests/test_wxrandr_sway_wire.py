"""wxrandr's own sway IPC client against a compositor that is wedged, dying or
lying.

wdotool's sway backend was hardened first (tests/test_wire_hardening.py:
SwayWireGuards) and wxrandr's was not, although it speaks the same i3-ipc
protocol on the same socket and is the one that applies layouts.  The two are
separate code -- `wdotool/backend_sway.py` against `wxrandr/core.py`'s
`SwayIPC` -- so nothing proved about one holds for the other, and this is the
other.

The double is `support.FakeSway`, the same one wdotool's guards use, in the six
shapes a real sway can present: answering, going away mid-chain, framing a reply
whose body is not JSON, accepting the connection and never answering again,
refusing an output command in words, and describing an output without the field
that says where it is.  Everything here goes through `cli.main` with `--backend
sway`, because that is what a user runs and because the answer that matters is
what reaches stderr and what the exit status is -- one line, never a traceback,
and never an unbounded wait.

sway lives on this host (live-measurements.md: test_windows_sway,
test_input_geometry_sway and test_wxrandr_live all pass against a real headless
sway 1.11 here), and tests/test_wxrandr_live.py is where the healthy path is
measured.  Nothing here needs a compositor.
"""

import contextlib
import gc
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
import warnings
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

import support
from wxrandr import cli, core

#: what `SwayIPC.__init__` arms the command socket with (wxrandr/core.py, a
#: literal rather than a module constant -- wdotool's twin is
#: `backend_sway.IPC_TIMEOUT` and is patchable).  A wedged sway is answered by
#: this and by nothing else, so the tests below replace the class with one that
#: shortens it rather than waiting ten seconds a piece.
SOCKET_TIMEOUT = 10.0

#: long enough that a real wait would show, short enough that the file stays
#: fast: every bounded assertion below is against this
SHORT = 0.5


class ShortIPC(core.SwayIPC):
    """`SwayIPC` with a deadline a test can wait for.

    Not a stub: it is the real client on the real socket, with one line of the
    constructor's effect changed.  Everything the assertions are about --
    framing, `_read_exact`, the JSON, the RUN_COMMAND result list -- is the
    shipped code."""

    def __init__(self, sockpath=None):
        super().__init__(sockpath)
        self.sock.settimeout(SHORT)


class SwayWire(unittest.TestCase):
    """One FakeSway per test, reached as the session's own compositor."""

    def sway(self, mode, **kw):
        srv = support.FakeSway(mode, **kw)
        self.addCleanup(srv.close)
        self.env("SWAYSOCK", srv.path)
        return srv

    def env(self, name, value):
        old = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        self.addCleanup(self._restore, name, old)

    def _restore(self, name, old):
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-sway-wire-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # the state file is a cache and belongs in this test's directory, never
        # in the runtime directory of whoever is running the suite
        self.env("XDG_RUNTIME_DIR", self.tmp)
        # there is no compositor here beyond the fake socket: the wlr
        # enrichment connection must not be attempted against a real one
        self.env("WAYLAND_DISPLAY", None)
        self.env("WXRANDR_BACKEND", None)

    def run_cli(self, *argv, short=True):
        """`(code, stdout, stderr, seconds)` for one `cli.main`."""
        out, err = io.StringIO(), io.StringIO()
        started = time.monotonic()
        with mock.patch.object(core, "SwayIPC", ShortIPC if short else core.SwayIPC):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(list(argv))
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 0
        return code, out.getvalue(), err.getvalue(), time.monotonic() - started

    def one_line(self, err):
        """The single `xrandr:` line, asserting that it is single."""
        lines = [ln for ln in err.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("xrandr: "), lines[0])
        return lines[0]

    # -- the healthy control -------------------------------------------------

    def test_an_answering_sway_needs_no_wayland_display(self):
        """The control every test below is measured against, and one claim of
        its own: the sway backend is the IPC socket and the IPC socket only.
        `wlr_snapshot_safe()` returns None without `$WAYLAND_DISPLAY` and the
        query is answered out of GET_OUTPUTS alone."""
        srv = self.sway("ok")
        self.assertIsNone(os.environ.get("WAYLAND_DISPLAY"))
        code, out, err, _s = self.run_cli("--backend", "sway", "--query")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("HEADLESS-1 connected 1280x720+0+0", out)
        self.assertIn("HEADLESS-2 connected 1280x1024+1280+0", out)
        self.assertEqual([mt for mt, _p in srv.requests], [support.GET_OUTPUTS])

    def test_the_command_socket_carries_a_deadline_at_all(self):
        """Which is the whole reason a wedged sway ends in a message rather than
        for ever.  The number is a literal in `SwayIPC.__init__`; what this
        pins is that there is one and that it is finite."""
        srv = self.sway("wedged")
        ipc = core.SwayIPC(srv.path)
        self.addCleanup(ipc.close)
        self.assertEqual(ipc.sock.gettimeout(), SOCKET_TIMEOUT)

    # -- the five ways it goes wrong -----------------------------------------

    def test_a_wedged_compositor_is_one_bounded_line(self):
        """Accepted, never answered.  The kernel completes the connect for a
        compositor stuck in its own event loop, so nothing before the first
        read can tell; only the socket's own deadline ends the wait."""
        srv = self.sway("wedged")
        code, out, err, secs = self.run_cli("--backend", "sway", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(self.one_line(err), "xrandr: timed out")
        self.assertGreater(secs, SHORT * 0.8, secs)
        self.assertLess(secs, 5.0, secs)
        self.assertEqual(srv.requests, [])          # it never read our frame

    def test_a_session_that_ends_mid_chain_is_one_line(self):
        """One answer, then the socket goes: ECONNRESET on the read, EPIPE on
        the next write.  An apply is two round trips (GET_OUTPUTS, then the
        `output ...` command), so it is the one that meets the second half.

        Either wording is accepted on purpose.  Which one the user gets is a
        race between the RST arriving and the read seeing a clean EOF, and both
        were seen on this host across repeated runs of this file -- so do not
        tighten it to one of them, it will be flaky."""
        self.sway("gone")
        code, out, err, secs = self.run_cli("--backend", "sway", "--output",
                                            "HEADLESS-2", "--pos", "0x720")
        self.assertEqual((code, out), (1, ""))
        line = self.one_line(err)
        self.assertTrue("Connection reset by peer" in line
                        or "compositor IPC connection closed" in line, line)
        self.assertLess(secs, 5.0, secs)

    def test_a_reply_that_is_not_json_is_one_line(self):
        """Well-framed, and the body is not JSON: the frame header is the
        compositor's word for how long the body is, and believing it is not the
        same as believing the body."""
        self.sway("badjson")
        code, out, err, secs = self.run_cli("--backend", "sway", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Expecting property name", self.one_line(err))
        self.assertLess(secs, 5.0, secs)

    def test_a_refusal_reaches_the_user_in_sways_words(self):
        """sway answers RUN_COMMAND with a result list, and a `success: false`
        entry carries its own sentence.  That sentence is the whole of what the
        user can act on, so it is quoted rather than summarised -- and the
        command it refused is named with it, because one `output ...` line can
        hold several sub-commands and sway runs them all."""
        self.sway("refuse")
        code, out, err, _s = self.run_cli("--backend", "sway", "--output",
                                          "HEADLESS-2", "--pos", "0x720")
        self.assertEqual((code, out), (1, ""))
        line = self.one_line(err)
        self.assertIn(support.FakeSway.ERROR, line)
        self.assertIn("output HEADLESS-2 position 0 720", line)

    def test_outputs_with_no_rect_do_not_raise(self):
        """GET_OUTPUTS rows without `rect` at all.  A compositor that is not
        sway on the sway socket, or a sway too old or too new for the field:
        every position and size in the snapshot comes from it, and reading it
        as a plain subscript is the shape that used to be a KeyError somewhere
        above the user.

        The claim here is the query survives: no traceback, exit 0, both heads
        still listed.  0x0+0+0 is the fallback the missing field leaves them at,
        and it is the fallback that stays once the line the expectedFailure
        below asks for is added -- that fix adds a sentence to stderr, it does
        not invent geometry -- so stderr is only held to "no traceback, at most
        one line of ours", not to empty."""
        self.sway("partial")
        code, out, err, secs = self.run_cli("--backend", "sway", "--query")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertLessEqual(len([ln for ln in err.splitlines() if ln.strip()]), 1, err)
        # both heads are listed, and both are where a missing rect leaves them:
        # at 0,0 with no size, which is also what the screen line adds up to
        self.assertIn("HEADLESS-1 connected 0x0+0+0", out)
        self.assertIn("HEADLESS-2 connected 0x0+0+0", out)
        self.assertIn("current 0 x 0", out)
        self.assertLess(secs, 5.0, secs)

    @unittest.expectedFailure
    def test_outputs_with_no_rect_say_which_field_was_missing(self):
        """No fix number covers this one, so it is written to fail.

        T32 asks for one line naming the field.  Today the rows are folded in
        silently: every output is placed at 0,0 with no size, the screen line
        reads `current 0 x 0`, and nothing anywhere says that what the
        compositor sent had no geometry in it.  Exit 0 on a query that answered
        nothing is worse than one line."""
        self.sway("partial")
        _code, _out, err, _s = self.run_cli("--backend", "sway", "--query")
        self.assertIn("rect", self.one_line(err))

    # -- what the run leaves behind ------------------------------------------

    def test_no_socket_is_left_to_the_collector(self):
        """`cli.Session.close()` exists because nothing used to call any
        backend's close, and every run handed its socket to the garbage
        collector -- which reports it as a ResourceWarning at whatever moment it
        gets round to it.  Each of these paths leaves through a different exit,
        and every one of them has to close."""
        for mode, argv in (("ok", ("--query",)),
                           ("wedged", ("--query",)),
                           ("badjson", ("--query",)),
                           ("refuse", ("--output", "HEADLESS-2", "--pos", "0x720")),
                           ("gone", ("--output", "HEADLESS-2", "--pos", "0x720"))):
            with self.subTest(mode=mode):
                srv = support.FakeSway(mode)
                self.addCleanup(srv.close)
                self.env("SWAYSOCK", srv.path)
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    self.run_cli("--backend", "sway", *argv)
                    gc.collect()
                self.assertEqual([str(w.message) for w in caught
                                  if issubclass(w.category, ResourceWarning)], [])


if __name__ == "__main__":
    unittest.main()
