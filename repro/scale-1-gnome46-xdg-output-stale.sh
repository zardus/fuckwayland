#!/bin/sh
# Where does the layout box we use stop matching the desktop the user sees?
#
# Walks GNOME through the states a real user walks through -- turn fractional
# scaling on while the monitor is already at 200%, which is what a HiDPI laptop
# owner does -- and after each step prints, side by side:
#
#   bbox     the daemon's layout box (zxdg_output_v1 logical size), which is
#            what every absolute pointer command is mapped across
#   shell    Mutter's own monitor list, through the bridge
#   dconf    Mutter's D-Bus DisplayConfig, through wxrandr --query
#   land     where `mousemove 400 100` actually put the cursor: the KMS cursor
#            plane, device pixels, hotspot included
#
# On a desktop that agrees with itself, bbox == dconf and land == 400 * scale.
set -eu
V=./vm/vmctl
VM=$1

step() {
    echo "--- $1"
    $V ssh "$VM" -- "cd /root/w11 && python3 probe.py '{\"label\":\"$1\",\"settle\":0.5,\"targets\":[[400,100],[400,100]]}'" \
      | python3 -c '
import json,sys
d=json.load(sys.stdin)
m=d["monitors"][0] if isinstance(d.get("monitors"),list) and d["monitors"] else {}
t=d["targets"][-1]
hw=list(t["hw"].values())
print("    bbox   %s" % (d["daemon_bbox"],))
print("    shell  %sx%s @%s" % (m.get("width"),m.get("height"),m.get("scale")))
print("    dconf  %s" % d["wxrandr_query"].splitlines()[0].split("current",1)[-1].split(",")[0].strip())
print("    land   asked 400,100 -> comp %s  plane %s" % (t["comp"], hw[0][:2] if len(hw)==1 else t["hw"]))
'
}

u() { $V user "$VM" -- sh -lc "$1" >/dev/null 2>&1 || true; sleep 3; }

u "gsettings set org.gnome.mutter experimental-features \"[]\""
u "wxrandr --output Virtual-1 --scale 1"
step "1 no-fractional, scale 1"

u "wxrandr --output Virtual-1 --scale 2"
step "2 no-fractional, scale 2   (physical layout mode)"

u "gsettings set org.gnome.mutter experimental-features \"['scale-monitor-framebuffer']\""
step "3 fractional turned ON while the monitor sits at 200%"

u "wxrandr --output Virtual-1 --scale 2"
step "4 the same scale re-applied afterwards"

u "wxrandr --output Virtual-1 --scale 1.5"
step "5 scale 1.5"
