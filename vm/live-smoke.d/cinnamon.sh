# live-smoke.d/cinnamon.sh -- Cinnamon 6.4.13 on X11 (resolute-cinnamon), the default session.
#
# The handover body is Xfce's, unchanged, for the reason kde-x11.sh gives: handing our argv to the real
# xdotool/wmctrl/xprop/xrandr is a property of the SESSION TYPE and not of the desktop.  What is added
# here are the two things that are Cinnamon's own and nobody else's:
#
#   * `muffin`.  muffin is Mutter's fork, so it answers `Mutter (Muffin)` on the _NET_SUPPORTING_WM_CHECK
#     window (measured on a live 6.4.13), keeps _NET_WM_STATE_SHADED -- which mutter dropped and KWin 6
#     refuses -- in a _NET_SUPPORTED of 70+ atoms, and puts `org.cinnamon.Muffin.DisplayConfig` on the
#     session bus OF AN X11 SESSION, answering GetCurrentState with `'renderer': <'xrandr'>`.  That last
#     one is the trap this phase exists for: it is GNOME-on-Xorg owning org.gnome.Shell wearing Cinnamon's
#     names, and a tool that picked its backend by bus name would take the D-Bus route on a session where
#     xrandr is the truth [recon2/cinnamon 2.2, 3.1].
#   * `x11root`.  common.sh's phase_root is a Wayland claim (root over ssh finds the seated user's
#     session), and xfce.sh's own comment says the X11 half is "a separate measurement nobody has made".
#     This is that measurement: with an EMPTY environment, root has no DISPLAY and no XAUTHORITY, so the
#     tools have to learn both from a process of the seated session -- `cinnamon-session` (comm
#     `cinnamon-sessio`, 16 bytes truncated to 15) and `cinnamon`, both in fwcommon/session.py's
#     _SESSION_LEADERS.  The check is not that we answer, it is that we answer where the ORIGINAL, run
#     the same way, cannot.
#
# The window every check here acts on is xfce.sh's `xterm -T fwsmoke`: the recon's byte-identical
# `wwmctl -lGpx` against `wmctrl -lGpx` was measured on an xterm on this very desktop, and gnome-terminal
# -- which cinnamon-core puts on the image -- is the NATIVE client of the Wayland twin, where it belongs
# [recon2/cinnamon 3.1].
#
# shellcheck source=live-smoke.d/xfce.sh
. "$STEPS/xfce.sh"

SMOKE_PHASES="install passthrough muffin x11root"

