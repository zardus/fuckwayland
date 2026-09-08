#!/usr/bin/env python3
"""The rpm's %post and %postun, sliced out of the spec and run as processes.

`tests/test_debian_scripts.py` does this for dpkg's two maintainer scripts and
found a real defect doing it (fix 55: a banner down a closed pipe took the whole
configuration with it).  The rpm's pair is the same procedure in a second
dialect, and it is the only code in the Fedora package that runs as root on a
stranger's machine, so it gets the same treatment: the shipped text, cut out of
packaging/rpm/fuckwayland.spec with the markers the file itself has, run under
`sh -e` (which is how rpm runs a scriptlet) against a fake root with stubbed
modprobe / udevadm / setfacl / chown / chmod / python3.

rpm has no DPKG_ROOT, so the redirection is the test's: three macros and two
absolute paths are rewritten into a temporary tree before the slice is run
(MACROS and ROOTED below).  That is the whole of the difference between what
runs here and what runs on a Fedora box, and it is why the last two tests
compare the result with debian/fuckwayland.postinst and .postrm phrase for
phrase: three copies of one procedure -- dpkg's, rpm's and pacman's -- and the
order of the four udev commands is the part that matters.  The trigger has to
follow the reload, or the node is re-tagged against the rule set that was in
force before the package arrived.

The recon ran exactly this against a real `rpm -qp --qf '%{POSTIN}'` and got the
same sequence in the same order as debian's [recon2/pkg-rpm.md 4]; what is here
needs no rpm at all.
"""

import os
import shutil
import subprocess
import sys
import tempfile
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

SPEC = os.path.join(ROOT, "packaging", "rpm", "fuckwayland.spec")
POSTINST = os.path.join(ROOT, "debian", "fuckwayland.postinst")
POSTRM = os.path.join(ROOT, "debian", "fuckwayland.postrm")

#: rpm macros the scriptlets use, and what they are here.  %{_sharedstatedir}
#: is /var/lib on Fedora and /usr/com on the rpm Ubuntu ships (measured), which
#: is one more reason the scriptlet names the macro and this table names the
#: test's tree.
MACROS = {
    "%{_sharedstatedir}": "$FW_ROOT/var/lib",
    "%{name}": "fuckwayland",
}

#: The two absolute paths a scriptlet cannot be talked out of, redirected into
#: the fake root so that the udev half really runs here.  Without this the
#: guards (`[ -d /run/udev ]`, `[ -e /dev/uinput ]`) decide the test by what
#: the machine running it happens to have, which is no test at all.
ROOTED = ("/run/udev", "/dev/uinput")

STUBBED = ("modprobe", "udevadm", "setfacl", "chown", "chmod", "python3")

#: The real commands the scriptlets need, symlinked into a directory of their
#: own.  PATH is that directory and the stubs and NOTHING ELSE: this box has a
#: real /usr/bin/setfacl, so "there is no setfacl here" cannot be arranged by
#: deleting a stub while /usr/bin is still on the path -- the fallback test
#: passed for the wrong reason until PATH stopped naming it.
REAL_TOOLS = ("mkdir", "rm", "rmdir", "sed")

#: `${0##*/}` rather than `basename`, so the stub needs nothing from REAL_TOOLS.
STUB = "#!/bin/sh\nprintf '%s\\n' \"${0##*/} $*\" >> \"$FAKE_LOG\"\nexit 0\n"

#: `udevadm info -q property` has to answer, or the major:minor branch of the
#: revoke never runs.  10:223 is the misc-device pair /dev/uinput has on every
#: Linux that has it.
UDEVADM = (
    "#!/bin/sh\n"
    "printf '%s\\n' \"udevadm $*\" >> \"$FAKE_LOG\"\n"
    "case $1 in info) printf 'MAJOR=10\\nMINOR=223\\n' ;; esac\n"
    "exit 0\n")

#: Every command phrase the three packagings' scriptlets are compared by.  A
#: phrase and not a token: `udevadm` alone would make the reload and the
#: trigger the same thing, and their ORDER is the claim.
PHRASES = (
    "modprobe uinput",
    "udevadm control --reload-rules",
    "udevadm trigger --name-match=uinput",
    "udevadm settle --timeout=5",
    "udevadm info -q property",
    "static_node-tags/uaccess/uinput",
    "setfacl -b",
    "removexattr",
    "chown root:root",
    "chmod 0600",
)


