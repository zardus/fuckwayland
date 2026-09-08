#!/usr/bin/env python3
"""wxrandr's backend selection: `--backend NAME`, `--print-backend`,
`--backends`.

Everything here is hermetic — the probes are replaced by a table, so no
compositor, bus or socket is touched.  The one thing that cannot be faked
in-process is the handover itself (`execve`), which lives with the rest of
it in tests/test_passthrough_exec.py against the fake install tree.
"""

import contextlib
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

from fwcommon import passthrough
from wxrandr import cli

#: a GNOME session, as the probes would find it
GNOME = {
    "sway": (False, "no sway or i3 IPC socket ($SWAYSOCK)"),
    "kwin": (False, "the compositor does not advertise "
                    "kde_output_management_v2"),
    "mutter": (True, "org.gnome.Mutter.DisplayConfig on the session bus"),
    "wlr": (False, "the compositor does not advertise "
                   "zwlr_output_manager_v1"),
    "x11": (True, "/usr/bin/xrandr"),
}
COMPOSITOR = {"sway": "sway 1.9", "kwin": "KWin", "mutter": "Mutter",
              "wlr": "wlroots", "x11": "X server (RandR)"}
PROTOCOL = {"sway": "sway IPC (i3-ipc)",
            "kwin": "kde_output_management_v2 version 12",
            "mutter": "org.gnome.Mutter.DisplayConfig (D-Bus)",
            "wlr": "zwlr_output_manager_v1 version 4"}


class ProbeStub:
    """Stands in for cli.probe_backend and records the order it was asked
    in — which is how "the detection order is unchanged" is checked."""

    def __init__(self, table):
        self.table = dict(table)
        self.calls = []

    def __call__(self, name, env=None, verbose=False):
        self.calls.append(name)
        ok, why = self.table.get(name, (False, "unknown backend"))
        return cli.Probe(name, ok, reason="" if ok else why,
                         detail=why if ok else "",
                         compositor=COMPOSITOR.get(name) if ok else None,
                         protocol=PROTOCOL.get(name) if ok else None)


class Stubbed(unittest.TestCase):
    """A test case whose probes are a table, not a session."""

    TABLE = GNOME

    def setUp(self):
        self.probe = ProbeStub(self.TABLE)
        self.orig = cli.probe_backend
        cli.probe_backend = self.probe
        self.addCleanup(setattr, cli, "probe_backend", self.orig)
        self.saved = os.environ.get("WXRANDR_BACKEND")
        os.environ.pop("WXRANDR_BACKEND", None)
        self.addCleanup(self.restore_env)

    def restore_env(self):
        if self.saved is None:
            os.environ.pop("WXRANDR_BACKEND", None)
        else:
            os.environ["WXRANDR_BACKEND"] = self.saved

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
        return code, out.getvalue(), err.getvalue()


class Names(unittest.TestCase):
    def test_canonical_and_aliases(self):
        for spelling, want in (("auto", "auto"), ("x11", "x11"),
                               ("sway", "sway"), ("WLR", "wlr"),
                               (" mutter ", "mutter"), ("gnome", "mutter"),
                               ("GNOME", "mutter"), ("kde", "kwin"),
                               ("kwin", "kwin")):
            self.assertEqual(cli.canonical_backend(spelling), want, spelling)
        for bad in ("", None, "wayland", "xrandr", "wlroots", "sway2"):
            self.assertIsNone(cli.canonical_backend(bad), bad)

    def test_the_options_are_not_in_xrandrs_usage(self):
        """A byte-parity clone may only add options the real one has none
        of, and must not mention them where it prints the original's text."""
        for opt in ("--backend", "--print-backend", "--backends"):
            self.assertNotIn(opt, cli.USAGE, opt)


