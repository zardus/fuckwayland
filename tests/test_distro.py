#!/usr/bin/env python3
"""w11common/distro.py: which family this box is, and the install line that follows from it.

Every "install the original and I will hand over to it" message named a Debian package and `apt`, on every
distribution, and three of the four families this project now ships for were measured being told something
untrue: Fedora 44 has no `x11-utils`, no `x11-xserver-utils` and no `xorg-x11-utils` either (the packages are
called `xprop` and `xrandr`) [M recon2/fedora.md], Arch calls them `xorg-xprop` and `xorg-xrandr`
[M recon2/arch.md], and a NixOS box with no apt at all was told to run `apt install xdotool`
[M recon2/nixos.md].

The strings are asserted whole rather than by shape. They are what a user reads and then types, and a hint
that is nearly right is worse than none: `dnf install x11-utils` fails with "no match" and teaches nothing.

The exit-127 line these feed and the other three consumers (wmirror's helper hint, warandr's GTK line,
wxrandr's x11 probe) are pinned where each of them lives; this file pins the table they all read."""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# and the tests directory, so `import support` resolves under every invocation
# form (SuiteGuard in tests/test_passthrough.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.
os.environ["W11_PASSTHROUGH"] = "never"

from w11common import distro

#: os-release bodies, as the distributions themselves write them. Ubuntu and
#: Rocky are here because neither says its own family in `ID`.
OS_RELEASE_BODIES = {
    "debian": 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"\nID=debian\n',
    "ubuntu": 'PRETTY_NAME="Ubuntu 26.04 LTS"\nNAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\n',
    "fedora": 'NAME="Fedora Linux"\nVERSION="44 (Cloud Edition)"\nID=fedora\nVERSION_ID=44\n',
    "arch": 'NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\nID=arch\nBUILD_ID=rolling\n',
    "nixos": 'NAME=NixOS\nID=nixos\nVERSION="26.05 (Xantusia)"\n',
    "rocky": 'NAME="Rocky Linux"\nID="rocky"\nID_LIKE="rhel centos fedora"\n',
    "manjaro": 'NAME="Manjaro Linux"\nID=manjaro\nID_LIKE=arch\n',
    "alpine": 'NAME="Alpine Linux"\nID=alpine\n',
}


