# live-smoke.d/common.sh -- the phases that are the same on every desktop.
#
# Sourced by vm/live-smoke.sh, which owns the helpers used here (pass/fail/
# want/same/xwant/guest/root/shot), the package axis (pkg_files, pkg_deploy,
# pkg_install_cmd, pkg_remove_cmd, pkg_left_behind, pkg_banner_re -- one arm per
# `# vmctl-distro:`) and the variables NAME/FLAVOR/DESKTOP/DISTRO/MODE/REPO/VM/
# DEB/VERSION/HEADS/SCALE.  A desktop step file is sourced AFTER this one and
# overrides any function below by redefining it; the hooks it must define are:
#
#   SMOKE_PHASES     the ordered default phase list for that desktop
#   EDITOR_CLASS     what `wdotool search --class` matches the text editor by.
#                    On GNOME it is TextEditor and not gnome-text-editor:
#                    WM_CLASS is org.gnome.TextEditor, exactly as under X, so
#                    `--class gnome-text-editor` matching nothing is not a bug
#   editor_start     start the editor on $SMOKE_FILE, detached (an ssh call
#                    that starts a GUI in the foreground never returns)
#   editor_save      make the editor write $SMOKE_FILE (usually ctrl+s)
#   oracle_outputs   the desktop's NATIVE display tool, one "<name> <x>,<y>"
#                    line per enabled output -- the second opinion wxrandr is
#                    checked against
#   layout_phase     the live keyboard-layout switch, which is the one thing
#                    with no portable shape at all (GNOME: the Super+Space
#                    GESTURE; KWin: kxkbrc + the /Layouts D-Bus call)
#
# Optional hooks: install_extra (extra deploy steps), set_scale2, phase_* of
# the desktop's own.

# Everything the smoke keeps in the guest lives in the seated user's HOME and
# not in /tmp: on noble (24.04) systemd-tmpfiles ships `D /tmp`, so a boot
# empties it -- and the default GNOME list reboots the guest up to three times
# (install, the "51" metadata line, the greeter check).  A bus log or an oracle
# left in /tmp would simply be gone by the phase that reads it, with no error.
# The strings are single-quoted on purpose: $HOME is expanded by the GUEST's
# shell, the same way gnome.sh's $MX is.
SMOKE_FILE='$HOME/w11-smoke.txt'
WIN=                      # the editor's window id, set by phase_windows
BUSLOG='$HOME/w11-bus.log'
ORACLE='$HOME/w11-oracle.py'

# --- helpers ------------------------------------------------------------------

# The tools under test, in the order build-pyz.sh names them.
SMOKE_TOOLS="wdotool wwmctl wxprop wxrandr warandr wmirror"

# `wdotool getwindowgeometry` prints two indented lines; the smoke wants them
# as one word, `100,100 800x600`, which is the shape the measurement is in.
win_geom() {
    guest "wdotool getwindowgeometry $1" | awk '
        /Position:/ { split($2, p, " "); pos = $2 }
        /Geometry:/ { geo = $2 }
        END { printf "%s %s\n", pos, geo }'
}

# Wait until a guest command prints something matching a regex, or give up.
# Nothing here is instant: a GNOME extension goes INITIALIZED -> ACTIVE about
# 12 s after the session comes up (measured on 46 and 50), a modeset needs a
# frame, and a hot-plugged head needs the compositor's next probe.
await() {   # await <seconds> <regex> <shell command>
    local secs=$1 re=$2 cmd=$3 out i
    for i in $(seq 1 "$secs"); do
        out=$(guest "$cmd" || true)
        if printf '%s\n' "$out" | grep -Eq -- "$re"; then printf '%s\n' "$out"; return 0; fi
        sleep 1
    done
    printf '%s\n' "$out"
    return 1
}

editor_text() { guest "cat $SMOKE_FILE 2>/dev/null" ; }

# The default oracle: live-smoke.d/oracle.py, copied to the guest by the driver.
# A step file overrides this only when its desktop needs something else.
oracle_outputs() { guest "python3 $ORACLE $DESKTOP" | grep -E '^[A-Za-z]' || true; }

