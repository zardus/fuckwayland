#!/usr/bin/env python3
"""script mode — tokenizing, quoting, $N/$ENV expansion, per-line
execution, stack persistence, stdin form, script-vs-args detection."""

import contextlib
import io
import os
import sys
import unittest
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fwcommon.errors import CmdError
from wdotool import cli, commands

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

CALLS = []
FAKE_MOD = "wdotool._test_fake_script_cmds"


def _install_fakes():
    mod = types.ModuleType(FAKE_MOD)

    def cmd_rec(ctx, args):  # records and consumes the rest of the line
        CALLS.append(tuple(args))
        return len(args)

    def cmd_rec1(ctx, args):  # records and consumes exactly one token
        CALLS.append((args[0],))
        return 1

    def cmd_boom(ctx, args):
        raise CmdError("boom happened")

    def cmd_pushwins(ctx, args):
        ctx.stack = [111, 222]
        return 0

    def cmd_usewin(ctx, args):
        CALLS.append(("usewin", ctx.resolve_window(), tuple(ctx.resolve_windows("%@"))))
        return 0

    for f in (cmd_rec, cmd_rec1, cmd_boom, cmd_pushwins, cmd_usewin):
        setattr(mod, f.__name__, f)
    sys.modules[FAKE_MOD] = mod
    for name in ("rec", "rec1", "boom", "pushwins", "usewin"):
        commands.REGISTRY[name] = (FAKE_MOD, "cmd_" + name)


def run(argv, stdin=None):
    CALLS.clear()
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["wdotool"] + argv)
    finally:
        sys.stdin = old_stdin
    return rc, out.getvalue(), err.getvalue()


