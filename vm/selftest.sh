#!/usr/bin/env bash
# vmctl rig self-test.  Boots <flavor>-t with 3 heads and checks: autologin into
# the flavor's desktop (vmctl session), 3 monitors in the desktop's NATIVE display
# tool with Virtual-1 at (0,0) (primary where the desktop has the notion), no
# stray first-run window, `vmctl user` exporting a working session environment
# (XDG_SESSION_ID/XDG_SESSION_TYPE and the display sockets), screenshots of every
# head that show a desktop (not a flat colour, not the kernel console), hot-plug
# of a 4th head and its removal as seen by that native tool.
# Native tool per desktop (`# vmctl-desktop:` in the flavor yaml), by $oracle:
#   mutter  org.gnome.Mutter.DisplayConfig.GetCurrentState (logical monitors, primary)
#   muffin  the same call under org.cinnamon.Muffin.DisplayConfig -- byte-identical
#           signatures to Mutter's, muffin being Mutter's fork [recon2/cinnamon]
#   kscreen kscreen-doctor -o  (Plasma 5.27 prints one line per output, Plasma 6 a block)
#   xrandr  xrandr --listmonitors (the X server's enabled outputs)
#   sway    swaymsg -t get_outputs
#   wlr     wlr-randr's human output, `Position: x,y` per enabled head.  Weak on
#           purpose: it speaks the same protocol wxrandr's wlr backend writes, and
#           the flavors that use it (labwc and everything on it, river) have no IPC
#           of their own [recon2/labwc, recon2/river].
#   hypr    hyprctl -j monitors
#   cosmic  cosmic-randr list --kdl
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
# tool: the native display tool as a human reads it; oracle: which parser below asks
# it; logind_type: what logind reports for the session; session_type: what `vmctl user`
# must export; sockets: which sockets that environment must carry; heads_differ: why
# head 0's screendump must differ from head 1's (empty = it legitimately may not).
# One arm per key of vm/vmctl's DESKTOPS (tests/test_vm_scripts.py R27 pins that).
bus_dest=org.gnome.Mutter.DisplayConfig
bus_path=/org/gnome/Mutter/DisplayConfig
case $desktop in
    gnome) tool="GetCurrentState";       oracle=mutter;  logind_type=wayland; session_type=wayland
           sockets=wayland
           heads_differ="the primary head carries the top bar and the dock" ;;
    gnome-x11) tool="GetCurrentState + xrandr"; oracle=mutter; logind_type=x11; session_type=x11
           # mutter 46's XRandR monitor manager answers DisplayConfig on Xorg too, and
           # must agree with the X server [recon2/gnome-xorg]
           sockets=x11
           heads_differ="the primary head carries the top bar and the dock" ;;
    kde)   tool="kscreen-doctor -o";     oracle=kscreen; logind_type=wayland; session_type=wayland
           sockets=wayland
           heads_differ="the panel is on the primary output only" ;;
    kde-x11) tool="kscreen-doctor -o";   oracle=kscreen; logind_type=x11;     session_type=x11
           sockets=x11
           heads_differ="the panel is on the primary output only" ;;
    xfce)  tool="xrandr --listmonitors"; oracle=xrandr;  logind_type=x11;     session_type=x11
           sockets=x11
           heads_differ="xfce4-panel is on the first monitor only" ;;
    xfce-wayland) tool="wlr-randr";      oracle=wlr;     logind_type=wayland; session_type=wayland
           # Xfce 4.20's Wayland session is startxfce4 --wayland over labwc; the panel
           # pair is the X11 session's [recon2/xfce-wayland]
           sockets=wlroots
           heads_differ="xfce4-panel is on the first monitor only" ;;
    sway)  tool="swaymsg -t get_outputs"; oracle=sway;   logind_type=wayland; session_type=wayland
           sockets=sway
           # greetd registers a tty session; sway's libseat switches it to wayland.
           # swaybar is on every output; only the workspace number in it differs
           # (workspace N is pinned to Virtual-N), so identical heads get a warning,
           # and the per-output workspaces are checked through swaymsg instead.
           heads_differ="" ;;
    hypr)  tool="hyprctl -j monitors";   oracle=hypr;    logind_type=wayland; session_type=wayland
           sockets=hypr
           # no bar in a default Hyprland, and the rig's config disables the logo and
           # the splash: every head is the same wallpaper [recon2/hyprland]
           heads_differ="" ;;
    labwc) tool="wlr-randr";             oracle=wlr;     logind_type=wayland; session_type=wayland
           sockets=wlroots
           # labwc paints nothing; swaybg puts the same wallpaper on every head
           heads_differ="" ;;
    wayfire) tool="wlr-randr";           oracle=wlr;     logind_type=wayland; session_type=wayland
           sockets=wayfire
           # wf-panel's per-output behaviour is unmeasured (the recon ran one output);
           # the first selftest of resolute-wayfire is where it gets decided
           heads_differ="" ;;
    river) tool="wlr-randr";             oracle=wlr;     logind_type=wayland; session_type=wayland
           sockets=wlroots
           # tinyrwm draws borders and nothing else [recon2/river]
           heads_differ="" ;;
    cosmic) tool="cosmic-randr list --kdl"; oracle=cosmic; logind_type=wayland; session_type=wayland
           sockets=wlroots
           # cosmic-panel's placement is unmeasured, as is whether cosmic-comp paints on
           # virtio-vga at all [recon2/cosmic]
           heads_differ="" ;;
    budgie) tool="wlr-randr";            oracle=wlr;     logind_type=wayland; session_type=wayland
           sockets=wlroots
           heads_differ="" ;;
    lxqt)  tool="xrandr --listmonitors"; oracle=xrandr;  logind_type=x11;     session_type=x11
           sockets=x11
           heads_differ="lxqt-panel is on the first monitor only" ;;
    lxqt-wayland) tool="wlr-randr";      oracle=wlr;     logind_type=wayland; session_type=wayland
           sockets=wlroots
           heads_differ="lxqt-panel is on the first monitor only" ;;
    cinnamon) tool="xrandr --listmonitors"; oracle=xrandr; logind_type=x11;   session_type=x11
           sockets=x11
           heads_differ="the panel is on the primary output only" ;;
    cinnamon-wayland) tool="Muffin GetCurrentState"; oracle=muffin
           bus_dest=org.cinnamon.Muffin.DisplayConfig
           bus_path=/org/cinnamon/Muffin/DisplayConfig
           logind_type=wayland; session_type=wayland; sockets=wayland
           heads_differ="the panel is on the primary output only" ;;
    mate)  tool="xrandr --listmonitors"; oracle=xrandr;  logind_type=x11;     session_type=x11
           sockets=x11
           # measured under Xvfb: mate-panel's strut moved _NET_WORKAREA from
           # 0,0 1920x1080 to 0,28 1920x1024 [recon2/mate]
           heads_differ="mate-panel and caja draw the first monitor only" ;;
    i3)    tool="xrandr --listmonitors"; oracle=xrandr;  logind_type=x11;     session_type=x11
           sockets=x11
           # i3bar is on every output and the rig paints the same xsetroot checkerboard
           # on all of them; only the workspace number in the bar differs [recon2/i3]
           heads_differ="" ;;
    *) echo "unknown desktop $desktop"; exit 1 ;;
