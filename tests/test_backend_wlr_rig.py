#!/usr/bin/env python3
"""What the rig's labwc flavors give the wlr backend's ext_workspace reader to read.

`WlrBackend`'s workspace half is the one capability the wlroots floor has that sway 1.11 has not, and
vm/live-smoke.d/labwc.sh is where it is exercised live -- `wwmctl -d` counting rows, `set_desktop` /
`get_desktop` round-tripping, `wwmctl -d` starring exactly one.  Every one of those checks is worth nothing
on a session with one workspace, and CI run 34308982263 ran exactly that:

    FAIL windows: wwmctl -d lists 1 desktop(s): ext_workspace_manager_v1 is what this flavor is for

on resolute-lxqt-wayland, while resolute-labwc counted three.  The difference is in the rig and not in the
reader: `vm/build-image.sh`'s `labwc_config` writes three desktop names into ~/.config/labwc/rc.xml for the
BARE labwc flavor, and the LXQt Wayland session does not use that file -- it is `startlxqtwayland` that
fills ~/.config/labwc, by copying /usr/share/lxqt/wayland/labwc the first time the directory is missing, and
lxqt-wayland-session 0.3.1 ships one desktop called `Default` in it.  Measured in the guest 2026-09-09:

    $ ps -u test -o args= | grep labwc
    labwc -C /home/test/.config/labwc -S lxqt-session
    $ sed -n '104p' /usr/bin/startlxqtwayland
       cp -av "$share_dir"/lxqt/wayland/labwc "$XDG_CONFIG_HOME"/  # use default location here

so the rig patches the SYSTEM copy, and the session's own first-run copy carries the extra names along with
everything else LXQt ships in that directory.  This file is that patch, run against the real rc.xml
(tests/fixtures/live/lxqt-wayland-session-0.3.1-labwc-rc.xml, 25288 bytes, copied out of the guest) rather
than against a hand-written stand-in -- because the trap in that file is a four-name `<desktops>` example
sitting in an XML comment eight lines above the live one, and only the real bytes have it.

The last class here is the other half of the same rig-not-the-reader story: on BARE labwc the step file's
own desktop click opens labwc's root menu, and the menu holds the keyboard grab until something releases
it, so the keystrokes the input phase sends afterwards land nowhere.  The fix is one `wdotool key Escape`
in the step file, and this is what keeps it there between VM runs -- the replay harness cannot, because a
replay checks that every command the step file asks for was recorded and not that every recorded command
is still asked for (measured: with that line deleted, selftest-offline pass 6 still replays resolute-labwc
31 pass / 0 fail).
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py (which covers pytest) and
# tests/test_passthrough.py.  This line is what covers `python3 tests/<file>.py`, where conftest is not
# loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BUILD = os.path.join(ROOT, "vm", "build-image.sh")
LABWC_SH = os.path.join(ROOT, "vm", "live-smoke.d", "labwc.sh")
SHIPPED_RC = os.path.join(ROOT, "tests", "fixtures", "live",
                          "lxqt-wayland-session-0.3.1-labwc-rc.xml")


def build_script() -> str:
    with open(BUILD, encoding="utf-8") as fh:
        return fh.read()


def desktops_block(text: str) -> str:
    """The rc.xml's LIVE `<desktops>` element -- the one at two spaces of indent, outside every comment."""
    m = re.search(r"(?m)^  <desktops>\n(?:.*\n)*?^  </desktops>$", text)
    assert m, "no live <desktops> block"
    return m.group(0)


class TheShippedRcXmlStillHasTheShapeThePatchAssumes(unittest.TestCase):
    """The fixture is a third party's file; when it moves, this is what says so before a golden build does."""

    def setUp(self):
        with open(SHIPPED_RC, encoding="utf-8") as fh:
            self.rc = fh.read()

    def test_one_desktop_named_default_at_six_spaces(self):
        self.assertIn("\n      <name>Default</name>\n", self.rc)
        self.assertEqual(desktops_block(self.rc).count("<name>"), 1)

    def test_the_commented_example_is_the_trap_and_is_indented_differently(self):
        """Four more `<name>` lines live in an XML comment above the real block, at eight spaces. A patch
        anchored on the tag rather than the whole line would rewrite those and change nothing that runs."""
        self.assertIn("\n        <name>Workspace 1</name>\n", self.rc)
        self.assertEqual(self.rc.count("<name>"), 11)


