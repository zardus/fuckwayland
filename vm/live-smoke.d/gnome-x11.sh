# live-smoke.d/gnome-x11.sh -- GNOME Shell 46 on XORG (noble-gnome-x11), the session 24.04's gear menu
# offers and 26.04 no longer has.
#
# The handover body is Xfce's, unchanged, for the reason kde-x11.sh gives: handing our argv to the real
# xdotool/wmctrl/xprop/xrandr is a property of the SESSION TYPE and not of the desktop.  Here that is
# the whole product claim under its hardest condition: org.gnome.Shell, org.gnome.Mutter.DisplayConfig
# AND org.fuckwayland.Bridge are all on this session's bus, every one of them a route this toolbox
# knows, and the answer is still the X server's -- because passthrough.session_kind() is asked before
# any backend is detected [M recon2/gnome-xorg.md 2, against a simulated bus owning all three].
#
# Four phases only this flavor can run:
#
#   * `bridge`.  GNOME-on-Xorg is the GNOME where the bridge needs no logout: `gnome-shell --replace`
#     hands the WM role to a second shell over the same X server and the extension comes back with it.
#     On Wayland gnome-shell IS the display server, so --replace ends the session; a reload with no
#     logout is NOT YET done there -- the route is code installed into the compositor (AGENTS.md route
#     3: the bridge re-exec'ing the shell's extension from inside it) and, failing that, a patched
#     gnome-shell (route 6).  gnome/install-bridge.sh's failure text asks for a logout today; nothing
#     has ever run the X11 move.
#   * `display`.  Two oracles that must agree: real xrandr, and org.gnome.Mutter.DisplayConfig, which
#     mutter 46 serves from MetaMonitorManagerXrandr on this very session (`strings` of libmutter-14:
#     MetaBackendX11Cm and meta-monitor-manager-xrandr.c are both in it, and both are gone from
#     libmutter-18 [M recon2/gnome-xorg.md 1]).  A tool that picked its backend by bus name would take
#     the D-Bus route on a session where xrandr is the truth.
#   * `root`.  GDM keeps an X11 session's cookie at <runtime dir>/gdm/Xauthority -- one directory below
#     where find_xauthority() used to look.  With the session leader gone it answered None although the
#     file was there [M recon2/gnome-xorg.md 3]; the glob that fixes it is in the tree and this is where
#     it meets a real GDM.
#   * `uinput`.  On X11 the events our own code writes into /dev/uinput are consumed by the X SERVER,
#     which is the one input path that differs from Wayland only in who reads it.  Nobody has run it:
#     the recon container had no /dev/uinput at all.
#
# The editor is gnome-text-editor, which ubuntu-desktop puts on the image, so this flavor's package set
# stays byte-identical to noble-gnome's -- the pair is a controlled experiment and one extra package
# would be one uncontrolled variable.  WM_CLASS is org.gnome.TextEditor here exactly as it is under
# Wayland, so EDITOR_CLASS is TextEditor and not gnome-text-editor.
#
# shellcheck source=live-smoke.d/xfce.sh
. "$STEPS/xfce.sh"

SMOKE_PHASES="install passthrough bridge display root uinput"
EDITOR_CLASS=TextEditor
BRIDGE_UUID=fuckwayland-bridge@fuckwayland
BR='--session -d org.fuckwayland.Bridge -o /org/fuckwayland/Bridge -m org.fuckwayland.Bridge1'

# xfce.sh's editor is an xterm, which this image does not carry.  A GUI started in the foreground over
# ssh never returns: detach it.
editor_start() {
    guest "setsid nohup gnome-text-editor $SMOKE_FILE >/dev/null 2>&1 </dev/null & sleep 3; true" >/dev/null || true
}
editor_save() { guest "wdotool key ctrl+s" >/dev/null || true; }

# The editor window's X id, as the REAL wmctrl prints it: the anchor several phases need and the one
# number both planes agree on.
editor_xid() {
    guest "wmctrl -l -x | awk '/TextEditor|Text Editor/ { print \$1; exit }'" | tr -d ' \r\n'
}

