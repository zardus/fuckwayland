#!/usr/bin/env python3
"""chain driver — dispatch, consumed-token accounting, error/exit
semantics, window-stack integration. Plain asserts, no pytest."""

import contextlib
import io
import os
import sys
import unittest
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from w11common.errors import CmdError
from wdotool import cli, commands

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

CALLS = []
FAKE_MOD = "wdotool._test_fake_cmds"


def _install_fakes():
    mod = types.ModuleType(FAKE_MOD)

    def cmd_grab2(ctx, args):
        CALLS.append(("grab2", tuple(args[:2]), getattr(ctx, "cmd_name", None)))
        return 2

    def cmd_boom(ctx, args):
        raise CmdError("boom happened")

    def cmd_abort7(ctx, args):
        raise cli.ChainAbort(7)

    def cmd_soft(ctx, args):
        ctx.exit_code = 1
        return 0

    def cmd_overeat(ctx, args):
        return 99

    def cmd_pushwins(ctx, args):
        ctx.stack = [111, 222]
        return 0

    def cmd_usewin(ctx, args):
        CALLS.append(("usewin", ctx.resolve_window(), tuple(ctx.resolve_windows("%@"))))
        return 0

    for f in (cmd_grab2, cmd_boom, cmd_abort7, cmd_soft, cmd_overeat,
              cmd_pushwins, cmd_usewin):
        setattr(mod, f.__name__, f)
    sys.modules[FAKE_MOD] = mod
    for name in ("grab2", "boom", "abort7", "soft", "overeat", "pushwins", "usewin"):
        commands.REGISTRY[name] = (FAKE_MOD, "cmd_" + name)


def run(argv):
    CALLS.clear()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["wdotool"] + argv)
    return rc, out.getvalue(), err.getvalue()


