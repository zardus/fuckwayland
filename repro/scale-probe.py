#!/usr/bin/env python3
"""Which pixel space does wdotool's pointer live in?  Guest-side probe.

Runs in the guest **as root** (it reads /sys/kernel/debug and needs
/dev/uinput), against a repo tree unpacked at $W11 (default /root/w11).

For every target it asks for, four readings are taken, in this order,
because the later ones disturb the earlier ones:

  asked   the coordinate handed to `wdotool mousemove`
  daemon  the input daemon's own model of where it put the pointer
          (DaemonClient.pointer(); read FIRST, since getmouselocation
          seeds it from the compositor and would overwrite it)
  hw      the KMS cursor plane's crtc-pos, per CRTC, from
          /sys/kernel/debug/dri/0/state -- device pixels on the scanout,
          the only reading no part of w11 produces (vm/README.md
          "The mouse cursor is not in a screendump")
  comp    the compositor's own statement: Mutter's global.get_pointer()
          through the bridge, or KWin's workspace.cursorPos
  query   what `wdotool getmouselocation` prints

Prints one JSON object on stdout.  Nothing here changes the layout: the
caller sets the scale and the heads first and passes the targets in.
"""
import json
import os
import re
import subprocess
import sys
import time

W11 = os.environ.get("W11", "/root/w11")
sys.path.insert(0, W11)
os.environ["W11_PASSTHROUGH"] = "never"

def drm_state_path():
    """The atomic-state dump of the card that is actually driving the heads.

    Ubuntu 26.04 numbers the virtio-gpu dri/1 (dri/0 is another node), so the
    path in vm/README.md is not portable: take the first `state` file whose
    directory also holds the Virtual-N connectors.
    """
    import glob
    for d in sorted(glob.glob("/sys/kernel/debug/dri/*/")):
        if os.path.exists(d + "state") and glob.glob(d + "Virtual-*"):
            return d + "state"
    return "/sys/kernel/debug/dri/0/state"


DRM_STATE = drm_state_path()
WDOTOOL = [sys.executable, "-m", "wdotool"]


def sh(*args, timeout=30):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def wdo(*args):
    return sh(*WDOTOOL, *args)


# ---------------------------------------------------------------- the hardware