esac

# --- the native display tool: "<name> <x>,<y>" per ENABLED monitor, one per line
heads() {
    case $oracle in
    mutter|muffin)
        "$VM" user "$name" -- gdbus call --session --dest "$bus_dest" \
            --object-path "$bus_path" --method "$bus_dest.GetCurrentState" \
            | grep -o "'Virtual-[0-9]*'" | tr -d "'" | sed 's/$/ ?,?/' ;;
    kscreen)
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
        geo = re.search(r"Geometry: (-?\d+),(-?\d+)", chunk)
        print("%s %s" % (m.group(1), "%s,%s" % geo.groups() if geo else "?,?"))' ;;
    xrandr)  # " 0: +*Virtual-1 1920/487x1080/274+0+0  Virtual-1"
        "$VM" user "$name" -- xrandr --listmonitors | awk 'NR > 1 {
            g = $3; sub(/^[0-9]+\/[0-9]+x[0-9]+\/[0-9]+\+/, "", g); sub(/\+/, ",", g);
            print $NF, g }' ;;
    sway)
        "$VM" user "$name" -- swaymsg -t get_outputs | python3 -c '
import json, sys
for o in json.load(sys.stdin):
    if o.get("active"): print("%s %d,%d" % (o["name"], o["rect"]["x"], o["rect"]["y"]))' ;;
    wlr)   # wlr-randr 0.4.1: a name line, then indented keys; a disabled head has
           # "Enabled: no" and NO Position line at all (measured on wlr-randr
           # 0.4.1-1build1 against headless sway 1.11, 2026-09-08)
        "$VM" user "$name" -- wlr-randr | python3 -c '
