#!/usr/bin/env python3
"""scripts/build-pyz.sh and scripts/build-deb.sh: what they build, and what they refuse.

Both are run against a temporary copy of the tree, never against the tree
itself: `build-pyz.sh` writes into `dist/`, which parallel test runs would race
on, and `build-deb.sh` edits `debian/` and moves files into `release/`, which is
committed.  The copy is what makes it safe to run them at all, and running them
is the only way to know that six zipapps still contain what their comments say
they do.

The zipapps are the second install route (`git clone` plus one command, no
dpkg), and the claim about them is a claim about bundle contents: `wxrandr`,
`warandr` and `wmirror` deliberately carry no `wdotool/` -- that is 680 kB per
file, and it means those three do not drag in the keysym table, the input daemon
and four window backends for three small modules.  Nothing checked it, so the
saving was one careless `build` line from being silently undone.

`build-deb.sh` is checked for its refusals rather than for a build: a real one
needs dpkg-buildpackage, debhelper and eight minutes.  The refusal that matters
is the version gate -- pyproject.toml and debian/changelog disagreeing is
exactly how a package named for the previous release gets committed, which is
what happened between 0.3 and 0.4.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fwcommon import VERSION, passthrough                          # noqa: E402

PACKAGES = ("fwcommon", "wdotool", "wwmctl", "wxprop", "wxrandr", "warandr",
            "wmirror")
TOOLS = ("wdotool", "wwmctl", "wxprop", "wxrandr", "warandr", "wmirror")

#: What each zipapp is supposed to contain, out of build-pyz.sh's own `build`
#: lines and the comments above them.  `fwcommon` is in all six (session
#: discovery, the X11 handover, CmdError, stdio, procs); `wdotool` rides with
#: the two tools that import its window backends and its X wire client; the
#: three display tools carry none of it.
BUNDLES = {
    "wdotool": {"fwcommon", "wdotool"},
    "wwmctl": {"fwcommon", "wdotool", "wwmctl"},
    "wxprop": {"fwcommon", "wdotool", "wxprop"},
    "wxrandr": {"fwcommon", "wxrandr"},
    "warandr": {"fwcommon", "wxrandr", "warandr"},
    "wmirror": {"fwcommon", "wxrandr", "wmirror"},
}


def tree_copy(cls_or_case, extra=("scripts",)):
    """A temporary copy of the packages plus `extra` directories."""
    tmp = tempfile.mkdtemp(prefix="fw-build-")
    cls_or_case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    for d in PACKAGES + tuple(extra):
        shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d),
                        ignore=shutil.ignore_patterns("__pycache__"))
    return tmp


class TheZipapps(unittest.TestCase):
    """One build for the whole class: it is six zipapps and takes a few
    seconds, and nothing here mutates what it produced."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fw-pyz-")
        for d in PACKAGES + ("scripts",):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(cls.tmp, d),
                            ignore=shutil.ignore_patterns("__pycache__"))
        got = subprocess.run(["sh", os.path.join(cls.tmp, "scripts", "build-pyz.sh")],
                             capture_output=True, text=True, timeout=300)
        if got.returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise AssertionError("build-pyz.sh failed:\n%s" % got.stderr)
        cls.built = got.stdout

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def pyz(self, name):
        return os.path.join(self.tmp, "dist", name)

    def top_level(self, name):
        """The package directories inside one zipapp."""
        with zipfile.ZipFile(self.pyz(name)) as z:
            return {n.split("/")[0] for n in z.namelist() if "/" in n}

    def test_all_six_are_built(self):
        for name in TOOLS:
            with self.subTest(name):
                self.assertTrue(os.path.exists(self.pyz(name)), name)
                self.assertTrue(os.access(self.pyz(name), os.X_OK), name)

    def test_each_bundle_holds_exactly_what_its_build_line_says(self):
        """The comments in build-pyz.sh are a design decision, and this is the
        only thing that keeps them true.  `wxrandr`, `warandr` and `wmirror`
        carrying no `wdotool/` is 680 kB off each of the three."""
        for name, want in BUNDLES.items():
            with self.subTest(name):
                self.assertEqual(self.top_level(name), want)

    def test_fwcommon_is_in_all_six(self):
        """Every tool here finds its session, hands over to the original on an
        X11 one, raises the same exception when it fails and flushes through
        the same stdout on the way out -- all of which is fwcommon."""
        for name in TOOLS:
            with self.subTest(name):
                self.assertIn("fwcommon", self.top_level(name))

    def test_no_display_tool_carries_the_input_stack(self):
        for name in ("wxrandr", "warandr", "wmirror"):
            with self.subTest(name):
                self.assertNotIn("wdotool", self.top_level(name))

    def test_the_marker_and_the_stamp_are_in_the_first_four_kilobytes(self):
        """`fwcommon.passthrough.is_us()` sniffs the head of a file to decide
        whether an executable it is about to `execve` is one of ours -- the only
        "not us" guard that survives two copies under two names in two PATH
        directories.  zipapp stores members uncompressed and `__main__.py`
        sorts first, so the stamp lands in plain text near the front; the guard
        reads 4 KiB and no more."""
        for name in TOOLS:
            with self.subTest(name):
                with open(self.pyz(name), "rb") as fh:
                    head = fh.read(4096)
                self.assertIn(b"fuckwayland-clone:", head, name)
                self.assertIn(b"_fuckwayland_marker", head, name)

    def test_the_guard_itself_says_each_of_them_is_us(self):
        """Not a re-implementation of the sniff: the shipped function, run over
        the built file.  This is the test that would have caught a build that
        compressed its members or sorted them differently."""
        for name in TOOLS:
            with self.subTest(name):
                self.assertTrue(passthrough.is_us(self.pyz(name)), name)

    def test_three_of_them_run_and_print_their_version(self):
        """`wmirror` and `wxprop` need no session at all; `wxrandr` prints its
        program version and then, with no display, xrandr's own `Can't open
        display` and exit 1 -- which is the original's behaviour verbatim, so
        the assertion is on the line it did print."""
        env = dict(os.environ, FUCKWAYLAND_PASSTHROUGH="never",
                   WWMCTL_WMCTRL_GENERATION="1.07", LC_ALL="C")
        env.pop("PYTHONPATH", None)
        for argv, first in (("wmirror --version", "wmirror "),
                            ("wxprop -version", "xprop 1.2.8"),
                            ("wxrandr --version", "xrandr program version")):
            name, flag = argv.split()
            with self.subTest(argv):
                got = subprocess.run([sys.executable, self.pyz(name), flag],
                                     capture_output=True, text=True, timeout=120,
                                     env=env)
                self.assertTrue(got.stdout.startswith(first),
                                (got.returncode, got.stdout, got.stderr))
                if name != "wxrandr":
                    self.assertEqual(got.returncode, 0, got.stderr)

    def test_the_script_reports_every_file_it_built(self):
        names = re.findall(r"^built dist/(\w+) \(\d+ bytes\)$", self.built, re.M)
        self.assertEqual(names, list(TOOLS))


