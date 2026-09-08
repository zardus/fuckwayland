# live-smoke.d/kde-x11.sh -- Plasma on Xorg (noble-kde-x11 / resolute-kde-x11).
#
# The handover is a property of the session type and not of the desktop, so the
# checks are Xfce's, unchanged; what differs is only which greeter got there and
# that libkscreen's XRandR backend answers `kscreen-doctor -o` here too (the
# oracle stays xrandr --query, which is what the X server itself will say).
# Neither golden is built on this host yet -- `vm/vmctl build resolute-kde-x11`,
# ~7 min -- so this file has not been exercised live; it is the step list T66
# names, ready for the day the golden exists.
# shellcheck source=live-smoke.d/xfce.sh
. "$STEPS/xfce.sh"
EDITOR_CLASS=konsole