# head_dark <connector>: the host-side screendump of that head is one flat colour
# (the same test vm/selftest.sh uses for "painted": sampled standard deviation of
# the pixels, 0.02 the line).  Virtual-N is QEMU head N-1.
head_dark() {
    local n=${1##*-} f; f=$(mktemp -t smoke-head-XXXXXX.png)
    "$VM" shot "$NAME" "$((n - 1))" "$f" >/dev/null 2>&1 || { rm -f "$f"; return 1; }
    local sd; sd=$(identify -format '%[fx:standard_deviation]' "$f" 2>/dev/null || echo 1); rm -f "$f"
    note "head $((n - 1)) ($1) standard deviation $sd"
    awk -v s="$sd" 'BEGIN { exit !(s + 0 < 0.02) }'
}

# The Plasma major, 5 or 6: the two spell their config tool and their layout
# object differently, and 5.27 reads kxkbrc at login only.
plasma_major() { guest 'plasmashell --version 2>/dev/null' | grep -o '[0-9]\+' | head -1; }

# vmctl scp logs in as root, so the oracle lands in /tmp first and the seated
# user's own shell copies it home.  Both halves run again after every reboot,
# because /tmp does not survive one on 24.04.
install_oracle() {
    "$VM" scp "$NAME" "$STEPS/oracle.py" "$NAME:/tmp/w11-oracle.py" >/dev/null 2>&1 || true
    guest "cp /tmp/w11-oracle.py $ORACLE && chmod 0755 $ORACLE" >/dev/null 2>&1 || true
}

# One output's position as the native tool reports it, `x,y`.
opos() { oracle_outputs | awk -v n="$1" '$1 == n { print $2 }' | head -1; }

# The two outputs the display family works with: the RIGHTMOST enabled output
# and its left-hand neighbour, by the oracle's own x coordinates.
#
# Not the first two lines.  The instances on this host carry three heads
# (Virtual-1/2/3 at 0, 1920 and 3840 in tests/fixtures/live/noble-gnome-46.0-
# capture.txt), and taking the middle one strands the third: `--output
# Virtual-2 --off` leaves a hole at 1920..3840, and wxrandr/mutter.py's
# keep_adjacent pulls nothing towards an output that went --off, so Mutter
# answers "Logical monitors not adjacent" and every check below would be
# failing on the script's own geometry instead of on the tool.  The rightmost
# head has no right-hand neighbour, so --off/--below/--right-of on it leave the
# rest contiguous.  With exactly two heads -- what the hand measurement used --
# this is that same pair.  (Sorting by x also settles sway, whose get_outputs
# order is Virtual-3, Virtual-2, Virtual-1.)
display_pair() {   # prints "<anchor> <mover>"; an empty mover means one head
    local sorted n
    sorted=$(oracle_outputs | awk -F'[ ,]' '{ print $2, $1 }' | sort -n | awk '{ print $2 }')
    n=$(printf '%s\n' "$sorted" | grep -c . || true)
    case ${n:-0} in
        0) printf ' \n' ;;
        1) printf '%s \n' "$(printf '%s\n' "$sorted" | head -1)" ;;
        *) printf '%s %s\n' "$(printf '%s\n' "$sorted" | sed -n "$((n - 1))p")" \
                            "$(printf '%s\n' "$sorted" | sed -n "${n}p")" ;;
    esac
}

# `--right-of` / `--below` in the oracle's numbers, which is the only way to
# state them once for a two-head and a three-head instance: the mover shares one
# coordinate with the anchor and is strictly past it on the other.
beside() {   # beside right|below <anchor> <mover> <what>
    local how=$1 a=$2 b=$3 what=$4 ap bp
    ap=$(opos "$a"); bp=$(opos "$b")
    if [ -z "$ap" ] || [ -z "$bp" ]; then fail "$what [no position for $a ($ap) or $b ($bp)]"; return 1; fi
    if [ "$how" = right ] && [ "${bp#*,}" = "${ap#*,}" ] && [ "${bp%,*}" -gt "${ap%,*}" ]; then
        pass "$what ($a at $ap, $b at $bp)"
    elif [ "$how" = below ] && [ "${bp%,*}" = "${ap%,*}" ] && [ "${bp#*,}" -gt "${ap#*,}" ]; then
        pass "$what ($a at $ap, $b at $bp)"
    else
        fail "$what [$a at $ap, $b at $bp]"
    fi
}

# Single-quote a string for the guest's /bin/sh (dash).  `printf %q` is bash's
# and emits $'...' for anything non-ASCII, which dash does not understand --
# and the strings typed here are `de: yz@ Stra\xc3\x9fe` on purpose.
sq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

editor_clear() {
    guest "wdotool windowactivate --sync $WIN; wdotool key ctrl+a; wdotool key BackSpace" >/dev/null || true
    sleep 0.5
}

