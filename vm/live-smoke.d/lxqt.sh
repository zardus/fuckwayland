# live-smoke.d/lxqt.sh -- LXQt 2.3 on Openbox over Xorg (resolute-lxqt), the Lubuntu session.
#
# The handover body is Xfce's, unchanged, for the reason kde-x11.sh gives: handing our argv to the real
# xdotool/wmctrl/xprop/xrandr is a property of the SESSION TYPE and not of the desktop.  Openbox is a
# full EWMH window manager (`wwmctl -m` -> Openbox, four desktops, panel struts in _NET_WORKAREA) and
# every one of those checks passed in the recon's live SDDM+LXQt VM with no code of ours in the path
# [recon2/openbox A].
#
# What is this file's own is `x11root`, and this desktop is where the whole X11-root story comes from.
# Measured in that VM, with an empty environment over ssh, ALL FOUR tools failed:
#
#     Authorization required, but no authorization protocol specified
#     Cannot open display.
#
# because SDDM 0.21 writes the cookie to /tmp/xauth_<random> -- neither ~/.Xauthority nor anything in a
# runtime directory -- and fwcommon/session.py's _SESSION_LEADERS named no LXQt or Openbox process to
# read it from.  With `lxqt-session`, `lxsession`, `openbox` and `labwc` appended to that tuple and
# nothing else touched, all four answered: `find_xauthority(1000)` -> /tmp/xauth_EfcmKF, `wwmctl -l` ->
# the three windows, `wdotool search --name fwsmoke` -> 37748756, `wxrandr --query` -> the Screen 0
# listing, `wxprop -root _NET_CLIENT_LIST` -> the three ids [recon2/openbox A].  That measurement was
# taken by patching a file in a guest; this phase is the same claim against the shipped tuple, on the
# display manager that bites.
#
# EDITOR_CLASS stays xfce.sh's xterm rather than the qterminal Lubuntu ships.  Every check here acts on
# the window `editor_start` opens, and xfce.sh's editor_start is `xterm -T fwsmoke`: naming a class
# nothing starts would be a class nobody matches.  qterminal is on the image (lubuntu-desktop pulls it)
# and the day a phase needs LXQt's own client it can start one; the byte-parity measurement was taken
# on an xterm [recon2/openbox A].
#
# shellcheck source=live-smoke.d/xfce.sh
. "$STEPS/xfce.sh"

SMOKE_PHASES="install passthrough x11root"

# The `-e` form of `editor_start` that this file used to override for itself is xfce.sh's own
# since 2026-09-09 (requests-batch-11.md item 1), so the override is gone and the reason lives
# where the function does.

# F2.3 for an X11 session, which is a different claim from common.sh's phase_root: there the tools look
# for a WAYLAND session, here they have to find a display AND a cookie, and on SDDM the cookie is the
# hard half.  `env -i` is what makes it a measurement rather than an accident -- ssh already hands root
# an environment with no DISPLAY, but it leaves PATH and XDG_* behind; with nothing at all the only
# route to the seated session is the scan of /proc for one of _SESSION_LEADERS' processes owned by that
# user.
phase_x11root() {
    # `env -i` and not `env -i PATH=...`: the tools are zipapps whose shebang is `/usr/bin/env python3`,
    # and with PATH unset execvp falls back to confstr(_CS_PATH) -- /bin:/usr/bin -- which is where the
    # interpreter is.  So an empty environment really is empty here.
    editor_start
    local uu ru orig
    uu=$(guest 'wwmctl -l | wc -l' | tr -d ' \r\n' || true)
    ru=$(root 'w=$(command -v wwmctl); env -i "$w" -l | wc -l' | tr -d ' \r\n' || true)
    # The anchor first: two identical answers prove nothing when both are "no windows".  On this desktop
    # the seated list is never empty anyway -- lxqt-panel and pcmanfm-qt's desktop window are both in
    # _NET_CLIENT_LIST [recon2/openbox A] -- and the xterm above makes it never empty by our own doing.
    if [ "${uu:-0}" -gt 0 ]; then
        same "root with an EMPTY environment lists the seated user's windows" "$uu" "$ru"
    else
        fail "the seated user lists no windows: there is nothing for root's list to be compared against"
    fi
    # The contrast, and the whole point: the original alone, run the same way, cannot open a display.
    orig=$(root 'env -i /usr/bin/wmctrl -l 2>&1' || true)
    want "the real wmctrl, run the same way, cannot open a display" "annot open display|X connection" "$orig"
    # The other three tool families, because the recon measured all four broken and all four fixed by
    # the same four strings -- one working tool would not say the cookie was found.
    want "root with an empty environment finds the fwsmoke window through us" "^[0-9]+$" \
         "$(root 'w=$(command -v wdotool); env -i "$w" search --name fwsmoke 2>&1 | head -1' || true)"
    uu=$(guest "wxrandr --query | awk '\$2==\"connected\"{print \$1}'" | tr '\n' ' ' || true)
    ru=$(root 'w=$(command -v wxrandr); env -i "$w" --query 2>/dev/null | awk "\$2==\"connected\"{print \$1}"' \
         | tr '\n' ' ' || true)
    if [ -n "$(printf '%s' "$uu" | tr -d ' ')" ]; then
        same "root with an empty environment names the same connected outputs" "$uu" "$ru"
    else
        fail "the seated user names no connected output: nothing to compare root's --query against"
    fi
    want "root reads the root window's client list through us" "_NET_CLIENT_LIST\(WINDOW\)" \
         "$(root 'w=$(command -v wxprop); env -i "$w" -root _NET_CLIENT_LIST 2>&1' || true)"
    # And the cookie itself, named: SDDM's /tmp/xauth_<random> is what makes this desktop the one the
    # tuple exists for.  A note rather than a check -- the file name is random and the claim above is
    # about the four answers, not about a path -- but a reader of a red run wants to see it.
    note "the seated session's cookie: $(guest 'echo $XAUTHORITY' | tr -d '\r' || true)"
    note "the leaders root could have learned it from: $(root 'ps -o comm= -u test | sort -u | tr "\n" " "' || true)"
    guest "pkill xterm; true" >/dev/null 2>&1 || true
}
