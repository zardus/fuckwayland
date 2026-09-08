#!/bin/sh
# The `windows` and `wm` phases' guest commands, asked in the order the smoke
# asks them, recorded in the smoke's own transcript format.
#
# This is what makes vm/live-smoke.d/selftest-offline.sh possible: the phases
# read the SAME command more than once and require DIFFERENT answers (the
# maximize pair asks `wdotool getwindowgeometry` three times and the middle one
# has to be the maximized geometry), so a capture that is not in the asking
# order cannot drive them.  Every byte below comes off a real desktop; nothing
# here is written by hand.
#
#   vmctl user <vm> -- sh /tmp/cap-winwm.sh TextEditor > \
#       tests/fixtures/live/<flavor>-windows-wm-replay.txt
set -u
CLASS=${1:-TextEditor}
F=/tmp/fw-smoke.txt

run() { echo "### $*"; sh -c "$*" 2>&1; echo "### rc=$?"; }

echo "### capture $(date -Is) $(gnome-shell --version 2>/dev/null || echo '?') class=$CLASS order=windows,wm"
rm -f "$F"; touch "$F"
pkill -f gnome-text-editor 2>/dev/null
sleep 1
setsid nohup gnome-text-editor "$F" >/dev/null 2>&1 </dev/null &
sleep 6

run "wdotool search --class $CLASS"
W=$(wdotool search --class "$CLASS" 2>/dev/null | head -1)
echo "### WIN=$W"
run "wdotool getwindowname $W"
wdotool windowmove "$W" 100 100; wdotool windowsize "$W" 800 600; sleep 1
run "wdotool getwindowgeometry $W"
wdotool windowactivate --sync "$W"
run 'wdotool getactivewindow'
wdotool windowminimize "$W"; sleep 1
run 'wdotool getactivewindow'
wdotool windowactivate --sync "$W"
wdotool set_desktop 1; sleep 1
run 'wdotool get_desktop'
wdotool set_desktop 0; sleep 1
run 'wdotool get_desktop'
wdotool windowactivate --sync "$W"
wdotool mousemove 300 300
run 'wdotool getmouselocation'
run 'wdotool mousemove 300 300 click 1'

# --- the wm phase starts by putting the window back where it was
wdotool windowactivate --sync "$W"; wdotool windowmove "$W" 100 100; wdotool windowsize "$W" 800 600
sleep 1
run "wdotool getwindowgeometry $W"
wwmctl -r :ACTIVE: -b add,maximized_vert,maximized_horz; sleep 2
run "wdotool getwindowgeometry $W"
run "wxprop -id $W _NET_WM_STATE"
wwmctl -r :ACTIVE: -b remove,maximized_vert,maximized_horz; sleep 2
run "wdotool getwindowgeometry $W"
run 'wwmctl -l'
run 'wwmctl -l -G'
run 'wwmctl -d'
run 'wxprop -root _NET_CLIENT_LIST'
echo "### end $(date -Is)"
