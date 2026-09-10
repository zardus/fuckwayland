#!/usr/bin/env python3
"""Claim 1: `wdotool mousemove` can put the pointer anywhere on a KDE layout.

Six complaints across three sources reduce to "move the pointer to my other
screen"; we answer them with the absolute tablet, whose axes we map across the
whole output layout.  That mapping was measured on GNOME and on sway and never
on KWin, and one complainant's own workaround parsed `kcminputrc`, which is
where KDE keeps per-device output mapping.  So: ask for a position, then read
where the pointer really is.

The oracle is KWin's own `workspace.cursorPos`, read through a KWin script this
file writes and loads itself -- no wdotool code is in the reading path.
`wdotool getmouselocation` is printed beside it as a cross-check of our
plumbing (it asks KWin the same question through backend_kwin).

Run it inside the Plasma session, as the desktop user, with `wdotool` on PATH
and /dev/uinput reachable (the udev rule, or run as root):

    python3 repro/kde-6-pointer-3head.py            # every output, generated
    python3 repro/kde-6-pointer-3head.py 4800 700   # one target

Needs python3-dbus and PyGObject, both on a stock Kubuntu.
"""
import json
import os
import random
import subprocess
import sys
import tempfile
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BUS_NAME = "org.w11.PtrOracle"
OBJ = "/oracle"

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
_bus = dbus.SessionBus()


class Oracle(dbus.service.Object):
    """The name KWin's script calls back into."""

    def __init__(self):
        self.value = None
        self.loop = None
        # dbus.service.Object.__init__ assigns self._name, so the BusName is
        # held under another attribute -- collected, it releases the name and
        # every callDBus lands nowhere.
        self._hold = dbus.service.BusName(BUS_NAME, _bus)
        dbus.service.Object.__init__(self, _bus, OBJ)

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="")
    def Result(self, s):
        self.value = str(s)
        if self.loop is not None:
            self.loop.quit()


_ORACLE = Oracle()

JS = """
var c = workspace.cursorPos;
var s = workspace.virtualScreenSize || {};
var outs = [];
try {
  /* Plasma 6: workspace.screens is a list of Output objects. */
  var scr = workspace.screens || [];
  for (var i = 0; i < scr.length; i++) {
    var g = scr[i].geometry;
    outs.push({n: String(scr[i].name), x: g.x, y: g.y, w: g.width, h: g.height});
  }
} catch (e) { outs = []; }
if (!outs.length) {
  /* 5.27 has no Output objects: ask for each screen's ScreenArea (7) on the
     current desktop, which is that screen's geometry. */
  try {
    var n = workspace.numScreens;
    var area = (typeof KWin !== "undefined" && KWin.ScreenArea !== undefined)
               ? KWin.ScreenArea : 7;
    for (var j = 0; j < n; j++) {
      var r = workspace.clientArea(area, j, workspace.currentDesktop);
      outs.push({n: "screen" + j, x: r.x, y: r.y, w: r.width, h: r.height});
    }
  } catch (e2) { outs = []; }
}
callDBus("%s", "%s", "%s", "Result",
         JSON.stringify({x: c.x, y: c.y, w: s.width, h: s.height, o: outs}));
""" % (BUS_NAME, OBJ, BUS_NAME)


