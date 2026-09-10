#!/usr/bin/env python3
"""A hardware-cursor oracle for the desktops that draw their own cursor.

  shotcursor.py <vm> <head> <outdir> <target-json> [park-json]

vm/README.md's cursor oracle is the KMS cursor plane, and it only works while
the compositor puts the sprite on that plane.  KWin at scale 2 does not: the
virtio-gpu cursor plane is 64x64 and a 2x sprite does not fit, so KWin
composites the cursor into the scanout instead -- which is exactly the case
QMP `screendump` CAN see.  So: park the pointer at a corner, shoot; move it to
the target, shoot; difference the two, and the blob that is not at the parked
corner is the cursor.  Positions come out in device pixels of that head, the
same space the cursor plane reports, and nothing in w11 produced them.

Prints JSON: one record per target with the blob's bounding box and centroid.
"""
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VMCTL = os.path.join(REPO, "vm", "vmctl")
_CC = re.compile(r"^\s*\d+:\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s+([\d.]+),([\d.]+)\s+(\d+)")


def run(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def move(vm, x, y):
    return run([VMCTL, "ssh", vm, "--", f"wdotool mousemove --sync {x} {y}"])


def shot(vm, head, path):
    run([VMCTL, "shot", vm, str(head), path])
    return path


def blobs(a, b, thresh="12%", minarea=40):
    """Connected components of |a - b|, biggest first."""
    p = run(["convert", a, b, "-compose", "difference", "-composite",
             "-colorspace", "gray", "-threshold", thresh,
             "-define", "connected-components:verbose=true",
             "-define", f"connected-components:area-threshold={minarea}",
             "-connected-components", "8", "null:"])
    out = []
    for line in p.stdout.splitlines():
        m = _CC.match(line)
        if not m:
            continue
        w, h, x, y, cx, cy, area = (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                    int(m.group(4)), float(m.group(5)), float(m.group(6)),
                                    int(m.group(7)))
        if w >= 200 or h >= 200:        # the whole frame, or a clock tick: not a cursor
            continue
        out.append({"box": [x, y, w, h], "centroid": [cx, cy], "area": area})
    out.sort(key=lambda d: -d["area"])
    return out


def main():
    vm, head, outdir = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    targets = json.loads(sys.argv[4])
    park = json.loads(sys.argv[5]) if len(sys.argv) > 5 else [5, 5]
    os.makedirs(outdir, exist_ok=True)

    move(vm, *park)
    ref = shot(vm, head, os.path.join(outdir, "park.png"))
    res = {"park": park, "head": head, "targets": []}
    for i, t in enumerate(targets):
        move(vm, t[0], t[1])
        img = shot(vm, head, os.path.join(outdir, f"t{i}.png"))
        bs = blobs(ref, img)
        # drop the blob sitting where the pointer was parked
        far = [b for b in bs
               if abs(b["centroid"][0] - park[0]) + abs(b["centroid"][1] - park[1]) > 80]
        res["targets"].append({"asked": t, "blobs": bs[:4], "cursor": far[0] if far else None})
    json.dump(res, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