class Lookahead(unittest.TestCase):
    """The argv walk that happens before any parsing, so that main() knows
    whether an X11 session hands over."""

    def scan(self, *argv):
        return cli.scan_backend_argv(list(argv))

    def test_both_spellings(self):
        self.assertEqual(self.scan("--backend", "sway", "--query"),
                         ("sway", False, ["--query"]))
        self.assertEqual(self.scan("--backend=x11", "--query"),
                         ("x11", False, ["--query"]))
        self.assertEqual(self.scan("--query"), (None, False, ["--query"]))

    def test_an_output_named_like_the_flag_is_a_value(self):
        """`--output --backend` names an output `--backend`; the scan must
        read it as parse() does, or it would suppress a handover nobody
        asked to suppress."""
        for argv in (["--output", "--backend", "--off"],
                     ["--output", "--backend=x11", "--auto"],
                     ["--output", "DP-1", "--mode", "--backend"],
                     ["--set", "--backend", "sway"],
                     ["--addmode", "--backend", "sway"],
                     ["--setmonitor", "--backend", "sway", "none"],
                     ["--newmode", "--backend", "1", "2", "3", "4", "5",
                      "6", "7", "8", "9"],
                     ["-d", "--backend"],
                     ["--display", "--backends"]):
            self.assertEqual(cli.scan_backend_argv(list(argv)),
                             (None, False, list(argv)), argv)

    def test_after_a_stanza_it_is_still_the_flag(self):
        self.assertEqual(
            self.scan("--output", "DP-1", "--off", "--backend", "mutter"),
            ("mutter", False, ["--output", "DP-1", "--off"]))
        self.assertEqual(
            self.scan("--newmode", "m", "1", "2", "3", "4", "5", "6", "7",
                      "8", "9", "+hsync", "--backend", "kwin")[0], "kwin")

    def test_informational_options(self):
        self.assertEqual(self.scan("--print-backend"),
                         (None, True, ["--print-backend"]))
        self.assertEqual(self.scan("--backends"), (None, True, ["--backends"]))
        self.assertEqual(self.scan("--backend", "kde", "--print-backend"),
                         ("kde", True, ["--print-backend"]))

    def test_last_one_wins_like_parse(self):
        self.assertEqual(self.scan("--backend", "sway", "--backend=kwin")[0],
                         "kwin")

    def test_a_flag_with_no_value_is_still_the_flag(self):
        """It names nothing, but it *is* there.  Lose that and an X11
        session hands `--backend` to the original, and the user gets its
        `unrecognized option` where ours says what is missing."""
        self.assertEqual(self.scan("--backend"), ("", False, []))
        self.assertEqual(self.scan("--query", "--backend"),
                         ("", False, ["--query"]))
        self.assertIsNone(cli.canonical_backend(""))

    def test_the_walk_agrees_with_the_parser(self):
        """The look-ahead's whole job is to read argv exactly as parse()
        will, one step earlier -- for the flag, for the two informational
        options, and for the argv left over once the flag is removed."""
        cases = [
            ["--query"],
            ["--backend", "sway", "--query"],
            ["--backend=kde", "--print-backend"],
            ["--backends", "--verbose"],
            ["-d", ":0", "--backend", "wlr", "--dryrun"],
            ["--fb", "1920x1080", "--backend=auto", "--dpi", "96"],
            ["--output", "--backend", "--off"],
            ["--output", "DP-1", "--mode", "--backend", "--pos", "0x0"],
            ["--output", "DP-1", "--set", "--backend", "--print-backend"],
            ["--output", "DP-1", "--gamma", "1:1:1", "--backend", "mutter"],
            ["--addmode", "--backend", "--backends"],
            ["--rmmode", "--print-backend"],
            ["--newmode", "--backend", "1", "2", "3", "4", "5", "6", "7",
             "8", "9", "+hsync", "--backend", "kwin"],
            ["--setmonitor", "--backend", "x", "--backends", "--query"],
            ["--output", "DP-1", "--auto", "--backend", "sway",
             "--output", "HDMI-1", "--off"],
        ]
        for argv in cases:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):     # "not supported" warns
                opts = cli.parse(list(argv))
                flag, info, rest = cli.scan_backend_argv(list(argv))
                stripped = cli.parse(list(rest))
            self.assertEqual(cli.canonical_backend(flag), opts.backend, argv)
            self.assertEqual(info, opts.print_backend or opts.list_backends,
                             argv)
            self.assertIsNone(stripped.backend, argv)
            self.assertEqual([s.name for s in stripped.stanzas],
                             [s.name for s in opts.stanzas], argv)
            self.assertEqual(stripped.mode_ops, opts.mode_ops, argv)
            self.assertEqual(stripped.monitor_op, opts.monitor_op, argv)

    def test_an_unparseable_argv_does_not_raise(self):
        for argv in (["--backend"], ["--output"], ["--newmode", "m"],
                     ["--set"], ["--zorp", "--backend", "sway"]):
            cli.scan_backend_argv(list(argv))     # must simply not raise
        self.assertEqual(self.scan("--zorp", "--backend", "sway")[0], "sway")

    def test_our_own_apply_options_are_stripped_too(self):
        """The 0.4 retest, on both X11 images: `wxrandr --persistent --output
        Virtual-2 --below Virtual-1` came back as the real xrandr's
        `unrecognized option '--persistent'`, exit 1, layout unchanged, where
        the documents say an X11 apply works and saves nothing."""
        self.assertEqual(self.scan("--persistent", "--output", "DP-1", "--auto"),
                         (None, False, ["--output", "DP-1", "--auto"]))
        self.assertEqual(self.scan("--unsafe-gnome-overlap", "--output", "DP-1",
                                   "--pos", "960x0"),
                         (None, False, ["--output", "DP-1", "--pos", "960x0"]))
        self.assertEqual(self.scan("--backend", "x11", "--persistent",
                                   "--output", "DP-1", "--off"),
                         ("x11", False, ["--output", "DP-1", "--off"]))

    def test_which_of_our_options_this_argv_really_carries(self):
        self.assertEqual(cli.own_flags_in(["--persistent"]), {"--persistent"})
        self.assertEqual(cli.own_flags_in(["--query"]), set())
        # ...and an output *named* like one of them is a value, not a flag
        for argv in (["--output", "--persistent", "--off"],
                     ["--output", "--unsafe-gnome-overlap", "--auto"],
                     ["--mode", "--persistent"]):
            self.assertEqual(cli.own_flags_in(list(argv)), set(), argv)
        self.assertEqual(
            cli.own_flags_in(["--output", "--persistent", "--off",
                              "--persistent"]), {"--persistent"})

    def test_an_ordinary_argv_is_handed_on_untouched(self):
        argv = ["--output", "DP-1", "--primary", "--mode", "1920x1080",
                "--pos", "0x0", "--rotate", "normal", "--output", "HDMI-2",
                "--off"]
        self.assertEqual(cli.scan_backend_argv(list(argv)),
                         (None, False, argv))


