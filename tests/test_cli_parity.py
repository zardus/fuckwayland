#!/usr/bin/env python3
"""byte-for-byte parity vs the real xdotool (help, version, usage,
option errors, exec/sleep semantics, script mode). Needs the real xdotool on
PATH (nix develop); skips cleanly otherwise. Cases that need input injection
or a window backend are out of scope here."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# This shim is named `xdotool` and the real xdotool is on PATH -- which on
# an X11 session is exactly the arrangement that makes wdotool hand over to
# it (w11common/passthrough.py). The oracle would then compare the real xdotool
# with itself and pass tautologically, so keep our own code in the loop.
os.environ["W11_PASSTHROUGH"] = "never"


def skip(reason):
    """Bow out of a comparison this box cannot make.

    Standalone (`python3 tests/test_cli_parity.py`) that is a message and a
    zero exit, the way it has always been. Under an importing runner --
    `python3 -m unittest discover -s tests` -- a SystemExit raised while the
    module is being imported is caught by the loader and reported as a
    *failed import*, so raise the skip that loader understands instead.
    """
    print("test_cli_parity: SKIP (%s)" % reason)
    if __name__ == "__main__":
        sys.exit(0)
    raise unittest.SkipTest(reason)


from wdotool import cli            # noqa: E402  (after the passthrough guard above)

REAL = shutil.which("xdotool")
if not REAL:
    skip("no real xdotool on PATH; run under nix develop")


def _oracle_version(exe):
    """The last word of `xdotool version`, or None when the binary cannot say.

    Both generations print one line, `xdotool version <v>`, and both answer it
    with no X display on this host (measured: rc 0 from Ubuntu's
    3.20160805.1)."""
    try:
        p = subprocess.run([exe, "version"], capture_output=True, timeout=30)
    except OSError:
        return None
    text = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode != 0 or not text.startswith("xdotool version "):
        return None
    return text.split()[-1]


# Every comparison below is byte-for-byte against ONE xdotool: the version
# string is in the help text, in `version`, and in the usage blocks. Ubuntu
# 26.04 ships 3.20160805.1, which fails at the very first compare (`version`)
# and takes the other ~40 with it -- a red file that says nothing about this
# clone. The pinned oracle is the flake's 4.20260303.1; scripts/parity-oracle.sh
# puts it on PATH and is what actually runs these.
ORACLE_VERSION = _oracle_version(REAL)
if ORACLE_VERSION != cli.XDO_VERSION:
    skip("%s is xdotool %s; this clone is written against %s -- run "
         "scripts/parity-oracle.sh (or nix develop)"
         % (REAL, ORACLE_VERSION or "unreadable", cli.XDO_VERSION))

# Shim named "xdotool" so prog-name-bearing messages compare byte-for-byte.
SHIMDIR = tempfile.mkdtemp(prefix="wdo_parity_")
SHIM = os.path.join(SHIMDIR, "xdotool")
with open(SHIM, "w") as f:
    f.write(
        "#!%s\nimport sys\nsys.path.insert(0, %r)\n"
        "from wdotool import cli\nsys.exit(cli.main())\n" % (sys.executable, ROOT)
    )
os.chmod(SHIM, 0o755)


def run(exe, argv, stdin=None, cwd=None):
    # argv[0] = "xdotool" for both (the C binary prints argv[0] verbatim;
    # wdotool prints its basename)
    p = subprocess.run(
        ["xdotool"] + argv, executable=exe, input=stdin,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, timeout=30
    )
    return p.returncode, p.stdout, p.stderr


def compare(argv, stdin=None, cwd=None, label=None):
    real = run(REAL, argv, stdin, cwd)
    ours = run(SHIM, argv, stdin, cwd)
    assert real == ours, "MISMATCH %s\n real=%r\n ours=%r" % (label or argv, real, ours)


def _run_all():
    # xdo_new() runs before the command dispatch, so without a display the real
    # xdotool answers "Can't open display" to `version` and `help` as well --
    # 3.20160805.1 does, measured -- and there is nothing left here to compare.
    # (The option spellings `--version`/`-h` are handled before that and would
    # still work; a half-oracle is not worth the special case.)
    have_display = run(REAL, ["sleep", "0"])[0] == 0
    if not have_display:
        skip("no X display; the real xdotool opens one before it dispatches")

    compare(["version"])
    compare(["-v"])
    compare(["--version"])
    compare(["-ver"])
    compare(["VERSION"])
    compare(["help"])
    compare(["HELP"])
    compare(["-h"])
    compare(["--help"])
    compare(["-help"])
    compare(["--h"])
    compare(["-hv"])
    compare([])
    compare(["-x"])
    compare(["--badopt"])
    compare(["help", "extra", "args"])
    compare(["version", "extra"])

    compare(["badcommand"])
    compare(["sleep", "0.01", "badcmd"])
    compare(["--", "version"])
    compare(["sleep"])
    compare(["sleep", "-x"])
    compare(["sleep", "-1"])
    compare(["sleep", "--help", "sleep", "5"])
    compare(["sleep", "--", "0.01"])
    compare(["sleep", "0.01", "0.01"])
    compare(["SLEEP", "0.01"])  # case-insensitive dispatch, usage name as typed
    compare(["exec"])
    compare(["exec", "--badopt", "true"])
    compare(["exec", "--help", "sleep", "5"])
    compare(["exec", "--sync", "echo", "hi", "sleep", "5"])
    compare(["exec", "--sync", "--args", "2", "echo", "hi", "sleep", "0.01"])
    compare(["exec", "--sync", "--terminator", "XX", "echo", "a", "b", "XX", "sleep", "0.01"])
    compare(["exec", "--sync", "--args", "1", "--terminator", "XX", "true"])
    compare(["exec", "--sync", "--args", "5", "echo", "hi"])
    compare(["exec", "--sync", "false", "sleep", "5"])
    compare(["exec", "--sync", "sh", "-c", "exit 7"])
    compare(["exec", "--sy", "true", "--args"], label="abbreviated --sy")
    compare(["exec", "--sync", "echo", "opt-like", "--sync", "-x"])

    # script mode
    tmp = tempfile.mkdtemp(prefix="wdo_parity_scripts_")


    def script(content):
        path = tempfile.mkstemp(dir=tmp, suffix=".xdo")[1]
        with open(path, "w") as f:
            f.write(content)
        return path


    # NOTE: the C tokenizer resumes parsing at name_start + len(expanded) + 1, so
    # an expansion only parses cleanly when the value length matches the source
    # span; other lengths corrupt the rest of the line (stale-buffer reads). Empty
    # quoted tokens ("") likewise drop the rest of the line. wdotool deliberately
    # implements the documented, clean semantics instead, so parity cases stick to
    # the clean paths: unquoted $N with 1-char values, quoted "$N" with 2-char
    # values, env values exactly as long as the variable name.
    os.environ["WD_PARITY_VAR"] = "two words xyz"  # len == len("WD_PARITY_VAR")
    os.environ.pop("WD_PARITY_UNSET", None)

    p = script(
        "# comment\n"
        "\n"
        'exec --sync echo one "two words" \'three words\' plain$HOME x#nc\n'
        "exec --sync echo before # trailing\n"
        "exec --sync echo $WD_PARITY_VAR\n"
    )
    compare([p], label="script quoting")
    p = script('exec --sync echo "$1" "$2" tail\n')
    compare([p, "AA", "BB"], label="script positionals quoted")
    p = script("exec --sync echo $1 $2 tail\n")
    compare([p, "A", "B"], label="script positionals unquoted")
    p = script("exec --sync echo first\nexec --sync echo $3\nexec --sync echo never\n")
    compare([p, "one"], label="script missing positional")
    p = script("exec --sync echo a\nexec --sync echo $WD_PARITY_UNSET\n")
    compare([p], label="script unset env")
    p = script("exec --sync echo ran\nsleep -x\nexec --sync echo still-ran\n")
    compare([p], label="script failing line continues")
    p = script("exec --sync echo ok\nsleep -x\n")
    compare([p], label="script failing last line rc")
    compare(["-"], stdin=b"exec --sync echo from stdin\n", label="stdin script")
    compare(["-"], stdin=b"exec --sync echo a\nbadcmd\n", label="stdin bad cmd")

    # script-vs-command detection: a file named like a command loses to the command
    d = tempfile.mkdtemp(prefix="wdo_parity_det_")
    with open(os.path.join(d, "sleep"), "w") as f:
        f.write("badcmd-in-file\n")
    compare(["sleep", "0.01"], cwd=d, label="command beats file")
    compare([os.path.join(d, "sleep")], label="explicit path runs as script")

    # directory as script arg
    compare([d + "-nonexistent" ], label="missing path -> unknown command")
    compare([d], label="directory as script")

    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(SHIMDIR, ignore_errors=True)


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
    print("test_cli_parity: OK")
