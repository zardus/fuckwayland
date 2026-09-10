"""warandr GUI test: the real GTK 3 editor under Xvfb, driven with xdotool.

Skipped unless GTK 3 is importable and Xvfb + xdotool are on PATH.  The
editor talks to tests/fixtures/fake_xrandr.py (WARANDR_XRANDR), a 3-output
RandR simulator that records every apply.  The editor's test hook
WARANDR_TEST_LAYOUT_DUMP writes root-window coordinates of the output boxes,
toolbar buttons and popup-menu items (and every status-bar change), so the
test clicks real pixels:

  hover HDMI-1 -> its description in the status bar; leave -> the command
  right-click HDMI-1 -> arandr's menu order, no Scale on X11
                     -> Orientation -> left
  drag DP-2 next to the (now portrait) HDMI-1; it snaps to its right edge
  Apply -> the recorded argv is exactly the arandr-shaped command
  Save As (WARANDR_TEST_SAVE_AS) -> arandr's two-line layout script
  a failing apply shows the error dialog, keeps the edits, editor survives
  the status bar's backend indicator names the live backend at all times
  Layout ▸ Backend -> radios, the unreachable ones insensitive with the
                      reason in their tooltip
                   -> GNOME (mutter): the layout is re-read *through* it,
                      the canvas redrawn, and Apply, the command in the
                      status bar and a saved script are that backend's
  a backend that cannot be reached -> the dialog, and the previous one back

Every popup dump also carries the *modelled* item positions — what the
editor computes from where GTK asked for the popup (at the pointer, right
of the parent item, below the menubar item), which is all a Wayland driver
gets — and X11, where GDK knows the truth, checks the model against it for
the context menu, its Orientation submenu, the menubar's Outputs drop-down
and a per-output submenu of that.

tests/fixtures/gui_probe.py runs in-process checks under the same Xvfb
(Apply on a worker thread, popup release, zoom radios, menu shapes, layout
dumps that wait for GTK's allocation, the Save As PATH hint), and a run
without any display must end in one line, not a traceback.

Set WARANDR_TEST_SHOTS=DIR to keep `import -window root` screenshots."""

import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, ROOT)


def _gtk_available():
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        return True
    except Exception:
        return False


HAVE_GTK = _gtk_available()
HAVE_X = bool(shutil.which("Xvfb") and shutil.which("xdotool"))
SHOTS = os.environ.get("WARANDR_TEST_SHOTS")

EXPECTED = ["--output", "DP-1", "--primary", "--mode", "1920x1080",
            "--pos", "0x0", "--rotate", "normal",
            "--output", "HDMI-1", "--mode", "1280x1024", "--pos", "1920x0",
            "--rotate", "left",
            "--output", "DP-2", "--mode", "1280x720", "--pos", "2944x0",
            "--rotate", "normal",
            "--output", "HDMI-2", "--off"]
ARANDR_MENU = ["Active", "Primary", "Resolution", "Orientation",
               "Refresh rate", "Reflection", "Mirror of"]
BACKEND_MENU = ["Automatic", "X11 (xrandr)", "sway", "Hyprland (hypr)", "wlroots (wlr)",
                "GNOME (mutter)", "Cinnamon (muffin)", "KDE (kwin)"]
# what tests/fixtures/fake_xrandr.py simulates for `--backends`
FAKE_AVAILABLE = {"sway": False, "hypr": False, "kwin": False, "mutter": True,
                  "cinnamon": False, "wlr": False, "x11": True}


def _base_env(tmp, display):
    env = dict(os.environ)
    env.update({
        "DISPLAY": display, "GDK_BACKEND": "x11",
        "NO_AT_BRIDGE": "1", "GSETTINGS_BACKEND": "memory",
        "HOME": tmp,
        # the tree first, then whatever the parent had: under nix the parent's PYTHONPATH is where
        # gi lives, and replacing it turns "no DISPLAY" into "GTK 3 for Python is not available"
        "PYTHONPATH": ROOT + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
        "WARANDR_XRANDR": "%s %s" % (sys.executable,
                                     os.path.join(FIXTURES, "fake_xrandr.py")),
        "FAKE_XRANDR_STATE": os.path.join(tmp, "state.json"),
    })
    env.pop("WAYLAND_DISPLAY", None)
    return env


@unittest.skipUnless(HAVE_GTK, "GTK 3 not importable (python3-gi + "
                     "gir1.2-gtk-3.0)")