class Precedence(Stubbed):
    def test_flag_beats_environment_beats_detection(self):
        os.environ["WXRANDR_BACKEND"] = "kwin"
        self.assertEqual(cli.resolve_backend("sway"),
                         ("sway", "flag", "--backend sway"))
        self.assertEqual(cli.resolve_backend(None),
                         ("kwin", "environment", "WXRANDR_BACKEND=kwin"))
        self.assertEqual(cli.resolve_backend("gnome")[0], "mutter")
        os.environ.pop("WXRANDR_BACKEND")
        self.assertEqual(cli.resolve_backend(None), (None, "detection", None))

    def test_auto_is_not_a_forcing(self):
        os.environ["WXRANDR_BACKEND"] = "auto"
        self.assertEqual(cli.resolve_backend("auto"), (None, "detection", None))
        self.assertEqual(cli.resolve_backend(None), (None, "detection", None))

    def test_x11_is_a_value_the_environment_may_hold(self):
        """It was meaningless before the flag existed; now it means what the
        flag means, and main() reads it there (see test_passthrough_exec)."""
        os.environ["WXRANDR_BACKEND"] = "x11"
        self.assertEqual(cli.resolve_backend(None),
                         ("x11", "environment", "WXRANDR_BACKEND=x11"))
        self.assertEqual(cli.resolve_backend("mutter")[0], "mutter")
        self.assertEqual(cli.resolve_backend("auto")[0], "x11")

    def test_detection_order_is_unchanged(self):
        name, _p = cli.detect_wayland()
        self.assertEqual(name, "mutter")
        self.assertEqual(self.probe.calls, ["sway", "kwin", "mutter"])
        self.probe.calls = []
        self.probe.table["sway"] = (True, "IPC socket /run/sway.sock")
        self.assertEqual(cli.detect_wayland()[0], "sway")
        self.assertEqual(self.probe.calls, ["sway"])
        self.probe.calls = []
        self.probe.table["kwin"] = (True, "kde_output_management_v2 version 12")
        self.probe.table["sway"] = (False, "no sway or i3 IPC socket")
        self.assertEqual(cli.detect_wayland()[0], "kwin")
        self.assertEqual(self.probe.calls, ["sway", "kwin"])

    def test_wlr_is_the_fallback_and_is_not_probed_for_it(self):
        self.probe.table["mutter"] = (False, "no session bus")
        self.assertEqual(cli.detect_wayland()[0], "wlr")
        self.assertNotIn("wlr", self.probe.calls)

    def test_chosen_backend_on_an_x11_session(self):
        """Detection asks the session kind first: X11 is where main() hands
        over, so that is the honest answer."""
        env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0", "PATH": "/nonexist"}
        self.assertEqual(cli.chosen_backend(None, env)[0], "x11")
        self.assertEqual(cli.chosen_backend("mutter", env)[0], "mutter")
        self.assertEqual(cli.chosen_backend("auto", env)[0], "x11")

    def test_the_flag_reaches_the_session(self):
        seen = []

        class FakeSession:
            def __init__(self, forced=None):
                seen.append(forced)
                raise cli.Fatal("stop here\n")
        orig, cli.Session = cli.Session, FakeSession
        try:
            self.assertEqual(self.run_cli("--query")[0], 1)
            self.assertEqual(self.run_cli("--backend", "kde", "--query")[0], 1)
        finally:
            cli.Session = orig
        self.assertEqual(seen, [None, "kwin"])


