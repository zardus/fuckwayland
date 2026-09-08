#!/usr/bin/env python3
"""The desktop's OWN display tool, as one line per enabled output: `<name> <x>,<y>`.

This runs INSIDE the guest (live-smoke.sh copies it to $HOME/fw-oracle.py, and
re-copies it after every reboot -- /tmp is emptied at boot on 24.04) and is
the second opinion every wxrandr apply in the smoke is checked against -- never
wxrandr's own `--query`, which would make the check a tautology.  One file for
every desktop so that a step file can say `oracle_outputs` and mean it:

  gnome            org.gnome.Mutter.DisplayConfig.GetCurrentState, logical monitors
                   (position and the FIRST monitor in the logical one, which is the
                   output name; a mirrored pair lists both under one position)
  cinnamon-wayland the same call under Muffin's names -- org.cinnamon.Muffin.
                   DisplayConfig on /org/cinnamon/Muffin/DisplayConfig, whose
                   GetCurrentState signature is byte-identical to Mutter's
                   [recon2/cinnamon 3.1]
  kde              kscreen-doctor -o, per-output block, `Geometry: x,y WxH`, enabled only
  sway             swaymsg -t get_outputs, active only
  hypr             hyprctl -j monitors, `x`/`y` per monitor, `disabled` skipped
  wayfire          the IPC's own `window-rules/list-outputs` (int32-LE length + JSON
                   over $WAYFIRE_SOCKET), which is independent of every protocol
                   wxrandr writes; wlr-randr second when there is no socket
  cosmic           cosmic-randr list --kdl, `position X Y` inside an enabled output
  labwc, xfce-wayland, budgie, lxqt-wayland, river
                   wlr-randr's human output.  Weak for labwc and its family -- it is
                   the same zwlr_output_manager_v1 wxrandr writes -- but it is the
                   only reader those compositors have; the flavors say so
  xfce, gnome-x11, cinnamon, mate, i3, lxqt
                   xrandr --query header lines

Stdlib only, and no gi import unless it is there: the KDE, sway and wlroots
goldens do not carry python3-gi.  Exit 0 with no output means "no enabled
output", which is a legitimate answer (`--output X --off` on a one-head guest)
and not an error.  The two ways of having no answer at all exit 2 instead, and
name what is missing: an unknown desktop token, and a desktop tool that is not
installed on this guest.  A silent empty answer would make every display check
in the smoke pass by default.
"""
import json
import os
import re
import socket
import subprocess
import sys


def run(*argv):
    """The tool's stdout -- or exit 2, loudly, when the tool is not installed.

    A missing binary is not an empty answer.  `oracle_outputs` (common.sh 73)
    reads an empty stdout as "no enabled output", so display_pair() would report
    one head and every `--output X --pos` check in the display phase would pass
    against nothing.  That is a real risk and not a theoretical one: wlr-randr
    is only *Suggested* by labwc [recon2/labwc], and hyprctl and cosmic-randr
    reach a guest through its flavor's EXTRA_PKGS.  Same status and same shape
    as the unknown-token branch at the foot of the file.  Every other OSError
    propagates with its traceback, which is also visible."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError:
        sys.stderr.write("fw-oracle: %s is not installed on this guest\n" % argv[0])
        raise SystemExit(2)
    return p.stdout


def _mutter(dest, path, iface):
    """Mutter's logical monitors, and Muffin's under its own three names.  gi
    when python3-gi is installed (it is on the GNOME and Cinnamon goldens), else
    the text form `gdbus call` prints, which is a GVariant literal:

        [(0, 0, 1.0, uint32 0, true, [('Virtual-1', 'unknown', ...)], @a{sv} {})]

    The `uint32 ` before the transform is not optional decoration: gdbus prints
    with g_variant_print(.., type_annotate=TRUE), which annotates every value
    whose type the syntax does not give away, and the transform field of
    `a(iiduba(ssss)a{sv})` is a `u`.  Measured here on 2026-09-08 by printing a
    variant of that exact signature through GLib's own printer -- the regex
    below used to require a bare digit run there, and to read a fourth capture
    group out of a pattern that has three -- so this fallback could never have
    answered at all.  Nobody noticed because every GNOME golden carries
    python3-gi and takes the branch above; the Cinnamon flavor and any image
    without the bindings are what make the text path real."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        st = bus.call_sync(dest, path, iface, "GetCurrentState",
                           None, None, 0, -1, None).unpack()
        return ["%s %d,%d" % (mons[0][0], x, y) for x, y, scale, tr, prim, mons, props in st[2]]
    except Exception:
        pass
    out = run("gdbus", "call", "--session", "--dest", dest,
              "--object-path", path, "--method", iface + ".GetCurrentState")
    return ["%s %s,%s" % (m.group(3), m.group(1), m.group(2)) for m in
            re.finditer(r"\((-?\d+), (-?\d+), [\d.]+, (?:uint32 )?\d+, (?:true|false), \[\('([^']+)'",
                        out)]