def strip_comments(text):
    """Shell comment lines out.  The prose around these scriptlets names the
    commands it is explaining, so a phrase scan over the raw text would count
    the explanation as a step."""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


def phrases(text):
    """Every PHRASE in `text`, in the order it occurs, duplicates kept."""
    text = strip_comments(text)
    found = []
    for phrase in PHRASES:
        start = 0
        while True:
            at = text.find(phrase, start)
            if at < 0:
                break
            found.append((at, phrase))
            start = at + 1
    return [p for _at, p in sorted(found)]


def scriptlet(name):
    """%post or %postun, as the spec has it.

    support.sh_block with the file's own directive lines as markers, so a
    slice that stops matching is a spec that changed under it."""
    if name == "post":
        body = support.sh_block(SPEC, "\n%post\n", "\n%postun\n")
        return body[len("\n%post\n"):-len("\n%postun\n")]
    body = support.sh_block(SPEC, "\n%postun\n", "\n%files -f ")
    return body[len("\n%postun\n"):-len("\n%files -f ")]


class ScriptletCase(unittest.TestCase):
    """A fake root, the six stubs, and the two scriptlets rewritten into it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fw-rpm-scripts-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "root")
        self.bin = os.path.join(self.tmp, "bin")
        self.realbin = os.path.join(self.tmp, "realbin")
        self.log = os.path.join(self.tmp, "calls.log")
        os.makedirs(os.path.join(self.root, "run", "udev"))
        os.makedirs(os.path.join(self.root, "dev"))
        os.makedirs(self.bin)
        os.makedirs(self.realbin)
        with open(os.path.join(self.root, "dev", "uinput"), "w"):
            pass
        for name in REAL_TOOLS:
            path = shutil.which(name)
            if path is None:
                self.skipTest("no %s on PATH" % name)
            os.symlink(path, os.path.join(self.realbin, name))
        for name in STUBBED:
            self.stub(name, UDEVADM if name == "udevadm" else STUB)

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def unstub(self, name):
        os.unlink(os.path.join(self.bin, name))

    def rewrite(self, text):
        for macro, value in MACROS.items():
            text = text.replace(macro, value)
        for path in ROOTED:
            text = text.replace(path, "$FW_ROOT" + path)
        return text

    def run_scriptlet(self, name, arg):
        path = os.path.join(self.tmp, "%s.sh" % name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.rewrite(scriptlet(name)))
        env = {"PATH": self.bin + ":" + self.realbin, "FAKE_LOG": self.log,
               "FW_ROOT": self.root, "LC_ALL": "C"}
        return subprocess.run(["/bin/sh", "-e", path, str(arg)], env=env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=60)

    def stamp(self):
        return os.path.join(self.root, "var", "lib", "fuckwayland", "installed")

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]

    def udev_calls(self):
        return [c for c in self.calls()
                if c.startswith(("modprobe", "udevadm", "setfacl", "chown", "chmod"))]


class TheyParse(ScriptletCase):

    def test_both_scriptlets_are_posix_sh(self):
        """rpm runs a scriptlet with /bin/sh -e; a syntax error in one is a
        package whose installation fails on the user's machine and nowhere
        else."""
        for name in ("post", "postun"):
            with self.subTest(name):
                path = os.path.join(self.tmp, "%s.sh" % name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(scriptlet(name))
                got = subprocess.run(["sh", "-n", path], capture_output=True,
                                     text=True, timeout=60)
                self.assertEqual((got.returncode, got.stderr), (0, ""))

    def test_the_slice_is_the_whole_scriptlet_and_not_a_neighbour(self):
        """The markers are the spec's own directive lines.  If %files ever
        moved above %postun the slice would silently become something else, so
        the contents are asserted, not just the fact that a slice happened."""
        post = scriptlet("post")
        self.assertIn("modprobe uinput", post)
        self.assertNotIn("%files", post)
        self.assertNotIn("%postun", post)
        self.assertTrue(post.rstrip().endswith("exit 0"), post[-80:])


class TheInstall(ScriptletCase):
    """%post, $1 = 1 (install) and $1 = 2 (upgrade)."""

    def test_a_first_install_applies_the_rule_and_writes_the_stamp(self):
        """The four udev commands are what makes injection work with no
        reboot: the rule tags /dev/uinput `uaccess` and logind gives the user
        at the active seat an ACL on it."""
        got = self.run_scriptlet("post", 1)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.udev_calls(), [
            "modprobe uinput",
            "udevadm control --reload-rules",
            "udevadm trigger --name-match=uinput",
            "udevadm settle --timeout=5",
        ])
        self.assertTrue(os.path.exists(self.stamp()))

    def test_an_upgrade_applies_the_rule_and_leaves_the_stamp_alone(self):
        """$1 = 2 is an upgrade.  The rule may have changed, so it is applied
        again; the stamp must not be rewritten, because the per-user enabler
        compares its own against it and a fresh one would set every user up a
        second time and overrule anyone who had turned the extension off."""
        self.run_scriptlet("post", 1)
        before = os.stat(self.stamp()).st_mtime_ns
        os.utime(self.stamp(), ns=(before - 10 ** 9, before - 10 ** 9))
        was = os.stat(self.stamp()).st_mtime_ns
        got = self.run_scriptlet("post", 2)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(os.stat(self.stamp()).st_mtime_ns, was)

    def test_nothing_touches_the_node_without_a_run_udev(self):
        """A container image build, an --installroot, a chroot: no /run/udev,
        so no modprobe, no trigger and no settle.  The rule and the
        modules-load file are on disk and apply at that image's first boot."""
        shutil.rmtree(os.path.join(self.root, "run", "udev"))
        got = self.run_scriptlet("post", 1)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue(os.path.exists(self.stamp()),
                        "the stamp is not part of the udev half")