class Base(unittest.TestCase):
    """A planted /etc/os-release and a NixOS marker that is not there."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w11_distro_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "os-release")
        self.marker = os.path.join(self.tmp, "NIXOS")
        for name, value in (("OS_RELEASE", self.path), ("NIXOS_MARKER", self.marker)):
            old = getattr(distro, name)
            setattr(distro, name, value)
            self.addCleanup(setattr, distro, name, old)

    def plant(self, key):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(OS_RELEASE_BODIES[key])


class Family(Base):
    def test_each_id_lands_on_its_own_family(self):
        for key in ("debian", "fedora", "arch", "nixos"):
            self.plant(key)
            self.assertEqual(distro.family(), key, key)

    def test_id_like_answers_for_a_derivative(self):
        """Ubuntu's ID is `ubuntu` and Rocky's is `rocky`; neither names the package manager it ships, and
        `ID_LIKE` is the field that does."""
        self.plant("ubuntu")
        self.assertEqual(distro.family(), "debian")
        self.plant("rocky")
        self.assertEqual(distro.family(), "fedora")
        self.plant("manjaro")
        self.assertEqual(distro.family(), "arch")

    def test_an_unknown_distribution_and_a_missing_file_are_both_none(self):
        self.plant("alpine")
        self.assertIsNone(distro.family())
        os.unlink(self.path)
        self.assertIsNone(distro.family())
        os.mkdir(self.path)          # unreadable in the other direction, too
        self.assertIsNone(distro.family())

    def test_the_nixos_marker_wins_over_a_shadowed_os_release(self):
        """NixOS's own /etc/NIXOS is there whatever /etc/os-release has been made to say -- a container image
        or a chroot built on another base keeps its os-release and is still a NixOS system."""
        self.plant("debian")
        self.assertEqual(distro.family(), "debian")
        open(self.marker, "w").close()
        self.assertEqual(distro.family(), "nixos")

    def test_quoted_and_bare_values_read_the_same(self):
        """`ID=fedora` and `ID="rocky"` are both in the files above; a parser that keeps the quotes matches
        nothing."""
        self.plant("rocky")
        self.assertEqual(distro.family(), "fedora")
        self.plant("fedora")
        self.assertEqual(distro.family(), "fedora")


class Hints(Base):
    """The bytes, per family and per thing. Measured package names, not guesses."""

    #: what -> family -> the whole install command
    TABLE = {
        "xdotool": {
            "debian": "apt install xdotool",
            "fedora": "dnf install xdotool",
            "arch": "pacman -S xdotool",
            "nixos": "nix-env -iA nixpkgs.xdotool",
        },
        "wmctrl": {
            "debian": "apt install wmctrl",
            "fedora": "dnf install wmctrl",
            "arch": "pacman -S wmctrl",
            "nixos": "nix-env -iA nixpkgs.wmctrl",
        },
        "xprop": {
            "debian": "apt install x11-utils",
            "fedora": "dnf install xprop",
            "arch": "pacman -S xorg-xprop",
            "nixos": "nix-env -iA nixpkgs.xorg.xprop",
        },
        "xrandr": {
            "debian": "apt install x11-xserver-utils",
            "fedora": "dnf install xrandr",
            "arch": "pacman -S xorg-xrandr",
            "nixos": "nix-env -iA nixpkgs.xorg.xrandr",
        },
        "wl-mirror": {
            "debian": "apt install wl-mirror",
            "fedora": "dnf install wl-mirror",
            "arch": "pacman -S wl-mirror",
            "nixos": "nix-env -iA nixpkgs.wl-mirror",
        },
        "gtk3-python": {
            "debian": "apt install python3-gi gir1.2-gtk-3.0",
            "fedora": "dnf install python3-gobject gtk3",
            "arch": "pacman -S python-gobject gtk3",
            "nixos": "nix-env -iA nixpkgs.python3Packages.pygobject3 nixpkgs.gtk3",
        },
    }

    def test_every_cell_of_the_table(self):
        for what, row in self.TABLE.items():
            for family, want in row.items():
                self.assertEqual(distro.hint(what, family), want, (what, family))

    def test_it_reads_this_box_when_no_family_is_named(self):
        self.plant("fedora")
        self.assertEqual(distro.hint("xprop"), "dnf install xprop")
        self.plant("arch")
        self.assertEqual(distro.hint("xrandr"), "pacman -S xorg-xrandr")

    def test_an_unknown_distribution_keeps_debians_bytes(self):
        """Debian's line is what every existing test pins and what four years of documentation says, so an
        unrecognised /etc/os-release must not move a single byte of it."""
        self.plant("alpine")
        for what, row in self.TABLE.items():
            self.assertEqual(distro.hint(what), row["debian"], what)
        os.unlink(self.path)
        self.assertEqual(distro.hint("xprop"), "apt install x11-utils")
        self.assertEqual(distro.hint("xprop", None), "apt install x11-utils")
        self.assertEqual(distro.hint("xprop", "plan9"), "apt install x11-utils")

    def test_the_package_names_alone(self):
        """The packaging tests want the names without the installer in front (the Fedora spec's Requires, the
        PKGBUILD's optdepends), so the table is reachable both ways and the two agree."""
        self.assertEqual(distro.package("xprop", "fedora"), "xprop")
        self.assertEqual(distro.package("xprop", "arch"), "xorg-xprop")
        self.assertEqual(distro.package("gtk3-python", "debian"),
                         "python3-gi gir1.2-gtk-3.0")
        for what in distro.WHAT:
            for family in distro.FAMILIES:
                self.assertIn(distro.package(what, family),
                              distro.hint(what, family))

    def test_the_four_tool_names_are_the_ones_passthrough_already_prints(self):
        """The Debian column is not a new opinion: it is `w11common/passthrough.py:_PACKAGE` copied, which is
        why the exit-127 line does not move on Debian when its consumer starts reading this table."""
        from w11common import passthrough
        for tool, package in passthrough._PACKAGE.items():
            self.assertEqual(distro.package(tool, "debian"), package, tool)

    def test_a_name_the_table_does_not_carry_still_installs(self):
        """`hint()` is not a closed enum: a caller asking for something new gets a command that works rather
        than a KeyError, and on NixOS that means the `nixpkgs.` prefix an attribute path needs."""
        self.assertEqual(distro.hint("foot", "debian"), "apt install foot")
        self.assertEqual(distro.hint("foot", "arch"), "pacman -S foot")
        self.assertEqual(distro.hint("foot", "nixos"), "nix-env -iA nixpkgs.foot")


if __name__ == "__main__":
    unittest.main()
