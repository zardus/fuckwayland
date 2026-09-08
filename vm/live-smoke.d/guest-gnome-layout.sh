#!/bin/sh
# Reach GNOME input source <code> with the Super+Space GESTURE, in the guest.
#
# The gesture and not gsettings, and this is the caveat the whole layout half of
# the smoke rests on: writing org.gnome.desktop.input-sources mru-sources does
# NOT switch the shell's active source.  Measured 2026-09-08 on GNOME 46/50/51 --
# after such a write wdotool believes German while the compositor is still US and
# types `de> zy Stra-e` instead of `de: yz@ Straße`.  Super+Space is what a user
# presses, so it is what this presses: hold Super, tap Space k times to reach the
# k-th most recently used source, release.  mru-sources is only READ here, as the
# shell's own report of where the gesture landed.
#
#   sh guest-gnome-layout.sh <code>     ->  prints the code reached, exit 0
#                                       ->  prints "unreached:<code>", exit 1
set -u
target=$1
mru() { gsettings get org.gnome.desktop.input-sources mru-sources | cut -d"'" -f4; }
k=1
while [ "$k" -le 5 ]; do
    [ "$(mru)" = "$target" ] && { echo "$target"; exit 0; }
    wdotool keydown super
    sleep 0.3
    i=0
    while [ "$i" -lt "$k" ]; do wdotool key space; sleep 0.4; i=$((i + 1)); done
    wdotool keyup super
    sleep 1.5
    k=$((k + 1))
done
echo "unreached:$(mru)"
exit 1
