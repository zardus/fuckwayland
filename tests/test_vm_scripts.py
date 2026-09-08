#!/usr/bin/env python3
"""vm/*.sh: the parts that can be run without a rig, and the two that were wrong.

Nothing in this file boots a virtual machine.  `vm/` is five bash scripts and a
Python `vmctl`, and the interesting failures in them are not "the image did not
come up" -- they are a status that was thrown away and a parser that counted a
head that was not there.  Both are reachable by slicing the shipped script and
feeding it text, which is what happens here; anything that needs the rig itself
is gated on `VMCTL_LIVE`.

* `vm/build-iso-golden.sh` ran stage 2 down a pipeline (`$SSH ... | tee | sed`)
  and then read `$?`, which is `sed`'s and is 0 whatever ssh did.  Every stage-2
  failure -- ssh dropped, sudo refused the password, the guest rebooted under
  it -- was reported as `stage 2 did not finish (rc 0)`, and the real status was
  gone.  Fix 53: `rc=${PIPESTATUS[0]}`.
* `vm/selftest.sh` had two parsers for the same tool.  The `kde-x11)` one reads
  `kscreen-doctor -o` per output BLOCK, because Plasma 5.27 puts the state on
  the `Output:` line and 6.x on the indented lines under it; the `kde)` one was
  a `grep` over the `Output:` line alone, so on Wayland it counted a *disabled*
  head as present -- and the rig test that unplugs a head and counts what is
  left was the one asking.  Fix 54: one parser for both.

The kscreen fixtures are synthetic, in the two shapes the comment in the script
describes: three heads with the third disabled, in 5.27's one-line format and
6.x's block format, plus a 6.x X11 capture with a disconnected connector, which
is the case libkscreen's XRandR backend produces and KWin on Wayland does not.
"""

import os
import re
import subprocess
import sys
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` is a bare import: it resolves only with the tests directory
# on sys.path, which `python3 tests/<file>.py` gives for free and
# `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                    # noqa: E402

VM = os.path.join(ROOT, "vm")
KSCREEN = os.path.join(ROOT, "tests", "fixtures", "kscreen")
SELFTEST = os.path.join(VM, "selftest.sh")
ISO_GOLDEN = os.path.join(VM, "build-iso-golden.sh")

#: The five bash scripts under vm/.  vmctl is Python and is covered elsewhere.
SCRIPTS = ("build-image.sh", "build-iso-golden.sh", "build-iso-image.sh",
           "selftest.sh", "setup-host.sh")

LIVE = unittest.skipUnless(os.environ.get("VMCTL_LIVE"),
                           "needs the QEMU rig: set VMCTL_LIVE=1")