def _run_all():
    orig_registry = dict(commands.REGISTRY)
    _install_fakes()
    try:
        # consumed-token accounting: grab2 eats 2 tokens, twice in a row
        rc, out, err = run(["grab2", "a", "b", "grab2", "c", "d"])
        assert rc == 0 and err == "", (rc, err)
        assert CALLS == [("grab2", ("a", "b"), "grab2"), ("grab2", ("c", "d"), "grab2")], CALLS

        # leftover token that is not a command -> xdotool's exact error, chain ran first
        rc, out, err = run(["grab2", "a", "b", "leftover"])
        assert rc == 1
        assert err == ("wdotool: Unknown command: leftover\n"
                       "Run 'wdotool help' if you want a command list\n"), err
        assert CALLS == [("grab2", ("a", "b"), "grab2")], CALLS

        # unknown first command
        rc, out, err = run(["nosuchcmd"])
        assert rc == 1
        assert err == ("wdotool: Unknown command: nosuchcmd\n"
                       "Run 'wdotool help' if you want a command list\n"), err

        # case-insensitive dispatch, cmd_name preserved as typed
        rc, out, err = run(["GRAB2", "a", "b"])
        assert rc == 0 and CALLS == [("grab2", ("a", "b"), "GRAB2")], CALLS

        # CmdError: message on stderr, exit 1, chain aborted
        rc, out, err = run(["boom", "grab2", "a", "b"])
        assert rc == 1 and err == "boom happened\n", (rc, err)
        assert CALLS == [], CALLS

        # ChainAbort: propagates code, silent
        rc, out, err = run(["abort7", "grab2", "a", "b"])
        assert rc == 7 and err == "", (rc, err)
        assert CALLS == [], CALLS

        # ctx.exit_code: non-fatal, chain continues, exit code sticks
        rc, out, err = run(["soft", "grab2", "a", "b"])
        assert rc == 1 and err == "", (rc, err)
        assert CALLS == [("grab2", ("a", "b"), "grab2")], CALLS

        # over-consumption clamps with the C bug message
        rc, out, err = run(["overeat", "x"])
        assert rc == 0
        assert err == "Can't consume 99 args; are only 1 available. This is a bug.\n", err

        # window stack: %1 default and %@ across chained commands
        rc, out, err = run(["pushwins", "usewin"])
        assert rc == 0 and CALLS == [("usewin", 111, (111, 222))], CALLS

        # %N out of range via a real ctx error path
        rc, out, err = run(["usewin"])
        assert rc == 1 and err != "", (rc, err)

        # "--" is not a command (C never consults optind for the chain start)
        rc, out, err = run(["--", "grab2", "a", "b"])
        assert rc == 1
        assert err.startswith("wdotool: Unknown command: --\n"), err
        assert CALLS == [], CALLS

        # bare invocation: usage on stderr, help on stdout, exit 1
        rc, out, err = run([])
        assert rc == 1 and err == "Usage: wdotool <cmd> <args>\n", (rc, err)
        assert out.startswith("Available commands:\n") and "  sleep\n" in out, out

        # -h/--help/-v/--version/help/version/HELP handling
        for argv in (["-h"], ["--help"], ["-help"], ["--h"], ["help"], ["HELP"],
                     ["help", "grab2", "a", "b"], ["-hv"], ["-h", "--badopt"]):
            rc, out, err = run(argv)
            assert rc == 0 and out.startswith("Available commands:\n"), (argv, rc, out)
            assert CALLS == [], (argv, CALLS)  # help at argv[1] short-circuits the chain
        for argv in (["-v"], ["--version"], ["-ver"], ["version"], ["VERSION"],
                     ["version", "grab2", "a", "b"]):
            rc, out, err = run(argv)
            assert rc == 0 and out == "xdotool version %s\n" % cli.XDO_VERSION, (argv, out)
            assert CALLS == [], (argv, CALLS)

        # unrecognized top-level option
        rc, out, err = run(["-x"])
        assert rc == 1 and out == ""
        assert err == ("wdotool: unrecognized option '-x'\n"
                       "Usage: wdotool <cmd> <args>\n"), err
        rc, out, err = run(["--badopt"])
        assert rc == 1
        assert err == ("wdotool: unrecognized option '--badopt'\n"
                       "Usage: wdotool <cmd> <args>\n"), err

        # help/version as chained (non-first) commands consume only themselves
        rc, out, err = run(["grab2", "a", "b", "version", "grab2", "c", "d"])
        assert rc == 0 and out == "xdotool version %s\n" % cli.XDO_VERSION, out
        assert CALLS == [("grab2", ("a", "b"), "grab2"), ("grab2", ("c", "d"), "grab2")], CALLS
        rc, out, err = run(["grab2", "a", "b", "help", "grab2", "c", "d"])
        assert rc == 0 and out.startswith("Available commands:\n"), out
        assert len(CALLS) == 2, CALLS

        # registry completeness: all 48 xdotool commands known
        expected = """getactivewindow getwindowfocus getwindowname getwindowclassname
            getwindowpid getwindowgeometry getdisplaygeometry search selectwindow help
            version behave behave_screen_edge click getmouselocation key keydown keyup
            mousedown mousemove mousemove_relative mouseup set_window type
            windowactivate windowfocus windowkill windowclose windowquit windowmap
            windowminimize windowmove windowraise windowlower windowreparent windowsize
            windowstate windowunmap set_num_desktops get_num_desktops set_desktop
            get_desktop set_desktop_for_window get_desktop_for_window
            get_desktop_viewport set_desktop_viewport exec sleep""".split()
        assert len(expected) == 48
        for name in expected:
            assert commands.is_command(name), name
            assert commands.is_command(name.upper()), name

        # a registered command whose cmd_* has not landed raises CmdError cleanly
        commands.REGISTRY["stubtest"] = ("wdotool.misc_cmds", "cmd_missing_xyz")
        rc, out, err = run(["stubtest", "a"])
        assert rc == 1 and err == "stubtest: not implemented\n", (rc, err)
    finally:
        commands.REGISTRY.clear()
        commands.REGISTRY.update(orig_registry)
        sys.modules.pop(FAKE_MOD, None)


class ScriptBody(unittest.TestCase):
    """The whole file, as one test.

    These four files are plain-assert scripts, and that is worth keeping:
    every line reads as the thing it pins. What is not worth keeping is
    running them at *import* time, where one broken assertion is a
    collection error that aborts the entire suite instead of failing one
    test. The body moved into _run_all() unchanged; splitting it into a
    method per group is a separate change."""

    def test_script_body(self):
        _run_all()


if __name__ == "__main__":
    _run_all()
    print("test_cli_chain: OK")