@unittest.skipUnless(HAVE_X, "Xvfb/xdotool not on PATH")
class XvfbCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r, w = os.pipe()
        cls.xvfb = subprocess.Popen(
            ["Xvfb", "-displayfd", str(w), "-screen", "0", "1280x1024x24",
             "-nolisten", "tcp"], pass_fds=[w],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(w)
        buf = b""
        deadline = time.time() + 15
        while b"\n" not in buf and time.time() < deadline:
            chunk = os.read(r, 16)
            if not chunk:
                break
            buf += chunk
        os.close(r)
        if not buf.strip():
            cls.xvfb.kill()
            raise unittest.SkipTest("Xvfb did not start")
        cls.display = ":" + buf.decode().strip()

    @classmethod
    def tearDownClass(cls):
        cls.xvfb.terminate()
        try:
            cls.xvfb.wait(5)
        except subprocess.TimeoutExpired:
            cls.xvfb.kill()


class GuiSession(XvfbCase):
    """One warandr window on the Xvfb display, talking to the fake xrandr,
    plus the driving helpers.  `STATE` seeds the fake's screen (None: its own
    three-output default) and `NBOXES` is how many boxes that screen draws."""

    STATE = None
    NBOXES = 3
    SLOW_QUERY = None        # seconds a *query* takes (tests/fixtures/
                             # slow_xrandr.py); None: the instant fake
    EXTRA_ENV = {}           # what this class needs the fake to pretend
    CLI_ARGS = ()            # warandr's own options, for a window started the
                             # way a hotkey or a .desktop entry starts one
    CONSENT = None           # a GNOME overlap agreement already on disk before
                             # the window opens (the fake's record shape)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="warandr-gui-")
        self.dump = os.path.join(self.tmp, "dump.jsonl")
        self.log = os.path.join(self.tmp, "apply.log")
        self.qlog = os.path.join(self.tmp, "query.log")
        self.saved = os.path.join(self.tmp, "saved")
        env = _base_env(self.tmp, self.display)
        if self.STATE is not None:
            with open(env["FAKE_XRANDR_STATE"], "w") as fh:
                json.dump(self.STATE, fh)
        env.update({
            "FAKE_XRANDR_LOG": self.log,
            "FAKE_XRANDR_QUERY_LOG": self.qlog,
            "WARANDR_TEST_LAYOUT_DUMP": self.dump,
            "WARANDR_TEST_SAVE_AS": self.saved,
        })
        if self.SLOW_QUERY is not None:
            env["WARANDR_XRANDR"] = "%s %s" % (
                sys.executable, os.path.join(FIXTURES, "slow_xrandr.py"))
            env["SLOW_QUERY"] = str(self.SLOW_QUERY)
        env.update(self.EXTRA_ENV)
        self.env = env
        if self.CONSENT is not None:
            # before the window opens: `--gnome-overlap-status` is asked once,
            # on the startup worker thread, and a record written after that is a
            # record this run never sees
            with open(self.consent_file(), "w") as fh:
                json.dump(self.CONSENT, fh)
        self.app_log = open(os.path.join(self.tmp, "app.log"), "w")
        self.launch()

    def consent_file(self):
        """Where the fake keeps the agreement: next to the apply log."""
        return os.path.join(self.tmp, "consent.json")

    def launch(self):
        after = len(self.dumps())      # a relaunch must not match old dumps
        self.mark = after              # where this run's dumps start
        self.app = subprocess.Popen([sys.executable, "-m", "warandr"]
                                    + list(self.CLI_ARGS),
                                    env=self.env, stdout=self.app_log,
                                    stderr=subprocess.STDOUT)
        return self.wait_dump("layout",
                              lambda d: len(d["boxes"]) == self.NBOXES,
                              after=after)

    def tearDown(self):
        if self.app.poll() is None:
            self.app.terminate()
            try:
                self.app.wait(5)
            except subprocess.TimeoutExpired:
                self.app.kill()
        self.app_log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def dumps(self):
        out = []
        try:
            with open(self.dump) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass     # a line the editor is still writing
        except OSError:
            pass
        return out

    def wait_dump(self, kind, pred=lambda d: True, timeout=15, after=0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            ds = self.dumps()
            for i in range(len(ds) - 1, after - 1, -1):
                d = ds[i]
                if d["kind"] == kind and pred(d):
                    return d, i + 1      # the next wait starts after it
            if self.app.poll() is not None:
                self.fail("warandr exited with %d:\n%s" % (
                    self.app.returncode, self.app_log_text()))
            time.sleep(0.05)
        self.fail("no %r dump within %ds; dumps=%r\napp log:\n%s" % (
            kind, timeout, self.dumps()[-3:], self.app_log_text()))

    def app_log_text(self):
        """warandr's own stdout+stderr, for a failure message.  Read through a
        context manager: an expectedFailure takes this path on every run, and a
        dangling handle there is a ResourceWarning in the file's output."""
        try:
            with open(self.app_log.name) as fh:
                return fh.read()
        except OSError as e:
            return "<log unreadable: %s>" % e

    def xdo(self, *args):
        subprocess.run(["xdotool"] + [str(a) for a in args], env=self.env,
                       check=True, timeout=30)

    def centre(self, rect):
        x, y, w, h = rect
        return x + w // 2, y + h // 2

    def click(self, rect, button=1):
        x, y = self.centre(rect)
        self.xdo("mousemove", x, y, "click", button)

    def click_for(self, rect, kind, pred, after, tries=3, wait=6):
        """Click `rect` until a `kind` dump satisfying `pred` lands.  A popup
        menu item under a bare Xvfb (no window manager) takes the first click
        most of the time and not always: GTK pops the submenu at the pointer
        and the item rectangle the app modelled is where it lands, but a click
        that arrives while the menu is still mapping is swallowed.  Measured
        on the CI containers and on this host: the same test passing twice and
        failing three times in a row with nothing else changed.  The bound
        keeps a real miss a failure; the retries keep a slow map from being
        one."""
        for attempt in range(tries):
            time.sleep(0.3)         # let a just-popped submenu finish mapping
            self.shot("click-%s-%d" % (kind, attempt))
            self.click(rect)
            try:
                return self.wait_dump(kind, pred, timeout=wait, after=after)
            except AssertionError:
                if attempt == tries - 1 or self.app.poll() is not None:
                    raise
                time.sleep(0.5)

    def drag(self, rect, tx, ty):
        """Press in the middle of `rect`, move to (tx, ty) in steps, drop."""
        sx, sy = self.centre(rect)
        self.xdo("mousemove", sx, sy, "mousedown", 1)
        for step in (0.25, 0.5, 0.75, 1.0):
            self.xdo("mousemove", int(sx + (tx - sx) * step),
                     int(sy + (ty - sy) * step))
            time.sleep(0.1)
        self.xdo("mouseup", 1)

    def menu_click(self, name, label, after):
        d, n = self.wait_dump("menu", lambda d: d["name"] == name
                              and label in d["items"], after=after)
        self.click(d["items"][label])
        return n

    def assert_modelled(self, menu, tol=2):
        """The popup-position model (all a Wayland driver gets) agrees, to
        `tol` px, with where X11 really put every item."""
        self.assertEqual(menu["coords"], "root")
        self.assertEqual(set(menu["modelled"]), set(menu["items"]), menu)
        for label, r in menu["items"].items():
            m = menu["modelled"][label]
            self.assertEqual(m[2:], r[2:], (label, r, m))
            self.assertLessEqual(max(abs(m[0] - r[0]), abs(m[1] - r[1])),
                                 tol, (label, r, m))

    def shot(self, name):
        if SHOTS and shutil.which("import"):
            os.makedirs(SHOTS, exist_ok=True)
            subprocess.run(["import", "-window", "root",
                            os.path.join(SHOTS, name + ".png")], env=self.env,
                           timeout=30)

    def layout(self):
        return self.wait_dump("layout")[0]

    def calls(self):
        try:
            with open(self.log) as fh:
                return [json.loads(ln) for ln in fh if ln.strip()]
        except OSError:
            return []

    def queries(self):
        """One entry per query the fake answered: which backend it was
        asked as."""
        try:
            with open(self.qlog) as fh:
                return [json.loads(ln) for ln in fh if ln.strip()]
        except OSError:
            return []

    def backend_dump(self, pred=lambda d: True, after=0):
        return self.wait_dump("backend", pred, after=after)

    def open_backend_menu(self, after):
        """Layout ▸ Backend, and the submenu's dump."""
        lay = self.layout()
        self.click(lay["menubar"]["Layout"])
        menu, n = self.wait_dump("menu", lambda d: d["name"] == "layout"
                                 and "Backend" in d["items"], after=after)
        self.click(menu["items"]["Backend"])
        return self.wait_dump("menu", lambda d: d["name"] == "backend"
                              and "Automatic" in d["items"], after=n)


class GuiDrive(GuiSession):
    """The drive above, against the fake's default three-output screen."""

    # -- the drive ----------------------------------------------------------

    def test_menu_drag_apply_save(self):
        lay = self.layout()
        self.assertEqual(set(lay["boxes"]), {"DP-1", "HDMI-1", "DP-2"})
        self.assertEqual(lay["coords"], "root")
        self.assertTrue(lay["settled"], lay)
        self.assertEqual(len(lay["window"]), 2)
        self.assertEqual(sorted(lay["menubar"]),
                         ["Help", "Layout", "Outputs", "View"])
        f = lay["factor"]
        self.assertEqual(f, 8)
        # 1:8 boxes: 1920x1080 -> 240x135, side by side without gaps
        dp1, hdmi, dp2 = (lay["boxes"][k] for k in ("DP-1", "HDMI-1", "DP-2"))
        self.assertEqual((dp1[2], dp1[3]), (240, 135))
        self.assertEqual((hdmi[2], hdmi[3]), (160, 128))
        self.assertEqual(hdmi[0], dp1[0] + 240)
        self.assertEqual(dp2[0], dp1[0] + 400)
        command_line = "xrandr " + " ".join(lay["command"])
        self.assertEqual(lay["status"], command_line)
        self.shot("warandr-1-start")

        # hovering a box describes it in the status bar (no tooltip that
        # could cover a menu); leaving brings the command back
        n = len(self.dumps())
        self.xdo("mousemove", *self.centre(hdmi))
        hover = "HDMI-1: 1280x1024 @ 60.02 Hz, normal, at 1920,0"
        _, n = self.wait_dump("status", lambda d: d["text"] == hover,
                              after=n)
        self.xdo("mousemove", dp2[0] + dp2[2] + 40, dp2[1] + dp2[3] + 60)
        _, n = self.wait_dump("status", lambda d: d["text"] == command_line,
                              after=n)

        # right-click HDMI-1: arandr's order (Refresh rate and the rest after
        # a separator), no Scale item on the X11 backend
        self.click(hdmi, button=3)
        menu, n = self.wait_dump("menu", lambda d: d["name"] == "output:HDMI-1"
                                 and "Orientation" in d["items"], after=n)
        order = sorted(menu["items"], key=lambda k: menu["items"][k][1])
        self.assertEqual(order, ARANDR_MENU)
        self.assert_modelled(menu)
        self.click(menu["items"]["Orientation"])
        self.shot("warandr-2-menu")
        sub, n = self.wait_dump("menu", lambda d: d["name"] == "Orientation"
                                and "left" in d["items"], after=n)
        self.assert_modelled(sub)
        self.click(sub["items"]["left"])
        lay, n = self.wait_dump(
            "layout", lambda d: d["boxes"]["HDMI-1"][2:] == [128, 160],
            after=n)
        self.assertTrue(lay["settled"], lay)
        i = lay["command"].index("HDMI-1")
        self.assertEqual(lay["command"][i:i + 7],
                         ["HDMI-1", "--mode", "1280x1024", "--pos", "1920x0",
                          "--rotate", "left"])
        self.shot("warandr-3-rotated")

        # drag DP-2 leftwards so it lands right of the portrait HDMI-1
        dp2 = lay["boxes"]["DP-2"]
        # (its right edge is now at layout x 2944 = screen 12 + 368);
        # dropping 3px short and 2px low snaps to (2944, 0)
        hdmi = lay["boxes"]["HDMI-1"]
        sx, sy = self.centre(dp2)
        dx = (hdmi[0] + hdmi[2]) - dp2[0] + 3
        self.drag(dp2, sx + dx, sy + 2)
        lay, n = self.wait_dump(
            "layout", lambda d: d["boxes"]["DP-2"][0] == hdmi[0] + hdmi[2]
            and d["boxes"]["DP-2"][1] == hdmi[1], after=n)
        self.assertEqual(lay["command"], EXPECTED)
        self.shot("warandr-4-dragged")

        # the menubar's Outputs drop-down and DP-1's submenu of it: the
        # model covers those placements too; Escape twice closes them
        self.click(lay["menubar"]["Outputs"])
        outputs, n = self.wait_dump("menu", lambda d: d["name"] == "outputs"
                                    and "DP-1" in d["items"], after=n)
        self.assertEqual(sorted(outputs["items"]),
                         ["DP-1", "DP-2", "HDMI-1", "HDMI-2"])
        self.assert_modelled(outputs)
        self.click(outputs["items"]["DP-1"])
        sub, n = self.wait_dump("menu", lambda d: d["name"] == "output:DP-1"
                                and "Primary" in d["items"], after=n)
        self.assert_modelled(sub)
        self.shot("warandr-4b-outputs-menu")
        self.xdo("key", "Escape", "key", "Escape")
        time.sleep(0.3)

        # Apply: the fake records exactly one command, arandr-shaped
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual(applied["rc"], 0, applied)
        self.assertEqual(applied["stderr"], "")
        self.assertEqual(self.calls(), [EXPECTED])
        # and the reload after Apply shows the same layout (the fake kept it)
        lay, n = self.wait_dump("layout", lambda d: not d["busy"], after=n)
        self.assertEqual(lay["command"], EXPECTED)
        self.shot("warandr-5-applied")

        # Save As (dialog bypassed by WARANDR_TEST_SAVE_AS): arandr's file,
        # shebang and the one line
        self.click(lay["buttons"]["save_as"])
        saved, n = self.wait_dump("saved", after=n)
        path = saved["path"]
        self.assertEqual(path, self.saved + ".sh")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
        with open(path) as fh:
            text = fh.read()
        self.assertEqual(text, "#!/bin/sh\nxrandr %s\n" % " ".join(EXPECTED))

    def test_an_overlapping_drop_is_taken_and_explained(self):
        """arandr allows overlaps and X11 has always drawn them, so a drop
        that lands DP-2 on top of DP-1 is taken — and because what the
        overlap *means* differs per desktop, the status bar says it at the
        moment of the drop and the saved script keeps it in its header.

        Measured on Xorg: `xrandr --pos 960x0` is accepted silently and the
        shared region comes back byte-identical on both heads."""
        lay = self.layout()
        n = len(self.dumps())
        dp1, dp2 = lay["boxes"]["DP-1"], lay["boxes"]["DP-2"]
        note = ("DP-2 overlaps DP-1. X11 draws both outputs from one "
                "framebuffer, so the shared region shows the same pixels "
                "on both.")
        # DP-2's centre onto DP-1's centre: the snap puts it on DP-1's
        # centre lines, 320,180 in layout pixels — a half overlap
        self.drag(dp2, *self.centre(dp1))
        lay, n = self.wait_dump(
            "layout", lambda d: d["status"] == note, after=n)
        self.assertTrue(lay["settled"], lay)
        i = lay["command"].index("DP-2")
        self.assertEqual(lay["command"][i:i + 5],
                         ["DP-2", "--mode", "1280x720", "--pos", "320x180"])
        self.assertEqual(lay["boxes"]["DP-2"][:2],
                         [dp1[0] + 320 // 8, dp1[1] + 180 // 8])
        self.shot("warandr-6-overlap")

        # the backend the window displays is also the one that says what an
        # overlap does here: the same sentence, from the same place
        b, n = self.backend_dump(after=0)
        self.assertEqual(b["overlap"], note.split(". ", 1)[1])

        # Apply sends it, unrefused, and the fake keeps it
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual((applied["rc"], applied["stderr"]), (0, ""))
        self.assertIn("320x180", self.calls()[-1])
        lay, n = self.wait_dump("layout", lambda d: not d["busy"], after=n)
        self.assertEqual(lay["command"][lay["command"].index("DP-2") + 4],
                         "320x180")

        # ...and the saved script carries the header: what overlaps, and
        # what that means on the backend that wrote it
        self.click(lay["buttons"]["save_as"])
        saved, n = self.wait_dump("saved", after=n)
        with open(saved["path"]) as fh:
            lines = fh.read().rstrip("\n").split("\n")
        self.assertEqual(lines[0], "#!/bin/sh")
        self.assertEqual(lines[1], "# warandr: partial overlap (DP-1 and "
                                   "DP-2 share 1280x720 at +320+180)")
        self.assertEqual(lines[2], "# " + note.split(". ", 1)[1])
        self.assertTrue(lines[3].startswith("xrandr --output "))
        self.assertIn("--pos 320x180", lines[3])
        self.assertEqual(len(lines), 4)

    def test_clicking_the_indicator_opens_the_backend_menu(self):
        """The indicator is not just a readout: an indicator that shows a
        setting should open it, so a click pops the same Layout ▸ Backend
        menu — same radio state, same insensitive entries."""
        d, n = self.backend_dump(lambda d: d["available"])
        self.assertEqual(d["name"], "x11")
        lay = self.layout()
        rect = lay["backend_indicator"]
        self.assertIsNotNone(rect, "the indicator reported no geometry")
        self.assertGreater(rect[2], 0, rect)     # it has a width to click

        self.click(rect)
        menu, n2 = self.wait_dump("menu", lambda d: d["name"] == "backend"
                                  and "Automatic" in d["items"], after=n)
        # the very same menu the menubar opens: same entries, same states
        self.assertEqual(sorted(menu["items"], key=lambda k: menu["items"][k][1]),
                         BACKEND_MENU)
        self.assertEqual(menu["active"],
                         {lbl: lbl == "Automatic" for lbl in BACKEND_MENU})
        self.assertEqual(
            {lbl: menu["sensitive"][lbl] for lbl in BACKEND_MENU},
            {"Automatic": True, "X11 (xrandr)": True, "sway": False, "Hyprland (hypr)": False,
             "wlroots (wlr)": False, "GNOME (mutter)": True, "Cinnamon (muffin)": False,
             "KDE (kwin)": False})
        # a Wayland driver only gets the model, so the indicator's popup has
        # to report one: it is popped at the pointer, like the canvas menus
        self.assert_modelled(menu)
        # ...and this is the menubar's own Backend menu, so its next drop
        # from Layout ▸ Backend is modelled from the item again
        self.xdo("key", "Escape")
        menu2, _n3 = self.open_backend_menu(len(self.dumps()))
        self.assert_modelled(menu2)
        self.xdo("key", "Escape", "key", "Escape")
        n = len(self.dumps())
        self.click(rect)
        menu, n2 = self.wait_dump("menu", lambda d: d["name"] == "backend"
                                  and "Automatic" in d["items"], after=n)

        # and it drives: pick GNOME from the menu the indicator opened
        d2, _ = self.click_for(menu["items"]["GNOME (mutter)"], "backend",
                               lambda d: d["forced"] == "mutter", after=n2)
        self.assertEqual(d2["name"], "mutter")
        self.assertEqual(d2["indicator"], "backend: mutter (Wayland)")

    def test_backend_indicator_menu_and_switch(self):
        # the indicator is up from the first frame and names the live
        # backend; the availability table arrives from `wxrandr --backends`
        d, n = self.backend_dump(lambda d: d["available"])
        self.assertEqual(d["indicator"], "backend: xrandr (X11)")
        self.assertEqual(d["name"], "x11")
        self.assertIsNone(d["forced"])
        self.assertEqual(d["available"], FAKE_AVAILABLE)
        lay = self.layout()
        self.assertEqual(lay["backend"], "x11")
        self.assertEqual(lay["backend_label"], "xrandr (X11)")
        self.assertTrue(lay["status"].startswith("xrandr --output "), lay)

        # Layout ▸ Backend: arandr's grammar, radios, and the backends this
        # session cannot reach are insensitive with the reason in a tooltip
        menu, n = self.open_backend_menu(n)
        order = sorted(menu["items"], key=lambda k: menu["items"][k][1])
        self.assertEqual(order, BACKEND_MENU)
        self.assert_modelled(menu)
        self.assertEqual(menu["active"],
                         {lbl: lbl == "Automatic" for lbl in BACKEND_MENU})
        self.assertEqual(
            {lbl: menu["sensitive"][lbl] for lbl in BACKEND_MENU},
            {"Automatic": True, "X11 (xrandr)": True, "sway": False, "Hyprland (hypr)": False,
             "wlroots (wlr)": False, "GNOME (mutter)": True, "Cinnamon (muffin)": False,
             "KDE (kwin)": False})
        self.assertEqual(menu["tooltips"]["sway"],
                         "not available in this session: no sway or i3 IPC "
                         "socket ($SWAYSOCK)")
        self.assertEqual(menu["tooltips"]["GNOME (mutter)"], "")
        self.shot("warandr-7-backend-menu")

        # choosing one re-reads the layout *through* it and redraws
        before, mark = len(self.queries()), len(self.dumps())
        d, n = self.click_for(menu["items"]["GNOME (mutter)"], "backend",
                              lambda d: d["forced"] == "mutter", after=mark)
        self.assertEqual(d["indicator"], "backend: mutter (Wayland)")
        self.assertEqual(d["word"], "wxrandr --backend mutter")
        self.assertEqual([q["backend"] for q in self.queries()[before:]],
                         ["mutter"])
        lay, _i = self.wait_dump("layout", lambda d: d["backend"] == "mutter",
                                 after=mark)
        n = len(self.dumps())
        self.assertEqual(set(lay["boxes"]), {"DP-1", "HDMI-1", "DP-2"})
        self.assertTrue(lay["settled"], lay)
        # (the pointer is left where the menu item was; park it on the
        # menubar so the status line is the command, not a hover)
        self.xdo("mousemove", 600, 5)
        _d, _i = self.wait_dump(
            "status", lambda d: d["text"] == "wxrandr --backend mutter "
            + " ".join(lay["command"]), after=mark)
        self.shot("warandr-8-backend-switched")

        # ...and everything after it is that backend's: the per-output menu
        # grows the Wayland-only Scale, Apply runs it, the saved script says
        # wxrandr and records the forced choice as a comment
        self.click(lay["boxes"]["DP-1"], button=3)
        out, n = self.wait_dump("menu", lambda d: d["name"] == "output:DP-1"
                                and "Scale" in d["items"], after=n)
        self.xdo("key", "Escape")
        time.sleep(0.3)
        lay = self.layout()
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual(applied["rc"], 0, applied)
        self.assertEqual(self.calls()[-1][:2], ["--backend", "mutter"])
        self.assertEqual(self.calls()[-1][2:], lay["command"])
        lay, n = self.wait_dump("layout", lambda d: not d["busy"], after=n)
        self.click(lay["buttons"]["save_as"])
        saved, n = self.wait_dump("saved", after=n)
        with open(saved["path"]) as fh:
            text = fh.read()
        self.assertEqual(text.split("\n")[:2],
                         ["#!/bin/sh",
                          "# warandr: backend mutter forced "
                          "(wxrandr --backend mutter)"])
        self.assertTrue(text.split("\n")[2].startswith("wxrandr --output "),
                        text)
        self.assertNotIn("--backend", text.split("\n")[2])

        # back to Automatic: the X11 backend and arandr's own script again
        menu, n = self.open_backend_menu(n)
        self.assertEqual(menu["active"]["GNOME (mutter)"], True)
        mark = len(self.dumps())
        d, n = self.click_for(menu["items"]["Automatic"], "backend",
                              lambda d: d["forced"] is None, after=mark)
        self.assertEqual(d["indicator"], "backend: xrandr (X11)")
        lay, n = self.wait_dump("layout", lambda d: d["backend"] == "x11",
                                after=mark)
        self.xdo("mousemove", 600, 5)
        self.wait_dump("status", lambda d: d["text"] == "xrandr "
                       + " ".join(lay["command"]), after=mark)

    def test_unreachable_backend_shows_the_dialog_and_reverts(self):
        self.env["FAKE_XRANDR_BACKEND_FAIL"] = "mutter"
        self.app.terminate()
        self.app.wait(5)
        lay, n = self.launch()
        # the identify answer and the first layout land in whichever order
        # the two startup reads finish, so look from the relaunch, not from
        # the layout dump
        d, n = self.backend_dump(lambda d: d["available"], after=self.mark)
        self.assertEqual(d["indicator"], "backend: xrandr (X11)")
        n = max(n, len(self.dumps()))
        menu, n = self.open_backend_menu(n)
        d, n = self.click_for(menu["items"]["GNOME (mutter)"], "backend", lambda d: not d["ok"], after=n)
        self.assertEqual(d["wanted"], "mutter")
        self.assertIn("the fake says so", d["error"])
        self.assertIsNone(d["forced"])
        time.sleep(0.5)
        self.shot("warandr-9-backend-refused")
        self.xdo("key", "Return")               # the modal dialog
        time.sleep(0.5)
        # the window is neither empty nor switched: the layout, the
        # indicator and the radio are the previous, working choice
        lay = self.layout()
        self.assertEqual(set(lay["boxes"]), {"DP-1", "HDMI-1", "DP-2"})
        self.assertEqual(lay["backend"], "x11")
        self.assertTrue(lay["status"].startswith("xrandr --output "), lay)
        self.assertIsNone(self.app.poll())
        menu, n = self.open_backend_menu(len(self.dumps()))
        self.assertEqual(menu["active"]["Automatic"], True)
        self.assertEqual(menu["active"]["GNOME (mutter)"], False)
        self.xdo("key", "Escape", "key", "Escape")

    def test_apply_failure_keeps_edits(self):
        self.env["FAKE_XRANDR_FAIL"] = "xrandr: Configure crtc 1 failed"
        # the editor inherits env at launch: relaunch with the failure set
        self.app.terminate()
        self.app.wait(5)
        lay, n = self.launch()
        # an edit first: rotate HDMI-1
        self.click(lay["boxes"]["HDMI-1"], button=3)
        n = self.menu_click("output:HDMI-1", "Orientation", n)
        n = self.menu_click("Orientation", "left", n)
        lay, n = self.wait_dump(
            "layout", lambda d: d["boxes"]["HDMI-1"][2:] == [128, 160],
            after=n)
        edited = lay["command"]
        self.assertIn("left", edited)
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual(applied["rc"], 1)
        self.assertIn("Configure crtc 1 failed", applied["stderr"])
        time.sleep(0.5)
        self.shot("warandr-6-error")
        # the modal error dialog has the focus: Return closes it; the editor
        # is still alive and still shows the *edited* layout (arandr raises
        # before re-reading the screen; a reload would throw the edit away)
        self.xdo("key", "Return")
        lay, n = self.wait_dump("layout", lambda d: not d["busy"], after=n)
        self.assertEqual(lay["command"], edited)
        self.assertEqual(lay["boxes"]["HDMI-1"][2:], [128, 160])
        self.assertEqual(len(self.calls()), 1)
        self.assertIsNone(self.app.poll())
        # and Apply is usable again (still failing, same dialog)
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual(applied["rc"], 1)
        self.xdo("key", "Return")
        self.wait_dump("layout", lambda d: not d["busy"], after=n)
        self.assertEqual(len(self.calls()), 2)
        self.assertIsNone(self.app.poll())


class GuiOverlapCase(GuiSession):
    """A window on a fake GNOME that has the overlap extension, and the two
    moves every test of it makes."""

    # WARANDR_BACKEND=wayland is what makes the fake a Wayland tool rather than
    # an X11 one; the fake then answers --print-backend with FAKE_XRANDR_AUTO_BACKEND
    EXTRA_ENV = {"WARANDR_BACKEND": "wayland",
                 "FAKE_XRANDR_AUTO_BACKEND": "mutter",
                 "FAKE_XRANDR_OVERLAP": "available"}

    #: the record `wxrandr --gnome-overlap-allow` writes, as the fake writes it
    AGREEMENT = {"format": 1, "agreed": "2026-01-01T00:00:00Z",
                 "how": "wxrandr --gnome-overlap-allow",
                 "shell": "50.1", "libmutter": 18, "struct_size": 80}

    def gnome(self, after=0):
        return self.backend_dump(lambda d: d["name"] == "mutter"
                                 and d["overlap_state"] is not None, after=after)

    def overlap_the_boxes(self, n):
        """DP-2's centre onto DP-1's centre: a half overlap, which Mutter
        refuses and the extension is the only way to get."""
        lay = self.layout()
        self.drag(lay["boxes"]["DP-2"], *self.centre(lay["boxes"]["DP-1"]))
        return self.wait_dump("layout", lambda d: "320x180" in d["command"], after=n)


class GuiOverlapConsent(GuiOverlapCase):
    """The GNOME path, end to end, with the fake pretending to be a GNOME that
    has the overlap extension: drag one output onto another, press Apply, get
    the one dialog, tick the box, and never see it again.

    The two halves this holds down are the two the feature is for: Apply on an
    overlapping layout really does pass `--unsafe-gnome-overlap` (and passes it
    to nothing else), and the second Apply asks nothing."""

    def test_the_dialog_the_box_and_then_never_again(self):
        d, n = self.gnome(after=self.mark)
        self.assertEqual(d["overlap_state"], "available")
        # GNOME stops refusing the drop, and says what it will do instead
        self.assertIn("w11-overlap extension", d["overlap"])
        lay, n = self.overlap_the_boxes(n)
        # ...and the drop's own sentence says what GNOME will be made to do
        self.assertIn("DP-2 overlaps DP-1", lay["status"])
        self.assertIn("w11-overlap extension", lay["status"])
        # with the pointer off the boxes the status bar goes back to the
        # command -- and it is the command Apply will really run, flag included
        self.xdo("mousemove", 690, 240)
        self.wait_dump("status", lambda d: "--unsafe-gnome-overlap" in d["text"]
                       and "--pos 320x180" in d["text"], after=n)

        # first Apply: the dialog, with the risk in it
        self.click(lay["buttons"]["apply"])
        dlg, n = self.wait_dump("overlap_dialog", after=n)
        self.assertEqual(dlg["shared"], ["DP-1 and DP-2 share 1280x720 at +320+180"])
        self.assertEqual(sorted(dlg["spots"]), ["apply anyway", "cancel", "check"])
        self.shot("warandr-10-overlap-consent")
        self.click(dlg["spots"]["check"])           # do not ask again
        self.click(dlg["spots"]["apply anyway"])
        ans, n = self.wait_dump("overlap_answer", after=n)
        self.assertEqual((ans["apply"], ans["remember"]), (True, True))

        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual((applied["rc"], applied["overlap"]), (0, True))
        self.assertEqual(applied["agreed"], [True, ""])
        # the flag really was passed, and only with the overlapping layout
        self.assertIn("--unsafe-gnome-overlap", self.calls()[-1])
        self.assertIn("--pos", self.calls()[-1])

        # the window says what it did, and that it is temporary
        st, n = self.wait_dump("status", lambda d: "gone at the next login"
                               in d["text"], after=n)
        self.assertIn("a layout GNOME refuses", st["text"])
        self.assertIn("will not ask again", st["text"])
        # the indicator's own explanation now says so too
        self.backend_dump(lambda d: d["overlap_state"] == "agreed", after=self.mark)

        # second Apply: no dialog at all, and the flag is still passed
        before, mark = len(self.calls()), len(self.dumps())
        lay = self.layout()
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=mark)
        self.assertEqual((applied["rc"], applied["overlap"]), (0, True))
        self.assertIsNone(applied["agreed"])
        self.assertEqual([d["kind"] for d in self.dumps()[mark:]
                          if d["kind"] in ("overlap_dialog", "overlap_answer")], [])
        self.assertIn("--unsafe-gnome-overlap", self.calls()[before])

    def test_cancel_applies_nothing(self):
        d, n = self.gnome(after=self.mark)
        lay, n = self.overlap_the_boxes(n)
        before = len(self.calls())
        self.click(lay["buttons"]["apply"])
        dlg, n = self.wait_dump("overlap_dialog", after=n)
        self.click(dlg["spots"]["cancel"])
        ans, n = self.wait_dump("overlap_answer", after=n)
        self.assertEqual((ans["apply"], ans["remember"]), (False, False))
        self.wait_dump("status", lambda d: d["text"] == "not applied", after=n)
        self.assertEqual(len(self.calls()), before)
        # the edit is still there, ready to be applied
        self.assertIn("320x180", self.layout()["command"])

    def test_an_ordinary_layout_carries_no_flag_and_asks_nothing(self):
        """The other half: on the same GNOME, with the same extension there, a
        layout GNOME accepts is applied exactly as it always was."""
        d, n = self.gnome(after=self.mark)
        lay = self.layout()
        self.assertNotIn("--unsafe-gnome-overlap", lay["status"])
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual((applied["rc"], applied["overlap"]), (0, False))
        self.assertNotIn("--unsafe-gnome-overlap", self.calls()[-1])


    def test_return_on_the_dialog_is_a_no(self):
        """Cancel is the dialog's default response, so Return on a dialog nobody
        read is a no -- and the box, which is a second and deliberate decision,
        records nothing on its own.  `_ask_overlap` returns `(apply, remember)`
        and only the first of them decides anything."""
        d, n = self.gnome(after=self.mark)
        lay, n = self.overlap_the_boxes(n)
        before = len(self.calls())
        self.click(lay["buttons"]["apply"])
        dlg, n = self.wait_dump("overlap_dialog", after=n)
        self.click(dlg["spots"]["check"])       # ticked -- and still not applied
        self.xdo("key", "Return")
        ans, n = self.wait_dump("overlap_answer", after=n)
        self.assertEqual((ans["apply"], ans["remember"]), (False, True))
        self.wait_dump("status", lambda d: d["text"] == "not applied", after=n)
        self.assertEqual(len(self.calls()), before)
        self.assertFalse(os.path.exists(self.consent_file()))
        # the edit survives: the question was asked about a layout that is still
        # there to be applied
        self.assertIn("320x180", self.layout()["command"])


class GuiOverlapApplyFails(GuiOverlapCase):
    """The apply the user said yes to, with the box ticked, that the extension
    refuses.

    The agreement is written after the apply and only after a successful one
    (warandr/gui.py: `if rc == 0 and remember`), which is the honest order: if
    the write that fails were the one that ended the session, waking up having
    agreed to something that never worked is the wrong state to wake up in."""

    REFUSAL = ("xrandr: --unsafe-gnome-overlap: the overlap extension refused "
               "(struct_size): MetaMonitorsConfig is 88 bytes, not 80")
    EXTRA_ENV = dict(GuiOverlapCase.EXTRA_ENV, FAKE_XRANDR_FAIL=REFUSAL)

    def test_a_refused_apply_records_nothing_and_keeps_the_edit(self):
        """T55: a refused apply leaves nothing behind at all -- neither the
        consent record nor a call that would have written one.

        The refusal here is the real one the audit produces
        (`struct_size`: MetaMonitorsConfig is 88 bytes, not 80 -- 80 is what
        libmutter-14 on GNOME 46.0 measured, and a mismatch is what an upgraded
        libmutter looks like from warandr's side).  So the assertion is on both
        halves: `applied.agreed` is None because `allow_overlap()` was never
        run, and the fake's argv log carries no `--gnome-overlap-allow`, which
        is the command that would have written the record."""
        before = len(self.calls())
        d, n = self.gnome(after=self.mark)
        lay, n = self.overlap_the_boxes(n)
        self.click(lay["buttons"]["apply"])
        dlg, n = self.wait_dump("overlap_dialog", after=n)
        self.click(dlg["spots"]["check"])
        self.click(dlg["spots"]["apply anyway"])
        ans, n = self.wait_dump("overlap_answer", after=n)
        self.assertEqual((ans["apply"], ans["remember"]), (True, True))
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual((applied["rc"], applied["overlap"]), (1, True))
        self.assertIn(self.REFUSAL, applied["stderr"])
        # `agreed` is None for exactly one reason: allow_overlap() was never run
        self.assertIsNone(applied["agreed"])
        self.assertFalse(os.path.exists(self.consent_file()))
        # ...and no run of ours asked for the agreement to be written: the
        # consent file's absence alone would also be satisfied by a fake that
        # simply stopped writing it
        self.assertEqual([c for c in self.calls()[before:]
                          if "--gnome-overlap-allow" in c], [])
        self.shot("warandr-11-overlap-refused")
        # the modal error dialog carries the extension's own line; Return closes
        # it, and the overlapping layout is still on the canvas
        self.xdo("key", "Return")
        lay, n = self.wait_dump("layout", lambda d: not d["busy"], after=n)
        self.assertIn("320x180", lay["command"])
        self.assertIn("--unsafe-gnome-overlap", lay["status"])
        self.assertIsNone(self.app.poll())


class GuiOverlapNeverAsks(GuiOverlapCase):
    """`warandr --unsafe-gnome-overlap`: somebody who has already decided,
    starting the window from a hotkey or a desktop entry where a dialog is the
    whole interaction.

    It waives the asking and nothing else: the flag still only goes on a layout
    that really overlaps, and it deliberately records no agreement -- how a
    program was started must not change what it leaves behind on disk
    (warandr/randr.py, `overlap_never_ask`)."""

    CLI_ARGS = ("--unsafe-gnome-overlap",)

    def test_the_flag_applies_without_asking_and_records_nothing(self):
        """T55: `warandr --unsafe-gnome-overlap` never shows the dialog, passes
        the flag on every overlapping apply, and writes no consent file.

        Twice over, because the state that would go wrong is a remembered one:
        a first apply that quietly recorded an agreement would make the second
        indistinguishable from a session where the user had been asked and had
        said yes.  Measured on GNOME 46.0 (live-measurements.md): the record is
        `~/.config/w11/gnome-overlap.json`, and nothing but the dialog's
        `remember` box or `wxrandr --gnome-overlap-allow` ever writes it."""
        d, n = self.gnome(after=self.mark)
        self.assertEqual(d["overlap_state"], "available")   # nothing agreed to
        lay, n = self.overlap_the_boxes(n)
        mark = len(self.dumps())
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=mark)
        self.assertEqual((applied["rc"], applied["overlap"]), (0, True))
        self.assertIsNone(applied["agreed"])
        self.assertEqual([e["kind"] for e in self.dumps()[mark:]
                          if e["kind"] in ("overlap_dialog", "overlap_answer")], [])
        self.assertIn("--unsafe-gnome-overlap", self.calls()[-1])
        self.assertFalse(os.path.exists(self.consent_file()))
        # and a second one is the same: no dialog, no record, the flag again
        before, mark = len(self.calls()), len(self.dumps())
        self.click(self.layout()["buttons"]["apply"])
        self.wait_dump("applied", after=mark)
        self.assertEqual([e["kind"] for e in self.dumps()[mark:]
                          if e["kind"] in ("overlap_dialog", "overlap_answer")], [])
        self.assertIn("--unsafe-gnome-overlap", self.calls()[before])
        self.assertFalse(os.path.exists(self.consent_file()))


class GuiOverlapWithdrawn(GuiOverlapCase):
    """wxrandr withdraws the agreement under warandr's feet.

    The audit wxrandr runs on the extension's reply
    (`gnome_overlap.consent_drift`) compares the libmutter generation and the
    MetaMonitorsConfig size that were agreed to against the ones the checks have
    just measured; a difference deletes the record and prints one line.  The
    fake does the same on `FAKE_XRANDR_OVERLAP_WITHDRAW_ON_APPLY=1`, with
    wxrandr's own sentence, after an apply that succeeded.

    Live (live-measurements.md, GNOME 46.0): the record holds
    `{"agreed", "format", "how", "libmutter", "libmutter_build", "shell",
    "struct_size"}` and an `apt upgrade` of libmutter is what really moves it,
    so this is the ordinary end of an agreement rather than an exotic one."""

    EXTRA_ENV = dict(GuiOverlapCase.EXTRA_ENV,
                     FAKE_XRANDR_OVERLAP="agreed",
                     FAKE_XRANDR_OVERLAP_WITHDRAW_ON_APPLY="1")
    CONSENT = GuiOverlapCase.AGREEMENT

    def first_apply(self):
        """The agreed apply that withdraws the agreement: no dialog, rc 0, and
        the record gone from disk when it returns."""
        d, n = self.gnome(after=self.mark)
        self.assertEqual(d["overlap_state"], "agreed")
        lay, n = self.overlap_the_boxes(n)
        mark = len(self.dumps())
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=mark)
        self.assertEqual((applied["rc"], applied["overlap"]), (0, True))
        self.assertEqual([e["kind"] for e in self.dumps()[mark:]
                          if e["kind"] == "overlap_dialog"], [])
        return applied, n

    def test_the_agreed_apply_lands_and_the_agreement_goes(self):
        """The withdrawal itself, before the question of what warandr does next.

        An agreed session applies with `--unsafe-gnome-overlap` and no dialog;
        wxrandr's audit then finds the extension's MetaMonitorsConfig is 72
        bytes where 80 was agreed to (72 is what libmutter-14 on live GNOME
        46.0 records, 80 what `OVERLAP_BUILD` in tests/fixtures/fake_xrandr.py
        answers with), deletes the record and says so on stderr with rc 0.  The
        apply is not undone -- the layout is already on the screen -- so what is
        pinned is: it landed, the sentence is there, the file is gone."""
        applied, _n = self.first_apply()
        self.assertIn("the agreement has been withdrawn", applied["stderr"])
        self.assertIn("MetaMonitorsConfig size 72, not 80", applied["stderr"])
        self.assertIn("--unsafe-gnome-overlap", self.calls()[-1])
        self.assertFalse(os.path.exists(self.consent_file()))

    @unittest.expectedFailure
    def test_the_next_apply_asks_again(self):
        """Fix 26 (deferred by the brief), finding F2.1 -- warandr half.

        `Backend.overlap_info` is filled once, on the startup worker thread
        (`identify()` -> `read_overlap_status()`), and `Backend.apply()` never
        re-reads it.  So the moment wxrandr withdraws the agreement warandr goes
        on believing `agreed` for the rest of the session: the second Apply
        passes `--unsafe-gnome-overlap` again with no dialog, on a GNOME that no
        longer has anything recorded -- which is the one thing the dialog exists
        to prevent.

        The fix is to re-read `read_overlap_status()` after every overlapping
        apply (or on the withdrawal line in stderr); when it lands this is what
        it has to produce."""
        _applied, n = self.first_apply()
        mark = len(self.dumps())
        self.click(self.layout()["buttons"]["apply"])
        self.wait_dump("overlap_dialog", after=mark, timeout=10)
        self.backend_dump(lambda d: d["overlap_state"] == "available", after=mark)


class GuiOverlapRefused(GuiSession):
    """The same GNOME with no extension installed: the drop is refused in
    Mutter's name, exactly as it was before any of this existed."""

    EXTRA_ENV = {"WARANDR_BACKEND": "wayland",
                 "FAKE_XRANDR_AUTO_BACKEND": "mutter"}

    def test_without_the_extension_gnome_still_refuses(self):
        d, n = self.backend_dump(lambda d: d["name"] == "mutter", after=self.mark)
        self.assertEqual(d["overlap_state"], "unavailable")
        self.assertIn("refuses monitors that are not edge-adjacent", d["overlap"])
        self.assertNotIn("w11-overlap", d["overlap"])
        lay = self.layout()
        self.drag(lay["boxes"]["DP-2"], *self.centre(lay["boxes"]["DP-1"]))
        st, n = self.wait_dump("status", lambda d: "Mutter refuses" in d["text"],
                               after=n)
        self.assertNotIn("--unsafe-gnome-overlap", st["text"])
        # and the box did not move: the model refused the drop
        self.assertNotIn("320x180", self.layout()["command"])


#: a screen whose *small* output comes first, which is the order that used
#: to be safe and no longer is: see GuiCoveredBox
SMALL_FIRST = {
    "primary": "DP-1",
    "outputs": [
        {"name": "DP-2", "active": True, "x": 0, "y": 0,
         "transform": "normal", "scale": 1.0, "current": 0, "ident": 0x44,
         "mm": [344, 194],
         "modes": [[1280, 720, 60000, True], [800, 600, 60317, False]]},
        {"name": "DP-1", "active": True, "x": 1280, "y": 0,
         "transform": "normal", "scale": 1.0, "current": 0, "ident": 0x42,
         "mm": [598, 336],
         "modes": [[1920, 1080, 60000, True], [1280, 720, 60000, False]]},
    ],
}


class GuiCoveredBox(GuiSession):
    """A box dropped wholly inside another must still be draggable.

    While overlaps were refused no box could ever hide another, so the
    canvas could leave stacking to Gtk.Fixed -- which hands the click to the
    child window created last, i.e. the last output in *server* order.  Now
    that a 1280x720 output may sit inside a 1920x1080 one, that order
    decides whether the user can get it back out: here the small output
    comes first, so without the canvas's own stacking rule the big box
    covers it and the drag moves the big one instead."""

    STATE = SMALL_FIRST
    NBOXES = 2

    def test_a_box_dropped_inside_another_can_be_dragged_back_out(self):
        lay = self.layout()
        n = len(self.dumps())
        small, big = lay["boxes"]["DP-2"], lay["boxes"]["DP-1"]
        self.assertLess(small[2] * small[3], big[2] * big[3])

        # drop DP-2 in the middle of DP-1: it lands wholly inside it
        self.drag(small, *self.centre(big))
        lay, n = self.wait_dump(
            "layout", lambda d: d["status"].startswith("DP-2 overlaps DP-1."),
            after=n)
        i = lay["command"].index("DP-2")
        self.assertEqual(lay["command"][i:i + 5],
                         ["DP-2", "--mode", "1280x720", "--pos", "320x180"])
        inside, outer = lay["boxes"]["DP-2"], lay["boxes"]["DP-1"]
        self.assertTrue(outer[0] <= inside[0]
                        and outer[1] <= inside[1]
                        and inside[0] + inside[2] <= outer[0] + outer[2]
                        and inside[1] + inside[3] <= outer[1] + outer[3],
                        (inside, outer))
        self.shot("warandr-covered")

        # a press in the middle of the covered box reaches *it*, not the
        # box that covers it: DP-2 moves right, DP-1 stays where it is
        before = list(lay["command"])
        cx, cy = self.centre(inside)
        self.drag(inside, cx + 80, cy)
        lay, n = self.wait_dump("layout", lambda d: d["command"] != before
                                and d["settled"], after=n)
        j = lay["command"].index("DP-1")
        self.assertEqual(lay["command"][j:j + 6],
                         ["DP-1", "--primary", "--mode", "1920x1080",
                          "--pos", "0x0"])
        i = lay["command"].index("DP-2")
        self.assertEqual(lay["command"][i + 3], "--pos")
        x, y = (int(v) for v in lay["command"][i + 4].split("x"))
        self.assertGreater(x, 320)
        self.assertEqual(y, 180)


class GuiProbe(XvfbCase):
    """tests/fixtures/gui_probe.py: the editor in-process with a stub
    backend whose apply() sleeps 1.5 s."""

    def test_probe(self):
        tmp = tempfile.mkdtemp(prefix="warandr-probe-")
        try:
            env = _base_env(tmp, self.display)
            p = subprocess.run([sys.executable,
                                os.path.join(FIXTURES, "gui_probe.py")],
                               env=env, capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            res = json.loads(p.stdout.strip().split("\n")[-1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        # Apply returns at once and the main loop keeps ticking while the
        # backend runs; Apply is greyed meanwhile and a second click is a no-op
        self.assertLess(res["apply_returned_s"], 0.5, res)
        self.assertTrue(res["busy_during"], res)
        self.assertTrue(res["status_during"].startswith("running: xrandr "))
        self.assertTrue(res["apply_finished"], res)
        self.assertLess(res["longest_gap_s"], 0.5, res)
        self.assertGreater(res["ticks_during_apply"], 10, res)
        self.assertEqual(res["applied_calls"], 1, res)
        self.assertEqual(res["snapshots_after_ok"], 2, res)
        self.assertTrue(res["template_kept"], res)
        self.assertTrue(res["apply_button_back"], res)
        # a failed Apply: dialog, no reload, the edit stays
        self.assertTrue(res["fail_dialog"].startswith("XRandR failed:"), res)
        self.assertIn("configure crtc failed", res["fail_dialog"])
        self.assertTrue(res["fail_keeps_edits"], res)
        self.assertEqual(res["snapshots_after_fail"], 2, res)
        # a layout dump waits for the frame clock's allocation: none reports
        # the box at its old size
        self.assertTrue(res["unsettled_after_redraw"], res)
        self.assertTrue(res["dumps_after_redraw"], res)
        self.assertEqual(res["dumps_after_redraw"],
                         [[[160, 128], True]] * len(res["dumps_after_redraw"]),
                         res)
        self.assertTrue(res["settled_now"], res)
        self.assertEqual(res["hdmi_alloc"], [160, 128], res)
        # Save As: the hint when wxrandr is missing from PATH, none when it
        # is there
        self.assertEqual(res["save_hint"], "saved %s - note: wxrandr is not "
                         "on PATH, the script needs it" % res["saved_path"])
        self.assertEqual(res["save_nohint"], "saved " + res["saved_path"])
        # Save As appends `.sh` after the chooser's overwrite check, so the
        # chooser never asks about the file that really gets replaced
        self.assertTrue(res["sh_overwrite_asks"], res)
        self.assertEqual(res["sh_overwrite_prompt"],
                         "A file named \u201cdesk.sh\u201d already exists.\n"
                         "Do you want to replace it?")
        self.assertTrue(res["sh_overwrite_quiet_when_new"], res)
        self.assertTrue(res["sh_overwrite_quiet_when_typed"], res)
        self.assertEqual(res["sh_overwrite_kept"],
                         "#!/bin/sh\n# an earlier layout\n")
        # popup menus do not accumulate
        self.assertTrue(res["popup_released"], res)
        self.assertLessEqual(res["popups_alive"], 1, res)
        # zoom: arandr's three levels, radios in step with Ctrl+/-
        self.assertEqual(res["zooms"], [4, 8, 16])
        self.assertEqual((res["zoom_in_factor"], res["zoom_in_radio"]),
                         (4, [4]), res)
        self.assertEqual((res["zoom_out_factor"], res["zoom_out_radio"]),
                         (16, [16]), res)
        # a redraw arriving while the Outputs drop-down is open leaves it
        # alone (destroying a mapped menu strands the X pointer grab) and
        # rebuilds it when it closes
        self.assertTrue(res["outputs_menu_mapped"], res)
        self.assertTrue(res["menu_kept_while_open"], res)
        self.assertTrue(res["menu_still_mapped"], res)
        self.assertTrue(res["menu_rebuilt_on_close"], res)
        self.assertEqual(res["menu_rebuilt_items"], ["DP-1", "HDMI-1"], res)
        # a backend read runs off the main loop too, not only Apply: Ctrl+N
        # returns at once against a backend that takes 1.5 s to answer, the
        # window keeps ticking, and the toolbar says so
        self.assertLess(res["reload_returned_s"], 0.5, res)
        self.assertTrue(res["reload_busy"], res)
        self.assertEqual(res["reload_status"],
                         "reading the screen configuration...")
        self.assertTrue(res["reload_finished"], res)
        self.assertLess(res["reload_longest_gap_s"], 0.5, res)
        self.assertEqual(res["reload_snapshots"], 1, res)
        # and one that fails keeps the layout that is on screen
        self.assertEqual(res["reload_fail_dialog"],
                         "Cannot read the screen configuration:\n"
                         "stub: cannot open display")
        self.assertTrue(res["reload_fail_keeps_layout"], res)
        # a layout script that is not UTF-8: the dialog says what is
        # wrong and the window is usable afterwards (the reader thread
        # used to die on the UnicodeDecodeError before it could clear
        # the busy flag, so Apply, Open and New became silent no-ops)
        self.assertTrue(res["latin1_finished"], res)
        self.assertTrue(res["latin1_dialog"].startswith("Cannot load "),
                        res)
        self.assertIn("Not a text file: ", res["latin1_dialog"])
        self.assertTrue(res["latin1_apply_live"], res)
        self.assertTrue(res["latin1_keeps_layout"], res)
        self.assertTrue(res["latin1_reload_after"], res)
        # the same for the Apply thread, which caught RandrError alone
        self.assertTrue(res["apply_boom_finished"], res)
        self.assertTrue(res["apply_boom_dialog"].startswith(
            "XRandR failed:"), res)
        self.assertIn("Permission denied", res["apply_boom_dialog"])
        self.assertTrue(res["apply_boom_apply_live"], res)
        # a menu that was open when an Apply landed edits the layout that
        # replaced the one it was built from, not the discarded one
        self.assertTrue(res["stale_menu_is_not_live"], res)
        self.assertEqual(res["stale_menu_edits_live"], "left", res)
        self.assertEqual(res["stale_layout_untouched"], "normal", res)
        # per-output menu shape
        self.assertEqual(res["menu_x11"],
                         ["Active", "Primary", "Resolution", "Orientation",
                          "-", "Refresh rate", "Reflection", "Mirror of"])
        self.assertEqual(res["menu_wayland"], res["menu_x11"] + ["Scale"])
        self.assertEqual(res["wayland_word"], "wxrandr")


class BackendCli(unittest.TestCase):
    """warandr's own `--backend NAME` / `--print-backend`, spelled like
    wxrandr's so a hotkey can pin one.  No display needed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="warandr-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = _base_env(self.tmp, "")
        self.env.pop("DISPLAY")
        self.env["FAKE_XRANDR_STATE"] = os.path.join(self.tmp, "state.json")

    def warandr(self, *args):
        return subprocess.run([sys.executable, "-m", "warandr"] + list(args),
                              env=self.env, capture_output=True, text=True,
                              timeout=60)

    def test_print_backend_is_one_token(self):
        p = self.warandr("--print-backend")
        self.assertEqual((p.returncode, p.stdout), (0, "x11\n"))
        p = self.warandr("--backend", "mutter", "--print-backend")
        self.assertEqual((p.returncode, p.stdout), (0, "mutter\n"))
        p = self.warandr("--backend", "gnome", "--print-backend")
        self.assertEqual(p.stdout, "mutter\n")
        p = self.warandr("--backend", "x11", "--print-backend")
        self.assertEqual(p.stdout, "x11\n")

    def test_print_backend_verbose_explains_the_choice(self):
        """`--verbose` is the command-line spelling of what the window's
        indicator explains in its tooltip: the token stays the first line,
        for scripts, and each question is answered once."""
        p = self.warandr("--print-backend", "--verbose")
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.splitlines()
        self.assertEqual(lines[:2], ["x11", "kind: X11"])
        self.assertTrue(lines[2].startswith("runs: "), lines)
        self.assertEqual([ln for ln in lines
                          if ln.startswith("chosen by:")],
                         ["chosen by: WARANDR_XRANDR"])
        p = self.warandr("--backend", "mutter", "--print-backend",
                         "--verbose")
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.splitlines()
        self.assertEqual(lines[:2], ["mutter", "kind: Wayland"])
        self.assertIn("compositor: Mutter (fake)", lines)
        self.assertIn("available: yes", lines)
        self.assertEqual(len([ln for ln in lines
                              if ln.startswith("chosen by:")]), 1)
        # ...and without it, still exactly the one token
        p = self.warandr("--backend", "mutter", "--print-backend")
        self.assertEqual((p.returncode, p.stdout), (0, "mutter\n"))

    def test_a_forced_backend_reaches_the_command_and_the_script(self):
        p = self.warandr("--backend", "mutter", "--command")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("wxrandr --backend mutter "
                                            "--output DP-1 "), p.stdout)
        out = os.path.join(self.tmp, "layout.sh")
        p = self.warandr("--backend", "mutter", "--save", out)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(out) as fh:
            lines = fh.read().split("\n")
        self.assertEqual(lines[:2], ["#!/bin/sh",
                                     "# warandr: backend mutter forced "
                                     "(wxrandr --backend mutter)"])
        self.assertTrue(lines[2].startswith("wxrandr --output DP-1 "))
        # ...and without one, arandr's own two lines
        p = self.warandr("--save", out)
        with open(out) as fh:
            self.assertEqual(len(fh.read().rstrip("\n").split("\n")), 2)

    def test_an_unknown_name_is_one_line(self):
        p = self.warandr("--backend", "banana", "--print-backend")
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertEqual(p.stderr, "warandr: unknown backend 'banana' "
                         "(valid: auto, x11, sway, hypr, wlr, mutter, cinnamon, kwin)\n")
        self.assertNotIn("Traceback", p.stderr)

    def test_help_lists_the_new_options(self):
        p = self.warandr("--help")
        self.assertEqual(p.returncode, 0)
        self.assertIn("--backend NAME", p.stdout)
        self.assertIn("--print-backend", p.stdout)


@unittest.skipUnless(HAVE_GTK, "GTK 3 not importable (python3-gi + "
                     "gir1.2-gtk-3.0)")
@unittest.skipUnless(HAVE_X, "Xvfb/xdotool not on PATH")
class MenuVsApply(GuiSession):
    """An Apply that lands while the menubar's Outputs drop-down is open used
    to rebuild it under itself: `set_submenu()` destroys the menu it replaces,
    and destroying a *mapped* menu destroys the window holding the X pointer
    grab -- which X then keeps, freezing every other client on the session
    until warandr exits.  The backend read is slowed down so the Apply can be
    made to land at exactly that moment."""

    SLOW_QUERY = 4

    def other_client(self):
        """A second GTK client on the display: it answers a click unless
        somebody holds a session-wide grab."""
        o = subprocess.Popen([sys.executable,
                              os.path.join(FIXTURES, "other_client.py")],
                             env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.addCleanup(o.terminate)
        self.assertEqual(o.stdout.readline().strip(), "READY")
        flags = fcntl.fcntl(o.stdout, fcntl.F_GETFL)
        fcntl.fcntl(o.stdout, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return o

    def other_answers(self, other, tries=4):
        """Escape, then up to `tries` clicks on it: an open menu legitimately
        eats the click that dismisses it, so one alone proves nothing."""
        self.xdo("key", "Escape")
        for i in range(tries):
            self.xdo("mousemove", 950, 730, "click", 1)
            time.sleep(0.6)
            try:
                if other.stdout.read():
                    return i + 1
            except (OSError, ValueError, TypeError):
                # a non-blocking text stream with nothing in it: the raw
                # read gives None, which the decoder refuses
                pass
        return 0

    def test_apply_under_the_open_outputs_menu(self):
        other = self.other_client()
        self.assertTrue(self.other_answers(other),
                        "the rig is wedged before warandr did anything")
        lay, n = self.wait_dump("layout", lambda d: d["settled"], timeout=30)
        self.click(lay["buttons"]["apply"])
        time.sleep(0.5)
        self.click(lay["menubar"]["Outputs"])
        # the drop-down is provably up: the editor dumps it when it pops
        self.wait_dump("menu", lambda d: d["name"] == "outputs", after=n,
                       timeout=15)
        time.sleep(self.SLOW_QUERY + 2.0)   # the Apply lands with it open
        self.assertTrue(self.other_answers(other),
                        "the X pointer grab was stranded: the rebuild "
                        "destroyed the menu that held it")
        self.app_log.flush()
        self.assertNotIn("Gtk-CRITICAL", open(self.app_log.name).read())
        # and the drop-down is still usable afterwards: it lists the outputs
        # the Apply's fresh layout has
        after = len(self.dumps())
        self.xdo("key", "Escape")
        self.click(lay["menubar"]["Outputs"])
        d, _ = self.wait_dump("menu", lambda d: d["name"] == "outputs",
                              after=after, timeout=15)
        self.assertEqual(sorted(d["items"]),
                         ["DP-1", "DP-2", "HDMI-1", "HDMI-2"])


@unittest.skipUnless(HAVE_GTK, "GTK 3 not importable")
class NoDisplay(unittest.TestCase):
    def test_no_display_is_one_line(self):
        tmp = tempfile.mkdtemp(prefix="warandr-nodisplay-")
        try:
            env = _base_env(tmp, "")
            env.pop("DISPLAY")
            p = subprocess.run([sys.executable, "-m", "warandr"], env=env,
                               capture_output=True, text=True, timeout=60)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr.strip().split("\n")[-1],
                         "warandr: cannot open display (DISPLAY is not set)")
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
