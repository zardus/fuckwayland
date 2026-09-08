#!/usr/bin/env bash
# vmctl rig self-test.  Boots <flavor>-t with 3 heads and checks: autologin into
# the flavor's desktop (vmctl session), 3 monitors in the desktop's NATIVE display
# tool with Virtual-1 at (0,0) (primary where the desktop has the notion), no
# stray first-run window, `vmctl user` exporting a working session environment
# (XDG_SESSION_ID/XDG_SESSION_TYPE and the display sockets), screenshots of every
# head that show a desktop (not a flat colour, not the kernel console), hot-plug
# of a 4th head and its removal as seen by that native tool.
# Native tool per desktop (`# vmctl-desktop:` in the flavor yaml):
#   gnome  mutter's org.gnome.Mutter.DisplayConfig.GetCurrentState (logical monitors, primary)
#   kde    kscreen-doctor -o  (Plasma 5.27 prints one line per output, Plasma 6 a block)
#   kde-x11  the same: libkscreen's XRandR backend answers in the same format on Xorg
#   xfce   xrandr --listmonitors (the X server's enabled outputs)
#   sway   swaymsg -t get_outputs
# Leaves the VM running.   usage: vm/selftest.sh <flavor> [name]
# The instance gets vmctl start's defaults (3 vCPU / 4 GB).  On a machine with
# less than that, pass the size it can give:  SELFTEST_VM_ARGS='--cpus 2 --mem 3G'
set -eu
VMDIR=$(cd -- "$(dirname -- "$0")" && pwd)
VM=$VMDIR/vmctl
flavor=$1; name=${2:-$flavor-t}
out=${OUT:-/tmp/vmctl-selftest-$name}; mkdir -p "$out"
t0=$(date +%s)
step() { echo "== [$(( $(date +%s) - t0 ))s] $*"; }
[ -f "$VMDIR/flavors/$flavor.yaml" ] || { echo "no flavor $flavor (vm/flavors/$flavor.yaml)"; exit 1; }
desktop=$(sed -n 's/^#[[:space:]]*vmctl-desktop:[[:space:]]*//p' "$VMDIR/flavors/$flavor.yaml" | head -1)
desktop=${desktop:-gnome}
# tool: the native display tool; logind_type: what logind reports for the session;
# session_type: what `vmctl user` must export; heads_differ: why head 0's screendump
# must differ from head 1's (empty = it legitimately may not)
case $desktop in
    gnome) tool="GetCurrentState";       logind_type=wayland; session_type=wayland
           heads_differ="the primary head carries the top bar and the dock" ;;
    kde)   tool="kscreen-doctor -o";     logind_type=wayland; session_type=wayland
           heads_differ="the panel is on the primary output only" ;;
    kde-x11) tool="kscreen-doctor -o";   logind_type=x11;     session_type=x11
           heads_differ="the panel is on the primary output only" ;;
    xfce)  tool="xrandr --listmonitors"; logind_type=x11;     session_type=x11
           heads_differ="xfce4-panel is on the first monitor only" ;;
    sway)  tool="swaymsg -t get_outputs"; logind_type=wayland; session_type=wayland
           # greetd registers a tty session; sway's libseat switches it to wayland.
           # swaybar is on every output; only the workspace number in it differs
           # (workspace N is pinned to Virtual-N), so identical heads get a warning,
           # and the per-output workspaces are checked through swaymsg instead.
           heads_differ="" ;;
    *) echo "unknown desktop $desktop"; exit 1 ;;
esac

# --- the native display tool: enabled monitor names, one per line, sorted
monitors() {
    case $desktop in
    gnome)
        "$VM" user "$name" -- gdbus call --session --dest org.gnome.Mutter.DisplayConfig \
            --object-path /org/gnome/Mutter/DisplayConfig \
            --method org.gnome.Mutter.DisplayConfig.GetCurrentState \
            | grep -o "'Virtual-[0-9]*'" | tr -d "'" | sort -u ;;
    kde|kde-x11)
              # kscreen-doctor -o, ANSI-coloured, one BLOCK per output: 5.27 puts the
              # whole state on the "Output:" line and 6.x on the indented lines under
              # it, so the state has to be read per block and not per line.  On X11
              # libkscreen's XRandR backend also lists every CONNECTOR the server has,
              # disconnected ones included ("Output: 69 Virtual-4 disabled
              # disconnected"), where KWin on Wayland exports only the plugged ones --
              # hence enabled AND connected.  One parser for both: the Wayland arm used
              # to grep the "Output:" line alone and counted a head this script had
              # just unplugged, which is the failure the block parser exists to avoid.
        "$VM" user "$name" -- kscreen-doctor -o 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' \
            | python3 -c '
