#!/usr/bin/env python3
"""The three CI images against the list of binaries this suite shells out to.

R24.  `.github/ci/Dockerfile`, `Dockerfile.fedora` and `Dockerfile.arch` are the
only reason a test file runs rather than skipping itself: every `skipUnless`,
every `shutil.which` in tests/support.py and every `subprocess.run` of a distro
tool answers out of a package one of these three files installs.  A forgotten
package does not turn a job red -- it turns a file green with everything in it
skipped, which is worse, and it is discovered forty minutes into a matrix if it
is discovered at all.

So the table below is the test.  It is keyed by the BINARY the suite calls and
carries the package that provides it on each distro, and each Dockerfile has to
name every package of its column.  The Ubuntu names were measured here with
`dpkg -S $(command -v <binary>)` on 26.04 on 2026-09-09 (which is how
`dbus-run-session` turned out to live in `dbus-daemon` and `g-ir-compiler` in
`gobject-introspection-bin`, a dependency of the `libgirepository1.0-dev` the
image asks for); the Fedora and Arch names come from recon2/fedora.md and
recon2/pkg-arch.md, and the handful neither recon ran -- Xvfb, xauth, gtk4, the
dbus split -- are confirmed by the first image build, which is the honest place
for them.

The four window managers the headless harnesses drive are NOT in that table:
they are the one thing in these images the RELEASE decides, so they have a
table of their own (`LIVE`) and a class of their own.  26.04 packages labwc
0.9.3, wayfire 0.10.0, i3 4.25.1 and openbox 3.6.1, which are the versions
tests/test_labwc_live.py and its three siblings were measured against; 24.04
packages labwc 0.7.1-1build1, wayfire 0.8.0+git20240110-2.1build1 and i3-wm
4.23-1build2 (Launchpad, noble, Published, read 2026-09-09).  Those files skip
on `shutil.which` alone, so on 24.04 the packages would not make them skip --
they would make them run against a compositor nobody has measured, and
HeadlessLabwc's desktops come from ext_workspace_manager_v1, which labwc grew
at 0.9 on wlroots 0.19.  So the Ubuntu Dockerfile installs the four in a
release-gated `case` arm and this file reads that arm.  They are in neither
foreign image, which is a documented absence with a cost (one word each and an
image rebuild, once somebody has run those four files on Fedora and on Arch).
river and tinyrwm are packaged on no runner at all, so `HeadlessRiver` skips
everywhere here and is measured on the rig's arch-river golden instead.
"""

import os
import re
import sys
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# The tests directory too, so this file keeps working under every one of the
# three documented invocation forms the day it grows a `import support`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CI = os.path.join(ROOT, ".github", "ci")
#: distro -> the Dockerfile that builds its image.  The keys are the `distro`
#: values of ci.yml's `image` matrix, which test_ci_workflow.py pins from the
#: other side.
IMAGES = {"ubuntu": "Dockerfile", "fedora": "Dockerfile.fedora",
          "arch": "Dockerfile.arch"}

#: binary the suite calls -> package per distro, `None` where the image does
#: not carry it (see the module docstring).
TOOLS = {
    "sway":             ("sway", "sway", "sway"),
    "Xwayland":         ("xwayland", "xorg-x11-server-Xwayland", "xorg-xwayland"),
    "xterm":            ("xterm", "xterm", "xterm"),
    "foot":             ("foot", "foot", "foot"),
    "Xvfb":             ("xvfb", "xorg-x11-server-Xvfb", "xorg-server-xvfb"),
    "xauth":            ("xauth", "xorg-x11-xauth", "xorg-xauth"),
    "xdotool":          ("xdotool", "xdotool", "xdotool"),
    "wmctrl":           ("wmctrl", "wmctrl", "wmctrl"),
    "xprop":            ("x11-utils", "xprop", "xorg-xprop"),
    "xrandr":           ("x11-xserver-utils", "xrandr", "xorg-xrandr"),
    "dbus-run-session": ("dbus-daemon", "dbus-daemon", "dbus"),
    "g-ir-compiler":    ("libgirepository1.0-dev", "gobject-introspection-devel",
                         "gobject-introspection"),
    "zstd":             ("zstd", "zstd", "zstd"),
    "git":              ("git", "git", "git"),
    "sudo":             ("sudo", "sudo", "sudo"),
}
DISTROS = ("ubuntu", "fedora", "arch")
#: binary -> (package, the file that goes from "runs" to "skipped, and nobody
#: noticed" without it, the version 26.04 packages).  These four are installed
#: by the Ubuntu image's second RUN and by no other instruction in any image.
LIVE = {
    "labwc":   ("labwc", "tests/test_labwc_live.py", "0.9.3"),
    "wayfire": ("wayfire", "tests/test_wayfire_live.py", "0.10.0"),
    "i3":      ("i3", "tests/test_i3_live.py", "4.25.1"),
    "openbox": ("openbox", "tests/test_openbox_live.py", "3.6.1"),
}
#: The Ubuntu release whose packages are NOT the measured versions, and which
#: the gated arm therefore installs nothing on.
NOT_ON = "24.04"


