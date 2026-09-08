"""wxrandr live tests: crazy multimonitor configurations against a real
headless sway, with real xrandr (through XWayland) as the byte oracle for
everything XWayland mirrors faithfully (Screen line, per-output header
geometry/rotation words, monitor listings — the mode TABLES differ by design:
XWayland fabricates a CVT list, wxrandr prints compositor truth).

The non-negotiables from docs/WXRANDR.md all live here: a 4-output session,
L-shape + staircase, mixed scales 1/1.5/2, portrait+landscape, a mirrored
pair, a --newmode custom mode on a created output, disabling the middle of a
row (holes are legal), one atomic multi-stanza call — and after EVERY layout
change the cross-tool invariant: `wdotool getdisplaygeometry` matches the new
bounding box and `wwmctl -d` geometry tracks.

KNOWN WDOTOOL BUG (flagged loudly, not papered over): the wdotool daemon
caches its layout bbox forever (daemon.py _Daemon.geometry: `if self.geom:
return self.geom`), while docs/WXRANDR.md expects a re-read per request. A daemon
that outlives a layout change serves STALE geometry. The invariant here is
therefore checked against a freshly spawned daemon (the old one is killed
first), and test_90_daemon_rereads_geometry_per_request asserts the DESIRED
behavior under @expectedFailure — when someone fixes the daemon, that test
flips to unexpected-success and fails the suite until the decorator is
removed.

Skipped outside `nix develop` (needs sway; xrandr oracle checks additionally
need xrandr + a working XWayland). Private XDG_RUNTIME_DIR per run."""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import HeadlessSway