class TheLxqtWaylandArmPatchesIt(unittest.TestCase):
    """`lxqt_wayland_desktops` out of vm/build-image.sh, run as the shell over the real file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-lxqt-rc-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        share = os.path.join(self.tmp, "usr", "share", "lxqt", "wayland", "labwc")
        os.makedirs(share)
        self.rc = os.path.join(share, "rc.xml")
        shutil.copyfile(SHIPPED_RC, self.rc)

    def run_arm(self, expect_rc=0):
        """The arm's own lines, with build-image.sh's `fail`/`say`/`written` stubbed to the same shapes."""
        text = build_script()
        m = re.search(r"(?m)^lxqt_wayland_desktops\(\) \{.*\n(?:.*\n)*?^\}$", text)
        self.assertTrue(m, "vm/build-image.sh has no lxqt_wayland_desktops()")
        script = (
            'VMCTL_ROOT=%s\n'
            'fail() { echo "FAILED: $*" >&2; exit 1; }\n'
            'say() { echo "vmctl-build: $*"; }\n'
            'written() { printf "%%s\\n" "$@" >> "$VMCTL_ROOT/written.txt"; }\n'
            % self.tmp) + m.group(0) + "\nlxqt_wayland_desktops\n"
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, expect_rc, p.stderr)
        return p

    def rc_text(self):
        with open(self.rc, encoding="utf-8") as fh:
            return fh.read()

    def test_the_live_block_gains_two_names_and_keeps_the_first(self):
        """The whole element, byte for byte -- the names, their order, and the indent they are written at.
        A `<name>` counted rather than read would pass on two lines glued into one, and one at a different
        indent would leave the file readable but not the file this patch says it writes."""
        self.run_arm()
        self.assertEqual(desktops_block(self.rc_text()),
                         "  <desktops>\n"
                         "    <popupTime>1000</popupTime>\n"
                         "    <names>\n"
                         "      <name>Default</name>\n"
                         "      <name>Workspace 2</name>\n"
                         "      <name>Workspace 3</name>\n"
                         "    </names>\n"
                         "  </desktops>")

    def test_nothing_outside_the_live_block_moves(self):
        """Two lines added inside the live `<desktops>` and not one byte outside it -- the commented example,
        the popupTime, every other setting LXQt ships in that file.

        Compared by splitting the file ON that block rather than by deleting the two lines from the result:
        the commented names are the same tag at eight spaces, so a plain string removal of the six-space form
        eats the tail of a comment line and reports a difference that is the test's own doing."""
        before = self.rc_text()
        self.run_arm()
        after = self.rc_text()
        self.assertEqual(after.count("<name>"), before.count("<name>") + 2)
        head_b, _, tail_b = before.partition(desktops_block(before))
        head_a, _, tail_a = after.partition(desktops_block(after))
        self.assertEqual((head_a, tail_a), (head_b, tail_b))
        self.assertIn("\n        <name>Workspace 1</name>\n", head_a)

    def test_it_is_recorded_for_the_selinux_relabel(self):
        self.run_arm()
        with open(os.path.join(self.tmp, "written.txt"), encoding="utf-8") as fh:
            self.assertIn(self.rc, fh.read().split())

    def test_a_file_that_no_longer_has_the_anchor_fails_the_build(self):
        """A silent no-op here would ship a golden with one workspace and a step file that says so 300
        seconds into a VM run; the build stops instead, naming the file."""
        with open(self.rc, "w", encoding="utf-8") as fh:
            fh.write("<labwc_config>\n  <desktops>\n    <names>\n"
                     "      <name>Something Else</name>\n    </names>\n  </desktops>\n</labwc_config>\n")
        p = self.run_arm(expect_rc=1)
        self.assertIn("no longer has the one-line <name>Default</name>", p.stderr)

    def test_a_missing_file_fails_the_build(self):
        os.unlink(self.rc)
        p = self.run_arm(expect_rc=1)
        self.assertIn("lxqt-wayland-session", p.stderr)