class Info(Stubbed):
    def test_print_backend_is_one_token(self):
        code, out, err = self.run_cli("--print-backend")
        self.assertEqual((code, out, err), (0, "mutter\n", ""))
        code, out, err = self.run_cli("--backend", "sway", "--print-backend")
        self.assertEqual((code, out), (0, "sway\n"))

    def test_print_backend_verbose(self):
        code, out, err = self.run_cli("--print-backend", "--verbose")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "mutter\n"
                              "session: wayland\n"
                              "chosen by: detection\n"
                              "compositor: Mutter\n"
                              "protocol: org.gnome.Mutter.DisplayConfig "
                              "(D-Bus)\n"
                              "available: yes\n")

    def test_print_backend_verbose_says_why_and_what_is_missing(self):
        os.environ["WXRANDR_BACKEND"] = "kwin"
        code, out, _err = self.run_cli("--print-backend", "--verbose")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "kwin", "session: wayland",
            "chosen by: environment (WXRANDR_BACKEND=kwin)",
            "available: no (the compositor does not advertise "
            "kde_output_management_v2)"])
        code, out, _err = self.run_cli("--backend", "sway", "--print-backend",
                                       "--verbose")
        self.assertEqual(out.splitlines()[2],
                         "chosen by: flag (--backend sway)")

    def test_print_backend_for_x11_names_the_real_xrandr(self):
        env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
        lines = cli.print_backend_lines(None, env, verbose=True)
        self.assertEqual(lines, ["x11", "session: x11", "chosen by: detection",
                                 "compositor: X server (RandR)",
                                 "real xrandr: /usr/bin/xrandr",
                                 "available: yes"])

    def test_backends_table(self):
        code, out, err = self.run_cli("--backends")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out,
                         "  sway    unavailable  no sway or i3 IPC socket "
                         "($SWAYSOCK)\n"
                         "  kwin    unavailable  the compositor does not "
                         "advertise kde_output_management_v2\n"
                         "* mutter  available    "
                         "org.gnome.Mutter.DisplayConfig on the session bus\n"
                         "  wlr     unavailable  the compositor does not "
                         "advertise zwlr_output_manager_v1\n"
                         "  x11     available    /usr/bin/xrandr\n")

    def test_backends_marks_what_auto_would_choose_not_what_is_forced(self):
        code, out, _err = self.run_cli("--backend", "sway", "--backends")
        self.assertEqual(code, 0)
        marked = [ln[2:8].strip() for ln in out.splitlines()
                  if ln.startswith("*")]
        self.assertEqual(marked, ["mutter"])

    def test_the_informational_options_never_open_a_session(self):
        class NoSession:
            def __init__(self, forced=None):
                raise AssertionError("--print-backend touched the layout")
        orig, cli.Session = cli.Session, NoSession
        try:
            self.assertEqual(self.run_cli("--print-backend")[0], 0)
            self.assertEqual(self.run_cli("--backends")[0], 0)
            self.assertEqual(self.run_cli("--backend", "kwin",
                                          "--print-backend")[0], 0)
        finally:
            cli.Session = orig


