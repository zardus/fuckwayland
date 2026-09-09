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
#
# The argument is the DESKTOP (the step file's own name), not the flavor, plus
# one order of its own: `i3ipc` records the i3 phase's command list instead of
# the windows/wm shapes below, because that phase asks a different set of
# questions in a different order [requests-batch-11.md item 5, from batch 16].
#
# THE LIST BELOW IS THE SHARED `windows`/`wm` PHASES, AND ONLY THOSE.  A step
# file that overrides `phase_windows`/`phase_wm` -- hypr.sh, wayfire.sh,
# cinnamon-wayland.sh, labwc.sh, cosmic.sh, river.sh all do -- asks a different
# set of commands in a different order, and `fake-vmctl` answers an unrecorded
# command with empty output and status 0, so a recording taken here would make
# its replay red, or worse, green on nothing.  Measured 2026-09-09: a hypr
# recording taken by this script replayed 14 pass / 13 fail against hypr.sh.
# For those flavors use `vm/live-smoke.d/capture-from-run`, which records the
# PHASES themselves off a live run and cannot disagree with them by
# construction; the recording committed for resolute-hypr was made that way and
# replays 30 pass / 0 fail.
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

# `sh /tmp/cap.sh i3ipc` -- the i3 step file's own command list, in its order.
# It produced tests/fixtures/live/resolute-i3-4.25.1-i3ipc-replay.txt on the
# resolute-i3 golden on 2026-09-09; before this arm existed it lived in a
# scratch script, so the committed recording was not reproducible from the tree
# [requests-batch-11.md item 5].  `wdotool getwindowgeometry` is recorded TWICE
# on purpose and in that order (before and after the move): fake-vmctl walks the
# two, which is what makes the move a claim rather than a reading.
capture_i3ipc() {
    OURS="FUCKWAYLAND_PASSTHROUGH=never env -u I3SOCK"
    run 'i3 --get-socketpath'
    run 'echo $I3SOCK'
    run 'wdotool --version'
    # -e, so there is no interactive shell: the test user's ~/.bashrc retitles an
    # xterm on its first prompt and `search --name fwsmoke` then matches nothing
    run "setsid nohup xterm -T fwsmoke -e sh -c 'while :; do sleep 3600; done' \
>/dev/null 2>&1 </dev/null & sleep 2; true"
    sleep 3
    run 'wdotool search --name fwsmoke'
    W=$(wdotool search --name fwsmoke 2>/dev/null | head -1); HEX=$(printf '0x%08x' "$W")
    run "$OURS wdotool search --name fwsmoke"
    run "$OURS wxprop -id $HEX WM_CLASS"
    run "$OURS wdotool getwindowpid $W"
    run "$OURS wdotool search --onlyvisible --name fwsmoke"
    run "i3-msg '[title=\"fwsmoke\"] floating enable'"; sleep 1
    run "wdotool getwindowgeometry $W"
    run "$OURS wdotool windowmove $W 120 140"; sleep 1
    run "wdotool getwindowgeometry $W"
    run "i3-msg '[title=\"fwsmoke\"] floating disable'"
    run "$OURS wdotool getdisplaygeometry"
    run "$OURS wxrandr --backend sway --print-backend --verbose"
    run "$OURS wxrandr --backend sway --output Virtual-1 --pos 0x0"
    run "$OURS wwmctl -m"
    run 'wmctrl -m'
    run "$OURS wdotool windowsize $W 400 300"
    run 'pkill xterm; true'
}

if [ "$DESKTOP" = i3ipc ]; then
    echo "### capture $(date -Is) desktop=i3 host=$(uname -sr)"
    capture_i3ipc
    echo "### end $(date -Is)"
    exit 0
fi

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
    sway|wayfire|hypr|labwc|river|cosmic)
           setsid nohup foot -- sh -c "cat > $F" >/dev/null 2>&1 </dev/null & CLASS=foot ;;
    # A regex, and it MUST stay quoted below: an unquoted bracket expression is a
    # glob the guest's shell expands against $HOME before wdotool ever sees it,
    # which is how this line silently searched for a filename [requests-batch-11.md,
    # from batch 15].  cinnamon-wayland.sh:145 quotes it for the same reason.
    cinnamon|cinnamon-wayland)
           setsid nohup gnome-terminal -- sh -c "cat > $F" >/dev/null 2>&1 </dev/null &
           CLASS='gnome[-.]terminal' ;;
    # -e, so the shell that would retitle the window on its first prompt is never
    # started (Ubuntu's /etc/skel/.bashrc, the `xterm*|rxvt*)` case)
    *)     setsid nohup xterm -T fwsmoke -e sh -c 'while :; do sleep 3600; done' \
               >/dev/null 2>&1 </dev/null & CLASS=xterm ;;
esac
sleep 6
# Quoted only when the class needs it.  An unquoted bracket expression is a glob
# the guest's shell expands against $HOME before wdotool sees it, which is why
# cinnamon-wayland.sh:146 quotes its `gnome[-.]terminal` -- but every other step
# file asks the PLAIN form (`wdotool search --class foot | head -1`) and
# fake-vmctl matches a recording to a command by longest prefix, so quoting a
# class that does not need it would leave the recording unable to drive the
# replay.  Measured: with `--class 'foot'` recorded, the hypr replay found no
# window at all and both its phases went red.
case $CLASS in
    *[][*?]*) run "wdotool search --class '$CLASS'" ;;
    *)        run "wdotool search --class $CLASS" ;;
esac
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
