"""Integration test: output geometry against a real headless sway.

Skipped when sway is not on PATH (run inside `nix develop`). Starts its own
sway on a private XDG_RUNTIME_DIR so concurrent test runs don't collide.
"""

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import stop_daemons_under

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"


@unittest.skipUnless(shutil.which("sway"), "sway not on PATH (run in nix develop)")
class TestSwayGeometry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rtdir = tempfile.mkdtemp(prefix="wdotool-sway-")
        os.chmod(cls.rtdir, 0o700)
        # Whatever the tests below do and however they fail, a daemon
        # spawned into this rig must not outlive it -- and it must be
        # stopped *before* the directory it listens in is removed, because
        # a daemon whose socket directory is gone is unreachable, which
        # used to mean immortal (tests/test_daemon_lifetime.py). Cleanups
        # run last-in-first-out, so this pair is in the order it reads.
        cls.addClassCleanup(shutil.rmtree, cls.rtdir, True)
        cls.addClassCleanup(stop_daemons_under, cls.rtdir)
        conf = os.path.join(cls.rtdir, "sway.conf")
        with open(conf, "w") as f:
            f.write("output HEADLESS-1 mode 1280x720\n")
        env = dict(
            os.environ,
            XDG_RUNTIME_DIR=cls.rtdir,
            WLR_BACKENDS="headless",
            WLR_LIBINPUT_NO_DEVICES="1",
            # dodge the nixpkgs sway wrapper's dbus-run-session fallback
            DBUS_SESSION_BUS_ADDRESS=f"unix:path={cls.rtdir}/no-bus",
        )
        cls.sway = subprocess.Popen(
            ["sway", "-c", conf],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(n.startswith("wayland-") and not n.endswith(".lock")
                   for n in os.listdir(cls.rtdir)):
                break
            if cls.sway.poll() is not None:
                raise unittest.SkipTest("sway exited at startup")
            time.sleep(0.1)
        else:
            raise unittest.SkipTest("sway did not create a wayland socket")
        time.sleep(0.3)  # let the headless output appear

        cls.env_backup = {
            k: os.environ.get(k)
            for k in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "WDOTOOL_UINPUT_PATH",
                      "WDOTOOL_FAKE_UINPUT")
        }
        os.environ["XDG_RUNTIME_DIR"] = cls.rtdir
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ["WDOTOOL_UINPUT_PATH"] = "/dev/null"
        os.environ["WDOTOOL_FAKE_UINPUT"] = "1"

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls.sway.send_signal(signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            cls.sway.wait(timeout=5)

    @classmethod
    def _swaymsg(cls, *args):
        sock = next(n for n in os.listdir(cls.rtdir) if n.startswith("sway-ipc."))
        out = subprocess.run(
            ["swaymsg", "-s", os.path.join(cls.rtdir, sock), *args],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            raise AssertionError(f"swaymsg {args} failed: {out.stderr}")
        return out.stdout

    def _wait_outputs(self, n):
        import json as _json

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            outs = _json.loads(self._swaymsg("-t", "get_outputs"))
            if len([o for o in outs if o.get("active")]) == n:
                return
            time.sleep(0.1)
        raise AssertionError(f"never reached {n} active outputs")

    def test_wayland_bbox(self):
        from wdotool.daemon import _wayland_bbox

        self.assertEqual(_wayland_bbox(), (0, 0, 1280, 720))

    def test_wayland_bbox_dual_output_negative_origin(self):
        from wdotool.daemon import _wayland_bbox

        self._swaymsg("create_output")
        self.addCleanup(lambda: (self._swaymsg("output", "HEADLESS-2", "unplug"),
                                 self._wait_outputs(1)))
        self._wait_outputs(2)
        self._swaymsg("--", "output", "HEADLESS-2", "mode", "--custom",
                      "1920x1080", "position", "-1920", "0")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if _wayland_bbox() == (-1920, 0, 3200, 1080):
                break
            time.sleep(0.1)
        self.assertEqual(_wayland_bbox(), (-1920, 0, 3200, 1080))

    def test_daemon_geometry_and_clamping(self):
        from wdotool.daemon import DaemonClient

        client = DaemonClient.connect_or_spawn()
        self.addCleanup(client.close)
        self.assertEqual(client.geometry(), (1280, 720))
        client.mousemove_abs(99999, 99999)
        self.assertEqual(client.pointer(), (1279, 719, True))
        client.mousemove_abs(0, 0)
        self.assertEqual(client.pointer(), (0, 0, True))


if __name__ == "__main__":
    unittest.main()