import re, sys
name = None
for line in sys.stdin:
    m = re.match(r"^(\S+) ", line)
    if m:
        name = m.group(1)
    elif name and re.match(r"^\s+Enabled: no", line):
        name = None
    elif name and re.match(r"^\s+Position: ", line):
        print("%s %s" % (name, line.split(":", 1)[1].strip()))
        name = None' ;;
    hypr)  # a runtime headless output came up with scale 2.0, so the rig pins scale 1
           # in hyprland.conf; "disabled" is the field that says a head is off
        "$VM" user "$name" -- hyprctl -j monitors | python3 -c '
import json, sys
for m in json.load(sys.stdin):
    if not m.get("disabled"): print("%s %d,%d" % (m["name"], m["x"], m["y"]))' ;;
    cosmic)  # KDL: `output "NAME" enabled=#true {` ... `  position X Y`
        "$VM" user "$name" -- cosmic-randr list --kdl | python3 -c '
import re, sys
name = None
for line in sys.stdin:
    m = re.match(r"^output \"([^\"]+)\" enabled=#(\w+)", line)
    if m:
        name = m.group(1) if m.group(2) == "true" else None
    elif name:
        m = re.match(r"\s+position (-?\d+) (-?\d+)", line)
        if m:
            print("%s %s,%s" % (name, m.group(1), m.group(2)))
            name = None' ;;
    esac
}
monitors() {   # enabled monitor names, one per line, sorted
    heads | awk '{print $1}' | grep '^Virtual-' | sort -u
}
layout() {   # "primary <name> at x,y" (or "output Virtual-1 at x,y"), then the stray-window line
    case $oracle in
    mutter|muffin)
        "$VM" user "$name" -- env FW_BUS_DEST="$bus_dest" FW_BUS_PATH="$bus_path" python3 - <<'PY'
import gi, os, subprocess
gi.require_version("Gio", "2.0")
from gi.repository import Gio
dest, path = os.environ["FW_BUS_DEST"], os.environ["FW_BUS_PATH"]
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
st = bus.call_sync(dest, path, dest, "GetCurrentState", None, None, 0, -1, None).unpack()
for x, y, scale, transform, primary, mons, props in st[2]:
    if primary:
        print("primary %s at %d,%d" % (mons[0][0], x, y))
# stray dialogs are Wayland-native GTK4 windows, invisible to wmctrl/xdotool (Xwayland),
# so look for the process instead: gnome-initial-setup (first-login or --upgrade-user
# "Welcome to Ubuntu" dialog) must not be running in a vmctl session
p = subprocess.run(["pgrep", "-a", "-f", "gnome-initial-setup|mintwelcome"],
                   capture_output=True, text=True)