def kwin(timeout=8.0):
    """workspace.cursorPos and the output list, straight from KWin."""
    name = "ptrprobe-%d-%d" % (os.getpid(), random.randrange(1 << 30))
    fd, path = tempfile.mkstemp(prefix="ptrprobe-", suffix=".js", dir="/tmp")
    os.write(fd, JS.encode())
    os.close(fd)
    os.chmod(path, 0o644)                      # KWin reads it as the session user
    scripting = _bus.get_object("org.kde.KWin", "/Scripting")
    _ORACLE.value = None
    try:
        sid = int(scripting.loadScript(path, name, signature="ss",
                                       dbus_interface="org.kde.kwin.Scripting"))
        if sid < 0:
            raise RuntimeError("loadScript returned %d" % sid)
        # Plasma 6 registers /Scripting/Script<n>, 5.27 /<n>.
        for objpath in ("/Scripting/Script%d" % sid, "/%d" % sid):
            try:
                _bus.get_object("org.kde.KWin", objpath).run(
                    dbus_interface="org.kde.kwin.Script")
                break
            except dbus.DBusException:
                continue
        else:
            raise RuntimeError("no runnable script object for id %d" % sid)
        loop = GLib.MainLoop()
        _ORACLE.loop = loop
        GLib.timeout_add(int(timeout * 1000), lambda: (loop.quit(), False)[1])
        if _ORACLE.value is None:
            loop.run()
        _ORACLE.loop = None
        if _ORACLE.value is None:
            raise RuntimeError("KWin ran the script and sent no reply")
        return json.loads(_ORACLE.value)
    finally:
        try:
            scripting.unloadScript(name, signature="s",
                                   dbus_interface="org.kde.kwin.Scripting")
        except dbus.DBusException:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass


def ours():
    """`wdotool getmouselocation`: x:<n> y:<n> screen:<n> window:<n>."""
    p = subprocess.run(["wdotool", "getmouselocation"],
                       capture_output=True, text=True, timeout=30)
    d = dict(t.split(":", 1) for t in p.stdout.split() if ":" in t)
    try:
        return int(d["x"]), int(d["y"])
    except (KeyError, ValueError):
        return None, None


def targets_for(outs, bbox):
    """Every corner and centre of every output, plus the layout's own corners.
    A target that is inside the bounding box but on no output is left out: no
    pixel is there, and KWin snaps the pointer to the nearest one that is."""
    t = []
    for o in outs:
        x, y, w, h = o["x"], o["y"], o["w"], o["h"]
        t += [(o["n"], "top-left", x, y),
              (o["n"], "top-left+1", x + 1, y + 1),
              (o["n"], "centre", x + w // 2, y + h // 2),
              (o["n"], "top-right", x + w - 1, y),
              (o["n"], "bottom-left", x, y + h - 1),
              (o["n"], "bottom-right", x + w - 1, y + h - 1)]
    return t


def main():
    first = kwin()
    outs = first["o"]
    print("KWin outputs: " + ", ".join(
        "%s %dx%d+%d+%d" % (o["n"], o["w"], o["h"], o["x"], o["y"])
        for o in outs))
    print("layout: %dx%d\n" % (first["w"], first["h"]))

    if len(sys.argv) > 2:
        targets = [("argv", "asked", int(sys.argv[1]), int(sys.argv[2]))]
    else:
        targets = targets_for(outs, (first["w"], first["h"]))

    print("%-10s %-12s %11s %11s %11s %8s"
          % ("output", "target", "asked", "KWin says", "wdotool says", "error"))
    worst = 0
    misses = []
    for name, what, tx, ty in targets:
        p = subprocess.run(["wdotool", "mousemove", str(tx), str(ty)],
                           capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            print("%-10s %-12s %11s  mousemove failed: %s"
                  % (name, what, "%d,%d" % (tx, ty), p.stderr.strip()))
            continue
        time.sleep(0.2)
        c = kwin()
        ox, oy = ours()
        dx, dy = c["x"] - tx, c["y"] - ty
        print("%-10s %-12s %11s %11s %11s %8s"
              % (name, what, "%d,%d" % (tx, ty), "%d,%d" % (c["x"], c["y"]),
                 "%s,%s" % (ox, oy), "%+d,%+d" % (dx, dy)))
        worst = max(worst, abs(dx), abs(dy))
        if dx or dy:
            misses.append((name, what, tx, ty, dx, dy))
        if (ox, oy) != (c["x"], c["y"]):
            print("    ^ getmouselocation disagrees with KWin's own cursorPos")
    print("\nworst error: %d px, %d of %d targets missed"
          % (worst, len(misses), len(targets)))
    for name, what, tx, ty, dx, dy in misses:
        print("  %s %s (%d,%d) off by %+d,%+d" % (name, what, tx, ty, dx, dy))


if __name__ == "__main__":
    main()