def script(content):
    fd, path = tempfile.mkstemp(prefix="wdo_script_", suffix=".xdo")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def _run_all():
    orig_registry = dict(commands.REGISTRY)
    _install_fakes()
    tmpfiles = []
    try:
        os.environ["WD_TEST_VAR"] = "hello world"
        os.environ["WD_TEST_EMPTY"] = ""
        os.environ.pop("WD_TEST_UNSET", None)

        # tokenizing: comments, blank lines, quoting, mid-token $ and # are literal.
        # An unquoted token starting with '#' ends the line as a comment wherever it
        # stands (measured against xdotool 4.20260303.1, the parity oracle: `echo
        # before # trailing` prints `before`; 3.2016 only honoured the first token),
        # while `x#notcomment`, "#quoted" and \#escaped are ordinary tokens. An empty
        # token is an argument, not something to drop: `echo A "" B` passes three.
        p = script(
            "# full-line comment\n"
            "\n"
            "   \t \n"
            'rec one "two words" \'three words\' plain$HOME x#notcomment\n'
            "rec before # trailing comment\n"
            'rec "" empty-kept\n'
            'rec quoted "#not" \'#either\' \\#nor\n'
        )
        tmpfiles.append(p)
        rc, out, err = run([p])
        assert rc == 0 and err == "", (rc, err)
        assert CALLS == [
            ("one", "two words", "three words", "plain$HOME", "x#notcomment"),
            ("before",),
            ("", "empty-kept"),
            ("quoted", "#not", "#either", "\\#nor"),
        ], CALLS

        # $N and $ENV expansion; quoted "$1" and '$2' still expand; env value with
        # spaces stays ONE token; an empty env value is an empty argument, exactly
        # as in the C tool (`xdotool exec --sync echo A $EMPTY B` prints "A  B" in
        # both); $0 is the script path.
        # DELIBERATE DIVERGENCE from the C tool: xdotool's tokenizer resumes at
        # name_start + len(expanded_value) + 1, so any expansion whose value length
        # differs from its source span corrupts the rest of the line (leftover name
        # chars become tokens, or stale buffer bytes get read). wdotool expands
        # cleanly regardless of length; test_cli_parity.py sticks to the
        # length-matched cases where the C tool is also clean.
        p = script('rec $1 "$1" \'$2\' $WD_TEST_VAR $WD_TEST_EMPTY tail\nrec $0\n')
        tmpfiles.append(p)
        rc, out, err = run([p, "AA", "BB"])
        assert rc == 0 and err == "", (rc, err)
        assert CALLS == [("AA", "AA", "BB", "hello world", "", "tail"), (p,)], CALLS

        # length-mismatched expansions stay clean (would corrupt the line in C)
        p = script("rec $1 $2 tail\n")
        tmpfiles.append(p)
        rc, out, err = run([p, "AAA", "BBBB"])
        assert rc == 0 and CALLS == [("AAA", "BBBB", "tail")], CALLS

        # an empty quoted token is an argument and the rest of the line survives;
        # measured against xdotool 3.20160805.1, which does the same
        p = script('rec "" kept\n')
        tmpfiles.append(p)
        rc, out, err = run([p])
        assert rc == 0 and CALLS == [("", "kept")], CALLS

        # $1abc parses like atoi: uses $1, drops the suffix
        p = script("rec $1abc\n")
        tmpfiles.append(p)
        rc, out, err = run([p, "AA"])
        assert rc == 0 and CALLS == [("AA",)], CALLS

        # missing positional: exact message, immediate abort, earlier lines ran
        p = script("rec first\nrec $3\nrec never\n")
        tmpfiles.append(p)
        rc, out, err = run([p, "only-one"])
        assert rc == 1, rc
        assert err == "wdotool: error: `%s' needs at least 3 arguments; only 1 given\n" % p, err
        assert CALLS == [("first",)], CALLS

        # singular form: needs at least 1 argument
        p = script("rec $1\n")
        tmpfiles.append(p)
        rc, out, err = run([p])
        assert err == "wdotool: error: `%s' needs at least 1 argument; only 0 given\n" % p, err
        assert rc == 1

        # unset env var: exact message, immediate abort
        p = script("rec ok\nrec $WD_TEST_UNSET\n")
        tmpfiles.append(p)
        rc, out, err = run([p])
        assert rc == 1
        assert err == "wdotool: error: environment variable $WD_TEST_UNSET is not set.\n", err
        assert CALLS == [("ok",)], CALLS

        # a failing line does not stop later lines; last executed line's rc wins
        p = script("rec line1\nboom\nrec line3\n")
        tmpfiles.append(p)
        rc, out, err = run([p])
        assert rc == 0 and err == "boom happened\n", (rc, err)
        assert CALLS == [("line1",), ("line3",)], CALLS

        p = script("rec line1\nboom\n")
        tmpfiles.append(p)
        rc, out, err = run([p])
        assert rc == 1, rc  # failing line last

        # failure mid-line skips the rest of that line only
        p = script("boom rec skipped\nrec ran\n")
        tmpfiles.append(p)
        rc, out, err = run([p])
        assert rc == 0 and CALLS == [("ran",)], CALLS

        # window stack persists across lines (single context)
        p = script("pushwins\nusewin\n")
        tmpfiles.append(p)
        rc, out, err = run([p])
        assert rc == 0 and CALLS == [("usewin", 111, (111, 222))], CALLS

        # chaining within a script line
        p = script("rec1 a rec1 b\n")
        tmpfiles.append(p)
        rc, out, err = run([p])
        assert rc == 0 and CALLS == [("a",), ("b",)], CALLS

        # unknown command inside a script line
        p = script("nosuchcmd\nrec after\n")
        tmpfiles.append(p)
        rc, out, err = run([p])
        assert rc == 0 and CALLS == [("after",)], CALLS
        assert err == ("wdotool: Unknown command: nosuchcmd\n"
                       "Run 'wdotool help' if you want a command list\n"), err

        # stdin form: wdotool -
        rc, out, err = run(["-"], stdin="rec via stdin\nrec $1\n" )
        # $1 with no extra argv -> error (argv is ['wdotool', '-'])
        assert rc == 1
        assert err == "wdotool: error: `-' needs at least 1 argument; only 0 given\n", err
        assert CALLS == [("via", "stdin")], CALLS

        rc, out, err = run(["-"], stdin='rec "from stdin"\n')
        assert rc == 0 and CALLS == [("from stdin",)], CALLS

        # detection: a file whose name is also a command runs as the command
        old_cwd = os.getcwd()
        d = tempfile.mkdtemp(prefix="wdo_det_")
        os.chdir(d)
        try:
            with open("rec", "w") as f:
                f.write("boom\n")  # would fail if executed as a script
            rc, out, err = run(["rec", "cmdwins"])
            assert rc == 0 and CALLS == [("cmdwins",)], (rc, CALLS)
        finally:
            os.chdir(old_cwd)
            os.unlink(os.path.join(d, "rec"))
            os.rmdir(d)

        # nonexistent path that is not a command: args mode -> unknown command
        rc, out, err = run(["/no/such/file/xyz"])
        assert rc == 1 and "Unknown command: /no/such/file/xyz" in err, err

        # unreadable file: C fopen failure message
        p = script("rec nope\n")
        tmpfiles.append(p)
        os.chmod(p, 0)
        rc, out, err = run([p])
        if rc == 0:  # running as root: chmod 0 still readable
            assert CALLS == [("nope",)], CALLS
        else:
            assert rc == 1 and err == "Failure opening '%s': Permission denied\n" % p, err
        os.chmod(p, 0o600)

        # directory as script: fopen succeeds, fgets EOF -> empty script, rc 0
        d = tempfile.mkdtemp(prefix="wdo_dir_")
        rc, out, err = run([d])
        assert rc == 0 and out == "" and err == "", (rc, out, err)
        os.rmdir(d)
    finally:
        commands.REGISTRY.clear()
        commands.REGISTRY.update(orig_registry)
        sys.modules.pop(FAKE_MOD, None)
        for p in tmpfiles:
            try:
                os.unlink(p)
            except OSError:
                pass


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
    print("test_cli_script: OK")