import re, sys
for chunk in re.split(r"(?m)^Output: ", sys.stdin.read())[1:]:
    m = re.match(r"\d+\s+(\S+)", chunk)
    if m and re.search(r"\benabled\b", chunk) and re.search(r"\bconnected\b", chunk):
        print(m.group(1))' | grep '^Virtual-' | sort -u ;;
    xfce)  # " 0: +*Virtual-1 1920/487x1080/274+0+0  Virtual-1"
        "$VM" user "$name" -- xrandr --listmonitors | awk 'NR > 1 { print $NF }' | grep '^Virtual-' | sort -u ;;
    sway)
        "$VM" user "$name" -- swaymsg -t get_outputs | python3 -c '
import json, sys
for o in json.load(sys.stdin):
    if o.get("active"): print(o["name"])' | sort -u ;;
    esac
}
layout() {   # "primary <name> at x,y" (or "output Virtual-1 at x,y"), then the stray-window line
    case $desktop in
    gnome)
        "$VM" user "$name" -- python3 - <<'PY'
import gi, json, subprocess
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
st = bus.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                   "org.gnome.Mutter.DisplayConfig", "GetCurrentState", None, None, 0, -1, None).unpack()
for x, y, scale, transform, primary, mons, props in st[2]:
    if primary:
        print("primary %s at %d,%d" % (mons[0][0], x, y))
# stray dialogs are Wayland-native GTK4 windows, invisible to wmctrl/xdotool (Xwayland),
# so look for the process instead: gnome-initial-setup (first-login or --upgrade-user
# "Welcome to Ubuntu" dialog) must not be running in a vmctl session
p = subprocess.run(["pgrep", "-a", "-f", "gnome-initial-setup"], capture_output=True, text=True)
print("initial-setup: %s" % (p.stdout.strip().replace("\n", "; ") or "none"))
PY
        ;;
    kde|kde-x11)   # Virtual-1's block: "priority 1" (Plasma >= 5.26: the primary output) and "Geometry: x,y WxH"
        "$VM" user "$name" -- python3 - <<'PY'
import re, subprocess
out = subprocess.run(["kscreen-doctor", "-o"], capture_output=True, text=True).stdout
out = re.sub(r"\x1b\[[0-9;]*m", "", out)
for chunk in re.split(r"(?m)^Output: ", out)[1:]:
    m = re.match(r"\d+ (\S+)", chunk)
    if m and m.group(1) == "Virtual-1":
        prio = re.search(r"priority (\d+)", chunk)
        geo = re.search(r"Geometry: (-?\d+),(-?\d+)", chunk)
        print("%s Virtual-1 at %s" % ("primary" if prio and prio.group(1) == "1" else "output",
                                      "%s,%s" % geo.groups() if geo else "?"))
# the Plasma welcome centre is a window on the primary output; it must not run here
p = subprocess.run(["pgrep", "-a", "-x", "plasma-welcome"], capture_output=True, text=True)
print("initial-setup: %s" % (p.stdout.strip().replace("\n", "; ") or "none"))
PY
        ;;
    xfce)  # "Virtual-1 connected primary 1920x1080+0+0 ..." (primary only if something set it)
        "$VM" user "$name" -- sh -c 'xrandr --query | awk '"'"'
$1=="Virtual-1" && $2=="connected" { p = ($3=="primary") ? "primary" : "output"; g = ($3=="primary") ? $4 : $3;
  sub(/^[0-9]+x[0-9]+\+/, "", g); sub(/\+/, ",", g); printf "%s Virtual-1 at %s\n", p, g }'"'"'
