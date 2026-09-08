#!/usr/bin/env python3
"""One X11 client, N top-level windows: the adversarial case for the
Plasma 6 xid matcher.  Same pid, same WM_CLASS, titles and geometry
exactly as told, so that nothing but the order of the two window lists
can tell the windows apart.  Prints "WIN <label> <xid>" per window, then
idles until killed.

    kde-xid-twins.py --win 'a|Term|800x600+100+100' \
                     --win 'b|Term|800x600+100+100'

WM_WINDOW_ROLE carries the label, which makes it the oracle: KWin
exposes the role to scripts as `ro` and the matcher never looks at it,
so `wwmctl -l` can be checked against the truth without perturbing what
is being measured.  Run it inside a Plasma session (`vmctl user <vm> --
python3 /tmp/kde-xid-twins.py ...`) with `_NET_CLIENT_LIST` and
`wwmctl -l` read alongside it.

Built on the repo's own wdotool.x11_mini connection (stdlib only, no
python-xlib): a checkout beside this file, else the wwmctl zipapp
`deploy-to-vm.sh` installs on a guest (zipimport reads it directly).
"""
import argparse
import os
import struct
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _cand in (_ROOT, "/usr/local/bin/wwmctl", "/usr/bin/wwmctl"):
    if os.path.exists(_cand):
        sys.path.insert(0, _cand)
try:
    from wdotool import x11_mini
except ImportError:                                        # pragma: no cover
    sys.exit("kde-xid-twins: no wwmctl to import: run me from a checkout, "
             "or install the wwmctl zipapp (repro/deploy-to-vm.sh)")

CW_BACK_PIXEL = 0x2
CW_EVENT_MASK = 0x800
EV_STRUCTURE_NOTIFY = 1 << 17
EV_EXPOSURE = 1 << 15


def create(x, xid, geo, pixel):
    gx, gy, gw, gh = geo
    payload = struct.pack("<IIhhHHHHII", xid, x.root(), gx, gy, gw, gh,
                          0, 1, 0, CW_BACK_PIXEL | CW_EVENT_MASK)
    payload += struct.pack("<II", pixel, EV_STRUCTURE_NOTIFY | EV_EXPOSURE)
    x._void(1, 0, payload)          # CreateWindow, depth = CopyFromParent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--class", dest="cls", default="xtwin:XTwin")
    ap.add_argument("--pid", action="store_true", default=True)
    ap.add_argument("--no-pid", dest="pid", action="store_false")
    ap.add_argument("--no-class", dest="wmclass", action="store_false",
                    default=True)
    ap.add_argument("--win", action="append", required=True,
                    help="LABEL|TITLE|WxH+X+Y  (repeatable)")
    ap.add_argument("--role", action="store_true", default=True,
                    help="set WM_WINDOW_ROLE to the label (the oracle)")
    ap.add_argument("--no-role", dest="role", action="store_false")
    a = ap.parse_args()

    inst, _, cls = a.cls.partition(":")
    x = x11_mini.X11Conn(os.environ.get("DISPLAY"))
    made = []
    for i, spec in enumerate(a.win):
        label, title, geom = spec.split("|", 2)
        wh, _, xy = geom.partition("+")
        gw, gh = (int(v) for v in wh.split("x"))
        gx, gy = (int(v) for v in xy.split("+"))
        xid = x._new_rid()
        create(x, xid, (gx, gy, gw, gh), 0x00FF0000 >> (8 * i))
        if a.wmclass:
            x.change_property(xid, "WM_CLASS", "STRING", 8,
                              inst.encode() + b"\0" + cls.encode() + b"\0")
        x.change_property(xid, "WM_NAME", "STRING", 8, title.encode("latin-1"))
        x.change_property(xid, "_NET_WM_NAME", "UTF8_STRING", 8,
                          title.encode("utf-8"))
        if a.role:
            x.change_property(xid, "WM_WINDOW_ROLE", "STRING", 8,
                              label.encode("latin-1"))
        if a.pid:
            x.change_property(xid, "_NET_WM_PID", "CARDINAL", 32,
                              struct.pack("<I", os.getpid()))
        # normal hints: PPosition|PSize|USPosition|USSize so KWin honours
        # the requested rectangle instead of placing the window itself
        hints = struct.pack("<i" + "i" * 17, 0x3 | 0x4 | 0x8,
                            gx, gy, gw, gh, 0, 0, 0, 0, 0, 0, 0, 0,
                            0, 0, 0, 0, 0)
        x.change_property(xid, "WM_NORMAL_HINTS", "WM_SIZE_HINTS", 32, hints)
        made.append((label, xid))
    for _label, xid in made:
        x._void(8, 0, struct.pack("<I", xid))       # MapWindow
    for label, xid in made:
        print("WIN %s 0x%x %d" % (label, xid, xid), flush=True)
    print("PID %d" % os.getpid(), flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