class BuildDebRefuses(unittest.TestCase):
    """build-deb.sh's three exits before it touches anything.

    A real build is not run here -- it wants dpkg-buildpackage and debhelper
    and it rewrites `release/` -- so the copy is given stub `sudo`, `apt-get`
    and `dpkg-query`, and every case below exits before the first of them is
    reached.  That the stubs are never called is itself asserted: a script that
    escalated before checking the version would be a script that asks for a
    password to tell you it will not build."""

    def setUp(self):
        self.tmp = tree_copy(self)
        for d in ("debian", "release"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(self.tmp, d))
        for name in ("pyproject.toml", "README.md"):
            shutil.copy2(os.path.join(ROOT, name), os.path.join(self.tmp, name))
        self.bin = os.path.join(self.tmp, "stubbin")
        self.log = os.path.join(self.tmp, "calls.log")
        os.makedirs(self.bin)
        for name in ("sudo", "apt-get", "dpkg-buildpackage", "dpkg-checkbuilddeps"):
            self.stub(name, "printf '%%s\\n' \"%s $*\" >> \"$FAKE_LOG\"\nexit 0\n" % name)
        # dpkg-query: every build tool reported installed, so the apt branch is
        # not reached unless a test says otherwise
        self.stub("dpkg-query", "printf '%s\\n' \"dpkg-query $*\" >> \"$FAKE_LOG\"\n"
                                "printf installed\nexit 0\n")

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def run_build(self, *args):
        env = {"PATH": self.bin + ":/usr/bin:/bin", "FAKE_LOG": self.log,
               "HOME": self.tmp, "LC_ALL": "C"}
        return subprocess.run(["sh", os.path.join(self.tmp, "scripts", "build-deb.sh")]
                              + list(args), env=env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=300)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]

    def set_changelog_version(self, version):
        path = os.path.join(self.tmp, "debian", "changelog")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        head, rest = text.split("\n", 1)
        head = re.sub(r"\(([^)]+)\)", "(%s)" % version, head, count=1)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(head + "\n" + rest)

    def test_a_changelog_left_behind_stops_the_build_before_anything_else(self):
        """The 0.3-package-in-a-0.4-tree failure, from the direction that
        prevents it: `dpkg-buildpackage` reads the version out of
        debian/changelog and would have produced a package named 0.3.0 without
        a word of complaint."""
        stale = "0.0.1" if VERSION != "0.0.1" else "0.0.2"
        self.set_changelog_version(stale)
        got = self.run_build()
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("version mismatch", got.stderr)
        # the remedy it prints is the tree's own version, not a literal: the
        # gate has to move with the release, and a test that pins 0.4.0 goes
        # red at the 0.5 bump for a reason that is not build-deb.sh's.
        self.assertIn("dch -v %s" % VERSION, got.stderr)
        self.assertEqual(self.calls(), [], "it escalated before checking")

    def test_no_deps_with_a_missing_build_tool_names_the_tool(self):
        """`--no-deps` is for a machine where apt is not the answer -- a build
        chroot, a CI image, a developer who wants to know what is missing
        rather than have it installed."""
        self.stub("dpkg-query", "printf '%s\\n' \"dpkg-query $*\" >> \"$FAKE_LOG\"\n"
                                "case \"$*\" in *debhelper*) printf 'not-installed'; exit 1 ;; esac\n"
                                "printf installed\nexit 0\n")
        got = self.run_build("--no-deps")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("missing build tools: debhelper", got.stderr)
        self.assertIn("sudo apt-get install -y debhelper", got.stderr)
        self.assertNotIn("sudo apt-get install -y debhelper",
                         "\n".join(self.calls()), "it installed it anyway")

    def test_an_unknown_option_is_two(self):
        """Two, not one: `sh scripts/build-deb.sh --lintain` is a typo in the
        caller, and a wrapper script that distinguishes "you asked for
        something impossible" from "the build failed" wants them apart."""
        got = self.run_build("--bogus")
        self.assertEqual(got.returncode, 2, got.stdout + got.stderr)
        self.assertIn("unknown option: --bogus", got.stderr)

    def test_help_is_the_scripts_own_header(self):
        got = self.run_build("--help")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("release/fuckwayland_<version>_all.deb", got.stdout)
        self.assertIn("--no-deps", got.stdout)
        self.assertEqual(self.calls(), [])

    def test_the_script_parses(self):
        got = subprocess.run(["sh", "-n", os.path.join(ROOT, "scripts", "build-deb.sh")],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual((got.returncode, got.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()
