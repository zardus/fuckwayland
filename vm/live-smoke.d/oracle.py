#!/usr/bin/env python3
"""The desktop's OWN display tool, as one line per enabled output: `<name> <x>,<y>`.

This runs INSIDE the guest (live-smoke.sh copies it to $HOME/fw-oracle.py, and
re-copies it after every reboot -- /tmp is emptied at boot on 24.04) and is
the second opinion every wxrandr apply in the smoke is checked against -- never
wxrandr's own `--query`, which would make the check a tautology.  One file for
all five desktops so that a step file can say `oracle_outputs` and mean it:

  gnome    org.gnome.Mutter.DisplayConfig.GetCurrentState, logical monitors
           (position and the FIRST monitor in the logical one, which is the
           output name; a mirrored pair lists both under one position)
  kde      kscreen-doctor -o, per-output block, `Geometry: x,y WxH`, enabled only
  sway     swaymsg -t get_outputs, active only
  xfce/x11 xrandr --query header lines

Stdlib only, and no gi import unless it is there: the KDE and sway goldens do
not carry python3-gi.  Exit 0 with no output means "no enabled output", which
is a legitimate answer (`--output X --off` on a one-head guest) and not an error.
"""
import re
import subprocess
import sys


def run(*argv):
    p = subprocess.run(argv, capture_output=True, text=True)
    return p.stdout


def gnome():
    """Mutter's logical monitors.  gi when python3-gi is installed (it is on the
    GNOME goldens), else the text form `gdbus call` prints, which is a GVariant
    literal: (0, 0, 1.0, 0, true, [('Virtual-1', ...)], ...)."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        st = bus.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                           "org.gnome.Mutter.DisplayConfig", "GetCurrentState",
                           None, None, 0, -1, None).unpack()
        return ["%s %d,%d" % (mons[0][0], x, y) for x, y, scale, tr, prim, mons, props in st[2]]
    except Exception:
        pass
    out = run("gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig",
              "--object-path", "/org/gnome/Mutter/DisplayConfig",
              "--method", "org.gnome.Mutter.DisplayConfig.GetCurrentState")
    return ["%s %s,%s" % (m.group(4), m.group(1), m.group(2)) for m in
            re.finditer(r"\((-?\d+), (-?\d+), [\d.]+, \d+, (?:true|false), \[\('([^']+)'", out)]


def kde():
    """kscreen-doctor -o.  ANSI-coloured; 5.27 puts the state on the `Output:`
    line and 6.x on the indented lines under it, so the whole block is searched
    (the same split vm/selftest.sh does)."""
    out = re.sub(r"\x1b\[[0-9;]*m", "", run("kscreen-doctor", "-o"))
    res = []
    for chunk in re.split(r"(?m)^Output: ", out)[1:]:
        m = re.match(r"\d+\s+(\S+)", chunk)
        geo = re.search(r"Geometry:\s*(-?\d+),(-?\d+)", chunk)
        if m and geo and re.search(r"\benabled\b", chunk) and not re.search(r"\bdisabled\b", chunk):
            res.append("%s %s,%s" % (m.group(1), geo.group(1), geo.group(2)))
    return res


def sway():
    import json
    outs = json.loads(run("swaymsg", "-t", "get_outputs") or "[]")
    return ["%s %d,%d" % (o["name"], o["rect"]["x"], o["rect"]["y"]) for o in outs if o.get("active")]


def x11():
    """`Virtual-1 connected primary 1920x1080+0+0 (normal ...)`: the geometry
    field is the first WxH+X+Y word, which is where `primary` shifts it."""
    res = []
    for line in run("xrandr", "--query").splitlines():
        f = line.split()
        if len(f) >= 3 and f[1] == "connected":
            for w in f[2:]:
                m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", w)
                if m:
                    res.append("%s %s,%s" % (f[0], m.group(3), m.group(4)))
                    break
    return res


DESKTOPS = {"gnome": gnome, "kde": kde, "kde-x11": kde, "sway": sway, "xfce": x11, "x11": x11}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "gnome"
    fn = DESKTOPS.get(which)
    if fn is None:
        sys.stderr.write("fw-oracle: no oracle for %s\n" % which)
        raise SystemExit(2)
    for line in fn():
        print(line)