class TheDesktopArmsCallIt(unittest.TestCase):
    """Which flavors get workspaces, and by which of the two routes."""

    def arm(self, name):
        """One shell function of vm/build-image.sh, `name() {` to the `}` in column 0. The opening line may
        carry a trailing comment (labwc_config's does), so the pattern stops at the brace, not at the newline.
        """
        m = re.search(r"(?m)^%s\(\) \{.*\n(?:.*\n)*?^\}$" % name, build_script())
        self.assertTrue(m, name)
        return m.group(0)

    def calls(self, name) -> set:
        """The helpers an arm CALLS, which is not the same as the names it mentions: three of these arms
        carry a comment about `labwc_config` saying why they do not call it."""
        out = set()
        for line in self.arm(name).splitlines()[1:-1]:
            word = line.split("#", 1)[0].strip().split(" ", 1)[0]
            if word:
                out.add(word)
        return out

    def test_lxqt_wayland_patches_the_system_copy_and_does_not_write_labwcs_own_dir(self):
        self.assertIn("lxqt_wayland_desktops", self.calls("desktop_lxqt_wayland"))
        # labwc_config would also write ~/.config/labwc/environment, and labwc OVERRIDES an already-set
        # XDG_CURRENT_DESKTOP out of it -- the reason the arm has never called it.
        self.assertNotIn("labwc_config", self.calls("desktop_lxqt_wayland"))

    def test_the_bare_labwc_arm_still_writes_its_own_three(self):
        self.assertIn("labwc_config", self.calls("desktop_labwc"))
        self.assertNotIn("lxqt_wayland_desktops", self.calls("desktop_labwc"))
        names = re.findall(r"<name>([^<]*)</name>", self.arm("labwc_config"))
        self.assertEqual(names, ["1", "2", "3"])

    def test_budgie_brings_its_own_and_neither_arm_touches_it(self):
        """Budgie ships four workspaces of its own [recon2/budgie]; the rig adds nothing, and the step file
        reads the count rather than assuming it."""
        calls = self.calls("desktop_budgie")
        self.assertNotIn("labwc_config", calls)
        self.assertNotIn("lxqt_wayland_desktops", calls)
        self.assertIn("autostart_wlr_layout", calls)


class TheRootMenuGrabIsReleasedBeforeAnythingIsTyped(unittest.TestCase):
    """vm/live-smoke.d/labwc.sh's phase_windows, read as text.

    `wdotool mousemove 300 300 click 1` lands on labwc's Root context, whose stock rc.xml binds button 1
    there to `ShowMenu root-menu`; the menu opens on Virtual-1's wallpaper and takes the keyboard grab.
    Measured on the rig 2026-09-09: after that click `wdotool type -- 'us: yz@'` left $SMOKE_FILE empty
    (the three phase_input FAILs of CI run 34308982263 on this flavor and no other), and one Escape before
    it landed the same string byte-exact.  Only bare labwc reaches labwc's Root -- Budgie, Xfce and LXQt
    put a desktop window under that pixel -- but xfce-wayland.sh sources this file whole, so the line is
    load-bearing for two flavors."""

    def setUp(self):
        with open(LABWC_SH, encoding="utf-8") as fh:
            self.sh = fh.read()

    def phase_windows(self) -> str:
        m = re.search(r"(?m)^phase_windows\(\) \{.*\n(?:.*\n)*?^\}$", self.sh)
        self.assertTrue(m, "vm/live-smoke.d/labwc.sh has no phase_windows()")
        return m.group(0)

    def tail_after_the_root_click(self) -> str:
        """Everything phase_windows RUNS after the click that opens the menu.

        Comment lines are dropped first: the six lines that explain the Escape quote `wdotool type -- 'us:
        yz@'` as the string that came back empty, and a test reading them would be reading prose."""
        body = "\n".join(ln for ln in self.phase_windows().splitlines()
                         if not ln.lstrip().startswith("#"))
        head, sep, tail = body.partition('guestq "wdotool mousemove 300 300 click 1"')
        self.assertTrue(sep, "phase_windows no longer clicks the desktop at 300,300")
        return tail

    def test_an_escape_follows_the_desktop_click(self):
        self.assertIn('guest "wdotool key Escape"', self.tail_after_the_root_click())

    def test_nothing_is_typed_into_the_menu_before_that_escape(self):
        """The order is the whole fix: an Escape sent after the next keystroke releases a grab that has
        already eaten it.  Read as the FIRST wdotool key/type in the tail, not as a substring anywhere in
        it, so moving the Escape below a `type` goes red rather than staying green on both being present."""
        tail = self.tail_after_the_root_click()
        keystrokes = re.findall(r"wdotool (?:key|type)\b[^\n]*", tail)
        self.assertTrue(keystrokes, "nothing keyboard-shaped after the click at all")
        self.assertTrue(keystrokes[0].startswith("wdotool key Escape"), keystrokes[:3])

    def test_the_window_is_activated_again_after_the_escape(self):
        """The click also moved the focus off the editor; phase_input types into whatever is focused."""
        _, _, after = self.tail_after_the_root_click().partition('guest "wdotool key Escape"')
        self.assertIn("wdotool windowactivate --sync $WIN", after)

    def test_xfce_wayland_inherits_it_by_sourcing_this_file(self):
        """The second flavor the line covers: xfce-wayland.sh defines no phase_windows of its own."""
        with open(os.path.join(ROOT, "vm", "live-smoke.d", "xfce-wayland.sh"), encoding="utf-8") as fh:
            xfce = fh.read()
        self.assertIn('. "$STEPS/labwc.sh"', xfce)
        self.assertNotIn("phase_windows()", xfce)


if __name__ == "__main__":
    unittest.main()