echo "initial-setup: $(wmctrl -l | grep -iE "display|welcome" | tr "\n" ";")"' | sed 's/^initial-setup: $/initial-setup: none/'
        ;;
    sway)  # the focused output plays "primary"; workspace N must sit on Virtual-N
        "$VM" user "$name" -- swaymsg -t get_outputs | python3 -c '
import json, sys
outs = [o for o in json.load(sys.stdin) if o.get("active")]
for o in outs:
    if o["name"] == "Virtual-1":
        print("%s Virtual-1 at %d,%d" % ("primary" if o.get("focused") else "output", o["rect"]["x"], o["rect"]["y"]))
print("workspaces: " + " ".join("%s=%s" % (o["name"], o.get("current_workspace")) for o in sorted(outs, key=lambda o: o["name"])))
print("initial-setup: none")'
        ;;
    esac
}
check_shot() {   # a flat single-colour screendump = the compositor never finished its start-up
    local f=$1 sd
    if ! command -v identify >/dev/null; then echo "   $f: (imagemagick missing, content not checked)"; return 0; fi
    sd=$(identify -format '%[fx:standard_deviation]' "$f")
    awk -v sd="$sd" 'BEGIN { exit !(sd > 0.01) }' || return 1
    echo "   $f: $(identify -format '%wx%h' "$f"), stddev $sd"
}
shots() {   # screendump every head; retry while one of them is still flat.
    # `vmctl session` already waits for a picture on every head, so one round is
    # the rule; a head that paints late (Xfce draws xfdesktop's wallpaper per
    # monitor after the panel) gets a few more seconds rather than a failure.
    local out=$1 tries=$2 i f bad
    for i in $(seq 1 "$tries"); do
        "$VM" shot "$name" --all "$out/shot" > /dev/null
        bad=
        for f in "$out"/shot-*.png; do check_shot "$f" || bad="$bad $f"; done
        [ -z "$bad" ] && return 0
        [ "$i" = "$tries" ] && break
        echo "   waiting for$bad to paint (attempt $i/$tries)"; sleep 4
    done
    for f in $bad; do
        echo "FAIL: $f is a flat image (stddev $(identify -format '%[fx:standard_deviation]' "$f")):" \
             "the desktop did not render on that head within $(( tries * 4 ))s"
    done
    exit 1
}
expect_monitors() {   # the compositor needs a moment after a hotplug event
    local want=$1 tries=${2:-10} got n
    for _ in $(seq 1 "$tries"); do
        got=$(monitors | tr '\n' ' '); n=$(echo $got | wc -w)
        [ "$n" = "$want" ] && { echo "   $tool: $n monitors: $got"; return 0; }
        sleep 2
    done
    echo "FAIL: expected $want monitors, $tool shows $n: $got  (waited $(( tries * 2 ))s)"
    diagnose_heads
    exit 1
}
diagnose_heads() {   # what the guest thinks the connectors are, and (on X11) what X kept
    "$VM" heads "$name" | sed 's/^/   drm: /' || true
    case $desktop in xfce|kde-x11) ;; *) return 0 ;; esac
    "$VM" user "$name" -- xrandr --query 2>/dev/null | grep -E '^Virtual-' | sed 's/^/   xrandr: /' || true
    echo "   (an output listed by xrandr as \"disconnected\" but with a mode/position is one the"
    echo "    X server has not let go of: the connector is gone, the CRTC is still scanning it out)"
}

vm_args=${SELFTEST_VM_ARGS:-}   # a list of options: unquoted below on purpose
step "vmctl start $name --flavor $flavor --heads 3 --fresh $vm_args  (desktop $desktop, native tool: $tool)"
port=$("$VM" start "$name" --flavor "$flavor" --heads 3 --fresh $vm_args)
step "vmctl session $name"
"$VM" session "$name"
step "$tool: expect Virtual-1..3, Virtual-1 at 0,0, no first-run window"
expect_monitors 3
lay=$(layout); echo "   $lay" | tr '\n' ' '; echo
case $desktop in
    gnome|kde|kde-x11) echo "$lay" | grep -q '^primary Virtual-1 at 0,0$' || { echo "FAIL: primary is not Virtual-1 at 0,0"; exit 1; } ;;
    *)         echo "$lay" | grep -Eq '^(primary|output) Virtual-1 at 0,0$' || { echo "FAIL: Virtual-1 is not at 0,0"; exit 1; } ;;