print("initial-setup: %s" % (p.stdout.strip().replace("\n", "; ") or "none"))
PY
        ;;
    kscreen)   # Virtual-1's block: "priority 1" (Plasma >= 5.26: the primary output) and "Geometry: x,y WxH"
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
    xrandr)  # "Virtual-1 connected primary 1920x1080+0+0 ..." (primary only if something set it)
        "$VM" user "$name" -- sh -c 'xrandr --query | awk '"'"'
$1=="Virtual-1" && $2=="connected" { p = ($3=="primary") ? "primary" : "output"; g = ($3=="primary") ? $4 : $3;
  sub(/^[0-9]+x[0-9]+\+/, "", g); sub(/\+/, ",", g); printf "%s Virtual-1 at %s\n", p, g }'"'"'
echo "initial-setup: $(wmctrl -l | grep -iE "display|welcome|i3-config" | tr "\n" ";")"' \
            | sed 's/^initial-setup: $/initial-setup: none/'
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
    # The compositors with no IPC and no session D-Bus name of their own: the same
    # oracle answers both questions.  Which first-run window to look for is NOT decided
    # by the oracle, though -- oracle=wlr also serves xfce-wayland, lxqt-wayland and
    # budgie, three full desktops whose X11 twins are exactly why the xrandr arm greps
    # wmctrl for a display/welcome dialog (Xfce's is the reason displays.xml carries
    # Notify=3).  A literal "none" on those three would make the first-run assertion
    # below vacuous, so they get a pgrep of their own; only the bare compositors
    # (labwc, river, Hyprland, cosmic) really ship no first-run dialog at all.
    wlr|hypr|cosmic)
        heads | awk '$1 == "Virtual-1" { printf "output Virtual-1 at %s\n", $2 }'
        case $desktop in
            xfce-wayland|lxqt-wayland|budgie)
                # -x and not -f: the pattern travels in this sh's own argv, which a
                # `pgrep -f` would match itself.  The names are the Wayland twins of
                # what the xrandr arm greps out of wmctrl.
                first_run='xfce4-display-settings|lxqt-config-monitor|budgie-welcome|update-notifier'
                "$VM" user "$name" -- sh -c \
                    'echo "initial-setup: $(pgrep -a -x "'"$first_run"'" | tr "\n" ";")"' \
                    | sed 's/^initial-setup: $/initial-setup: none/' ;;
            *)  echo "initial-setup: none" ;;
        esac
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
    [ "$sockets" = x11 ] || return 0
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
    gnome|gnome-x11|kde|kde-x11|cinnamon-wayland)
        echo "$lay" | grep -q '^primary Virtual-1 at 0,0$' || { echo "FAIL: primary is not Virtual-1 at 0,0"; exit 1; } ;;
    *)  echo "$lay" | grep -Eq '^(primary|output) Virtual-1 at 0,0$' || { echo "FAIL: Virtual-1 is not at 0,0"; exit 1; } ;;
esac
echo "$lay" | grep -q '^initial-setup: none$' || { echo "FAIL: a first-run window is running in the session"; exit 1; }
if [ $desktop = sway ]; then
    echo "$lay" | grep -q '^workspaces: Virtual-1=1 Virtual-2=2 Virtual-3=3$' || { echo "FAIL: workspace N is not on Virtual-N"; exit 1; }
fi
if [ $desktop = gnome-x11 ]; then
    # The tool for this flavor is "GetCurrentState + xrandr", so both must be asked:
    # mutter 46's XRandR monitor manager answers DisplayConfig on Xorg too
    # [recon2/gnome-xorg], and a rig that trusted only one of the two would not notice
    # them disagreeing -- which is exactly the failure this flavor exists to catch (an
    # output X still scans out after mutter has let it go).  heads() is DisplayConfig.
    xr=$("$VM" user "$name" -- sh -c 'xrandr --query | awk '"'"'$2=="connected" {print $1}'"'"'' \
         | grep '^Virtual-' | sort -u) || true
    [ "$(monitors)" = "$xr" ] || {
        echo "FAIL: DisplayConfig and xrandr disagree about the connected outputs:"
        echo "   DisplayConfig: $(monitors | tr '\n' ' ')"
        echo "   xrandr:        $(echo "$xr" | tr '\n' ' ')"; exit 1; }
    echo "   DisplayConfig and xrandr agree: $(echo "$xr" | tr '\n' ' ')"
