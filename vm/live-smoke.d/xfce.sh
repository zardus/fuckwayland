# live-smoke.d/xfce.sh -- Xfce 4.20 on X11 (resolute-xfce), the handover.
#
# On an X11 session every one of the six tools is supposed to become the real
# one: `execve` the distribution's xdotool/wmctrl/xprop/xrandr with the argv it
# was given, untouched, except that wxrandr strips the two options real xrandr
# has never heard of (--persistent, --unsafe-gnome-overlap).  That is what this
# file measures, and the way it measures it is a recorder shim earlier on PATH
# than /usr/bin: the shim writes its own argv to a file and then execs the real
# binary, so what the assertion compares is the bytes the original was called
# with -- not our own idea of what we passed on.
#
# The same file serves Plasma-on-X11 (kde-x11.sh sources it): the handover is a
# session-type property, not a desktop one.

# No `root` phase here.  common.sh's phase_root is F2.3: root over ssh has no
# session environment, and the claim is that the tools find the SEATED user's
# WAYLAND session by themselves.  On X11 root has no DISPLAY and no XAUTHORITY
# either, so every tool hands over to the real xdotool/xrandr, which cannot
# open a display and fail -- a FAIL that says nothing about session discovery.
# The X11 root story is a separate measurement nobody has made; when somebody
# does, it belongs in a phase of this file with DISPLAY/XAUTHORITY exported
# from the seated session, asserting the handover.
SMOKE_PHASES="install passthrough"
EDITOR_CLASS=xterm
ARGV_LOG=/tmp/fw-argv.log

editor_start() { guest "setsid nohup xterm -T fwsmoke >/dev/null 2>&1 </dev/null & sleep 2; true" >/dev/null || true; }
editor_save()  { :; }

# /usr/local/bin comes before /usr/bin, and passthrough walks PATH for the real
# tool by name, skipping anything that is us -- a shim that is neither is what
# it finds.  It is installed for the four tools that have an original.
install_shims() {
    # Written by the guest's own shell, one heredoc-free line at a time: `$t`
    # expands here, `\$*` and `\$@` are left for the shim itself to expand when
    # the tool under test execs it.  /tmp/fw-argv.log is $ARGV_LOG below.
    root 'for t in xdotool wmctrl xprop xrandr; do
            [ -x /usr/bin/$t ] || continue
            { echo "#!/bin/sh"
              echo "echo \"$t:\$*\" >> /tmp/fw-argv.log"
              echo "exec /usr/bin/$t \"\$@\""
            } > /usr/local/bin/$t
            chmod 0755 /usr/local/bin/$t
          done; head -99 /usr/local/bin/xdotool' >/dev/null 2>&1 || true
    note "recorder shims:" \
         "$(root 'ls /usr/local/bin/xdotool /usr/local/bin/wmctrl /usr/local/bin/xprop /usr/local/bin/xrandr' \
             2>&1 | tr '\n' ' ' || true)"
}

phase_passthrough() {
    install_shims
    guest "rm -f $ARGV_LOG; touch $ARGV_LOG" >/dev/null || true
    editor_start
    local out
    # 1. argv untouched, tool by tool.
    guest "wdotool search --name fwsmoke" >/dev/null 2>&1 || true
    want "wdotool handed its argv to the real xdotool untouched" "^xdotool:search --name fwsmoke$" \
         "$(guest "cat $ARGV_LOG" || true)"
    guest "wwmctl -l -G" >/dev/null 2>&1 || true
    want "wwmctl handed its argv to the real wmctrl untouched" "^wmctrl:-l -G$" "$(guest "cat $ARGV_LOG" || true)"
    guest "wxprop -root _NET_CLIENT_LIST" >/dev/null 2>&1 || true
    want "wxprop handed its argv to the real xprop untouched" "^xprop:-root _NET_CLIENT_LIST$" \
         "$(guest "cat $ARGV_LOG" || true)"
    guest "wxrandr --query" >/dev/null 2>&1 || true
    want "wxrandr handed its argv to the real xrandr untouched" "^xrandr:--query$" "$(guest "cat $ARGV_LOG" || true)"
    # 2. ...except its own two options, which real xrandr would reject outright.
    out=$(guest "wxrandr --output Virtual-1 --auto --persistent 2>&1" || true)
    want "--persistent is stripped before the handover" "^xrandr:--output Virtual-1 --auto$" \
         "$(guest "cat $ARGV_LOG" || true)"
    xwant "T43: --persistent on X11 says in one line that nothing is saved here" \
          "saves nothing|no layout is saved|X11" "$out"
    out=$(guest "wxrandr --unsafe-gnome-overlap --output Virtual-1 --pos 100x0 2>&1" || true)
    want "--unsafe-gnome-overlap is refused here, in this session's own words" \
         "x11|not GNOME|only means anything on GNOME" "$out"
    out=$(guest "wxrandr --unsafe-gnome-overlap-unmeasured 51 2>&1" || true)
    xwant "T26: the force flag alone gets OUR usage error, not the original's" \
          "unsafe-gnome-overlap-unmeasured" "$out"
    # 3. wmirror has no original at all and says so before it names an apt line.
    out=$(guest "wmirror --check 2>&1" || true)
    want "wmirror --check says this is an X11 session" "this is an X11 session" "$out"
    local firstline; firstline=$(printf '%s\n' "$out" | head -1)
    want "...and says it before any apt line" "X11" "$firstline"
    guest "pkill xterm; true" >/dev/null 2>&1 || true
    root "rm -f /usr/local/bin/xdotool /usr/local/bin/wmctrl /usr/local/bin/xprop /usr/local/bin/xrandr" \
        >/dev/null 2>&1 || true
}