esac
echo "$lay" | grep -q '^initial-setup: none$' || { echo "FAIL: a first-run window is running in the session"; exit 1; }
if [ $desktop = sway ]; then
    echo "$lay" | grep -q '^workspaces: Virtual-1=1 Virtual-2=2 Virtual-3=3$' || { echo "FAIL: workspace N is not on Virtual-N"; exit 1; }
fi
step "vmctl user: session id/type (logind Type $logind_type, XDG_SESSION_TYPE $session_type, display sockets)"
"$VM" user "$name" -- sh -c 'echo "   XDG_SESSION_ID=$XDG_SESSION_ID XDG_SESSION_TYPE=$XDG_SESSION_TYPE XDG_CURRENT_DESKTOP=$XDG_CURRENT_DESKTOP WAYLAND_DISPLAY=$WAYLAND_DISPLAY DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY${SWAYSOCK:+ SWAYSOCK=$SWAYSOCK}"; loginctl show-session "$XDG_SESSION_ID" -p Type -p State' | tr '\n' ' '; echo
"$VM" user "$name" -- sh -c 'loginctl show-session "$XDG_SESSION_ID" -p Type -p State' | grep -q "^Type=$logind_type" || { echo "FAIL: XDG_SESSION_ID is not a $logind_type session"; exit 1; }
"$VM" user "$name" -- sh -c '[ "$XDG_SESSION_TYPE" = '"$session_type"' ]' || { echo "FAIL: vmctl user does not export XDG_SESSION_TYPE=$session_type"; exit 1; }
case $desktop in
    gnome|kde) "$VM" user "$name" -- sh -c '[ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && xdpyinfo >/dev/null' || { echo "FAIL: no WAYLAND_DISPLAY socket / working Xwayland DISPLAY in vmctl user"; exit 1; } ;;
    xfce|kde-x11) "$VM" user "$name" -- sh -c '[ -z "$WAYLAND_DISPLAY" ] && xdpyinfo >/dev/null' || { echo "FAIL: vmctl user is not a working X11 environment"; exit 1; } ;;
    sway)      "$VM" user "$name" -- sh -c '[ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && [ -S "$SWAYSOCK" ] && xdpyinfo >/dev/null' || { echo "FAIL: no WAYLAND_DISPLAY/SWAYSOCK sockets / working Xwayland DISPLAY in vmctl user"; exit 1; } ;;
esac
echo "   ok: $session_type session, display sockets present, X (xdpyinfo) reachable"
step "vmctl heads $name"
"$VM" heads "$name"
step "vmctl shot $name --all $out/shot (each must show a desktop, not a flat colour)"
shots "$out" 6
if [ -f "$out/shot-0.png" ] && [ -f "$out/shot-1.png" ] && command -v md5sum >/dev/null; then
    # Identical images mean the screendumps were taken before the desktop painted
    # (the kernel console is mirrored on every head by fbdev) -- provided the desktop
    # draws head 0 differently from a secondary head ($heads_differ says why).
    if [ "$(md5sum < "$out/shot-0.png")" = "$(md5sum < "$out/shot-1.png")" ]; then
        if [ -n "$heads_differ" ]; then
            echo "FAIL: shot-0.png and shot-1.png are identical: the desktop had not painted yet ($heads_differ)"; exit 1
        fi
        echo "   warning: shot-0.png and shot-1.png are identical ($desktop may draw both heads alike)"
    else
        echo "   shot-0.png differs from shot-1.png (${heads_differ:-as expected})"
    fi
fi
step "vmctl head $name 3 1280x1024: expect a 4th monitor in $tool"
"$VM" head "$name" 3 1280x1024
expect_monitors 4
"$VM" heads "$name" | grep Virtual-4
step "vmctl head $name 3 off: expect 3 monitors again"
"$VM" head "$name" 3 off
expect_monitors 3 30
step "PASS: $name ($flavor, $desktop) running, ssh port $port, screenshots in $out"