# Type a string, save, read the file back.  The read-back is deliberately the
# FILE and not the clipboard: wl-clipboard is not on the goldens, and the file
# is also the artefact worth keeping (tests/fixtures/live/).
type_and_read() {   # type_and_read <text>
    editor_clear
    guest "wdotool type --delay 30 -- $(sq "$1")" >/dev/null || true
    sleep 0.6
    editor_save
    sleep 1.0
    editor_text
}

# --- phases -------------------------------------------------------------------

# T65: the recorder.  It has to be armed before anything else runs, because the
# claim is about the WHOLE smoke: not one portal request other than the
# Settings read, and not one PolicyKit call, from session start to the end.
# It APPENDS, because it is re-armed after every reboot (a reboot takes the
# session bus with it) and what phase_nodialog reads at the end is the
# concatenation of every session in the run.
arm_busrec() {
    guest "setsid nohup dbus-monitor --session >> $BUSLOG 2>&1 </dev/null & sleep 1; true" >/dev/null 2>&1 || true
}

# Called by every phase that reboots the guest, once the session is back: what
# a reboot destroys, put back.  Without it the bus log stops at the first
# reboot and the oracle -- which lived in /tmp -- is gone on 24.04, which would
# make the display family quietly report "one head only".
after_reboot() {
    install_oracle
    arm_busrec
    note "re-armed after the reboot: the oracle in \$HOME and the dbus-monitor recording"
}

phase_busrec() {
    guest "pkill -x dbus-monitor >/dev/null 2>&1; rm -f $BUSLOG; true" >/dev/null 2>&1 || true
    arm_busrec
    # Matched by NAME, never by command line.  `pkill -f 'dbus-monitor
    # --session'` SIGTERMs the `sh -c` wrapper that is about to START the
    # recorder (its own command line contains the pattern; -f excludes only
    # pkill itself), and `pgrep -f ... | wc -l` then counts the next wrapper
    # shell -- so the phase used to print PASS whether or not a recorder
    # existed.  `-x dbus-monitor` matches the process name exactly, which a
    # shell never has.
    local pid n1 n2
    pid=$(guest "pgrep -x dbus-monitor | head -1" | tr -d ' \r\n' || true)
    n1=$(guest "wc -l < $BUSLOG 2>/dev/null || echo 0" | tr -d ' \r\n' || true)
    # ...and a pid is not a recording: dbus-monitor asks for BecomeMonitor and
    # then sees every message on the bus, so one deliberate call has to make the
    # log grow.  A recorder that started and was refused the monitor role would
    # sit there with a pid and an empty file.
    # dbus-send, not gdbus: it comes with dbus itself, which every guest that has a
    # dbus-monitor has, where gdbus is glib's and NixOS's session has none on PATH
    # (measured on nixos-sway, CI run 34364127816: a recorder with a pid and a log
    # that never grew, because the deliberate call never happened).
    guest "dbus-send --session --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus \
           org.freedesktop.DBus.ListNames >/dev/null 2>&1; sleep 1; true" >/dev/null 2>&1 || true
    n2=$(guest "wc -l < $BUSLOG 2>/dev/null || echo 0" | tr -d ' \r\n' || true)
    if [ -n "$pid" ] && [ "${n2:-0}" -gt "${n1:-0}" ]; then
        pass "dbus-monitor (pid $pid) is recording into $BUSLOG (the log grew $n1 -> $n2 lines on one bus call)"
    else
        fail "no session-bus recording: pid '$pid', log $n1 -> $n2 lines (the no-dialog phase has nothing to read)"
    fi
}

