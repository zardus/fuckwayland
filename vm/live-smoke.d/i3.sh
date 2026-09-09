# live-smoke.d/i3.sh -- i3 4.25.1 on Xorg (resolute-i3), the tiling window manager README has named
# since before anybody ran one.
#
# The handover body is Xfce's, unchanged, for the reason kde-x11.sh gives: handing our argv to the real
# xdotool/wmctrl/xprop/xrandr is a property of the SESSION TYPE and not of the desktop.  On i3 that
# sentence has a second half worth the flavor on its own: there IS an i3 IPC socket sitting right there,
# a socket this toolbox speaks, and the session is still X11, so the four tools still become the
# originals.  `WDOTOOL_BACKEND=sway` does not defeat that either -- the policy is settled by session
# type before any backend is detected [M recon2/i3.md 2a].
#
# `i3ipc` is what is underneath, reached only by FUCKWAYLAND_PASSTHROUGH=never, and it is where this
# flavor earns its build.  Against a real 4.25.1 the sway backend used to call itself sway and get four
# things wrong, every one of them measured [M recon2/i3.md 2b, 2c]:
#
#   * the socket: i3 writes $XDG_RUNTIME_DIR/i3/ipc-socket.<pid> -- one directory DOWN -- and exports
#     $I3SOCK into the processes it spawns but never into its own environ, so find_sway_socket() (which
#     scanned one level for a name no i3 or sway has ever written, `i3-ipc.*.sock`) answered None;
#   * the ids: i3's con ids are 47-bit pointers (`97479943571072`), and `wxprop -id` on one truncated to
#     `0x5168c680` and answered BadWindow;
#   * the floating flag: i3 wraps a floating view in a `floating_con`, the walk reset the flag on the
#     way down, and every `windowmove` refused with "cannot move a tiled window" on a window i3 itself
#     moved happily;
#   * `getdisplaygeometry`: it asked the wl_output box and refused with "no Wayland session found" on a
#     session whose outputs the same process was already reading over GET_OUTPUTS.
#
# Every one of those is fixed in the tree (U01-U06), and this phase is the same measurement against a
# real session instead of an Xvfb -- the only place the runtime-dir socket layout and the display
# manager's cookie are the real ones.  It ran on 2026-09-09 on the resolute-i3 golden: 18 checks, none
# red, the socket at /run/user/1000/i3/ipc-socket.1434 found with $I3SOCK unset, the X id 10485774 where
# i3's con id is a 47-bit pointer, the floating view moving 2640,398 -> 125,159, `getdisplaygeometry`
# answering `3840 1080` across two heads, and `compositor: i3 4.25.1 (2026-02-06)`
# (vm/live-smoke.out/resolute-i3-20260909-022725.log).  The recording next to it,
# tests/fixtures/live/resolute-i3-4.25.1-i3ipc-replay.txt, is a SECOND run of the same commands on the
# same guest a minute later (02:28:30), and its move reads 720,398 -> 125,159 -- the same window, opened
# on the other head that time (the layout is 3840x1080 over two 1920s, so 2640 is on the second head and
# 720 on the first).  Each check runs with `env -u I3SOCK` on purpose: with the variable set, the scan is
# never asked.
#
# T21 (wmirror naming X11 before the helper line) stays XFAIL here for the reason it is XFAIL on Xfce:
# on this image `wl-mirror` is not installed, so the `helper: not installed` line comes first
# [M recon2/i3.md 2a].
#
# shellcheck source=live-smoke.d/xfce.sh
. "$STEPS/xfce.sh"

SMOKE_PHASES="install passthrough i3ipc"

# xfce.sh's `xterm -T fwsmoke` does not keep that title on these images: the test user's ~/.bashrc --
# Ubuntu's /etc/skel/.bashrc, the `xterm*|rxvt*)` case, which prepends `\[\e]0;\u@\h: \w\a\]` to PS1 --
# retitles the window on the first prompt, so WM_NAME became `test@resolute-i3-smoke: ~` a second after
# the window appeared and `wdotool search --name fwsmoke` matched nothing at all (measured on the first
# resolute-i3 run, 2026-09-09: `wmctrl -l` showed the prompt as the title; /etc/bash.bashrc's own
# PROMPT_COMMAND for the same case is commented out on Ubuntu and is not what did it).  With `-e` there
# is no interactive shell to rewrite it, so the title xterm is given is the title it keeps.
editor_start() {
    guest "setsid nohup xterm -T fwsmoke -e sh -c 'while :; do sleep 3600; done' \
           >/dev/null 2>&1 </dev/null & sleep 2; true" >/dev/null || true
}

#: What the phase below runs its tools with: our own code, and no environment variable handing it the
#: socket.  `never` is the documented escape hatch; `env -u I3SOCK` is what makes the discovery a claim.
OURS="FUCKWAYLAND_PASSTHROUGH=never env -u I3SOCK"

