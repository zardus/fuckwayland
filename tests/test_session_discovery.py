#!/usr/bin/env python3
"""Session discovery under sudo / `ssh root@box`: which X socket and which
Wayland socket the two finders answer with.

`fwcommon/session.py` and `fwcommon/passthrough.py` answer different questions
(who the compositor is, versus what to hand the original tool) and each has
its own copy of the search. This file pins the two rules they must agree on,
both over temporary trees so the answers do not depend on the box the suite
runs on:

* the *session user's* X socket beats a root-owned one (SDDM leaves its
  greeter's Xorg on the lower number, so "lowest wins" hands out a DISPLAY
  the session's cookie cannot open);
* `$WAYLAND_DISPLAY` names the socket by itself -- it does not need
  `$XDG_RUNTIME_DIR` beside it, which under `sudo` is root's own dir or
  unset, and which `wxrandr -d wayland-1` never sets at all.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# and the tests directory, so `import support` resolves under every
# invocation form -- `python3 -m unittest tests/<file>.py` puts only the
# repository root on sys.path (SuiteGuard in tests/test_passthrough.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fwcommon import passthrough
from support import env
from fwcommon import session
from wxrandr import cli as wxrandr_cli

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"


class Tree(unittest.TestCase):
    """A temporary /run/user and /tmp/.X11-unix, wired into both modules."""

    #: the session user, and a uid that is nobody in this tree
    UID = 1000
    OTHER = 125

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fw_sessdisc_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # the finders read the environment first: start from a session that
        # says nothing, so a developer's own DISPLAY cannot answer for them
        clear = env(DISPLAY=None, XAUTHORITY=None, WAYLAND_DISPLAY=None,
                    XDG_RUNTIME_DIR=None, SUDO_UID=None, PKEXEC_UID=None,
                    DBUS_SESSION_BUS_ADDRESS=None)
        clear.__enter__()
        self.addCleanup(clear.__exit__, None, None, None)
        self.runuser = self.mkdir("run", "user")
        self.x11 = self.mkdir("x11")
        self.owners = {}
        for mod, name in ((session, "RUN_USER_DIR"),
                          (passthrough, "_RUN_USER_DIR")):
            self.patch(mod, name, self.runuser)
        for mod, name in ((session, "X11_SOCKET_DIR"),
                          (passthrough, "_X11_SOCK_DIR")):
            self.patch(mod, name, self.x11)
        # logind and the session leaders are the *other* sources both finders
        # consult; silence them so the socket rule is what is under test
        self.patch(passthrough, "_LOGIND_DIR", self.mkdir("logind"))
        self.patch(session, "_shell_environ", lambda uid: {})
        for mod in (session, passthrough):
            self.patch(mod, "_owner",
                       lambda p, _m=mod: self.owners.get(os.path.realpath(p)))
        passthrough.reset_cache()
        self.addCleanup(passthrough.reset_cache)

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def mkdir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def touch(self, path, uid):
        with open(path, "w"):
            pass
        self.owners[os.path.realpath(path)] = uid
        return path

    def xsock(self, num, uid):
        """An X server socket owned by `uid` (only existence + owner count)."""
        return self.touch(os.path.join(self.x11, "X%d" % num), uid)

    def rtdir(self, uid):
        d = self.mkdir("run", "user", str(uid))
        self.owners[os.path.realpath(d)] = uid
        return d

    def wsock(self, uid, name="wayland-0"):
        return self.touch(os.path.join(self.rtdir(uid), name), uid)


class XSocketOwnership(Tree):
    """SDDM's greeter Xorg is root's and sits on :0; the KDE session's
    Xwayland is the user's and sits on :1."""

    def sddm(self):
        self.xsock(0, 0)                 # the greeter's, root-owned
        self.xsock(1, self.UID)          # the session's Xwayland

    def test_passthrough_prefers_the_session_users_socket(self):
        self.sddm()
        # the bug: ":0", a display the session's cookie cannot open
        self.assertEqual(passthrough.find_x_display({}, self.UID), ":1")

    def test_session_prefers_the_session_users_socket(self):
        self.sddm()
        self.assertEqual(session.find_x_display(self.UID), ":1")

    def test_both_finders_agree(self):
        self.sddm()
        self.assertEqual(passthrough.find_x_display({}, self.UID),
                         session.find_x_display(self.UID))

    def test_sudo_warandr_gets_the_session_display(self):
        """The reported trigger: `sudo warandr` on KDE+SDDM. The child
        environment repair is what the real xrandr is run with."""
        self.sddm()
        self.rtdir(self.UID)
        with mock.patch.object(passthrough.os, "getuid", lambda: 0):
            e = passthrough.repair_x_env({"SUDO_UID": str(self.UID)})
        self.assertEqual(e["DISPLAY"], ":1")

    def test_a_plain_x11_session_still_gets_roots_xorg(self):
        """A real Xorg *is* root's: with no socket of the user's own, the
        root-owned one is still the answer, not None."""
        self.xsock(0, 0)
        self.assertEqual(passthrough.find_x_display({}, self.UID), ":0")
        self.assertEqual(session.find_x_display(self.UID), ":0")

    def test_another_users_socket_is_never_the_answer(self):
        """Neither ours nor root's: a second seat's server, or a planted
        one. `mine or root` must not quietly widen to "anybody's"."""
        self.xsock(3, self.OTHER)
        self.assertIsNone(passthrough.find_x_display({}, self.UID))
        self.assertIsNone(session.find_x_display(self.UID))

    def test_unknown_uid_keeps_the_lowest_socket(self):
        """With no target uid there is nothing to prefer, and the old
        answer stands (test_passthrough pins this one too)."""
        self.sddm()
        self.assertEqual(passthrough.find_x_display({}, None), ":0")