phase_install() {
    if [ "$REUSE" = 1 ]; then
        note "--reuse: nothing installed; what is in the image is what is measured"
        guest "wdotool --version; wxrandr --version" | sed 's/^/     /' || true
        return 0
    fi
    # NixOS installs nothing: the module is baked into the image, and what the
    # phase owes instead is proof that the running system is the one with it on
    # (vm/nixos/common.nix bakes two specialisations, and `--remove` switches
    # into the other).  A store path with the version in its name is what
    # `programs.w11.enable` put on PATH; the without-w11
    # specialisation directory exists only under the DEFAULT system, because a
    # specialisation has no specialisations of its own.
    if [ "$DISTRO" = nixos ]; then
        want "wdotool is a /nix/store path carrying $VERSION" \
             "^/nix/store/[a-z0-9]+-w11-$VERSION/" \
             "$(guest 'readlink -f $(command -v wdotool)' | tr -d ' \r' || true)"
        same "/run/current-system is the default specialisation, not without-w11" "yes" \
             "$(guest 'test -d /run/current-system/specialisation/without-w11 && echo yes || echo no' \
                  | tr -d ' \r' || true)"
        return 0
    fi
    pkg_deploy || return 1
    local out st=0 cmd
    cmd=$(pkg_install_cmd)
    out=$(root "$cmd") || st=$?
    ok "$cmd" "$st"
    # The postinst/scriptlet banner is printed on a FIRST install only (the
    # stamp /var/lib/w11/installed decides), which a --fresh instance
    # is.  dpkg and pacman print the same paragraph; the rpm prints nothing at
    # all on purpose, and what it owes instead is README.Fedora in %doc
    # [recon2/pkg-rpm 292].
    local banner; banner=$(pkg_banner_re)
    if [ -n "$banner" ]; then
        want "the first-install banner names the relogin and /dev/uinput" "$banner" "$out"
    elif [ "$DISTRO" = fedora ]; then
        want "no banner, but the rpm ships README.Fedora where the advice went" "README.Fedora" \
             "$(root 'rpm -ql w11 2>/dev/null | grep -i readme' || true)"
    fi
    if [ "$MODE" = tree ]; then
        step "deploying the WORKING TREE over the package (the tree carries fixes the package does not)"
        ( cd "$REPO" && sh scripts/build-pyz.sh >/dev/null )
        local t
        for t in $SMOKE_TOOLS; do
            [ -f "$REPO/dist/$t" ] || continue
            "$VM" scp "$NAME" "$REPO/dist/$t" "$NAME:/tmp/$t" >/dev/null
        done
        root "for t in $SMOKE_TOOLS; do [ -f /tmp/\$t ] && install -m 0755 /tmp/\$t /usr/local/bin/\$t; done" \
            >/dev/null
        want "the tree's zipapps shadow the package's tools" "/usr/local/bin/wdotool" \
             "$(guest 'command -v wdotool' || true)"
        if declare -F install_extra >/dev/null; then install_extra; fi
    fi
    step "reboot: gnome-shell scans extension directories at login and GDM's autologin fires once per boot"
    root "( sleep 1; reboot ) >/dev/null 2>&1 &" >/dev/null 2>&1 || true
    sleep 8
    wait_session >/dev/null || { fail "no session after the install reboot"; return 1; }
    sleep 15        # packaging/common/enable-bridge runs from /etc/xdg/autostart; ~12 s on 46/50
    after_reboot
    pass "reboot into a session with the package installed"
}

# The two distro phases.  live-smoke.sh appends them to the step file's own
# SMOKE_PHASES for the distros that name them (distro_phases), so no desktop
# step file carries a distro branch.

# Fedora runs SELinux Enforcing out of the box, and nothing in the recon tripped
# it: the tools and the daemon are unconfined_t, `install-bridge.sh --udev` and
# the rpm's %post write into /etc/udev/rules.d with the right label, and the
# uinput node keeps its own event_device_t [recon2/fedora 2, recon2/pkg-rpm].
# That is a negative claim about a whole run, so this is the phase that would
# say so if an AVC ever appeared.  The three names are the only processes of
# ours that touch a labelled object: the tools run as `python3` under their
# zipapp names, the daemon execs `wdotool`, and the scriptlets run `udevadm`.
phase_selinux() {
    local mode avc
    # `|| true` on both: `getenforce` is not installed off Fedora and `grep -c`
    # exits 1 when it counts zero -- which is the PASSING case -- and either
    # status would abort the phase under the driver's `set -e` before its own
    # `same` ran.
    mode=$(root 'getenforce 2>/dev/null' | tr -d ' \r' || true)
    same "SELinux is Enforcing (the state every Fedora measurement was taken in)" "Enforcing" "$mode"
    avc=$(root "journalctl -b _TRANSPORT=audit --no-pager 2>/dev/null \
                | grep -c 'AVC.*comm=\"\\(wdotool\\|python3\\|udevadm\\)\"'" | tr -d ' \r' || true)
    same "no SELinux denial for wdotool, python3 or udevadm in this boot" "0" "${avc:-0}"
    note "audit lines this boot: $(root 'journalctl -b _TRANSPORT=audit --no-pager 2>/dev/null | wc -l' \
             | tr -d ' \r' || true)"
}