class TheErase(ScriptletCase):
    """%postun, $1 = 1 (upgrade) and $1 = 0 (the last erase)."""

    def plant_udev_db(self):
        """What udev leaves behind for a tagged node: its database entry and
        one tag link.  Both have to go, or a node re-triggered while the rule
        is still known keeps the grant."""
        data = os.path.join(self.root, "run", "udev", "data")
        tags = os.path.join(self.root, "run", "udev", "tags", "uaccess")
        static = os.path.join(self.root, "run", "udev", "static_node-tags", "uaccess")
        for d in (data, tags, static):
            os.makedirs(d, exist_ok=True)
        made = [os.path.join(data, "c10:223"), os.path.join(tags, "c10:223"),
                os.path.join(static, "uinput")]
        for path in made:
            with open(path, "w"):
                pass
        return made

    def test_an_upgrade_revokes_nothing(self):
        """$1 = 1 is the old package's %postun during an upgrade.  Running the
        revoke there would take the grant away from a session that is using it
        and never put it back -- the new package's %post has already run."""
        self.run_scriptlet("post", 1)
        planted = self.plant_udev_db()
        got = self.run_scriptlet("postun", 1)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.udev_calls(), [
            "modprobe uinput",
            "udevadm control --reload-rules",
            "udevadm trigger --name-match=uinput",
            "udevadm settle --timeout=5",
        ], "only %post's four are in the log")
        self.assertTrue(os.path.exists(self.stamp()))
        for path in planted:
            self.assertTrue(os.path.exists(path), path)

    def test_the_last_erase_puts_the_node_back(self):
        self.run_scriptlet("post", 1)
        planted = self.plant_udev_db()
        os.truncate(self.log, 0)
        got = self.run_scriptlet("postun", 0)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.udev_calls(), [
            "udevadm control --reload-rules",
            "udevadm info -q property %s/dev/uinput" % self.root,
            "udevadm info -q property %s/dev/uinput" % self.root,
            "setfacl -b %s/dev/uinput" % self.root,
            "chown root:root %s/dev/uinput" % self.root,
            "chmod 0600 %s/dev/uinput" % self.root,
        ])
        for path in planted:
            self.assertFalse(os.path.exists(path), path)

    def test_the_last_erase_drops_the_stamp_and_its_directory(self):
        """The next install is then a fresh start for this machine, so the
        per-user enabler runs again for everyone -- which is right, because the
        erase took the extension with it."""
        self.run_scriptlet("post", 1)
        self.assertTrue(os.path.isdir(os.path.dirname(self.stamp())))
        self.run_scriptlet("postun", 0)
        self.assertFalse(os.path.exists(self.stamp()))
        self.assertFalse(os.path.isdir(os.path.dirname(self.stamp())))

    def test_the_python_fallback_runs_where_there_is_no_setfacl(self):
        """Fedora's cloud image has neither getfacl nor setfacl (measured), so
        this is the normal path there and not a corner.  The ACL entry has to
        GO rather than be masked by the 0600 below it: the uaccess builtin only
        ever adds entries, so a left-over one makes a later reinstall a no-op
        and leaves input broken with nothing to see."""
        self.run_scriptlet("post", 1)
        self.unstub("setfacl")
        os.truncate(self.log, 0)
        got = self.run_scriptlet("postun", 0)
        self.assertEqual(got.returncode, 0, got.stderr)
        calls = self.calls()
        self.assertEqual([c for c in calls if c.startswith("setfacl")], [])
        self.assertEqual(len([c for c in calls if c.startswith("python3 -c")]), 1, calls)
        self.assertIn("removexattr", "\n".join(calls))

    def test_a_node_that_is_not_there_is_not_chmodded(self):
        """`[ -e /dev/uinput ]`: a machine where the module was never loaded
        has nothing to revoke, and a chown of a path that does not exist is
        an error message in somebody's dnf output."""
        self.run_scriptlet("post", 1)
        os.unlink(os.path.join(self.root, "dev", "uinput"))
        os.truncate(self.log, 0)
        got = self.run_scriptlet("postun", 0)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.stamp()), "the stamp still goes")