# muffin's own name for itself, and the states an EWMH window manager of this generation has.
phase_muffin() {
    editor_start
    local out win
    out=$(await 30 '[0-9]' "wdotool search --name fwsmoke | head -1" || true)
    win=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$win" ]; then fail "no fwsmoke xterm to work on [$(ev "$out")]"; return 1; fi
    pass "wdotool search --name fwsmoke -> $win (through the real xdotool)"
    want "wwmctl -m names the window manager muffin's check window names" "^Name: Mutter \(Muffin\)$" \
         "$(guest 'wwmctl -m' || true)"
    note "muffin --version: $(guest 'muffin --version' | tr -d '\r' || true) (muffin calls itself mutter)"
    # Byte parity, the oracle this whole tree is measured against, taken in one command so that nothing
    # can move between the two reads.  On an X11 session the two are the same binary by construction --
    # which is the claim: the handover happened and nothing mangled the argv on the way.
    local diffrc
    diffrc=$(guest 'wwmctl -lGpx > /tmp/fw-a.txt 2>&1; wmctrl -lGpx > /tmp/fw-b.txt 2>&1;
                    diff /tmp/fw-a.txt /tmp/fw-b.txt >/dev/null 2>&1; echo $?' | tr -d ' \r\n' || true)
    same "wwmctl -lGpx is byte-identical to wmctrl -lGpx" "0" "$diffrc"
    note "the list: $(ev "$(guest 'cat /tmp/fw-a.txt' || true)")"
    # Shading: muffin kept it, so it is a live state here.  wmctrl does the work; what is asserted is
    # that the state STICKS, which is what a script that depends on it needs.
    guest "wwmctl -r fwsmoke -b add,shaded" >/dev/null 2>&1 || true
    sleep 1
    want "-b add,shaded sets _NET_WM_STATE_SHADED (muffin kept what mutter dropped)" \
         "_NET_WM_STATE_SHADED" "$(guest "wxprop -id $win _NET_WM_STATE" || true)"
    guest "wwmctl -r fwsmoke -b remove,shaded" >/dev/null 2>&1 || true
    sleep 1
    wantnot "-b remove,shaded takes it off again" "_NET_WM_STATE_SHADED" \
            "$(guest "wxprop -id $win _NET_WM_STATE" || true)"
    # The trap.  Muffin's DisplayConfig IS on this bus; the tools must still take the X11 route.
    same "org.cinnamon.Muffin.DisplayConfig is on the session bus of this X11 session" "(true,)" \
         "$(guest 'gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
                   --method org.freedesktop.DBus.NameHasOwner org.cinnamon.Muffin.DisplayConfig' \
            | tr -d ' \r\n' || true)"
    same "...and wxrandr still picks x11, because session_kind() is asked before any bus name" "x11" \
         "$(guest 'wxrandr --print-backend' | tr -d ' \r\n' || true)"
    # Both halves of that sentence, because the second is the one that proves the handover: the recon read
    # `x11 / session: x11 / chosen by: detection / compositor: X server (RandR) / real xrandr:
    # /usr/bin/xrandr` here [recon2/cinnamon 3.1], and cli.py:987 prints the `real xrandr:` line only when
    # the x11 probe is available -- i.e. only when there is an original on PATH to hand our argv to.
    local vb; vb=$(guest 'wxrandr --print-backend --verbose' || true)
    want "--print-backend --verbose says the session is x11" "session: x11" "$vb"
    want "...and names the real xrandr it hands over to" "real xrandr: /usr/bin/xrandr" "$vb"
    same "warandr agrees" "x11" "$(guest 'warandr --print-backend' | tr -d ' \r\n' || true)"
}

# F2.3 for an X11 session, which is a different claim from common.sh's phase_root: there the tools look
# for a WAYLAND session, here they have to find a display and a cookie.  `env -i` is what makes it a
# measurement rather than an accident -- ssh already hands root an environment with no DISPLAY, but it
# leaves PATH and XDG_* behind; with nothing at all, the only route to the seated session is the scan of
# /proc for one of _SESSION_LEADERS' processes owned by that user.
phase_x11root() {
    # `env -i` and not `env -i PATH=...`: the tools are zipapps whose shebang is `/usr/bin/env python3`,
    # and with PATH unset execvp falls back to confstr(_CS_PATH) -- /bin:/usr/bin -- which is where the
    # interpreter is.  So an empty environment really is empty here, and nothing is handed to the tool.
    local uu ru orig
    uu=$(guest 'wwmctl -l | wc -l' | tr -d ' \r\n' || true)
    ru=$(root 'w=$(command -v wwmctl); env -i "$w" -l | wc -l' | tr -d ' \r\n' || true)
    # The anchor first: two identical answers prove nothing when both are "no windows".  phase_muffin leaves
    # its fwsmoke xterm up, so the seated user's list is >= 1 whenever this phase runs after it.
    if [ "${uu:-0}" -gt 0 ]; then
        same "root with an EMPTY environment lists the seated user's windows" "$uu" "$ru"
    else
        fail "the seated user lists no windows: there is nothing for root's list to be compared against"
    fi
    # The contrast, and the whole point: the original alone, run the same way, cannot open a display.
    orig=$(root 'env -i /usr/bin/wmctrl -l 2>&1' || true)
    want "the real wmctrl, run the same way, cannot open a display" "annot open display|X connection" "$orig"
    uu=$(guest "wxrandr --query | awk '\$2==\"connected\"{print \$1}'" | tr '\n' ' ' || true)
    ru=$(root 'w=$(command -v wxrandr); env -i "$w" --query 2>/dev/null | awk "\$2==\"connected\"{print \$1}"' \
         | tr '\n' ' ' || true)
    # Same anchor: an X11 session with no connected output is not a thing, so an empty user-side answer means
    # the awk found nothing to compare and the `same` below would pass on two blanks.
    if [ -n "$(printf '%s' "$uu" | tr -d ' ')" ]; then
        same "root with an empty environment names the same connected outputs" "$uu" "$ru"
    else
        fail "the seated user names no connected output: nothing to compare root's --query against"
    fi
    note "the session leader root learned DISPLAY and XAUTHORITY from is one of cinnamon-session/cinnamon:"
    note "  $(root 'ps -o comm= -u test | sort -u | tr "\n" " "' || true)"
}