def gnome():
    return _mutter("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                   "org.gnome.Mutter.DisplayConfig")


def muffin():
    return _mutter("org.cinnamon.Muffin.DisplayConfig", "/org/cinnamon/Muffin/DisplayConfig",
                   "org.cinnamon.Muffin.DisplayConfig")


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
    outs = json.loads(run("swaymsg", "-t", "get_outputs") or "[]")
    return ["%s %d,%d" % (o["name"], o["rect"]["x"], o["rect"]["y"]) for o in outs if o.get("active")]


def hypr():
    """`hyprctl -j monitors`.  The position is `x`/`y` at the top level of each
    monitor object, not inside a rect, and a monitor the compositor has turned
    off is still listed with `"disabled": true` [recon2/hyprland fixture
    hyprctl-monitors.json]."""
    mons = json.loads(run("hyprctl", "-j", "monitors") or "[]")
    return ["%s %d,%d" % (m["name"], m["x"], m["y"]) for m in mons if not m.get("disabled")]


def wlr_parse(text):
    """wlr-randr 0.4.1's human output, as a state machine.

    A head is `NAME "description"` at column 0 and its fields are indented; a
    DISABLED head prints `Enabled: no` and NO `Position:` line at all, so a
    `grep Position` would silently drop the head that owns the next one's
    coordinates [tests/fixtures/vm/wlr-randr-0.4.1-3heads-one-disabled.txt].
    `--json` exists in later releases and is unverified on 0.4.1, which is what
    the Ubuntu 26.04 archive ships [recon2/labwc]."""
    res, name, enabled, pos = [], None, False, None

    def flush():
        if name and enabled and pos:
            res.append("%s %s" % (name, pos))
    for line in text.splitlines():
        if line[:1] not in (" ", "\t", ""):
            flush()
            name, enabled, pos = line.split()[0], False, None
        elif line.strip().startswith("Enabled:"):
            enabled = line.split(":", 1)[1].strip() == "yes"
        elif line.strip().startswith("Position:"):
            pos = line.split(":", 1)[1].strip()
    flush()
    return res


def wlr():
    return wlr_parse(run("wlr-randr"))


def cosmic_parse(text):
    """`cosmic-randr list --kdl`: a KDL document, one `output "NAME" enabled=#true {`
    block per head with a `position X Y` node inside it.  A disabled head carries
    `enabled=#false` and no `position` node [tests/fixtures/vm/cosmic-randr-*.kdl]."""
    res, name, enabled = [], None, False
    for line in text.splitlines():
        m = re.match(r'\s*output\s+"([^"]+)"\s+enabled=#(true|false)', line)
        if m:
            name, enabled = m.group(1), m.group(2) == "true"
            continue
        m = re.match(r"\s*position\s+(-?\d+)\s+(-?\d+)\s*$", line)
        if m and name and enabled:
            res.append("%s %s,%s" % (name, m.group(1), m.group(2)))
            name = None
    return res


def cosmic():
    return cosmic_parse(run("cosmic-randr", "list", "--kdl"))


def wayfire_socket():
    """$WAYFIRE_SOCKET first, then the same `wayfire-*.socket` glob the tools'
    own finder walks (fwcommon.session.find_wayfire_socket).  The measured name
    is /tmp/wfrt1/wayfire-wayland-1-.socket -- `<display>-<pid>` with an empty
    pid field [recon2/wayfire 1.2], which is why nothing here matches on a pid."""
    for var in ("WAYFIRE_SOCKET", "_WAYFIRE_SOCKET"):
        p = os.environ.get(var)
        if p and os.path.exists(p):
            return p
    rt = os.environ.get("XDG_RUNTIME_DIR") or ""
    if rt and os.path.isdir(rt):
        cand = sorted(n for n in os.listdir(rt)
                      if n.startswith("wayfire-") and n.endswith(".socket"))
        if cand:
            return os.path.join(rt, cand[0])
    return None


def wayfire_call(sock, method):
    """Wayfire's IPC frame: a 4-byte little-endian length, then that many bytes
    of JSON, in both directions [recon2/wayfire 1.1].  `sock` is already
    connected, so the test can hand this half of a socketpair."""
    body = json.dumps({"method": method, "data": {}}).encode()
    sock.sendall(len(body).to_bytes(4, "little") + body)
    head = b""
    while len(head) < 4:
        chunk = sock.recv(4 - len(head))
        if not chunk:
            return None
        head += chunk
    want = int.from_bytes(head, "little")
    buf = b""
    while len(buf) < want:
        chunk = sock.recv(want - len(buf))
        if not chunk:
            return None
        buf += chunk
    return json.loads(buf.decode())


def wayfire_lines(outputs):
    """`window-rules/list-outputs` answers a list of outputs, each with a
    `geometry` rect in layout coordinates [recon2/wayfire captures/
    window-rules_list-outputs.json]."""
    res = []
    for o in outputs or []:
        g = o.get("geometry") or {}
        if "name" in o and "x" in g and "y" in g:
            res.append("%s %d,%d" % (o["name"], g["x"], g["y"]))
    return res


def wayfire():
    path = wayfire_socket()
    if path:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(5.0)
            sock.connect(path)
            ans = wayfire_call(sock, "window-rules/list-outputs")
        except (OSError, ValueError):
            ans = None
        finally:
            sock.close()
        lines = wayfire_lines(ans)
        if lines:
            return lines
    # No `ipc` plugin, or a stale socket file: fall back to the neutral reader.
    return wlr()


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


#: One entry per `# vmctl-desktop:` token of vm/vmctl's DESKTOPS, plus the bare
#: alias `x11` the X11 step files pass when they mean "whatever xrandr says".
DESKTOPS = {
    "gnome": gnome, "gnome-x11": x11,
    "kde": kde, "kde-x11": kde,
    "xfce": x11, "xfce-wayland": wlr,
    "sway": sway, "hypr": hypr, "wayfire": wayfire, "cosmic": cosmic,
    "labwc": wlr, "budgie": wlr, "lxqt-wayland": wlr, "river": wlr,
    "lxqt": x11, "cinnamon": x11, "cinnamon-wayland": muffin,
    "mate": x11, "i3": x11,
    "x11": x11,
}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "gnome"
    fn = DESKTOPS.get(which)
    if fn is None:
        sys.stderr.write("fw-oracle: no oracle for %s (want %s)\n"
                         % (which, "|".join(sorted(DESKTOPS))))
        raise SystemExit(2)
    for line in fn():
        print(line)