def dockerfile(distro):
    with open(os.path.join(CI, IMAGES[distro]), encoding="utf-8") as fh:
        return fh.read()


def instructions(distro):
    """One Dockerfile as its logical instructions.

    Backslash continuations joined, comment lines dropped.  Written out rather
    than regexed over the whole file because the first version of this read the
    install block with `.*?` up to the next `RUN` and swallowed the prose
    comment between them: every word of that comment became a "package", and
    `foot` would have passed on an image that installs `# ... foot ...` and not
    the package."""
    out, buf = [], ""
    for line in dockerfile(distro).splitlines():
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        buf += line[:-1] + " " if line.endswith("\\") else line
        if not line.endswith("\\"):
            out.append(buf)
            buf = ""
    return out


#: the words of an install instruction that are the command and not a package
VERBS = {"RUN", "apt-get", "dnf", "pacman", "pacman-key", "update", "install",
         "clean", "all", "rm", "true", "&&"}


def installed(distro):
    """The package names one Dockerfile asks its package manager for.

    The UNCONDITIONAL instruction only: the Ubuntu image's release-gated arm is
    a second RUN and is read by `live_arm()` below, so a package that moved
    into the gate cannot pass as one that is always there.

    Options are dropped by shape (`--flag`, `-x`), paths by the `/` in them,
    and the manager's own verbs by the table above."""
    for ins in instructions(distro):
        if not re.match(r"RUN (apt-get update|dnf -y install|pacman-key --init)", ins):
            continue
        return {w for w in ins.split()
                if not w.startswith("-") and w not in VERBS and "/" not in w}
    raise AssertionError("%s: no package-install RUN instruction" % IMAGES[distro])


def live_arm():
    """The Ubuntu image's release-gated instruction, as one line.

    `RUN . /etc/os-release && case "$VERSION_ID" in ...`: the only instruction
    in any of the three files whose effect depends on the base image."""
    found = [i for i in instructions("ubuntu") if "VERSION_ID" in i]
    if len(found) != 1:
        raise AssertionError(".github/ci/Dockerfile: %d release-gated RUN "
                             "instructions, expected 1" % len(found))
    return found[0]


class TheToolTable(unittest.TestCase):
    """Every binary of TOOLS has a package in every image that claims it."""

    def test_each_image_installs_every_package_of_its_column(self):
        for i, distro in enumerate(DISTROS):
            have = installed(distro)
            for binary, packages in sorted(TOOLS.items()):
                package = packages[i]
                # no `if package is None: continue` arm: TOOLS is the
                # unconditional list and a None in it would be a claim with no
                # test behind it.  The one release-dependent group is LIVE.
                with self.subTest("%s: %s -> %s" % (distro, binary, package)):
                    self.assertIn(package, have,
                                  "%s provides %s and %s does not install it"
                                  % (package, binary, IMAGES[distro]))

    def test_the_table_has_a_column_per_image(self):
        """The premise: three distros, three Dockerfiles, three-tuples."""
        self.assertEqual(len(DISTROS), len(IMAGES))
        for binary, packages in TOOLS.items():
            with self.subTest(binary):
                self.assertEqual(len(packages), len(DISTROS), binary)

