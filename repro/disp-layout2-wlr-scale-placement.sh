#!/bin/sh
# layout-2 reproducer: on the wlr backend, --right-of used to land the
# neighbour at a position computed from the *requested* scale, while wlroots
# runs a quantised (120ths, float32) one -- a gap or overlap of 1-10 px.
# Sweeps 1.00..3.00 by 0.01 on both backends and counts the misses.
# a headless sway session's env (XDG_RUNTIME_DIR, SWAYSOCK,
# WAYLAND_DISPLAY, W11_PASSTHROUGH=never)
. ${SWAYENV:-/tmp/fixsway.env}
cd "${SD:-$HOME/work/sd-fix}" || exit 2
PYTHONPATH=$PWD python3 - "$@" <<'PY'
import json, os, subprocess, sys
def swaymsg(*a):
    return json.loads(subprocess.run(["swaymsg", "-r"] + list(a),
                                     capture_output=True, text=True,
                                     check=True).stdout)
for backend in ("wlr", "sway"):
    bad = []
    for i in range(0, 201):
        s = 1.0 + i * 0.01
        subprocess.run([sys.executable, "-m", "wxrandr", "--backend", backend,
                        "--output", "HEADLESS-1", "--scale", "%.4f" % s,
                        "--pos", "0x0", "--output", "HEADLESS-2",
                        "--scale", "1", "--right-of", "HEADLESS-1"],
                       capture_output=True, text=True)
        o = {x["name"]: x for x in swaymsg("-t", "get_outputs")}
        gap = o["HEADLESS-2"]["rect"]["x"] - (o["HEADLESS-1"]["rect"]["x"]
                                              + o["HEADLESS-1"]["rect"]["width"])
        if gap:
            bad.append((round(s, 2), gap))
    print("backend=%-4s wrong placements: %3d/201  %s"
          % (backend, len(bad), bad[:6]))
PY