@unittest.skipUnless(shutil.which("sway"), "sway not on PATH (nix develop)")
class WxrandrLiveTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessSway("wxrandr-live-", need_display=False)
        cls.rtdir, cls.sock = cls.rig.rtdir, cls.rig.sock
        cls.display, cls.sway = cls.rig.display, cls.rig.proc
        # X oracle available? (needs xrandr AND an XWayland that answers)
        cls.have_oracle = bool(cls.display) and bool(shutil.which("xrandr"))
        if cls.have_oracle:
            # XWayland starts on the first client; poke it
            r = cls._oracle_raw()
            cls.have_oracle = r is not None
        # grow the wall: 3 more outputs, names discovered (sway numbers
        # headless outputs globally, never reusing)
        for _ in range(3):
            cls.swaymsg_cls("create_output")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if len(cls.outs_cls()) == 4:
                break
            time.sleep(0.2)
        names = [o["name"] for o in cls.outs_cls()]
        if len(names) != 4:
            cls.rig.stop()
            raise unittest.SkipTest("could not grow to 4 headless outputs")
        cls.n1, cls.n2, cls.n3, cls.n4 = names

    @classmethod
    def tearDownClass(cls):
        cls._kill_daemon()
        cls.rig.stop()

    # -- helpers -------------------------------------------------------------

    @classmethod
    def _env(cls, **extra):
        env = dict(
            os.environ,
            XDG_RUNTIME_DIR=cls.rtdir,
            SWAYSOCK=cls.sock,
            WAYLAND_DISPLAY="wayland-1",
            WDOTOOL_FAKE_UINPUT="1",
            WDOTOOL_UINPUT_PATH="/dev/null",
        )
        if cls.display:
            env["DISPLAY"] = cls.display
        env.update(extra)
        return env

    @classmethod
    def wx(cls, *args, **envextra):
        p = subprocess.run(
            [sys.executable, "-m", "wxrandr", *args],
            env=cls._env(**envextra), capture_output=True, text=True,
            cwd=ROOT, timeout=60,
        )
        return p.returncode, p.stdout, p.stderr

    @classmethod
    def swaymsg_cls(cls, *args):
        subprocess.run(
            ["swaymsg", "-s", cls.sock, *args],
            env=dict(os.environ, XDG_RUNTIME_DIR=cls.rtdir),
            capture_output=True, timeout=10, check=True,
        )

    @classmethod
    def outs_cls(cls):
        p = subprocess.run(
            ["swaymsg", "-s", cls.sock, "-r", "-t", "get_outputs"],
            env=dict(os.environ, XDG_RUNTIME_DIR=cls.rtdir),
            capture_output=True, timeout=10, check=True,
        )
        return json.loads(p.stdout)

    def outs(self):
        return {o["name"]: o for o in self.outs_cls()}

    def rect(self, name):
        r = self.outs()[name]["rect"]
        return (r["x"], r["y"], r["width"], r["height"])

    def bbox(self):
        act = [o for o in self.outs_cls() if o.get("active")]
        if not act:
            return (0, 0)
        return (max(o["rect"]["x"] + o["rect"]["width"] for o in act),
                max(o["rect"]["y"] + o["rect"]["height"] for o in act))

    def wait_layout(self, pred, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if pred():
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    # wdotool daemon management -----------------------------------------------

    @classmethod
    def _daemon_pid(cls):
        path = os.path.join(cls.rtdir, "wdotool.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(3)
            s.connect(path)
            s.sendall(b'{"op": "ping"}\n')
            data = s.recv(4096)
            return json.loads(data).get("pid")
        except (OSError, ValueError):
            return None
        finally:
            s.close()

    @classmethod
    def _kill_daemon(cls):
        pid = cls._daemon_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.05)
                except OSError:
                    break
        try:
            os.unlink(os.path.join(cls.rtdir, "wdotool.sock"))
        except OSError:
            pass

    def getdisplaygeometry(self, fresh=True):
        if fresh:
            # the daemon caches geometry forever (see module docstring);
            # a fresh daemon is the only way to read post-change truth
            self._kill_daemon()
        p = subprocess.run(
            [sys.executable, "-m", "wdotool", "getdisplaygeometry"],
            env=self._env(), capture_output=True, text=True, cwd=ROOT,
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        w, h = p.stdout.split()
        return (int(w), int(h))

    def assert_invariant(self):
        """The cross-tool invariant, after every layout change."""
        w, h = self.bbox()
        self.assertEqual(self.getdisplaygeometry(), (w, h),
                         "wdotool getdisplaygeometry drifted from the "
                         "compositor layout")
        p = subprocess.run(
            [sys.executable, "-m", "wwmctl", "-d"],
            env=self._env(), capture_output=True, text=True, cwd=ROOT,
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        first = p.stdout.splitlines()[0]
        self.assertIn(" DG: %dx%d " % (w, h), first,
                      "wwmctl -d DG does not track the layout: %r" % first)

    # xrandr oracle ------------------------------------------------------------

    @classmethod
    def _oracle_raw(cls):
        try:
            p = subprocess.run(
                ["xrandr"], env=cls._env(), capture_output=True, text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if p.returncode != 0:
            return None
        return p.stdout

    def oracle_geometry_lines(self):
        out = self._oracle_raw()
        if out is None:
            return None
        return [ln for ln in out.splitlines()
                if ln.startswith("Screen ") or ln.startswith("HEADLESS")]

    def our_geometry_lines(self):
        rc, out, err = self.wx()
        self.assertEqual(rc, 0, err)
        return [ln.replace(" primary", "") for ln in out.splitlines()
                if ln.startswith("Screen ")
                or (ln.startswith("HEADLESS")
                    # XWayland REMOVES disabled outputs from RandR entirely;
                    # wxrandr lists them like real X ("connected", no
                    # geometry) — exclude those from the byte comparison
                    and not ln.endswith("connected (normal left inverted "
                                        "right x axis y axis)"))]

    def assert_oracle_agrees(self):
        """Screen line + per-output header lines must match real xrandr's
        XWayland view byte for byte (polling: XWayland trails sway)."""
        if not self.have_oracle:
            self.skipTest("no xrandr/XWayland oracle in this environment")
        ours = self.our_geometry_lines()
        deadline = time.monotonic() + 10
        theirs = None
        while time.monotonic() < deadline:
            theirs = self.oracle_geometry_lines()
            if theirs is not None and sorted(theirs) == sorted(ours):
                return
            time.sleep(0.4)
        self.assertEqual(sorted(theirs or []), sorted(ours),
                         "XWayland's RandR view disagrees with wxrandr")

    # -- the scenarios ---------------------------------------------------------

    def test_01_initial_row_one_atomic_chain(self):
        # one invocation: give the three grown outputs real modes and chain
        # them into a row — relative placement against PENDING geometry
        rc, _o, err = self.wx(
            "--output", self.n2, "--mode", "1024x768", "--right-of", self.n1,
            "--output", self.n3, "--mode", "800x600", "--right-of", self.n2,
            "--output", self.n4, "--mode", "640x480", "--right-of", self.n3)
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(
            lambda: self.rect(self.n4) == (3104, 0, 640, 480)),
            self.outs())
        self.assertEqual(self.rect(self.n2), (1280, 0, 1024, 768))
        self.assertEqual(self.rect(self.n3), (2304, 0, 800, 600))
        self.assertEqual(self.bbox(), (3744, 768))
        self.assert_invariant()

    def test_02_oracle_agrees_on_row(self):
        self.assert_oracle_agrees()

    def test_03_listmonitors_matches_oracle_bytes(self):
        if not self.have_oracle:
            self.skipTest("no xrandr/XWayland oracle in this environment")
        rc, ours, err = self.wx("--listmonitors")
        self.assertEqual(rc, 0, err)

        def theirs():
            p = subprocess.run(["xrandr", "--listmonitors"], env=self._env(),
                               capture_output=True, text=True, timeout=15)
            return p.stdout
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and theirs() != ours:
            time.sleep(0.4)
        self.assertEqual(theirs(), ours)

    def test_10_l_shape(self):
        rc, _o, err = self.wx(
            "--output", self.n2, "--right-of", self.n1,
            "--output", self.n3, "--below", self.n1,
            "--output", self.n4, "--off")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(
            lambda: not self.outs()[self.n4]["active"]
            and self.rect(self.n3) == (0, 720, 800, 600)), self.outs())
        self.assertEqual(self.rect(self.n2), (1280, 0, 1024, 768))
        self.assertEqual(self.bbox(), (2304, 1320))  # the oracle L-shape
        self.assert_invariant()
        self.assert_oracle_agrees()

    def test_11_staircase(self):
        # n1 top-left, n2 right-of n1, n3 below n2, n4 right-of n3: stairs,
        # every step resolved against PENDING geometry in one call
        rc, _o, err = self.wx(
            "--output", self.n4, "--auto",
            "--output", self.n2, "--right-of", self.n1,
            "--output", self.n3, "--below", self.n2,
            "--output", self.n4, "--right-of", self.n3)
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(
            lambda: self.rect(self.n3) == (1280, 768, 800, 600)),
            self.outs())
        self.assertEqual(self.rect(self.n2), (1280, 0, 1024, 768))
        self.assertEqual(self.rect(self.n4), (2080, 768, 640, 480))
        self.assertEqual(self.bbox(), (2720, 1368))
        self.assert_invariant()

    def test_12_mixed_scales(self):
        # scales 1 / 1.5 / 2 in a row; logical sizes are the compositor's
        # truncated px/scale (1024/1.5 -> 682, 800/2 -> 400)
        rc, _o, err = self.wx(
            "--output", self.n1, "--scale", "1x1", "--pos", "0x0",
            "--output", self.n2, "--scale", "1.5", "--right-of", self.n1,
            "--output", self.n3, "--scale", "2x2", "--right-of", self.n2,
            "--output", self.n4, "--off")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(
            lambda: self.rect(self.n3) == (1962, 0, 400, 300)), self.outs())
        self.assertEqual(self.rect(self.n2), (1280, 0, 682, 512))
        self.assertEqual(self.bbox(), (2362, 720))
        self.assert_invariant()
        self.assert_oracle_agrees()

    def test_13_portrait_landscape_mix(self):
        rc, _o, err = self.wx(
            "--output", self.n2, "--scale", "1", "--rotate", "left",
            "--right-of", self.n1,
            "--output", self.n3, "--scale", "1", "--rotate", "right",
            "--right-of", self.n2)
        self.assertEqual(rc, 0, err)
        # left/right rotation swaps W/H: 1024x768 -> 768x1024
        self.assertTrue(self.wait_layout(
            lambda: self.rect(self.n2) == (1280, 0, 768, 1024)), self.outs())
        self.assertEqual(self.rect(self.n3), (2048, 0, 600, 800))
        self.assertEqual(self.outs()[self.n2]["transform"], "270")  # left
        self.assertEqual(self.outs()[self.n3]["transform"], "90")   # right
        self.assertEqual(self.bbox(), (2648, 1024))
        self.assert_invariant()
        self.assert_oracle_agrees()  # rotation words: `left`, `right`

    def test_14_mirrored_pair(self):
        rc, _o, err = self.wx(
            "--output", self.n2, "--rotate", "normal",
            "--output", self.n3, "--rotate", "normal", "--mode", "1280x720",
            "--same-as", self.n1)
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(
            lambda: self.rect(self.n3) == self.rect(self.n1)), self.outs())
        self.assertEqual(self.rect(self.n3), (0, 0, 1280, 720))
        self.assert_invariant()

    def test_20_custom_mode_on_created_output(self):
        rc, _o, err = self.wx("--newmode", "wxr-fancy", "74.50",
                              "1280", "1344", "1472", "1664",
                              "720", "723", "728", "748", "-hsync", "+vsync")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_o, "")
        rc, _o, err = self.wx("--addmode", self.n4, "wxr-fancy")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.wx("--output", self.n4, "--auto", "--mode",
                              "wxr-fancy", "--right-of", self.n2)
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(
            lambda: self.outs()[self.n4]["active"]
            and self.rect(self.n4)[2:] == (1280, 720)), self.outs())
        # the modeline math landed in the compositor: 74.50MHz/1664x748
        cm = self.outs()[self.n4]["current_mode"]
        self.assertEqual((cm["width"], cm["height"]), (1280, 720))
        self.assertAlmostEqual(cm["refresh"] / 1000.0, 59.855, delta=0.01)
        # and the query names it, starred as current
        rc, out, err = self.wx()
        self.assertEqual(rc, 0, err)
        self.assertIn("   wxr-fancy     59.86* ", out)
        self.assert_invariant()

    def test_21_rmmode_removes_from_store(self):
        rc, _o, err = self.wx("--rmmode", "wxr-fancy")
        self.assertEqual(rc, 0, err)
        rc, out, _e = self.wx()
        self.assertNotIn("wxr-fancy", out)
        rc, _o, err = self.wx("--rmmode", "wxr-fancy")
        self.assertEqual(rc, 1)
        self.assertEqual(err, 'xrandr: cannot find mode "wxr-fancy"\n')

    def test_30_disable_middle_of_row_leaves_hole(self):
        rc, _o, err = self.wx(
            "--output", self.n1, "--pos", "0x0",
            "--output", self.n2, "--mode", "1024x768", "--right-of", self.n1,
            "--output", self.n3, "--mode", "800x600", "--right-of", self.n2,
            "--output", self.n4, "--off")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(
            lambda: self.rect(self.n3) == (2304, 0, 800, 600)), self.outs())
        rc, _o, err = self.wx("--output", self.n2, "--off")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(
            lambda: not self.outs()[self.n2]["active"]), self.outs())
        # the hole is legal: n3 keeps its position, nothing reflows
        self.assertEqual(self.rect(self.n3), (2304, 0, 800, 600))
        self.assertEqual(self.bbox(), (3104, 720))
        self.assert_invariant()
        self.assert_oracle_agrees()
        # --auto brings the middle back at its recorded mode
        rc, _o, err = self.wx("--output", self.n2, "--auto")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(
            lambda: self.outs()[self.n2]["active"]), self.outs())
        self.assertEqual(self.rect(self.n2)[2:], (1024, 768))
        self.assert_invariant()

    def test_40_atomic_multi_stanza_reshape(self):
        # ONE invocation reshaping everything: modes, rotation, scale,
        # chained relatives, and a disable
        rc, _o, err = self.wx(
            "--output", self.n1, "--mode", "1280x720", "--pos", "0x0",
            "--rotate", "normal",
            "--output", self.n2, "--mode", "1024x768", "--rotate", "left",
            "--scale", "2", "--right-of", self.n1,
            "--output", self.n3, "--mode", "800x600", "--scale", "1.5",
            "--below", self.n1,
            "--output", self.n4, "--off")
        self.assertEqual(rc, 0, err)
        # n2: rotate-left swaps to 768x1024, scale 2 halves -> 384x512
        # n3: 800x600 / 1.5 -> 533x400 (truncated), below n1
        self.assertTrue(self.wait_layout(
            lambda: self.rect(self.n2) == (1280, 0, 384, 512)), self.outs())
        self.assertEqual(self.rect(self.n3), (0, 720, 533, 400))
        self.assertFalse(self.outs()[self.n4]["active"])
        self.assertEqual(self.bbox(), (1664, 1120))
        self.assert_invariant()
        self.assert_oracle_agrees()

    def test_41_wlr_backend_atomic_apply(self):
        # the generic-wlroots backend: one zwlr_output_configuration
        rc, _o, err = self.wx(
            "--output", self.n2, "--rotate", "normal", "--scale", "1",
            "--right-of", self.n1,
            "--output", self.n3, "--scale", "1", "--same-as", self.n1,
            WXRANDR_BACKEND="wlr")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(
            lambda: self.rect(self.n2) == (1280, 0, 1024, 768)), self.outs())
        self.assertEqual(self.rect(self.n3)[:2], (0, 0))
        self.assertEqual(self.outs()[self.n2]["transform"], "normal")
        self.assert_invariant()
        # and the wlr backend can query, agreeing on the bounding box
        rc, out, err = self.wx(WXRANDR_BACKEND="wlr")
        self.assertEqual(rc, 0, err)
        w, h = self.bbox()
        self.assertIn("current %d x %d," % (w, h), out.splitlines()[0])

    def test_42_wlr_relative_placement_at_fractional_scales(self):
        """--right-of has to land edge to edge at any scale.  wlroots runs a
        scale quantised to 120ths in float32, and the wl_fixed the wlr
        backend sends is itself truncated to 256ths, so a position computed
        from the number the user typed misses by 1-10 px -- a gap or an
        overlap nobody asked for, and (unlike the sway backend) there is no
        second phase to correct it."""
        for asked in ("1.03", "1.08", "1.14", "1.35", "1.6", "2.67"):
            with self.subTest(scale=asked):
                rc, _o, err = self.wx(
                    "--output", self.n1, "--scale", asked, "--pos", "0x0",
                    "--output", self.n2, "--scale", "1", "--right-of",
                    self.n1, WXRANDR_BACKEND="wlr")
                self.assertEqual(rc, 0, err)
                self.assertTrue(self.wait_layout(
                    lambda: self.rect(self.n2)[0] ==
                    self.rect(self.n1)[0] + self.rect(self.n1)[2]),
                    "%s: %r" % (asked, self.outs()))
        # put the wall back the way test_41 left it: the later tests build
        # on that layout
        rc, _o, err = self.wx(
            "--output", self.n1, "--scale", "1", "--pos", "0x0",
            "--output", self.n2, "--scale", "1", "--right-of", self.n1,
            "--output", self.n3, "--scale", "1", "--same-as", self.n1,
            WXRANDR_BACKEND="wlr")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(
            lambda: self.rect(self.n2) == (1280, 0, 1024, 768)), self.outs())

    def test_50_negative_origin_normalizes_like_xrandr(self):
        rc, _o, err = self.wx("--output", self.n3, "--pos", "-500x-300")
        self.assertEqual(rc, 0, err)
        # xrandr normalizes min x = min y = 0: n3 to origin, others shifted
        self.assertTrue(self.wait_layout(
            lambda: self.rect(self.n3)[:2] == (0, 0)
            and self.rect(self.n1)[:2] == (500, 300)), self.outs())
        self.assertEqual(self.rect(self.n2)[:2], (1780, 300))
        self.assert_invariant()

    def test_51_dryrun_prints_plan_mutates_nothing(self):
        before = [(o["name"], o["rect"], o.get("transform"))
                  for o in self.outs_cls()]
        rc, out, err = self.wx(
            "--dryrun",
            "--output", self.n1, "--mode", "640x480", "--rotate", "inverted",
            "--output", self.n2, "--off")
        self.assertEqual(rc, 0, err)
        self.assertIn("crtc ", out)
        self.assertIn("screen 0: ", out)
        self.assertIn('"%s"' % self.n1, out)
        time.sleep(1.0)
        self.assertEqual(before, [(o["name"], o["rect"], o.get("transform"))
                                  for o in self.outs_cls()])

    def test_52_primary_persists_and_lists(self):
        rc, _o, err = self.wx("--output", self.n2, "--primary")
        self.assertEqual(rc, 0, err)
        rc, out, _e = self.wx()
        self.assertIn("%s connected primary " % self.n2, out)
        rc, out, _e = self.wx("--listmonitors")
        self.assertIn("+*%s " % self.n2, out)
        rc, _o, err = self.wx("--noprimary")
        self.assertEqual(rc, 0, err)
        rc, out, _e = self.wx()
        self.assertNotIn(" primary ", out)

    def test_53_gamma_on_headless_fails_like_lutless_crtc(self):
        # headless outputs have no gamma LUT: the compositor refuses the
        # zwlr_gamma_control and wxrandr reports it exactly as xrandr does
        # for a gamma-less crtc (real-hardware sway sessions do get the
        # detached holder; that machinery is proven in test_wxrandr_gamma)
        rc, out, err = self.wx("--output", self.n1, "--brightness", "0.5")
        self.assertEqual(rc, 1)
        self.assertEqual(err, "xrandr: Gamma size is 0.\n")

    def test_54_unknown_output_and_mode_error_bytes(self):
        rc, out, err = self.wx("--output", "NOSUCH", "--mode", "800x600")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "warning: output NOSUCH not found; ignoring\n")
        rc, out, err = self.wx("--output", self.n1, "--left-of", "NOSUCH")
        self.assertEqual(rc, 1)
        self.assertEqual(err, 'xrandr: cannot find output "NOSUCH"\n')

    def test_55_version_bytes(self):
        rc, out, err = self.wx("--version")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "xrandr program version       1.5.4\n"
                              "Server reports RandR version 1.6\n")
        # and NO XWayland warning: wxrandr talks to the compositor
        self.assertEqual(err, "")

    def test_90_daemon_rereads_geometry_per_request(self):
        """Per docs/WXRANDR.md: the daemon re-reads geometry per request, so a
        layout change is visible to an already-running daemon immediately."""
        geom1 = self.getdisplaygeometry(fresh=True)  # daemon now caching
        rc, _o, err = self.wx("--output", self.n3, "--pos",
                              "%dx0" % (geom1[0] + 640))
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait_layout(lambda: self.bbox() != geom1))
        stale = self.getdisplaygeometry(fresh=False)  # same daemon
        self.assertEqual(stale, self.bbox(),
                         "wdotool daemon served stale geometry %r, layout "
                         "is %r" % (stale, self.bbox()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