_PLANE = re.compile(r"^plane\[\d+\]:\s*(\S+)")
_KV = re.compile(r"^\t(\S+)=(.*)$")
_RECT = re.compile(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$")


def drm_planes(path=None):
    path = path or DRM_STATE
    out, cur = [], None
    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        return [{"error": str(e)}]
    for line in text.splitlines():
        m = _PLANE.match(line)
        if m:
            cur = {"plane": m.group(1)}
            out.append(cur)
            continue
        if cur is None:
            continue
        m = _KV.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "crtc":
            cur["crtc"] = v
        elif k == "fb":
            cur["fb"] = v
        elif k == "crtc-pos":
            r = _RECT.match(v)
            cur["crtc_pos"] = [int(x) for x in r.groups()] if r else v
    return out


def cursor_planes(planes):
    """The cursor plane of every CRTC that has one lit: {crtc: [x, y, w, h]}.

    A cursor sprite is small (64x64 at scale 1, 128x128 or 192x192 when the
    compositor scales it for HiDPI) and it is the only small plane a desktop
    lights up, so "attached to a crtc, has a framebuffer, at most 256x256"
    picks it out without depending on the plane's index or name.
    """
    got = {}
    for p in planes:
        pos = p.get("crtc_pos")
        crtc = p.get("crtc")
        fb = p.get("fb", "0")
        if not isinstance(pos, list) or not crtc or crtc == "(null)":
            continue
        if fb in ("0", "", None) or fb.startswith("0 "):
            continue
        w, h, x, y = pos
        if w == 0 or h == 0 or w > 256 or h > 256:
            continue
        got[crtc] = [x, y, w, h]
    return got


# ------------------------------------------------------------- the compositors


def ctx_new():
    from wdotool.ctx import Context

    return Context()


def comp_pointer(ctx):
    try:
        p = ctx.backend().pointer()
    except Exception as e:                      # noqa: BLE001 -- report, do not die
        return {"error": f"{type(e).__name__}: {e}"}
    return None if p is None else [int(p[0]), int(p[1])]


def daemon_pointer(ctx):
    try:
        p = ctx.daemon().pointer()
    except Exception as e:                      # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return [int(p[0]), int(p[1])]


def monitors(ctx):
    """What the compositor says its monitors are, in its own units."""
    try:
        b = ctx.backend()
    except Exception as e:                      # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    fn = getattr(b, "monitors", None)
    if fn is None:
        return None
    try:
        return fn()
    except Exception as e:                      # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def kscreen():
    rc, out, _ = sh("kscreen-doctor", "-j")
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return [
        {
            "name": o.get("name"),
            "enabled": o.get("enabled"),
            "scale": o.get("scale"),
            "pos": o.get("pos"),
            "size": o.get("size"),
            "mode_size": (o.get("currentModeId"), o.get("modes") and None),
        }
        for o in data.get("outputs", [])
    ]


_QOUT = re.compile(r"^(\S+) connected (?:primary )?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")


def monitors_from_query(qtext, raws):
    """A monitor list for the backends that have no ListMonitors (KWin).

    `wxrandr --query` prints each output's geometry in the compositor's own
    layout coordinates; the scale that geometry implies is the raw scanout
    of the head divided by it, which the DRM dump already gives us.  So the
    scale is *derived from the framebuffer*, not taken on trust.
    """
    out = []
    for line in (qtext or "").splitlines():
        m = _QOUT.match(line.strip())
        if not m:
            continue
        name, w, h, x, y = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        raw = raws.get(f"crtc-{len(out)}")
        scale = round(raw[0] / w, 4) if raw and w else 1
        out.append({"index": len(out), "connector": name, "x": x, "y": y,
                    "width": w, "height": h, "scale": scale, "from": "wxrandr --query"})
    return out


def raw_sizes(planes):
    """{crtc: (w, h)} -- the biggest plane on each crtc is the scanout."""
    got = {}
    for p in planes or []:
        pos, crtc = p.get("crtc_pos"), p.get("crtc")
        if not isinstance(pos, list) or not crtc or crtc == "(null)":
            continue
        if pos[0] > got.get(crtc, (0, 0))[0]:
            got[crtc] = (pos[0], pos[1])
    return got


def parse_shell(text):
    d = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


# --------------------------------------------------------------------- the run


def one_target(ctx, x, y, settle):
    rec = {"asked": [x, y]}
    rc, out, err = wdo("mousemove", "--sync", str(x), str(y))
    rec["move_rc"] = rc
    if err:
        rec["move_err"] = err[:400]
    time.sleep(settle)
    rec["daemon"] = daemon_pointer(ctx)          # before anything reseeds it
    planes = drm_planes()
    rec["hw"] = cursor_planes(planes)
    if not rec["hw"]:
        # the plane went dark: keep the whole dump so the report can say why
        # (virtio-gpu cannot place a cursor plane at a negative crtc-pos, so a
        # compositor near the layout origin composites the sprite instead)
        rec["hw_raw"] = planes
    rec["comp"] = comp_pointer(ctx)
    rc, out, err = wdo("getmouselocation", "--shell")
    d = parse_shell(out)
    rec["query"] = [int(d["X"]), int(d["Y"])] if "X" in d and "Y" in d else None
    rec["query_rc"] = rc
    if err:
        rec["query_err"] = err[:400]
    return rec


def resolve_targets(spec, bbox, mons):
    """A target is [x, y], or {"frac": [fx, fy]} of the layout box, or
    {"mon": i, "off": [dx, dy]} from monitor i's own origin -- so one config
    file asks the same questions of every layout without the caller having to
    know what space the answer will come back in."""
    out = []
    bx, by, bw, bh = bbox if isinstance(bbox, list) and len(bbox) == 4 else (0, 0, 1920, 1080)
    for t in spec:
        if isinstance(t, dict) and "frac" in t:
            fx, fy = t["frac"]
            out.append([int(bx + fx * bw), int(by + fy * bh)])
        elif isinstance(t, dict) and "mon" in t:
            i = int(t["mon"])
            dx, dy = t.get("off", [0, 0])
            if isinstance(mons, list) and i < len(mons):
                m = mons[i]
                out.append([int(m.get("x", 0)) + int(dx), int(m.get("y", 0)) + int(dy)])
        else:
            out.append([int(t[0]), int(t[1])])
    return out


def main():
    cfg = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    settle = float(cfg.get("settle", 0.35))
    ctx = ctx_new()

    res = {"label": cfg.get("label", ""), "targets": []}
    rc, out, err = wdo("getdisplaygeometry", "--shell")
    d = parse_shell(out)
    res["getdisplaygeometry"] = {"rc": rc, "W": d.get("WIDTH"), "H": d.get("HEIGHT"),
                                 "raw": out, "err": err[:300]}
    try:
        res["daemon_bbox"] = list(ctx.daemon().geometry_full())
    except Exception as e:                       # noqa: BLE001
        res["daemon_bbox"] = {"error": f"{type(e).__name__}: {e}"}
    res["monitors"] = monitors(ctx)
    ks = kscreen()
    if ks:
        res["kscreen"] = ks
    rc, out, err = sh(sys.executable, "-m", "wxrandr", "--query")
    res["wxrandr_query"] = out if rc == 0 else f"rc={rc} {err[:300]}"
    res["drm_state_path"] = DRM_STATE
    res["planes_first"] = drm_planes()
    if not isinstance(res.get("monitors"), list) or not res["monitors"]:
        res["monitors"] = monitors_from_query(res.get("wxrandr_query"),
                                              raw_sizes(res["planes_first"]))

    targets = resolve_targets(cfg.get("targets") or [[300, 200]],
                              res.get("daemon_bbox"), res.get("monitors"))
    res["resolved_targets"] = targets
    for t in targets:
        res["targets"].append(one_target(ctx, int(t[0]), int(t[1]), settle))

    json.dump(res, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
