#!/usr/bin/env python3
"""What the compositor advertises on the wire, wl_output and xdg_output side
by side -- the two numbers _wayland_bbox() chooses between."""
import os
import sys

sys.path.insert(0, os.environ.get("W11", "/root/w11"))
from w11common import session                      # noqa: E402
from w11common.wayland_mini import WlConn          # noqa: E402

hit = session.find_wayland_socket()
conn = WlConn(hit[2])
conn.sock.settimeout(3.0)
outs = []
for name, (iface, ver) in sorted(conn.get_registry().items()):
    if iface != "wl_output":
        continue
    o = {"name": name, "ver": ver, "x": 0, "y": 0, "w": 0, "h": 0, "scale": 1,
         "lx": None, "ly": None, "lw": None, "lh": None, "xdg_name": None}
    o["oid"] = conn.bind(name, "wl_output", min(ver, 2))

    def h(op, cur, fds, o=o):
        if op == 0:
            o["x"], o["y"] = cur.i32(), cur.i32()
        elif op == 1:
            flags, w, hh = cur.u32(), cur.i32(), cur.i32()
            if flags & 1:
                o["w"], o["h"] = w, hh
        elif op == 3:
            o["scale"] = cur.i32()

    conn.on(o["oid"], h)
    outs.append(o)
conn.roundtrip()
mgr = conn.find_global("zxdg_output_manager_v1")
print("zxdg_output_manager_v1:", mgr)
if mgr:
    mid = conn.bind(mgr[0], "zxdg_output_manager_v1", min(mgr[1], 3))
    for o in outs:
        xid = conn.alloc()
        conn.send(mid, 1, [("u", xid), ("u", o["oid"])])

        def xh(op, cur, fds, o=o):
            if op == 0:
                o["lx"], o["ly"] = cur.i32(), cur.i32()
            elif op == 1:
                o["lw"], o["lh"] = cur.i32(), cur.i32()
            elif op == 2:
                o["xdg_name"] = cur.string()

        conn.on(xid, xh)
    conn.roundtrip()
    conn.roundtrip()          # a second one: nothing may be pending on the first
for o in outs:
    print(f"  wl_output mode {o['w']}x{o['h']} at {o['x']},{o['y']} scale {o['scale']}"
          f"   ->  wl fallback {o['w']//max(o['scale'],1)}x{o['h']//max(o['scale'],1)}")
    print(f"  xdg_output logical {o['lw']}x{o['lh']} at {o['lx']},{o['ly']}"
          f"  name={o['xdg_name']}")
conn.close()