# The bridge, reloaded into a running shell.  A reload elsewhere costs a logout; here it does not, and
# that is the claim -- plus the one that matters more: the four tools go on handing over while the bridge
# holds its name, because the session type decided that before any bus name was read.
phase_bridge() {
    note "gnome-shell $(guest 'gnome-shell --version' | tr -d '\r\n' || true), session $(guest \
         'echo $XDG_SESSION_TYPE' | tr -d '\r' || true)"
    local pid0 pid1 info owned ownerpid ours oid xid
    # A window first: it is the thing that must still be there after a second shell takes the WM role,
    # and phase_install's reboot may have left none.
    editor_start
    # The move is made on EVERY run and not only when the extension is found disabled.  A bridge that is
    # already ACTIVE because the image booted into it says nothing at all about `--replace`, and the
    # check below is named after `--replace`: the pid before and after is what makes it a measurement.
    pid0=$(guest 'pgrep -x gnome-shell | head -1' | tr -d ' \r\n' || true)
    guest "gnome-extensions enable $BRIDGE_UUID" >/dev/null 2>&1 || true
    # THE X11-only move: a second gnome-shell takes the WM role over the same X server, and the session's
    # windows, buses and cookie stay exactly where they were.
    guest "setsid nohup gnome-shell --replace >/dev/null 2>&1 </dev/null & sleep 12; true" \
        >/dev/null 2>&1 || true
    pid1=$(await 60 '^[0-9]+$' 'pgrep -x gnome-shell | head -1' | grep -E '^[0-9]+$' | head -1 || true)
    if [ -n "$pid0" ] && [ -n "$pid1" ] && [ "$pid0" != "$pid1" ]; then
        pass "gnome-shell --replace put a NEW shell on this X session (pid $pid0 -> $pid1)"
    else
        fail "no second shell took over [before '$pid0', after '$pid1']: every line below would be about \
the session and not about --replace"
    fi
    info=$(await 40 'State: ACTIVE' "gnome-extensions info $BRIDGE_UUID" || true)
    want "the bridge is ACTIVE in the shell that replaced the old one, with nobody logged out" \
         "State: ACTIVE" "$info"
    owned=$(guest "gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
                   --method org.freedesktop.DBus.ListNames 2>&1 | tr ',' '\n' | grep fuckwayland" || true)
    want "org.fuckwayland.Bridge is owned on this Xorg session" "org.fuckwayland.Bridge" "$owned"
    # Owned by WHICH process: the name has to be held by the shell that came up, not left behind by the
    # one that went away.  `GetConnectionUnixProcessID` is the bus's own answer to that question.
    ownerpid=$(guest "gdbus call --session --dest org.freedesktop.DBus \
                      --object-path /org/freedesktop/DBus \
                      --method org.freedesktop.DBus.GetConnectionUnixProcessID org.fuckwayland.Bridge \
                      2>&1 | sed -n 's/.*uint32 \([0-9]*\).*/\1/p'" | tr -d ' \r\n' || true)
    if [ -n "$pid1" ] && [ -n "$ownerpid" ]; then
        same "...and the name is held by that new shell's own process" "$pid1" "$ownerpid"
    else
        fail "nothing holds org.fuckwayland.Bridge [shell '$pid1', owner '$ownerpid']"
    fi
    want "the bridge answers GetVersion" "uint32|[0-9]" "$(guest "gdbus call $BR.GetVersion 2>&1" || true)"
    # And now the point of the whole phase: with all three GNOME names on the bus, the tools are still
    # the originals.  `--version` is the shortest proof -- the answer is the INSTALLED xdotool's version
    # string (3.x on Ubuntu), never our 4.20260303.1.
    want "wdotool still hands over while the bridge is owned" "^xdotool version 3\." \
         "$(guest 'wdotool --version' || true)"
    want "...and wxrandr still says x11, not mutter" "^x11$" \
         "$(guest 'wxrandr --print-backend' | tr -d ' \r' || true)"
    # The route the bridge exists for is reachable when it is asked for by name, which is what
    # `FUCKWAYLAND_PASSTHROUGH=never` means: a GNOME backend over a session whose windows are X windows.
    ours=$(guest 'FUCKWAYLAND_PASSTHROUGH=never wwmctl -l -x 2>&1' || true)
    want "forced onto our own code, the bridge lists this session's windows" "^0x[0-9a-f]+ " "$ours"
    # The same window on both planes, by the number both of them print.  The bridge hands out X ids on an
    # Xorg session [M recon2/gnome-xorg.md 2]; the real wmctrl is the oracle for what that id is, and the
    # editor is the window that survived the replace above.
    oid=$(printf '%s\n' "$ours" | awk '/TextEditor|Text Editor/ { print $1; exit }' | tr -d ' \r\n')
    xid=$(editor_xid)
    if [ -n "$xid" ]; then
        same "...and prints the editor under the X id the real wmctrl prints for it" "$xid" "$oid"
    else
        fail "the real wmctrl named no editor window: nothing for the bridge's id to be compared against"
    fi
    note "through the bridge: $(ev "$ours")"
    note "through the real wmctrl: $(ev "$(guest 'wmctrl -l -x' || true)")"
}

# The X server is authoritative here; DisplayConfig is a second opinion that has to agree with it.
phase_display() {
    common_display_phase
    want "wxrandr --backends lists mutter as available on this Xorg session" "mutter +available" \
         "$(guest 'wxrandr --backends' || true)"
    # oracle.py keyed on `gnome` is the DisplayConfig reader; keyed on this flavor's own token it is
    # xrandr.  One guest, two readers, and the claim is that they say the same thing.
    local x11 mutter
    x11=$(oracle_outputs | sort | tr '\n' ' ' || true)
    mutter=$(guest "python3 $ORACLE gnome" | grep -E '^[A-Za-z]' | sort | tr '\n' ' ' || true)
    if [ -n "$(printf '%s' "$x11" | tr -d ' ')" ]; then
        same "org.gnome.Mutter.DisplayConfig agrees with xrandr, output for output" "$x11" "$mutter"
    else
        fail "xrandr named no output: there is nothing for DisplayConfig to be compared against"
    fi
    # The overlap pair.  X places overlapping monitors natively -- that is why --unsafe-gnome-overlap
    # refuses an X11 session in the first place -- and Mutter's own config manager refuses the same
    # layout with `Logical monitors not adjacent`, from meta-monitor-config-manager.c, which is backend
    # independent and present in libmutter-14 [M recon2/gnome-xorg.md 9].  Whether the mutter backend
    # can be made to REFUSE it on Xorg is the one thing the recon could not measure at all.
    local pair first second pos0
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    if [ -n "$second" ]; then
        pos0=$(opos "$second")
        guest "wxrandr --output $second --pos $(opos "$first" | tr ',' 'x')" >/dev/null 2>&1 || true
        sleep 2
        same "an overlapping layout is accepted through the X server, which places one natively" \
             "$(opos "$first")" "$(opos "$second")"
        xwant "--backend mutter refuses the same layout in Mutter's own words (until noble-gnome-x11 runs once)" \
              "not adjacent" \
              "$(guest "wxrandr --backend mutter --output $second --pos $(opos "$first" | tr ',' 'x') 2>&1" || true)"
        guest "wxrandr --output $second --pos $(printf '%s' "$pos0" | tr ',' 'x')" >/dev/null 2>&1 || true
        sleep 2
        same "...and the layout is put back where the phase found it" "$pos0" "$(opos "$second")"
    fi
    # U18: the status query answers before any handover, so on an X11 session it used to point at an
    # extension nobody needs here.  It names the session now, in the sentence wxrandr/cli.py:1073-1075
    # writes -- the whole sentence and not the token `x11`, which any of the old answers could carry.
    local st; st=$(guest 'wxrandr --gnome-overlap-status 2>&1' || true)
    want "--gnome-overlap-status says this session is x11, in the words the tool prints" \
         "this session is x11, which places overlapping monitors" "$st"
    # And what must never come back: the pre-U18 answer pointed at a shell extension on a session that
    # has no shell in the path at all [M recon2/gnome-xorg.md 7].
    wantnot "...and never sends an X11 user after the overlap extension" \
            "overlap extension is not running" "$st"
}

# F2.3 for an X11 session, which is a different claim from common.sh's phase_root: there the tools look
# for a WAYLAND session, here they have to find a display and a cookie -- and GDM's cookie is the one
# that lives a directory down, at <runtime dir>/gdm/Xauthority.
phase_root() {
    # `env -i` and not `env -i PATH=...`: the tools are zipapps whose shebang is `/usr/bin/env python3`,
    # and with PATH unset execvp falls back to confstr(_CS_PATH) -- /bin:/usr/bin -- which is where the
    # interpreter is.
    editor_start
    local uu ru orig
    uu=$(guest 'wwmctl -l | wc -l' | tr -d ' \r\n' || true)
    ru=$(root 'w=$(command -v wwmctl); env -i "$w" -l | wc -l' | tr -d ' \r\n' || true)
    if [ "${uu:-0}" -gt 0 ]; then
        same "root with an EMPTY environment lists the seated user's windows" "$uu" "$ru"
    else
        fail "the seated user lists no windows: there is nothing for root's list to be compared against"
    fi
    orig=$(root 'env -i /usr/bin/wmctrl -l 2>&1' || true)
    want "the real wmctrl, run the same way, cannot open a display" "annot open display|X connection" "$orig"
    uu=$(guest "wxrandr --query | awk '\$2==\"connected\"{print \$1}'" | tr '\n' ' ' || true)
    ru=$(root 'w=$(command -v wxrandr); env -i "$w" --query 2>/dev/null | awk "\$2==\"connected\"{print \$1}"' \
         | tr '\n' ' ' || true)
    if [ -n "$(printf '%s' "$uu" | tr -d ' ')" ]; then
        same "root with an empty environment names the same connected outputs" "$uu" "$ru"
    else
        fail "the seated user names no connected output: nothing to compare root's --query against"
    fi
    # The file the glob was added for, named: this is the one display manager that keeps it here.
    note "GDM's cookie: $(root 'ls -l /run/user/1000/gdm/Xauthority 2>&1' | tr -d '\r' || true)"
    note "the seated session's own XAUTHORITY: $(guest 'echo $XAUTHORITY' | tr -d '\r' || true)"
    # And the harder half of the same claim: with gnome-shell's environ unreadable the glob is the ONLY
    # route left, which is what the recon measured going to None.  `pkill -STOP` would stop the session;
    # naming the file is as far as a smoke should go, so the check is that root reads it directly.
    want "root can read that cookie and talk to the display with it" "^[0-9]+$" \
         "$(root 'w=$(command -v wdotool); env -i "$w" search --class TextEditor 2>&1 | head -1' || true)"
}

# The input path X and Wayland share up to the kernel: our own uinput devices, created UNPRIVILEGED
# under the package's udev rule, with the X server as the consumer instead of the compositor.
phase_uinput() {
    want "the package's rule grants the seated user /dev/uinput" "^user:[a-z]" \
         "$(root 'getfacl -p /dev/uinput 2>/dev/null' || true)"
    local out win
    editor_start
    out=$(await 30 '[0-9]' "wdotool search --class $EDITOR_CLASS | head -1" || true)
    win=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$win" ]; then fail "no $EDITOR_CLASS window to type into [$(ev "$out")]"; return 1; fi
    guest "wdotool windowactivate --sync $win" >/dev/null 2>&1 || true
    guest "rm -f $SMOKE_FILE; touch $SMOKE_FILE" >/dev/null || true
    # Forced onto our own code, because the handover would type this with XTEST and prove nothing about
    # uinput.  No sudo anywhere in the line: the whole point of the rule is that there is none.
    out=$(guest "FUCKWAYLAND_PASSTHROUGH=never wdotool type --delay 30 -- 'x11 uinput' 2>&1" || true)
    wantnot "wdotool creates its uinput devices without sudo" "cannot create uinput devices" "$out"
    sleep 1
    editor_save
    sleep 1
    # An `xwant`: whether the X server picks the events off our virtual keyboard and delivers them to
    # the focused window has never been run -- the recon's container had no /dev/uinput at all
    # [M recon2/gnome-xorg.md 2].  The day it passes this line is promoted to a `same`.
    xwant "the keystrokes reach the focused X window (until noble-gnome-x11 runs once)" \
          "x11 uinput" "$(editor_text || true)"
    note "the editor holds: $(ev "$(editor_text || true)")"
}