class Errors(Stubbed):
    def test_an_unknown_name_lists_the_valid_ones(self):
        code, out, err = self.run_cli("--backend", "banana", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err,
                         "xrandr: --backend: invalid argument 'banana'; "
                         "valid: auto, x11, sway, wlr, mutter, kwin\n"
                         "Try 'xrandr --help' for more information.\n")
        code, _out, err = self.run_cli("--backend=nope")
        self.assertEqual(code, 1)
        self.assertIn("invalid argument 'nope'", err)

    def test_forcing_an_unavailable_backend_names_what_is_missing(self):
        code, out, err = self.run_cli("--backend", "sway", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: --backend sway is not available in "
                              "this session: no sway or i3 IPC socket "
                              "($SWAYSOCK)\n")
        code, _out, err = self.run_cli("--backend", "kde", "--output", "DP-1",
                                       "--off")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xrandr: --backend kwin is not available in "
                              "this session: the compositor does not "
                              "advertise kde_output_management_v2\n")

    def test_no_silent_fallback(self):
        """The forced one fails; the one auto would have picked is not tried
        behind the user's back."""
        code, _out, err = self.run_cli("--backend", "wlr", "--query")
        self.assertEqual(code, 1)
        self.assertIn("--backend wlr is not available", err)
        self.assertNotIn("mutter", err)

    def test_the_environment_variable_keeps_its_own_behaviour(self):
        """WXRANDR_BACKEND is *not* pre-checked (that would change bytes
        every existing test pins): the backend itself says what is wrong."""
        os.environ["WXRANDR_BACKEND"] = "sway"
        code, _out, err = self.run_cli("--query")
        self.assertEqual(code, 1)
        self.assertNotIn("is not available in this session", err)
        self.assertIn("Can't open display", err)

    def test_x11_from_a_library_call_is_one_line(self):
        """At a command line `--backend x11` never reaches here: main() has
        exec'd the real xrandr.  Embedded, where a process may not be
        replaced, it is a fatal, not a traceback."""
        code, out, err = self.run_cli("--backend", "x11", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: --backend x11 hands over to the real "
                              "xrandr, which an embedded call cannot do\n")


class EnvironmentX11(Stubbed):
    """`WXRANDR_BACKEND=x11` is the same request as `--backend x11`.  At a
    command line main() hands over before parsing (proved with a real
    `execve` in tests/test_passthrough_exec.py); embedded, where a process
    may not be replaced, it is one fatal line -- naming the variable that
    asked, not a flag nobody typed."""

    def test_it_is_one_line_naming_the_variable(self):
        os.environ["WXRANDR_BACKEND"] = "x11"
        code, out, err = self.run_cli("--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: WXRANDR_BACKEND=x11 hands over to the "
                              "real xrandr, which an embedded call cannot "
                              "do\n")

    def test_the_flag_still_beats_it(self):
        os.environ["WXRANDR_BACKEND"] = "x11"
        code, _out, err = self.run_cli("--backend", "sway", "--query")
        self.assertEqual(code, 1)
        self.assertIn("--backend sway is not available", err)
        self.assertNotIn("WXRANDR_BACKEND", err)

    def test_the_informational_options_answer_for_it(self):
        os.environ["WXRANDR_BACKEND"] = "x11"
        code, out, _err = self.run_cli("--print-backend")
        self.assertEqual((code, out), (0, "x11\n"))