class TheLiveCompositors(unittest.TestCase):
    """labwc, wayfire, i3 and openbox: one release-gated arm of the Ubuntu
    image, and nothing anywhere else.

    The harnesses skip on `shutil.which`, so what the arm decides is not
    whether tests/test_labwc_live.py runs somewhere -- it is which compositor
    it runs against.  A 24.04 that had labwc 0.7.1 on it would run the file
    against a labwc with no ext_workspace_manager_v1, in a job that is not
    continue-on-error."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "tests", "support.py"), encoding="utf-8") as fh:
            cls.support = fh.read()

    def test_the_gated_arm_installs_all_four(self):
        arm = live_arm()
        install = arm.split("apt-get install -y --no-install-recommends", 1)
        self.assertEqual(len(install), 2, arm)
        words = install[1].split("&&")[0].split()
        self.assertEqual(sorted(words), sorted(p for p, _f, _v in LIVE.values()))

    def test_the_release_with_the_older_packages_installs_none_of_them(self):
        """`24.04)` is an arm that echoes and does not install.  The claim is
        the whole point of the gate, so it is asserted on the arm and not on
        the absence of a word."""
        arm = live_arm()
        head, _, tail = arm.partition("%s)" % NOT_ON)
        self.assertTrue(tail, "no %s) arm in %s" % (NOT_ON, IMAGES["ubuntu"]))
        noble = tail.split(";;")[0]
        self.assertNotIn("apt-get install", noble)
        self.assertIn("*)", tail, "the arm for every other release is gone")
        for version in ("0.7.1", "0.8.0", "4.23"):
            with self.subTest(version):
                self.assertIn(version, head + noble,
                              "the arm no longer says what %s packages" % NOT_ON)

    def test_no_image_installs_them_unconditionally(self):
        """Including the Ubuntu one: a package that moved back into the first
        RUN is on 24.04 again, and that is the failure this class exists for."""
        for distro in DISTROS:
            for binary, (package, _f, _v) in sorted(LIVE.items()):
                with self.subTest("%s: %s" % (distro, binary)):
                    self.assertNotIn(package, installed(distro))

    def test_the_measured_versions_are_written_beside_the_arm(self):
        """26.04's numbers are the ones the four live files were measured
        against; they are in the Dockerfile's comment because that is where
        somebody adding a fifth compositor will look."""
        text = dockerfile("ubuntu")
        for binary, (_p, _f, version) in sorted(LIVE.items()):
            with self.subTest(binary):
                self.assertIn(version, text)

    def test_every_harness_binary_is_in_one_of_the_two_tables(self):
        """`BINARY = "labwc"` / `WM = "i3"` in tests/support.py is the list;
        river and tinyrwm are the two neither table carries."""
        named = set(re.findall(r"^\s+(?:BINARY|WM) = \"([a-z0-9]+)\"$",
                               self.support, re.M))
        self.assertTrue(named, "support.py names no headless compositor any more")
        self.assertEqual(named - set(TOOLS) - set(LIVE), {"river", "tinyrwm"})

    def test_each_live_file_exists_and_names_its_harness(self):
        for binary, (_p, path, _v) in sorted(LIVE.items()):
            with self.subTest(path):
                full = os.path.join(ROOT, path)
                self.assertTrue(os.path.exists(full), path)
                with open(full, encoding="utf-8") as fh:
                    self.assertIn("Headless", fh.read(), path)


class ThePackagingBuildDeps(unittest.TestCase):
    """The other half of each foreign image: what its packaging script says it
    needs on PATH.

    TOOLS above is the binaries the SUITE shells out to; the rpm and pkgbuild
    jobs run scripts/build-rpm.sh and scripts/build-pkgbuild.sh with
    `--no-deps`, which turns a missing build tool into a refusal instead of an
    install -- so the list each script names is the second thing these images
    have to carry, and it is named in one line of each script.  `cpio`, which
    is neither a suite tool nor a build dep, is pinned by its own test below."""

    def deps(self, script, variable):
        with open(os.path.join(ROOT, "scripts", script), encoding="utf-8") as fh:
            text = fh.read()
        found = re.findall(r"^%s=(?:'([^']*)'|\"([^\"]*)\")$" % variable,
                           text, re.M)
        self.assertTrue(found, "%s no longer sets %s" % (script, variable))
        words = []
        for single, double in found:
            words += [w for w in (single or double).split()
                      if not w.startswith("$")]
        return words

    def test_the_fedora_image_installs_every_rpm_build_dep(self):
        """`build-rpm.sh --no-deps` in the container: a name missing here is
        `build-rpm.sh: missing build tools: ...` and no rpm at all."""
        have = installed("fedora")
        for package in self.deps("build-rpm.sh", "DNF_BUILD_DEPS"):
            with self.subTest(package):
                self.assertIn(package, have)
        self.assertIn("rpmlint", have, "the job passes --lint")

    def test_the_arch_image_installs_every_pkgbuild_dep(self):
        """Same claim for makepkg's side.  base-devel is the exception and is
        not installed by name: it IS the base image (`ARG BASE=archlinux:
        base-devel`), which is where makepkg itself comes from."""
        have = installed("arch")
        for package in self.deps("build-pkgbuild.sh", "PACMAN_BUILD_DEPS"):
            with self.subTest(package):
                if package == "base-devel":
                    self.assertIn("ARG BASE=archlinux:base-devel",
                                  dockerfile("arch"))
                    continue
                self.assertIn(package, have)


class TheImagesThemselves(unittest.TestCase):
    """Three files, three things they must agree on with each other."""

    def test_every_image_makes_the_same_uid_1000_user(self):
        """The sudo fixtures name SUDO_UID=1000 and the handover resolves it
        through passwd, so `ci` on 1000 is not a convention -- it is what
        tests/test_passthrough_exec.py's sudo cases read back."""
        for distro in DISTROS:
            with self.subTest(distro):
                text = dockerfile(distro)
                self.assertIn("useradd -m -u 1000 -s /bin/bash ci", text)
                self.assertIn("-o ci -g ci /tmp/xdg-ci", text)

    def test_every_image_takes_the_same_node(self):
        """tests/test_bridge_js.py runs the bridge's JavaScript under node, and
        one version across three images is one behaviour to explain."""
        versions = set()
        for distro in DISTROS:
            m = re.search(r"^ARG NODE=(\S+)$", dockerfile(distro), re.M)
            self.assertIsNotNone(m, distro)
            versions.add(m.group(1))
        self.assertEqual(len(versions), 1, versions)

    def test_the_two_foreign_images_can_sudo_as_ci(self):
        """scripts/build-rpm.sh dnf-installs what it is missing and makepkg
        REFUSES to run as root, so both foreign jobs run as `ci` and both need
        a passwordless sudo.  The Ubuntu image does not: its deb job is root."""
        for distro in ("fedora", "arch"):
            with self.subTest(distro):
                self.assertIn("ci ALL=(ALL) NOPASSWD:ALL", dockerfile(distro))

    def test_the_two_foreign_images_carry_acl_for_the_revoke_path(self):
        """`setfacl` is stubbed in every unit test, so it is not in TOOLS -- but
        the rpm and pkgbuild jobs INSTALL and then REMOVE the real package in
        their own container, and the revoke half of %postun / post_remove runs
        `setfacl -b /dev/uinput` for real there.  Without acl the packaging
        falls through to its python fallback and the job stops testing the path
        the distro actually takes."""
        for distro in ("fedora", "arch"):
            with self.subTest(distro):
                self.assertIn("acl", installed(distro))

    def test_the_fedora_image_carries_cpio_or_the_rpm_job_proves_nothing(self):
        """tests/test_release_rpm.py unpacks the built rpms with `rpm2cpio |
        cpio -idmu` and its `unpack()` SKIPS every payload-against-the-tree
        case when either binary is missing.  rpm2cpio is in `rpm` and is in
        fedora:44 already; cpio is not, and without it the `rpm` job installs,
        lints and reports green while the one claim it exists to make -- the
        payload IS the tree -- never runs.  Same class of failure as a missing
        compositor, one line to prevent."""
        self.assertIn("cpio", installed("fedora"))
        with open(os.path.join(ROOT, "tests", "test_release_rpm.py"),
                  encoding="utf-8") as fh:
            self.assertIn("cpio", fh.read(),
                          "test_release_rpm.py no longer shells out to cpio")

    def test_the_base_is_a_build_arg_on_all_three(self):
        """ci.yml's `image` matrix passes `--build-arg BASE=`, so a Dockerfile
        that hard-coded its FROM would build the same image five times."""
        for distro in DISTROS:
            with self.subTest(distro):
                text = dockerfile(distro)
                self.assertRegex(text, r"(?m)^ARG BASE=\S+$")
                self.assertIn("FROM ${BASE}", text)


if __name__ == "__main__":
    unittest.main()
