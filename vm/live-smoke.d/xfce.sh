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
# session-type property, not a desktop one, and the X11 step files that come
# after it (cinnamon, mate, i3, lxqt, gnome-x11) source this body too.
#
# Every flavor that runs it is an Ubuntu one, which is why the four shim names
# below are Debian's: the packages that carry the originals are x11-utils and
# x11-xserver-utils here, `xprop`/`xrandr` on Fedora and `xorg-xprop`/
# `xorg-xrandr` on Arch [recon2/fedora 4, recon2/arch 2].  Nothing in this file
# says a package name -- the tools do, out of fwcommon's own distro table -- so
# the day an X11 session exists on a non-Ubuntu flavor these checks port with
# no edit.

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

# `-e`, and not a bare `xterm -T fwsmoke`: an interactive bash retitles the window on its first prompt
# (Ubuntu's /etc/skel/.bashrc, the `xterm*|rxvt*)` case, which prepends `\[\e]0;\u@\h: \w\a\]` to PS1 --
# not /etc/bash.bashrc, whose PROMPT_COMMAND for the same case is commented out on Ubuntu).  Measured on
# the resolute-i3 golden on 2026-09-09: about a second after the window appeared `wmctrl -l` said
# `test@resolute-i3-smoke: ~` and `wdotool search --name fwsmoke` matched nothing, and the first live i3
# run died at `no fwsmoke xterm to work on`.  With `-e` there is no interactive shell, so the title xterm
# is given is the title it keeps.  Every X11 step file inherits this one [requests-batch-11.md item 1].
editor_start() {
    guest "setsid nohup xterm -T fwsmoke -e sh -c 'while :; do sleep 3600; done' \
           >/dev/null 2>&1 </dev/null & sleep 2; true" >/dev/null || true
}
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
    xwant "--persistent on X11 says in one line that nothing is saved here (fix T43)" \
          "saves nothing|no layout is saved|X11" "$out"
    out=$(guest "wxrandr --unsafe-gnome-overlap --output Virtual-1 --pos 100x0 2>&1" || true)
    want "--unsafe-gnome-overlap is refused here, in this session's own words" \
         "x11|not GNOME|only means anything on GNOME" "$out"
    out=$(guest "wxrandr --unsafe-gnome-overlap-unmeasured 51 2>&1" || true)
    xwant "the force flag alone gets OUR usage error, not the original's (fix T26)" \
          "unsafe-gnome-overlap-unmeasured" "$out"
    # 3. wmirror has no original at all and says so before it names an apt line.
    out=$(guest "wmirror --check 2>&1" || true)
    want "wmirror --check says this is an X11 session" "this is an X11 session" "$out"
    local firstline; firstline=$(printf '%s\n' "$out" | head -1)
    xwant "...and says it before any apt line, not after the helper line (fix 40, T21)" "X11" "$firstline"
    guest "pkill xterm; true" >/dev/null 2>&1 || true
    root "rm -f /usr/local/bin/xdotool /usr/local/bin/wmctrl /usr/local/bin/xprop /usr/local/bin/xrandr" \
        >/dev/null 2>&1 || true
}