fi
step "vmctl user: session id/type (logind Type $logind_type, XDG_SESSION_TYPE $session_type, display sockets)"
# The session environment `vmctl user` rebuilt, printed as one line: the four
# socket variables are per-desktop and only the ones that exist are shown.
env_report='for v in XDG_SESSION_ID XDG_SESSION_TYPE XDG_CURRENT_DESKTOP WAYLAND_DISPLAY \
                     DISPLAY XAUTHORITY SWAYSOCK I3SOCK WAYFIRE_SOCKET HYPRLAND_INSTANCE_SIGNATURE; do
  eval "val=\${$v:-}"; [ -n "$val" ] && printf "%s=%s " "$v" "$val"
done; echo; loginctl show-session "$XDG_SESSION_ID" -p Type -p State'
"$VM" user "$name" -- sh -c "$env_report" | sed 's/^/   /' | tr '\n' ' '; echo
"$VM" user "$name" -- sh -c 'loginctl show-session "$XDG_SESSION_ID" -p Type -p State' | grep -q "^Type=$logind_type" || { echo "FAIL: XDG_SESSION_ID is not a $logind_type session"; exit 1; }
"$VM" user "$name" -- sh -c '[ "$XDG_SESSION_TYPE" = '"$session_type"' ]' || { echo "FAIL: vmctl user does not export XDG_SESSION_TYPE=$session_type"; exit 1; }
# Which sockets that environment must carry.  The wlroots family (labwc and the
# desktops on it, river, cosmic) has no IPC socket at all -- the labwc package is
# labwc, labnag and lab-sensible-terminal and nothing else -- so its check is the sway
# branch minus SWAYSOCK; and labwc, like sway, starts Xwayland with no -auth, so a
# working DISPLAY with an empty XAUTHORITY is correct there and not a finding
# [recon2/labwc, recon2/xfce-wayland].
wl='[ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]'
x=' && xdpyinfo >/dev/null'
need() {   # need "<what must be true>" "<sh test run as user test in the session>"
    "$VM" user "$name" -- sh -c "$2" || { echo "FAIL: $1"; exit 1; }
}
case $sockets in
    wayland) need "no WAYLAND_DISPLAY socket / working Xwayland DISPLAY" "$wl$x" ;;
    x11)     need "not a working X11 environment (WAYLAND_DISPLAY set, or no X)" \
                  '[ -z "$WAYLAND_DISPLAY" ]'"$x" ;;
    sway)    need "no WAYLAND_DISPLAY/SWAYSOCK sockets / working Xwayland DISPLAY" \
                  "$wl"' && [ -S "$SWAYSOCK" ]'"$x" ;;
    hypr)    need "no WAYLAND_DISPLAY / Hyprland instance socket / working Xwayland DISPLAY" \
                  "$wl"' && [ -S "$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock" ]'"$x" ;;
    wayfire) need "no WAYLAND_DISPLAY/WAYFIRE_SOCKET sockets / working Xwayland DISPLAY" \
                  "$wl"' && [ -S "$WAYFIRE_SOCKET" ]'"$x" ;;
    wlroots) need "no WAYLAND_DISPLAY socket / working Xwayland DISPLAY" "$wl$x"
             "$VM" user "$name" -- sh -c '[ -z "$XAUTHORITY" ]' \
               && echo "   note: Xwayland here runs without -auth; DISPLAY alone works" || true ;;
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