# `rpm -V` and `pacman -Qkk` compare every installed file against the package's
# own record of it.  Both print NOTHING when everything agrees, so the assertion
# is emptiness -- which also means the phase has to notice a package that is not
# installed at all rather than call that a pass.
phase_pkgverify() {
    local cmd out
    case "$DISTRO" in
    fedora) cmd="rpm -V w11" ;;
    arch)   cmd="pacman -Qkk w11" ;;
    *)      note "no package verifier for distro $DISTRO"; return 0 ;;
    esac
    # Tree mode is not a weaker version of this check, it is a different run:
    # phase_install deploys the zipapps into /usr/local/bin (which no package
    # owns, so the verifier is right to ignore them) AND, on gnome.sh, installs
    # the tree's extension.js/metadata.json/org.w11.Bridge1.xml over the
    # package's copies under /usr/share/gnome-shell/extensions -- which the
    # single Arch package DOES own, so `pacman -Qkk` would report altered files
    # and this phase would be red by construction on arch-gnome.  So the tree
    # run says what it did not check and stops.
    if [ "$MODE" != pkg ]; then
        note "(--pkg not given: the tree deploy overwrote package-owned files, so $cmd has nothing to prove here)"
        return 0
    fi
    out=$(root "$cmd 2>&1" || true)
    # pacman -Qkk prints one `... 0 altered files` summary line on success; rpm
    # -V prints nothing at all.  Anything else -- a `5` size mismatch, a missing
    # file, `package w11 is not installed` -- is what this phase exists
    # to catch, so the accepted shapes are named and everything else is a FAIL.
    if [ -z "$(printf '%s' "$out" | tr -d ' \r\n')" ] \
       || printf '%s\n' "$out" | grep -Eq '^w11: .* 0 altered files$'; then
        pass "$cmd is clean: every installed file is as the package recorded it"
    else
        fail "$cmd reported a difference [$(ev "$out")]"
    fi
}

phase_windows() {
    guest "rm -f $SMOKE_FILE; touch $SMOKE_FILE" >/dev/null || true
    editor_start
    local out
    out=$(await 30 '[0-9]' "wdotool search --class $EDITOR_CLASS | head -1" || true)
    WIN=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$WIN" ]; then fail "wdotool search --class $EDITOR_CLASS found no window [$(ev "$out")]"; return 1; fi
    pass "wdotool search --class $EDITOR_CLASS -> $WIN"
    want "getwindowname is not empty" "." "$(guest "wdotool getwindowname $WIN" || true)"
    guest "wdotool windowmove $WIN 100 100; wdotool windowsize $WIN 800 600" >/dev/null || true
    sleep 1
    same "windowmove 100 100 + windowsize 800 600 -> getwindowgeometry" "100,100 800x600" "$(win_geom "$WIN")"
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    same "windowactivate --sync then getactivewindow is that window" "$WIN" \
         "$(guest 'wdotool getactivewindow' | tr -d ' \n' || true)"
    guest "wdotool windowminimize $WIN" >/dev/null || true
    sleep 1
    local act; act=$(guest 'wdotool getactivewindow 2>&1' | tr -d ' \n' || true)
    if [ "$act" = "$WIN" ]; then fail "windowminimize left the window active (getactivewindow still $WIN)";
    else pass "windowminimize: the window is no longer the active one (getactivewindow: $(ev "$act"))"; fi
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    # Desktops.  GNOME's are dynamic, so 1 always exists; the editor stays on 0.
    # A desktop whose count is fixed (KWin ships ONE virtual desktop and refuses
    # an index past the count) has to make the second one first, which is what
    # the optional pre_desktop_pair/post_desktop_pair hooks are for -- otherwise
    # the pair below would be a FAIL the tool does not own.
    if declare -F pre_desktop_pair >/dev/null; then pre_desktop_pair; fi
    # The count is read from the compositor, not assumed: GNOME's dynamic
    # workspaces always leave a spare (the recorded `wwmctl -d` on 46.0 lists
    # two), a stock Plasma session has ONE and KWin refuses an index past the
    # count, and switching to a desktop that does not exist is not a claim
    # anyone has made about the tool.
    local nd; nd=$(guest 'wwmctl -d' | grep -c '^[0-9]' || true)
    if [ "${nd:-0}" -lt 2 ]; then
        note "$nd virtual desktop(s): the set_desktop pair needs two and this desktop's count is fixed, skipped"
    else
        guest "wdotool set_desktop 1" >/dev/null || true
        sleep 1
        same "set_desktop 1 -> get_desktop (of $nd desktops)" "1" \
             "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
        guest "wdotool set_desktop 0" >/dev/null || true
        sleep 1
        same "set_desktop 0 -> get_desktop" "0" "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
    fi
    if declare -F post_desktop_pair >/dev/null; then post_desktop_pair; fi
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    # Pointer.  getmouselocation prints `x:300 y:300 screen:0 window:...`.
    guest "wdotool mousemove 300 300" >/dev/null || true
    want "mousemove 300 300 -> getmouselocation" "x:300 y:300" "$(guest 'wdotool getmouselocation' || true)"
    local st=0
    guestq "wdotool mousemove 300 300 click 1" || st=$?
    ok "mousemove 300 300 click 1" "$st"
}

