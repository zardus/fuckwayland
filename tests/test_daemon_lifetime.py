"""What ends an input daemon: an unreachable socket, and an idle timer.

The reproduction this file was written for, in the smallest form there is:
spawn a daemon with $XDG_RUNTIME_DIR on a temporary path, delete that
directory, and the socket file every client dials is gone. Nothing can
connect to a name that is not there, and the daemon used to neither notice
nor time out -- 161 of them were found alive at once on the test rig, ~3GB
between them, sockets in per-test runtime directories deleted hours
earlier, the oldest 18 hours old. On a real machine that happens every
time somebody logs out: the runtime directory goes with the session.

Both timers here are turned right down through the documented overrides,
so a test that waits for one waits a second and not a quarter of an hour.
"""

import contextlib
import io
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# ...and the tests directory itself, for the bare `import support`
# below: running this file by path puts it on sys.path for free,
# `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support
from wdotool import daemon

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

IDLE = 0.5          # WDOTOOL_DAEMON_IDLE for these tests
CHECK = 0.1         # ...and WDOTOOL_DAEMON_CHECK: five ticks per period
SETTLE = 4 * IDLE   # long enough that a daemon still alive after it means it


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_gone(pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def reachable(path):
    """What a client does, and nothing more: can this socket be dialled?"""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except OSError:
        return False
    finally:
        sock.close()
    return True


@unittest.skipIf(os.geteuid() == 0, "the root daemon listens on /run, "
                 "not in a runtime directory a test can own")
class DaemonCase(unittest.TestCase):
    """A really spawned daemon (fake uinput) on its own runtime directory.

    The cleanups are registered before anything is spawned and in the
    order they have to run in -- stop the daemons, then remove the
    directory they listen in -- so that a test which fails anywhere below
    still leaves nothing behind."""

    IDLE = str(IDLE)
    CHECK = str(CHECK)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wdotool-life-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(support.stop_daemons_under, self.tmp)
        cm = support.env(
            XDG_RUNTIME_DIR=self.tmp,
            WDOTOOL_UINPUT_PATH="/dev/null",
            WDOTOOL_FAKE_UINPUT="1",
            # never read the runner's keyboards
            WDOTOOL_NO_KEYSTATE="1",
            WDOTOOL_DAEMON_IDLE=self.IDLE,
            WDOTOOL_DAEMON_CHECK=self.CHECK,
            WAYLAND_DISPLAY=None, SWAYSOCK=None, I3SOCK=None,
        )
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        self.path = daemon.socket_path()

    def spawn(self):
        """(client, pid) -- exactly what a wdotool command does."""
        client = daemon.DaemonClient.connect_or_spawn()
        self.addCleanup(client.close)
        return client, client._rpc(op="ping")["pid"]


class TheDaemonCannotBeReached(DaemonCase):
    """Its socket is not there any more. With the idle timer off, this
    check is the only thing that can end these daemons -- and it has
    nothing to do with how long one has been running: an unreachable
    daemon is finished the moment its last client goes."""

    IDLE = "0"

    def test_the_runtime_directory_was_deleted_under_it(self):
        client, pid = self.spawn()
        self.assertTrue(reachable(self.path))
        client.close()

        shutil.rmtree(self.tmp, ignore_errors=True)     # logging out
        self.assertFalse(reachable(self.path))          # unreachable...
        self.assertTrue(wait_gone(pid), "daemon outlived its socket")

    def test_a_client_it_still_has_is_not_cut_off(self):
        """The socket file is gone, so no *new* client can arrive -- but
        the one already talking must be answered to the end."""
        client, pid = self.spawn()
        shutil.rmtree(self.tmp, ignore_errors=True)
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline:
            self.assertEqual(client._rpc(op="ping")["pid"], pid)
            time.sleep(0.05)
        client.close()
        self.assertTrue(wait_gone(pid), "daemon outlived its socket")

    def test_another_daemon_has_the_path_now(self):
        """Same name, a different inode: the socket was replaced under it,
        which is the case the name alone cannot see. The exit must leave
        the replacement alone -- the startup lock's rule (a loser never
        unlinks the winner's socket) applied to the way out."""
        client, pid = self.spawn()
        client.close()
        os.unlink(self.path)
        successor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(successor.close)
        successor.bind(self.path)
        successor.listen(1)
        ino = os.stat(self.path).st_ino

        self.assertTrue(wait_gone(pid), "daemon kept a path it had lost")
        self.assertEqual(os.stat(self.path).st_ino, ino)


class TheDaemonIsIdle(DaemonCase):
    """Nobody has used it. This one is a timer, and everything that counts
    as "in use" has to switch it off."""

    def test_a_daemon_nobody_uses_exits_and_takes_its_socket_with_it(self):
        client, pid = self.spawn()
        client.close()
        self.assertTrue(wait_gone(pid), "daemon never timed out")
        self.assertFalse(os.path.exists(self.path))

    def test_a_connected_client_is_not_idle(self):
        """Held open and silent for four idle periods: a connection is a
        client, whether or not it is saying anything, and the timer must
        not close one."""
        client, pid = self.spawn()
        time.sleep(SETTLE)
        self.assertTrue(alive(pid), "the timer killed a connected client")
        self.assertEqual(client._rpc(op="ping")["pid"], pid)
        client.close()
        self.assertTrue(wait_gone(pid))

    def test_a_daemon_holding_a_key_down_is_not_idle(self):
        """`wdotool keydown ctrl` ... `wdotool keyup ctrl`: two commands,
        so between them there is no client at all. The hold is the whole
        reason that daemon still exists, and its exit would release it."""
        client, pid = self.spawn()
        with contextlib.redirect_stderr(io.StringIO()):
            client.key("ctrl", "down", 0, False)
        client.close()

        time.sleep(SETTLE)
        self.assertTrue(alive(pid), "the timer dropped a held key")
        later, again = self.spawn()
        self.assertEqual(again, pid)                    # the same daemon
        with contextlib.redirect_stderr(io.StringIO()):
            later.key("ctrl", "up", 0, False)
        later.close()
        self.assertTrue(wait_gone(pid), "still holding nothing")


class TheChecksAreOverridable(DaemonCase):
    IDLE = "0"
    CHECK = "0"

    def test_zero_is_never(self):
        """WDOTOOL_DAEMON_CHECK=0 is the bare accept loop this daemon has
        always had: no tick, so neither check ever runs and the daemon
        outlives its own socket exactly as it used to. Somebody has a
        reason -- a socket on a path they manage themselves, a daemon
        under a debugger -- and the old behaviour stays reachable."""
        client, pid = self.spawn()
        client.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        time.sleep(SETTLE)
        self.assertTrue(alive(pid))
        # ...and setUp's cleanup is what stops it, which is the rule for
        # every test in the suite that spawns one.


class _HoldsNothing:
    """The two fields _exit_reason reads off the daemon."""

    def __init__(self, down=(), btns=()):
        self.down, self.btns = set(down), set(btns)


class TheChecksThemselves(unittest.TestCase):
    """The decision, without a process in it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wdotool-life-unit-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "wdotool.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.sock.close)
        self.sock.bind(self.path)
        self.ident = daemon._socket_ident(self.path)
        self.clients = daemon._Clients()

    def reason(self, idle=0.0, d=None):
        return daemon._exit_reason(self.path, self.ident, self.clients,
                                   d or _HoldsNothing(), idle)

    def test_a_bound_socket_and_no_timer_is_no_reason(self):
        self.assertIsNone(self.reason())

    def test_the_socket_is_gone(self):
        os.unlink(self.path)
        self.assertIn("is gone", self.reason())

    def test_the_socket_is_somebody_elses(self):
        os.unlink(self.path)
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(other.close)
        other.bind(self.path)
        self.assertIn("different socket", self.reason())

    def test_a_connected_client_answers_both(self):
        os.unlink(self.path)
        self.clients.opened()
        self.assertIsNone(self.reason(idle=0.001))

    def test_idle(self):
        time.sleep(0.02)
        self.assertIsNone(self.reason(idle=10))
        self.assertIn("no client", self.reason(idle=0.01))

    def test_a_held_key_or_button_is_not_idle(self):
        time.sleep(0.02)
        self.assertIsNone(self.reason(idle=0.01, d=_HoldsNothing(down=[29])))
        self.assertIsNone(self.reason(idle=0.01,
                                      d=_HoldsNothing(btns=[0x110])))

    def test_the_count_comes_back_down(self):
        self.clients.opened()
        self.assertIsNone(self.clients.quiet_for())
        self.clients.closed()
        self.assertIsNotNone(self.clients.quiet_for())


class TheKnobs(unittest.TestCase):
    def seconds(self, value):
        with support.env(WDOTOOL_DAEMON_IDLE=value):
            return daemon._lifetime_seconds(daemon.IDLE_ENV, 900.0)

    def test_a_number_of_seconds(self):
        self.assertEqual(self.seconds("30"), 30.0)
        self.assertEqual(self.seconds(" 2.5 "), 2.5)

    def test_zero_is_never(self):
        self.assertEqual(self.seconds("0"), 0.0)

    def test_unset_and_nonsense_are_the_default(self):
        self.assertEqual(self.seconds(None), 900.0)
        self.assertEqual(self.seconds(""), 900.0)
        self.assertEqual(self.seconds("soon"), 900.0)
        self.assertEqual(self.seconds("-5"), 900.0)

    def test_the_tick_is_never_coarser_than_the_period(self):
        """Ask for a five-second idle timeout and the daemon must not look
        every fifteen. Neither knob is invented from the other: 0 stays 0
        on both sides."""
        self.assertEqual(daemon._tick_seconds(5.0, 15.0), 5.0)
        self.assertEqual(daemon._tick_seconds(900.0, 15.0), 15.0)
        self.assertEqual(daemon._tick_seconds(0.0, 15.0), 15.0)
        self.assertEqual(daemon._tick_seconds(5.0, 0.0), 0.0)

    def test_the_defaults(self):
        """Fifteen minutes, checked four times a minute: long enough that
        no gap between two commands of one piece of work reaches it, short
        enough to bound what a forgotten daemon costs."""
        self.assertEqual(daemon._IDLE_SECONDS, 900.0)
        self.assertEqual(daemon._CHECK_SECONDS, 15.0)

    def test_a_spawned_daemon_is_told(self):
        """Both are WDOTOOL_*, so clean_env() carries them to a daemon the
        client spawns -- which is the only way these tests can set them."""
        with support.env(WDOTOOL_DAEMON_IDLE="7", WDOTOOL_DAEMON_CHECK="1"):
            env = daemon.clean_env()
        self.assertEqual(env.get("WDOTOOL_DAEMON_IDLE"), "7")
        self.assertEqual(env.get("WDOTOOL_DAEMON_CHECK"), "1")


if __name__ == "__main__":
    unittest.main()
