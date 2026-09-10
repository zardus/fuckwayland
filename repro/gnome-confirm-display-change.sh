#!/bin/sh
# ConfirmDisplayChange against the real "Keep these display settings?" dialog.
#
#   vmctl user <vm> -- sh /tmp/gnome-confirm-display-change.sh
#
# Needs two heads, `wxrandr` on PATH, and the bridge installed *and loaded*
# (gnome/install-bridge.sh, then one re-login -- on these guests a reboot,
# because a GNOME logout parks the VM at the greeter).  The five cases below
# are the whole claim: a persistent apply raises a dialog that reverts the
# layout in 20 s unless it is answered, the bridge can answer it either way,
# answering it when there is nothing to answer changes nothing, and its own
# buttons still work after the bridge has pressed one.  Most cases outlive one
# countdown, so this takes about four minutes.
#
# From the host, a second or two after case 1 says the dialog is up (the shot
# is of the framebuffer, so give the modeset time to land):
#     vmctl shot <vm> 0 dialog.png
# -- the dialog itself is the one thing this script cannot show you.
set -u

MX="${XDG_CONFIG_HOME:-$HOME/.config}/monitors.xml"
BR='--session -d org.w11.Bridge -o /org/w11/Bridge -m org.w11.Bridge1'

q() { wxrandr --query 2>/dev/null; }
# `<name> connected [primary] <W>x<H>+<X>+<Y> (...)`: the geometry field.
geom() { q | awk -v o="$1" '$1==o && $2=="connected" {
             for (i=3; i<=NF; i++) if ($i ~ /^[0-9]+x[0-9]+\+/) { print $i; exit } }'; }
mode() { geom "$1" | sed 's/+.*//'; }
# every mode wxrandr lists under one output
modes() { q | awk -v o="$1" '
             $1==o && $2=="connected" { in_it=1; next }
             /^[^ \t]/ { in_it=0 }
             in_it && $1 ~ /^[0-9]+x[0-9]+$/ { print $1 }'; }
sum() { md5sum "$MX" 2>/dev/null | cut -d' ' -f1 || true; }
saved() { if [ -e "$MX" ]; then echo "monitors.xml $(sum) ($(wc -c <"$MX") bytes)";
          else echo "monitors.xml absent"; fi; }
confirm() { gdbus call $BR.ConfirmDisplayChange "$1" 2>&1; }
apply() { wxrandr --output "$OUT2" --mode "$1" --right-of "$OUT1" --persistent 2>&1 |
          sed 's/^/      /'; }
state() { echo "      $OUT1 $(geom "$OUT1")   $OUT2 $(geom "$OUT2")   $(saved)"; }

if ! gdbus call $BR.GetVersion >/dev/null 2>&1; then
    echo "the bridge is not on the session bus: install it and re-login first" >&2
    exit 2
fi
OUT1=$(q | awk '$2=="connected" {print $1; exit}')
OUT2=$(q | awk '$2=="connected" {n++} n==2 {print $1; exit}')
[ -n "${OUT2:-}" ] || { echo "this needs two connected outputs" >&2; exit 2; }

# Two modes neither of which is the one on screen now, so every case below is a
# real modeset and `--persistent` never short-circuits. Nearest in area to what
# is on screen, and smaller before larger: on a real monitor this should be a
# modest change, not the biggest mode the EDID happens to advertise.
NOW=$(mode "$OUT2")
set -- $(modes "$OUT2" | awk -v now="$NOW" '
             { split($1, d, "x"); a = d[1] * d[2]
               if ($1 == now) { cur = a } else { area[$1] = a } }
             END { for (m in area) { g = cur - area[m]
                       print (g >= 0 ? "0 " g : "1 " -g), m } }' |
         sort -k1,1n -k2,2n | head -2 | awk '{print $3}')
A=${1:-}; B=${2:-}
[ -n "$A" ] && [ -n "$B" ] || { echo "$OUT2 has too few modes to test with" >&2; exit 2; }

echo "outputs: $OUT1 (left) and $OUT2 (right), $OUT2 at $NOW, testing with $A and $B"
echo "bridge:  $(gdbus call $BR.GetVersion)"
echo
BEFORE=$(saved)
echo "1. persistent apply, nothing answers the dialog"
echo "   before:"; state
apply "$A"
echo "   applied (the dialog is up now -- screendump it from the host):"; state
echo "   ...waiting out the 20 s countdown"; sleep 26
echo "   after the countdown, with nothing pressed -- expect $NOW back and the"
echo "   saved configuration as it was ($BEFORE):"
state
echo
echo "2. ConfirmDisplayChange(true): keep it"
apply "$A"
echo "   applied:"; state
echo "   ConfirmDisplayChange(true) -> $(confirm true)   (expect true: a dialog was pressed)"
sleep 2
echo "   answered -- expect $A kept and monitors.xml written:"; state
echo "   ...waiting past where the countdown would have reverted it"; sleep 24
echo "   expect $A still there:"; state
KEPT=$(sum)
echo
echo "3. ConfirmDisplayChange(false): put it back"
apply "$B"
echo "   applied:"; state
echo "   ConfirmDisplayChange(false) -> $(confirm false)  (expect true)"
sleep 2
echo "   answered -- expect $A back and monitors.xml untouched ($KEPT):"; state
echo "   ...waiting past the countdown"; sleep 24
echo "   expect $A still, file still $KEPT:"; state
echo
echo "4. no dialog on screen at all -- assumption 2, that this is harmless"
echo "   before:"; state
i=0
while [ $i -lt 4 ]; do
    echo "   ConfirmDisplayChange(true)  -> $(confirm true)   (expect false)"
    echo "   ConfirmDisplayChange(false) -> $(confirm false)  (expect false)"
    i=$((i + 1))
done
echo "   after -- expect nothing moved and nothing written:"; state
echo "   the shell is still answering: $(gdbus call $BR.GetVersion)"
echo
echo "5. the dialog's own buttons still work after the bridge pressed one"
apply "$B"
echo "   applied:"; state
if ! command -v wdotool >/dev/null 2>&1; then
    echo "   no wdotool here: press Escape on the guest's screen, or from the host"
    echo "   run  vmctl ssh <vm> -- wdotool key Escape  within the next 20 s"
elif wdotool key Escape 2>/tmp/wdotool-escape.err; then
    echo "   pressed Escape, which is the dialog's own Revert Settings key"
else
    echo "   wdotool could not inject ($(head -1 /tmp/wdotool-escape.err)):"
    echo "   from the host, vmctl ssh <vm> -- wdotool key Escape  within 20 s"
fi
sleep 4
echo "   expect $A back (Escape = revert) and monitors.xml still $KEPT:"; state
echo "   ...waiting past the countdown"; sleep 22
echo "   expect the same again:"; state
