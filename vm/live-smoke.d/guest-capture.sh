#!/bin/sh
# Capture, in ONE ssh round trip, the bytes every step of the smoke reads.
#
# It exists because a round trip to this rig costs whatever the host's load
# average makes it cost (54 s per `vmctl ssh` at load 98, measured on
# 2026-09-08), and because a recorded capture is worth more than a live run
# that nobody can repeat: the output of this script IS the fixture under
# tests/fixtures/live/, and it is what vm/live-smoke.d/fake-vmctl replays so
# that the smoke's own checks can be exercised -- and shown to fail -- without
# a VM at all.
#
#   vmctl scp <vm> vm/live-smoke.d/guest-capture.sh <vm>:/tmp/cap.sh
#   vmctl user <vm> -- sh /tmp/cap.sh gnome > tests/fixtures/live/<vm>-<date>.txt
#
# Every section is `### <the exact command>` followed by its output and
# `### rc=<status>`, so the reader of the fixture can see what was asked as
# well as what came back.
set -u
DESKTOP=${1:-gnome}
F=/tmp/fw-smoke.txt
# The smoke keeps its copy of the oracle in $HOME (a reboot empties /tmp on
# 24.04); a hand run of this script may have scp'd one to /tmp instead, so both
# are looked for and the section prints under one name either way.
OR=$HOME/fw-oracle.py
[ -f "$OR" ] || OR=/tmp/fw-oracle.py

run() {
    echo "### $*"
    sh -c "$*" 2>&1
    echo "### rc=$?"
}

echo "### capture $(date -Is) desktop=$DESKTOP host=$(uname -sr)"
run 'lsb_release -ds'
run 'gnome-shell --version || kwin_wayland --version || sway --version'
run 'wdotool --version'
run 'dpkg -l fuckwayland | tail -1'
[ "$DESKTOP" = gnome ] && run 'gnome-extensions info fuckwayland-bridge@fuckwayland'
[ "$DESKTOP" = gnome ] && run 'gnome-extensions info fuckwayland-overlap@fuckwayland'

# --- a window to work on
rm -f "$F"; touch "$F"
case $DESKTOP in
    gnome) setsid nohup gnome-text-editor "$F" >/dev/null 2>&1 </dev/null & CLASS=TextEditor ;;
    kde)   setsid nohup kate -n "$F"        >/dev/null 2>&1 </dev/null & CLASS=kate ;;
    sway)  setsid nohup foot -- sh -c "cat > $F" >/dev/null 2>&1 </dev/null & CLASS=foot ;;
    *)     setsid nohup xterm -T fwsmoke    >/dev/null 2>&1 </dev/null & CLASS=xterm ;;
esac
sleep 6
run "wdotool search --class $CLASS"
W=$(wdotool search --class "$CLASS" 2>/dev/null | head -1)
echo "### WIN=$W"
run "wdotool getwindowname $W"
run "wdotool windowmove $W 100 100"
run "wdotool windowsize $W 800 600"
sleep 1
run "wdotool getwindowgeometry $W"
run "wdotool windowactivate --sync $W"
run "wdotool getactivewindow"
run 'wdotool get_desktop'
run 'wdotool set_desktop 1'
run 'wdotool get_desktop'
run 'wdotool set_desktop 0'
run 'wdotool mousemove 300 300'
run 'wdotool getmouselocation'
run 'wdotool mousemove 300 300 click 1'
run 'wdotool keys explain yz@'

# --- the maximize pair, which is what b7a60f0 fixed
run 'wwmctl -r :ACTIVE: -b add,maximized_vert,maximized_horz'
sleep 2
run "wdotool getwindowgeometry $W"
run "wxprop -id $W _NET_WM_STATE"
run 'wwmctl -r :ACTIVE: -b remove,maximized_vert,maximized_horz'
sleep 2
run "wdotool getwindowgeometry $W"
run 'wwmctl -l'
run 'wwmctl -l -G'
run 'wwmctl -d'
run 'wxprop -root _NET_CLIENT_LIST'
run "wxprop -id $W WM_CLASS"

# --- display
run 'wxrandr --listmonitors'
run 'wxrandr --query'
run 'wxrandr --backends'
run 'wxrandr --print-backend --verbose'
case $DESKTOP in
    gnome) run "python3 $OR gnome"
           run 'wxrandr --output Virtual-2 --pos 4000x0'
           run 'wxrandr --gnome-overlap-status'
           run 'wxrandr --dryrun --unsafe-gnome-overlap --output Virtual-2 --pos 1000x0'
           run 'ls -l ~/.config/monitors.xml ~/.config/monitors.xml.wxrandr-backup' ;;
    kde)   run "python3 $OR kde"
           run 'kscreen-doctor -o'
           run 'cat ~/.config/kwinoutputconfig.json' ;;
    sway)  run "python3 $OR sway"
           run 'swaymsg -t get_outputs' ;;
esac
run 'gsettings get org.gnome.desktop.input-sources sources'
run 'gsettings get org.gnome.desktop.input-sources mru-sources'
run 'getfacl -p /dev/uinput'
run 'stat -c "%U %G %a" /dev/uinput'
run 'ls /usr/lib/udev/rules.d/60-fuckwayland-uinput.rules'
echo "### end $(date -Is)"