phase_wm() {
    [ -n "$WIN" ] || { fail "no window from phase_windows"; return 1; }
    guest "wdotool windowactivate --sync $WIN; wdotool windowmove $WIN 100 100; wdotool windowsize $WIN 800 600" \
        >/dev/null || true
    sleep 1
    same "the pair starts at" "100,100 800x600" "$(win_geom "$WIN")"
    guest "wwmctl -r :ACTIVE: -b add,maximized_vert,maximized_horz" >/dev/null || true
    sleep 1.5
    local maxed; maxed=$(win_geom "$WIN")
    if [ "$maxed" = "100,100 800x600" ]; then fail "add,maximized_vert,maximized_horz did not change the geometry";
    else pass "add,maximized_vert,maximized_horz -> $maxed"; fi
    want "wxprop -id says the window is maximized on both axes" \
         "_NET_WM_STATE_MAXIMIZED_(HORZ|VERT)" "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    guest "wwmctl -r :ACTIVE: -b remove,maximized_vert,maximized_horz" >/dev/null || true
    sleep 1.5
    # b7a60f0: the pair used to restore to the size the first of the two
    # toggles left behind.  Measured exact on 46, 50 and 51 after the fix.
    same "b7a60f0: remove,maximized_vert,maximized_horz restores exactly" "100,100 800x600" "$(win_geom "$WIN")"
    want "wwmctl -l lists the editor" "." "$(guest 'wwmctl -l' || true)"
    want "wwmctl -l -G carries a geometry column" "[0-9]+ +[0-9]+ +[0-9]+ +[0-9]+" "$(guest 'wwmctl -l -G' || true)"
    want "wwmctl -d lists desktops" "^0 " "$(guest 'wwmctl -d' || true)"
    want "wxprop -root _NET_CLIENT_LIST is a window-id list" "window id|0x[0-9a-f]+|WINDOW" \
         "$(guest 'wxprop -root _NET_CLIENT_LIST' || true)"
}

phase_input() {
    [ -n "$WIN" ] || { fail "no window from phase_windows"; return 1; }
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    same "wdotool type arrives byte-exact in the editor" "us: yz@" "$(type_and_read 'us: yz@')"
    editor_clear
    guest "wdotool type --delay 30 -- abc; wdotool key Return; wdotool type --delay 30 -- def" >/dev/null || true
    sleep 0.6; editor_save; sleep 1
    same "wdotool key Return is a keystroke, not text" "abc
def" "$(editor_text)"
    if declare -F layout_phase >/dev/null; then layout_phase; else note "no layout hook for $DESKTOP"; fi
}

phase_display() { common_display_phase; }

