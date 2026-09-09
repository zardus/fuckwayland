# live-smoke.d/lxqt-wayland.sh -- LXQt 2.3's Wayland session, which is labwc under a panel.
#
# This file is three assignments and a source, and that IS the measurement: LXQt on Wayland
# advertises byte-for-byte the globals a bare labwc does (the recon diffed the two registry
# dumps and found no difference), has no IPC socket and no D-Bus control surface, and every
# tool answered on it exactly as on labwc -- `wxrandr --print-backend --verbose` printing
# `wlr` / `compositor: wlroots` / `zwlr_output_manager_v1 version 4`, rotate and fractional
# scale and off/auto applying, `wdotool --vkbd on` typing with no privilege on a box with no
# /dev/uinput [recon2/openbox B, recon2/xfce-wayland 1].  So the checks are labwc.sh's, and a
# check that has to differ between the two would be a discovery, not a maintenance chore.
#
# What differs is the terminal.  Lubuntu ships qterminal and not foot, and `-e` is what makes
# it run the rest of the line.  qterminal's app_id under Wayland is `qterminal`, which is what
# `wdotool search --class` matches.
#
# The other difference is on the rig side and not here: SDDM resolves an autologin session
# name against /usr/share/wayland-sessions FIRST, and `lxqt` exists in BOTH directories on
# this image (resolute-lxqt is the X11 half), so only the `-wayland` suffix keeps the two
# apart -- vm/flavors/resolute-lxqt-wayland.yaml says so at length, and dm_sddm's
# no_session_ambiguity guard is what would catch it going wrong.
EDITOR_CLASS=qterminal
EDITOR_TERM=qterminal
EDITOR_TERM_EXEC=-e

# shellcheck source=live-smoke.d/labwc.sh
. "$STEPS/labwc.sh"