phase_i3ipc() {
    editor_start
    local out win hexid
    # The handover first, on a session that has an IPC socket we speak: `--version` is the shortest
    # proof, because the answer is the INSTALLED xdotool's version string and not ours (4.20260303.1).
    note "i3 --get-socketpath: $(guest 'i3 --get-socketpath' | tr -d '\r' || true); \
I3SOCK=$(guest 'echo $I3SOCK' | tr -d '\r' || true)"
    want "an i3 IPC socket does not defeat the handover: --version is the installed xdotool's" \
         "^xdotool version 3\." "$(guest 'wdotool --version' || true)"
    out=$(await 30 '[0-9]' "wdotool search --name fwsmoke | head -1" || true)
    win=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$win" ]; then fail "no fwsmoke xterm to work on [$(ev "$out")]"; return 1; fi
    pass "wdotool search --name fwsmoke -> $win (through the real xdotool)"

    # 1. The socket, found by the scan alone.  A window id back is the whole proof: without a socket the
    # backend cannot be built at all and the tool exits 2 naming every route it tried.
    local ours; ours=$(guest "$OURS wdotool search --name fwsmoke 2>&1 | head -1" || true)
    want "find_sway_socket() finds \$XDG_RUNTIME_DIR/i3/ipc-socket.* with no \$I3SOCK" "^[0-9]+$" "$ours"
    # 2. ...and the id it hands back is the X id, not a 47-bit pointer.  The number is compared against
    # 2^32 rather than against the X id, because what must never come back is the truncation.
    if printf '%s' "$ours" | grep -Eq '^[0-9]+$' && [ "$ours" -lt 4294967296 ]; then
        pass "the id is an X id and not i3's 47-bit con pointer ($ours < 2^32)"
    else
        fail "the id $ours is i3's con id: every X-shaped consumer of it truncates to 32 bits"
    fi
    same "...and it is the same window the real xdotool found" "$win" "$ours"
    # 3. Which is what makes wxprop answer at all: on the con id it used to be BadWindow.
    hexid=$(printf '0x%08x' "$ours" 2>/dev/null || echo "$ours")
    out=$(guest "$OURS wxprop -id $hexid WM_CLASS 2>&1" || true)
    want "wxprop -id <the id wdotool minted> reads the window's class" '"xterm", "XTerm"' "$out"
    wantnot "...and does not answer BadWindow" "BadWindow" "$out"
    # 4. The pid i3's tree does not carry, filled in off _NET_WM_PID on the X plane.
    want "getwindowpid answers where i3's tree has no pid field" "^[0-9]+$" \
         "$(guest "$OURS wdotool getwindowpid $ours 2>&1 | head -1" || true)"
    # 5. --onlyvisible, which matched nothing at all because i3 nodes carry no `visible` key.
    want "search --onlyvisible matches the window on the visible workspace" "^[0-9]+$" \
         "$(guest "$OURS wdotool search --onlyvisible --name fwsmoke 2>&1 | head -1" || true)"

    # 6. The floating window that would not move.  i3 itself moves it (the same criteria, run by i3-msg,
    # is the control), so a refusal here is ours and nobody else's.
    guest "i3-msg '[title=\"fwsmoke\"] floating enable'" >/dev/null 2>&1 || true
    sleep 1
    local g0 g1 mv
    g0=$(guest "wdotool getwindowgeometry $win" | tr -d '\r' | tr '\n' ' ' || true)
    mv=$(guest "$OURS wdotool windowmove $ours 120 140 2>&1" || true)
    sleep 1
    g1=$(guest "wdotool getwindowgeometry $win" | tr -d '\r' | tr '\n' ' ' || true)
    wantnot "windowmove on a floating i3 view is not refused as tiled" "cannot move a tiled window" "$mv"
    if [ "$g0" = "$g1" ]; then
        fail "windowmove left the window where it was [$(ev "$g0") -> $(ev "$g1"), said: $(ev "$mv")]"
    else
        pass "windowmove moved it: $(ev "$g0") -> $(ev "$g1")"
    fi
    guest "i3-msg '[title=\"fwsmoke\"] floating disable'" >/dev/null 2>&1 || true

    # 7. getdisplaygeometry, which used to refuse on a session with a perfectly good layout.
    out=$(guest "$OURS wdotool getdisplaygeometry 2>&1" || true)
    want "getdisplaygeometry answers off GET_OUTPUTS" "^[0-9]+ [0-9]+$" "$out"
    wantnot "...and never says there is no session on a session it is talking to" \
            "no Wayland session found" "$out"

    # 8. wxrandr: the backend is reachable here and must say whose it is, and refuse an apply in one
    # line -- i3 has no `output` command at all, and every apply died with a 30-token parse error.
    local vb; vb=$(guest "$OURS wxrandr --backend sway --print-backend --verbose 2>&1" || true)
    want "--print-backend --verbose names the compositor i3 and its version" "compositor: i3 4\." "$vb"
    wantnot "...and does not call i3 sway" "compositor: sway" "$vb"
    out=$(guest "$OURS wxrandr --backend sway --output Virtual-1 --pos 0x0 2>&1" || true)
    want "an apply is one line naming i3 and the X server that owns the layout" \
         "i3, which has no output command" "$out"
    wantnot "...and not the compositor's 30-token parse error" "Expected one of these tokens" "$out"

    # 9. Byte parity where our own code answers it: `wwmctl -m` through the sway backend against the
    # real wmctrl's bytes.  Measured identical on the live 4.25.1 [M recon2/i3.md 2b].
    local a b
    a=$(guest "$OURS wwmctl -m 2>&1" | tr -d '\r' || true)
    b=$(guest "wmctrl -m 2>&1" | tr -d '\r' || true)
    same "wwmctl -m over the i3 IPC is byte-identical to wmctrl -m over EWMH" "$b" "$a"
    # The one wording this file has no fix in flight for: a refusal our sway backend prints on i3 still
    # opens with `sway:`.  Recorded as evidence, not asserted, so that the day it is reworded the note
    # says so instead of a check going red.
    note "a tiled refusal still opens with the backend's own name: \
$(ev "$(guest "$OURS wdotool windowsize $ours 400 300 2>&1" || true)")"
    guest "pkill xterm; true" >/dev/null 2>&1 || true
}