# The body, as its own name, so a desktop file can wrap it (GNOME adds the
# gapped-layout refusal after it) instead of copying it.
common_display_phase() {
    local outs; outs=$(oracle_outputs || true)
    note "native oracle: $(ev "$outs")"
    local pair first second
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    want "wxrandr --listmonitors lists the outputs the compositor has" \
         "Monitors: [0-9]+" "$(guest 'wxrandr --listmonitors' || true)"
    if [ -z "$second" ]; then note "one head only: the two-head steps need --heads 2"; return 0; fi
    # However many heads this instance was started with -- the rig hands out up
    # to four and the existing instances carry three, so the count is read, not
    # assumed, and the pair worked on is the rightmost head and its neighbour
    # (see display_pair: the middle of three cannot be moved without leaving
    # Mutter a hole).
    local n0; n0=$(printf '%s\n' "$outs" | grep -c .)
    note "pair: anchor $first, mover $second, of $n0 enabled output(s)"
    guest "wxrandr --output $second --off" >/dev/null || true
    sleep 2
    local left; left=$(oracle_outputs | grep -c .)
    if [ "$left" = "$((n0 - 1))" ]; then
        pass "--output $second --off leaves $((n0 - 1)) enabled outputs ($DESKTOP's own tool)"
    elif head_dark "$second"; then
        # Plasma 5.27, measured 2026-09-08 on noble-kde: KWin stops painting the
        # output (its screendump goes flat, standard deviation 0.012 against
        # 0.06-0.10 on the live heads) but never releases the DRM connector, and
        # kscreen-doctor 5.27 keeps reporting it enabled for as long as we cared
        # to poll (and segfaults every other call).  wxrandr's own --query says
        # `connected` with no mode.  The pixels are the honest oracle there.
        pass "--output $second --off: $DESKTOP's tool still counts $left, but $second's head went dark (the native tool does not see a disable here; Plasma 5.27 does this)"
    else
        fail "--output $second --off leaves $left enabled outputs, wanted $((n0 - 1)), and $second is still painted"
    fi
    guest "wxrandr --output $second --auto" >/dev/null || true
    sleep 2
    same "--output $second --auto brings it back" "$n0" "$(oracle_outputs | grep -c .)"
    guest "wxrandr --output $second --right-of $first" >/dev/null || true
    sleep 2
    beside right "$first" "$second" "--right-of puts $second to the right of $first"
    guest "wxrandr --output $second --below $first" >/dev/null || true
    sleep 2
    beside below "$first" "$second" "--below puts $second under $first"
    guest "wxrandr --output $second --right-of $first" >/dev/null || true
    sleep 2
    if [ "$SCALE" = 1 ] && declare -F set_scale2 >/dev/null; then
        step "the scale axis: the same layout calls again at scaling-factor 2"
        set_scale2
        want "--listmonitors still answers at scale 2" "Monitors: [0-9]+" "$(guest 'wxrandr --listmonitors' || true)"
        guest "wxrandr --output $second --below $first" >/dev/null || true
        sleep 2
        beside below "$first" "$second" "--below at scale 2"
        guest "wxrandr --output $second --right-of $first" >/dev/null || true
        sleep 2
    fi
}

# F2.3 and the root-vs-user axis: root over ssh has no session environment at
# all (pam_systemd hands `ssh root@` its own empty /run/user/0), so every tool
# has to find the seated user's session by itself.  The check is that it finds
# the SAME session: the outputs must agree with the user's, not merely exist.
phase_root() {
    local ru uu
    uu=$(guest 'wwmctl -l | wc -l' || true); ru=$(root 'wwmctl -l | wc -l' || true)
    same "wwmctl -l as root sees the seated user's windows" "$(echo "$uu" | tr -d ' ')" "$(echo "$ru" | tr -d ' ')"
    uu=$(guest "wxrandr --query | awk '\$2==\"connected\"{print \$1}'" || true)
    ru=$(root "wxrandr --query | awk '\$2==\"connected\"{print \$1}'" || true)
    same "wxrandr --query as root names the same outputs" "$(echo "$uu" | tr '\n' ' ')" "$(echo "$ru" | tr '\n' ' ')"
    local out st=0
    out=$(root 'wmirror --list' ) || st=$?
    ok "wmirror --list as root" "$st"
    note "wmirror --list (root): $(ev "$out")"
    out=$(root 'wxrandr --persistent --query' ) || st=$?
    note "wxrandr --persistent --query (root): $(ev "$out")"
}