class DroppedOnTheHandover(Stubbed):
    """What an X11 session does with the two options only we have.

    Measured on both X11 images in the 0.4 retest: `wxrandr --persistent
    --output Virtual-2 --below Virtual-1` came back as the real xrandr's
    `unrecognized option '--persistent'`, exit 1, layout unchanged -- so the
    option had to be stripped before the handover, which it now is
    (`Lookahead.test_our_own_apply_options_are_stripped_too`).  Stripping it
    silently is the other half of the same problem: a script that has been
    asking for a persistent layout on an X11 box has never been getting one and
    would never learn that from the output.

    `--unsafe-gnome-overlap` is the opposite case and stays refused: X11 places
    overlapping monitors by itself, so a user who typed it has misunderstood
    something rather than asked for something unavailable."""

    def handover(self, *argv):
        """`main()` with a real argv, so `entry` is true and the handover is the
        one a command line gets; `maybe_exec_real` is recorded rather than run,
        because the real one would `execve` this process."""
        seen = []

        def record(tool, args=None, **kw):
            seen.append((tool, list(args or []), kw))
            return 0

        out, err = io.StringIO(), io.StringIO()
        real, passthrough.maybe_exec_real = passthrough.maybe_exec_real, record
        saved_argv = list(sys.argv)
        sys.argv = ["wxrandr"] + list(argv)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main()
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 0
        finally:
            passthrough.maybe_exec_real = real
            sys.argv = saved_argv
        return code, out.getvalue(), err.getvalue(), seen

    def test_persistent_is_dropped_and_said_once(self):
        code, out, err, seen = self.handover("--backend", "x11", "--persistent",
                                             "--output", "DP-1", "--auto")
        self.assertEqual((code, out), (0, ""))
        # the real xrandr gets the command without our option, and nothing else
        # about it changed
        self.assertEqual(len(seen), 1, seen)
        tool, args, kw = seen[0]
        self.assertEqual((tool, args), ("xrandr", ["--output", "DP-1", "--auto"]))
        self.assertEqual((kw["entry"], kw["force"]), (True, True))
        # exactly one line, and it names the option and the session
        lines = [ln for ln in err.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, err)
        self.assertIn("--persistent", lines[0])
        self.assertIn("X11", lines[0])
        self.assertTrue(lines[0].startswith("xrandr: "), lines[0])

    def test_an_ordinary_handover_says_nothing(self):
        """The control: every command that does not carry one of our options
        hands over in silence, which is the whole promise of the clone."""
        code, out, err, seen = self.handover("--backend", "x11", "--output",
                                             "DP-1", "--auto")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(seen[0][1], ["--output", "DP-1", "--auto"])

    def test_the_overlap_flag_is_still_refused_rather_than_dropped(self):
        code, out, err, seen = self.handover("--backend", "x11",
                                             "--unsafe-gnome-overlap",
                                             "--output", "DP-1", "--pos", "960x0")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(seen, [])              # nothing was handed over at all
        self.assertIn("--unsafe-gnome-overlap only means anything on GNOME", err)
        self.assertNotIn("--persistent", err)

    def test_both_at_once_is_the_refusal_alone(self):
        """The refusal is the whole answer, and the drop line is not said with
        it.  Measured here: stderr is exactly the one refusal line and `seen` is
        empty -- the refusal returns 1 before the PERSISTENT_FLAG branch is
        reached, and nothing was handed over, so nothing was dropped either.
        Saying both would name an option that never got as far as mattering."""
        code, _out, err, seen = self.handover(
            "--backend", "x11", "--persistent", "--unsafe-gnome-overlap",
            "--output", "DP-1", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertEqual(seen, [])
        lines = [ln for ln in err.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, err)
        self.assertIn("--unsafe-gnome-overlap only means anything on GNOME", lines[0])
        self.assertNotIn("is dropped on X11", err)


class ReadmeOnTheHandover(unittest.TestCase):
    """The paragraph that promises the handover, against what it does.

    F7.5(a): README said `execve` and argv *untouched*, and argv is not
    untouched -- `scan_backend_argv` strips `--backend`, `--persistent` and
    `--unsafe-gnome-overlap` before the original ever sees them, because the
    original has never had any of the three and would answer `unrecognized
    option` to the whole command."""

    def readme(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            return f.read()

    def sentences(self):
        """Every sentence of README, unwrapped: the claim and the exception to
        it have to be in the same one, or a reader meets the claim alone."""
        out = []
        for para in self.readme().split("\n\n"):
            out.extend(" ".join(para.split()).split(". "))
        return out

    def test_untouched_argv_is_only_claimed_where_our_options_are_named(self):
        claims = [s for s in self.sentences() if "argv untouched" in s]
        self.assertTrue(claims, "the handover paragraph is gone from README")
        for s in claims:
            for flag in cli.OWN_APPLY_FLAGS:
                self.assertIn(flag, s, s)

    def test_the_readme_says_what_becomes_of_each_of_them(self):
        """Not just that they are stripped: dropped and refused are different
        outcomes and the two options get different ones."""
        text = " ".join(self.readme().split())
        self.assertIn("`--persistent` is dropped", text)
        self.assertIn("`--unsafe-gnome-overlap` is refused", text)


class X11Probe(unittest.TestCase):
    """The one probe with no compositor in it: which real xrandr `x11` is."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxr-x11-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_finds_the_real_xrandr(self):
        p = cli.probe_backend("x11", {"PATH": self.tmp})
        self.assertFalse(p.available)
        self.assertEqual(p.reason,
                         "no real xrandr on PATH (install x11-xserver-utils)")
        real = os.path.join(self.tmp, "xrandr")
        with open(real, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(real, stat.S_IRWXU)
        passthrough.reset_cache()
        p = cli.probe_backend("x11", {"PATH": self.tmp})
        self.assertTrue(p.available, p.reason)
        self.assertEqual(p.detail, real)

    def test_an_unusable_override_is_the_reason(self):
        p = cli.probe_backend("x11", {"PATH": self.tmp,
                                      "WXRANDR_REAL_XRANDR": "/no/such/tool"})
        self.assertFalse(p.available)
        self.assertIn("WXRANDR_REAL_XRANDR", p.reason)
        self.assertNotIn("\n", p.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