class WaylandDisplayNamesTheSocket(Tree):
    """`sudo wxrandr -d wayland-1`: WAYLAND_DISPLAY is set, XDG_RUNTIME_DIR
    is root's own (or unset), and the session runs two compositors."""

    def two_sockets(self):
        self.wsock(self.UID, "wayland-0")
        self.wsock(self.UID, "wayland-1")
        self.rtdir(0)                    # root's own runtime dir, empty

    def test_named_display_wins_under_sudo(self):
        self.two_sockets()
        with env(XDG_RUNTIME_DIR=os.path.join(self.runuser, "0"),
                 WAYLAND_DISPLAY="wayland-1", SUDO_UID=str(self.UID),
                 PKEXEC_UID=None):
            hit = session.find_wayland_socket()
        # the bug: the scan answered with wayland-0, whatever -d said
        self.assertEqual(os.path.basename(hit[2]), "wayland-1")
        self.assertEqual(hit[1], os.path.join(self.runuser, str(self.UID)))

    def test_named_display_wins_with_no_runtime_dir_at_all(self):
        """`ssh root@box` / cron: XDG_RUNTIME_DIR is not in the environment."""
        self.two_sockets()
        with env(XDG_RUNTIME_DIR=None, WAYLAND_DISPLAY="wayland-1",
                 SUDO_UID=None, PKEXEC_UID=None):
            hit = session.find_wayland_socket()
        self.assertEqual(os.path.basename(hit[2]), "wayland-1")

    def test_absolute_display_needs_no_runtime_dir(self):
        self.two_sockets()
        sock = os.path.join(self.runuser, str(self.UID), "wayland-1")
        with env(XDG_RUNTIME_DIR=None, WAYLAND_DISPLAY=sock,
                 SUDO_UID=None, PKEXEC_UID=None):
            hit = session.find_wayland_socket()
        self.assertEqual(hit[2], sock)

    def test_in_session_runtime_dir_still_wins(self):
        """Unchanged for the normal case: the name is resolved against
        $XDG_RUNTIME_DIR before anything is scanned."""
        self.wsock(self.UID, "wayland-0")
        other = self.rtdir(self.OTHER)
        self.touch(os.path.join(other, "wayland-0"), self.OTHER)
        mine = os.path.join(self.runuser, str(self.UID))
        with env(XDG_RUNTIME_DIR=mine, WAYLAND_DISPLAY="wayland-0",
                 SUDO_UID=None, PKEXEC_UID=None):
            hit = session.find_wayland_socket()
        self.assertEqual(hit[2], os.path.join(mine, "wayland-0"))
        self.assertEqual(hit[1], mine)

    def test_a_stale_name_still_falls_through_to_the_scan(self):
        """A WAYLAND_DISPLAY naming a socket that exists nowhere is not an
        answer and must not become one: the scan still runs."""
        self.two_sockets()
        with env(XDG_RUNTIME_DIR=None, WAYLAND_DISPLAY="wayland-9",
                 SUDO_UID=None, PKEXEC_UID=None):
            hit = session.find_wayland_socket()
        self.assertEqual(os.path.basename(hit[2]), "wayland-0")

    def test_wxrandr_d_option_reaches_the_finder(self):
        """End to end over the option itself: `-d wayland-1` is what sets
        WAYLAND_DISPLAY, and the finder is what the backends then use."""
        self.two_sockets()
        with env(XDG_RUNTIME_DIR=os.path.join(self.runuser, "0"),
                 WAYLAND_DISPLAY=None, SUDO_UID=str(self.UID),
                 PKEXEC_UID=None):
            wxrandr_cli.parse(["-d", "wayland-1", "--query"])
            self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-1")
            hit = session.find_wayland_socket()
        self.assertEqual(os.path.basename(hit[2]), "wayland-1")


if __name__ == "__main__":
    unittest.main()