# T65.  The claim in the measurement README and Technical.md is a NEGATIVE one,
# so this is the only shape it can take: record the session bus for the whole
# run and read it at the end.  `Settings.ReadAll`/`Read` on
# org.gnome.desktop.input-sources is the one portal call the tools make, and
# they make it without any consent step -- everything else would be a dialog.
phase_nodialog() {
    local n log
    log=$(guest "wc -l < $BUSLOG 2>/dev/null || echo 0" | tr -d ' \r\n' || true)
    if [ "${log:-0}" -lt 10 ]; then fail "the bus log is $log lines: the recorder was not running"; return 1; fi
    note "$log lines of session bus recorded (every session in this run, appended)"
    # METHOD CALLS only, and by interface.  xdg-desktop-portal is D-Bus
    # activated, so the very first Settings read -- ours or gnome-text-editor's
    # -- produces NameOwnerChanged signals and a StartServiceByName naming
    # org.freedesktop.portal.Desktop and .Documents.  Those are the bus telling
    # us a service appeared, not a request anyone made, and matching the bare
    # string would turn this phase red on a perfectly healthy session.  The
    # claim T65 records is about calls: nobody asks the portal for anything but
    # Settings, and nobody asks PolicyKit for anything at all.  Measured on
    # gnome46 on 2026-09-08: 7555 lines recorded, ZERO portal method calls, and
    # 36 name lines naming org.freedesktop.portal.Desktop/.Documents/.IBus/
    # .Tracker -- every one of which the bare-string check would have called a
    # portal request.
    local calls other
    calls="grep -E '^method call ' $BUSLOG"
    n=$(guest "$calls | grep -c 'interface=org.freedesktop.portal\\.'" | tr -d ' \r\n' || true); n=${n:-0}
    # A GTK 4 app registers a session-inhibit monitor through the portal and
    # then talks to it (measured on GNOME 50.1 and 51.beta: Inhibit.CreateMonitor,
    # Request.Close and Session.Close from gnome-text-editor's connection, none
    # of it on 46.0).  None of the six tools ever inhibits, so every connection
    # that called Inhibit is an app, and its portal traffic is not ours to judge.
    # What is left has to be Settings and nothing else.
    local apps pat
    apps=$(guest "$calls | grep 'interface=org.freedesktop.portal.Inhibit' | sed -n 's/.*sender=\\([^ ]*\\).*/\\1/p' | sort -u" | tr -d '\r' || true)
    # only the dot is special in an ERE; a backslash before the colon is a "stray \\" warning on
    # grep 3.12 (Fedora 44), printed into the very output the check reads
    pat=$(printf '%s\n' "$apps" | grep . | sed 's/\./\\&/g' | sed 's/^/sender=/' | paste -sd'|' || true)
    # Request and Session are the portal's lifecycle objects (a Close on
    # /request/<sender>/... or /session/<sender>/...), never a request for a
    # dialog; they belong to whoever opened them, which is never one of the tools.
    #
    # `Documents.GetMountPoint` joins them, and only that member of that interface.  It reads the path
    # the document portal is mounted at and answers a string; there is no dialog behind it and no
    # consent to give.  Measured on resolute-hypr 2026-09-09: one call, `path=/org/freedesktop/portal/
    # documents; interface=org.freedesktop.portal.Documents; member=GetMountPoint`, on a run whose 1152
    # bus lines carried no other portal method call at all -- the portal is D-Bus activated and asks
    # itself this on startup.  Widened by member and not by interface: `Documents` also has AddFull and
    # friends, and T65's claim is about what the tools provoke, so the smallest widening that is still
    # a fact is the right one (the same reasoning that put Request and Session here).
    other=$(guest "$calls | grep 'interface=org.freedesktop.portal\\.' \
                 | grep -vE 'interface=org.freedesktop.portal.(Settings|Request|Session)' \
                 | grep -v 'interface=org.freedesktop.portal.Documents; member=GetMountPoint' \
                 ${pat:+| grep -vE '$pat'} | head -5" || true)
    if [ -z "$other" ]; then
        if [ "$n" = 0 ]; then pass "no portal method call at all during the whole smoke"
        else pass "no portal method call other than Settings from anything but the editor ($n portal calls; app connections: ${apps:-none})"; fi
    else fail "a portal method call that is not Settings and not the editor's: $(ev "$other")"; fi
    other=$(guest "$calls | grep 'interface=org.freedesktop.PolicyKit1' | head -3" || true)
    if [ -z "$other" ]; then pass "no PolicyKit method call on the session bus during the whole smoke"
    else fail "a PolicyKit call: $(ev "$other")"; fi
    local sigs
    sigs=$(guest "grep -Ec '^signal ' $BUSLOG" | tr -d ' \r\n' || true)
    note "reported, not asserted: ${sigs:-0} signal line(s) in the log -- NameOwnerChanged for the portal's own"
    note "activation is traffic ABOUT the portal, not a request to it, and is not what T65 claims"
    guest "grep -o 'org.freedesktop.portal.[A-Za-z]*' $BUSLOG | sort | uniq -c | sort -rn | head" \
        | sed 's/^/     portal: /' || true
    "$VM" shot "$NAME" --all "$SHOTDIR/zz-no-dialog" >/dev/null 2>&1 || true
    note "final screenshots of every head: $SHOTDIR/zz-no-dialog-*.png (a dialog on any of them refutes the claim)"
}