class TheThreePackagingsAgree(ScriptletCase):
    """The rpm's pair against dpkg's, phrase for phrase.

    This is the test the whole file exists for.  Three copies of one procedure
    is the arrangement; what keeps it honest is that a change to one of them
    turns another red.  tests/test_pkgbuild.py holds pacman's third copy
    against the same list."""

    def deb_arm(self, path, first, last):
        return strip_comments(support.sh_block(path, first, last))

    def test_post_is_debians_configure_arm(self):
        deb = self.deb_arm(POSTINST, "configure)", "esac")
        self.assertEqual(phrases(scriptlet("post")), phrases(deb))
        self.assertEqual(phrases(deb), [
            "modprobe uinput",
            "udevadm control --reload-rules",
            "udevadm trigger --name-match=uinput",
            "udevadm settle --timeout=5",
        ], "debian/fuckwayland.postinst changed; the spec has to follow")

    def test_postun_is_debians_remove_arm(self):
        deb = self.deb_arm(POSTRM, "remove)", "purge)")
        self.assertEqual(phrases(scriptlet("postun")), phrases(deb))
        self.assertEqual(phrases(deb), [
            "udevadm control --reload-rules",
            "static_node-tags/uaccess/uinput",
            "udevadm info -q property",
            "udevadm info -q property",
            "setfacl -b",
            "removexattr",
            "chown root:root",
            "chmod 0600",
        ], "debian/fuckwayland.postrm changed; the spec has to follow")

    def test_the_phrase_scan_is_not_fooled_by_the_prose(self):
        """The comments around both scriptlets name the commands they are
        explaining.  If strip_comments() stopped working, every comparison
        above would pass for the wrong reason -- so it is asserted here on a
        line taken from the spec itself."""
        self.assertEqual(phrases("# modprobe uinput is what this does\n"), [])
        self.assertEqual(phrases("modprobe uinput\n"), ["modprobe uinput"])
        self.assertIn("systemd-udev", scriptlet("post"),
                      "the transfiletrigger reason is a comment inside %post")

    def test_the_stamp_path_is_the_one_the_enabler_reads(self):
        """packaging/common/enable-bridge defaults SYSTEM_STAMP to
        /var/lib/fuckwayland/installed, and %{_sharedstatedir} is /var/lib on
        Fedora.  Three files, one path, and no test between them until now."""
        self.run_scriptlet("post", 1)
        self.assertEqual(
            os.path.relpath(self.stamp(), self.root),
            "var/lib/fuckwayland/installed")
        with open(os.path.join(ROOT, "packaging", "common", "enable-bridge"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(
            'SYSTEM_STAMP="${FUCKWAYLAND_SYSTEM_STAMP:-/var/lib/fuckwayland/installed}"',
            src)


if __name__ == "__main__":
    unittest.main()
