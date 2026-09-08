#!/usr/bin/env python3
"""Does a window-relative pointer move use the same pixel space as the window?

`wdotool mousemove --window ID x y` adds the window's own origin to the
coordinate, so the window geometry and the pointer have to be in ONE space.
Under a scale the two could plausibly diverge (Mutter's frame rects vs
Mutter's pointer), and nothing has ever checked it, so: take the window's
geometry, ask for a point inside it both ways, and compare.

Run in the guest as root, with a window already on screen.
"""
import importlib.util
import json
import os
import sys
import time

FW = os.environ.get("FW", "/root/fw")
sys.path.insert(0, FW)
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"
# scale-probe.py has a hyphen in it, so it is loaded by path rather than
# imported by name; it is the file that owns the four readings.
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "scale_probe", os.path.join(_HERE, "scale-probe.py"))
_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_probe)
cursor_planes, drm_planes = _probe.cursor_planes, _probe.drm_planes
monitors, parse_shell, wdo = _probe.monitors, _probe.parse_shell, _probe.wdo


def geom(wid):
    """`wdotool getwindowgeometry --shell ID` -> (x, y, w, h)."""
    rc, out, err = wdo("getwindowgeometry", "--shell", str(wid))
    d = parse_shell(out)
    if "X" not in d:
        return None, f"rc={rc} {out} {err}"
    return (int(d["X"]), int(d["Y"]), int(d["WIDTH"]), int(d["HEIGHT"])), ""


def main():
    from wdotool.ctx import Context

    ctx = Context()
    label = sys.argv[1] if len(sys.argv) > 1 else ""
    rc, out, err = wdo("search", "--onlyvisible", "--name", ".")
    wids = [int(w) for w in out.split() if w.strip().isdigit()]
    res = {"label": label, "monitors": monitors(ctx), "wids": wids[:6], "cases": []}
    if not wids:
        res["error"] = f"no window found: rc={rc} {err[:200]}"
        json.dump(res, sys.stdout, indent=1)
        return
    wid = wids[-1]
    g, gerr = geom(wid)
    res["window"] = {"id": wid, "geometry": g, "err": gerr}
    if not g:
        json.dump(res, sys.stdout, indent=1)
        return
    for off in ([10, 10], [50, 40], [g[2] // 2, g[3] // 2]):
        rc, out, err = wdo("mousemove", "--window", str(wid), str(off[0]), str(off[1]))
        time.sleep(0.35)
        p = ctx.backend().pointer()
        res["cases"].append({
            "offset": off, "rc": rc, "err": err[:200],
            "expected_layout": [g[0] + off[0], g[1] + off[1]],
            "comp": [int(p[0]), int(p[1])] if p else None,
            "hw": cursor_planes(drm_planes()),
        })
    json.dump(res, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