class TheyAllParse(unittest.TestCase):
    """`bash -n` over every script in vm/.

    They are run by hand, months apart, against a build that takes forty
    minutes, so a syntax error in a branch nobody took that day is expensive in
    a way an ordinary script's is not."""

    def test_every_script_parses(self):
        for name in SCRIPTS:
            with self.subTest(name):
                got = subprocess.run(["bash", "-n", os.path.join(VM, name)],
                                     capture_output=True, text=True, timeout=60)
                self.assertEqual((got.returncode, got.stderr), (0, ""))

    def test_vmctl_compiles(self):
        got = subprocess.run([sys.executable, "-m", "py_compile",
                              os.path.join(VM, "vmctl")],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(got.returncode, 0, got.stderr)


class StageTwoReportsSshsStatus(unittest.TestCase):
    """Fix 53, finding F7.6.  The shipped pipeline, sliced, with a stand-in ssh
    that fails."""

    def block(self, rc):
        """The pipeline out of build-iso-golden.sh, with `$SSH` bound to a
        function that produces output and then fails with `rc`."""
        body = ("set -eu\n"
                "B=$(mktemp -d)\n"
                "trap 'rm -rf \"$B\"' EXIT\n"
                "ssh() { echo 'stage 2 output'; return %d; }\n"
                "SSH=ssh\n"
                % rc)
        # The slice ends at the `set -e` after the assignment, not at the
        # assignment itself: `rc=${PIPESTATUS[0]}` is the fix under test, and a
        # marker that quotes it turns a regression to `rc=$?` into a missing
        # marker inside sh_block -- an error about slicing, not the RC=0 this
        # test exists to print.
        body += support.sh_block(ISO_GOLDEN,
                                 "set +e\n$SSH 'sudo -S -p \"\" bash /tmp/build-iso-image.sh'",
                                 "\nset -e\n")
        body += "\nprintf 'RC=%s\\n' \"$rc\"\n"
        return body

    def run_block(self, rc):
        got = subprocess.run(["bash", "-c", self.block(rc)],
                             capture_output=True, text=True, timeout=120,
                             env=dict(os.environ, LC_ALL="C"))
        self.assertEqual(got.returncode, 0, got.stderr)
        return got.stdout.strip()

    def test_a_failing_ssh_is_reported_with_its_own_status(self):
        """3 is what ssh exits with when the remote command exits 3; the
        message the script then prints -- `stage 2 did not finish (rc N)` -- is
        the only trace of a forty-minute build that stopped for a reason worth
        knowing."""
        self.assertEqual(self.run_block(3), "RC=3")

    def test_a_succeeding_ssh_is_still_zero(self):
        """The positive control: PIPESTATUS[0] of a pipeline whose first
        command succeeded is 0, and the script has to carry on."""
        self.assertEqual(self.run_block(0), "RC=0")

    def test_the_script_does_not_read_the_bare_dollar_question_there(self):
        """Read out of the source as well, because the slice above only proves
        the one pipeline: a second `rc=$?` after a pipeline would be the same
        bug in a new place."""
        with open(ISO_GOLDEN, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "rc=$?" and i and "|" in lines[i - 1]:
                self.fail("%s:%d: rc=$? after a pipeline" % (ISO_GOLDEN, i + 1))


class TheKscreenParser(unittest.TestCase):
    """Fix 54, finding F7.7.  One parser, two Plasma generations, three
    captures.

    `kscreen-doctor -o` is the only way to ask KWin what heads it has, and the
    rig test that matters plugs and unplugs them: a parser that counts a
    disabled head reports three when the test has just made it two, and the
    selftest passes on a rig that is not doing what it claims."""

    @classmethod
    def setUpClass(cls):
        with open(SELFTEST, encoding="utf-8") as fh:
            cls.sh = fh.read()

    def parser(self):
        """The embedded `python3 -c` program out of selftest.sh's monitors().

        Sliced from the shipped file so that it cannot drift from what runs on
        the rig -- which is the entire reason this is not a copy of the same
        five lines."""
        m = re.search(r"\| python3 -c '\n(import re, sys\n.*?)'", self.sh, re.S)
        self.assertTrue(m, "the kscreen block parser is gone from selftest.sh")
        return m.group(1)

    def outputs(self, fixture):
        path = os.path.join(KSCREEN, fixture)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        got = subprocess.run([sys.executable, "-c", self.parser()], input=text,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        return sorted({ln for ln in got.stdout.split() if ln.startswith("Virtual-")})

    def test_plasma_5_27_one_line_format(self):
        """5.27 writes the whole state on the `Output:` line: `Output: 70
        Virtual-3 disabled connected priority 0 ...`."""
        self.assertEqual(self.outputs("plasma-5.27-wayland-3heads-one-disabled.txt"),
                         ["Virtual-1", "Virtual-2"])

    def test_plasma_6_x_block_format(self):
        """6.x writes `Output: 3 Virtual-3` and then one indented line per
        property, so `disabled` is nowhere near the name."""
        self.assertEqual(self.outputs("plasma-6.6-wayland-3heads-one-disabled.txt"),
                         ["Virtual-1", "Virtual-2"])

    def test_the_x11_backend_lists_connectors_that_are_not_plugged(self):
        """libkscreen's XRandR backend reports every CONNECTOR the X server
        has, disconnected ones included, where KWin on Wayland exports only the
        plugged ones -- which is why the condition is enabled AND connected and
        not enabled alone."""
        self.assertEqual(self.outputs("plasma-6.6-x11-3heads-one-disconnected.txt"),
                         ["Virtual-1", "Virtual-2"])

    def test_wayland_and_x11_share_the_one_parser(self):
        """The fix itself: inside monitors(), `kde)` and `kde-x11)` are one
        case arm.  Two arms is two chances for the next Plasma format change to
        be handled in only one of them -- and the arm that was wrong was the
        Wayland one, which is the flavor every Plasma measurement in this
        repository was taken on.  (`kde|kde-x11)` also appears in the layout
        and geometry helpers; this looks inside monitors() alone.)"""
        body = support.sh_function(SELFTEST, "monitors")
        arms = re.findall(r"^    (\S+?\))", body, re.M)
        self.assertIn("kde|kde-x11)", arms, arms)
        self.assertNotIn("kde)", arms)
        self.assertEqual(body.count("| python3 -c '\nimport re, sys"), 1)


class TheDeviationLists(unittest.TestCase):
    """The ISO flavors claim "every deviation from an untouched install is
    listed here": four in the flavor yaml, four in the stage-2 script, eight in
    vm/README's table.  Three lists, one set of numbers."""

    @classmethod
    def setUpClass(cls):
        cls.docs = support.documents()
        with open(os.path.join(VM, "build-iso-image.sh"), encoding="utf-8") as fh:
            cls.stage2 = fh.read()

    @staticmethod
    def numbers(text):
        return sorted({int(n) for n in re.findall(r"DEVIATION (\d+)", text)})

    def test_the_flavor_yaml_carries_one_to_four(self):
        for name in ("resolute-gnome-iso.yaml", "noble-gnome-iso.yaml"):
            with self.subTest(name):
                with open(os.path.join(VM, "flavors", name), encoding="utf-8") as fh:
                    self.assertEqual(self.numbers(fh.read()), [1, 2, 3, 4])

    def test_the_stage_two_script_carries_five_to_eight(self):
        self.assertEqual(self.numbers(self.stage2), [5, 6, 7, 8])

    def test_the_readme_table_carries_all_eight(self):
        section = self.docs["vm/README.md"].split(
            "*Every deviation from an untouched install, and why.*")[1]
        table = section.split("\n\n")[1]
        rows = [ln for ln in table.splitlines() if ln.startswith("| ")]
        numbered = [int(m.group(1)) for ln in rows
                    for m in [re.match(r"\| (\d+) \|", ln)] if m]
        self.assertEqual(numbered, [1, 2, 3, 4, 5, 6, 7, 8])

    def test_the_prose_counts_them_the_same_way(self):
        section = self.docs["vm/README.md"]
        self.assertIn("There are eight, four in the flavor\nyaml and four in the "
                      "stage-2 script", section)


@LIVE
class TheRigItself(unittest.TestCase):
    """The only tests here that need a machine with ~/vm-data and QEMU.  Off by
    default: a unit test that boots a virtual machine is not a unit test, and
    the goldens are not in the repository."""

    def test_vmctl_lists_the_flavors(self):
        got = subprocess.run([sys.executable, os.path.join(VM, "vmctl"), "flavors"],
                             capture_output=True, text=True, timeout=300)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("resolute-gnome", got.stdout)


if __name__ == "__main__":
    unittest.main()
