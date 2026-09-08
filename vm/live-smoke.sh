#!/usr/bin/env bash
# live-smoke.sh -- the hand smoke of 2026-09-08, scripted, one flavor per run.
#
# Everything this checks was measured by hand first, on this rig, on
# noble-gnome (GNOME Shell 46.0), resolute-gnome (50.1), stonking-gnome
# (51.beta / libmutter-51.so.0), resolute-kde (Plasma 6.6 / KWin 6.6.6) and
# resolute-sway (sway 1.11); the write-up of that session is the oracle every
# step here replays.  A step that disagrees with it prints `FAIL <what>` with
# the bytes it actually saw, and the script's exit status is the number of
# FAILs (capped at 125, since a shell status is a byte and 126/127/128+ mean
# other things).  A run that cannot even get a desktop exits 2.
#
#   usage: vm/live-smoke.sh <flavor> [options]
#
#     --name NAME     instance name (default <flavor>-smoke)
#     --reuse         reuse the existing instance and whatever is installed in
#                     it; skips start --fresh and the install phase.  This is
#                     the iteration mode -- gnome46/gnome50/kde66/sway1 already
#                     carry a .deb -- and NOT the mode a release is measured in
#     --deb           install release/fuckwayland_0.4.0_all.deb and nothing
#                     else (the packaging axis).  The default deploys the
#                     WORKING TREE over the .deb: scripts/build-pyz.sh into
#                     /usr/local/bin the way repro/deploy-to-vm.sh does, plus
#                     gnome/fuckwayland-bridge@fuckwayland over the package's
#                     copy, because the tree carries fixes the shipped .deb
#                     does not (b7a60f0's maximize pair among them)
#     --heads N       monitors (default 2; the display and overlap phases need
#                     two, the layout phases do not care)
#     --cpus N        vCPUs (default 2)   } this host runs seven other agents
#     --mem M         guest RAM (default 3G)
#     --phases a,b,c  run only these, in this order (see --list-phases)
#     --skip a,b      run the default list without these
#     --list-phases   print the flavor's default phase list and exit
#     --scale         re-run the display phase with scaling-factor 2 (the
#                     scale axis: 1.0 first, then 2 on the same instance)
#     --keep          leave the instance running at the end (default: stop it,
#                     because the memory rule on this host is ONE VM at a time)
#     --remove        after the phases, `apt-get remove -y fuckwayland` and
#                     check that every piece of it went: the files, the
#                     autostart symlink, the stamp, both extensions, and the
#                     grant on /dev/uinput.  Only meaningful with --deb: the
#                     default mode installs the working tree over the package
#                     into /usr/local/bin, which apt does not own
#
# Results land in vm/live-smoke.out/<flavor>-<date>.log with a screenshot of
# every head per phase next to it in <flavor>-<date>-shots/.
set -euo pipefail

HERE=$(cd -- "$(dirname -- "$0")" && pwd)          # <repo>/vm
REPO=$(dirname "$HERE")
# The rig, or the recorded stand-in vm/live-smoke.d/selftest-offline.sh uses to
# exercise these phases with no VM at all.  Nothing but that self-test may set it.
VM=${LIVE_SMOKE_VMCTL:-$HERE/vmctl}
STEPS=$HERE/live-smoke.d
DEB=$REPO/release/fuckwayland_0.4.0_all.deb

FLAVOR=${1:-}
[ -n "$FLAVOR" ] || { sed -n '2,47p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
shift
NAME=""; MODE=tree; REUSE=0; HEADS=2; CPUS=2; MEM=3G; KEEP=0; SCALE=0; REMOVE=0
ONLY=""; SKIP=""; LIST=0
while [ $# -gt 0 ]; do
    case $1 in
        --name)   NAME=$2; shift 2 ;;
        --reuse)  REUSE=1; shift ;;
        --deb)    MODE=deb; shift ;;
        --heads)  HEADS=$2; shift 2 ;;
        --cpus)   CPUS=$2; shift 2 ;;
        --mem)    MEM=$2; shift 2 ;;
        --phases) ONLY=$2; shift 2 ;;
        --skip)   SKIP=$2; shift 2 ;;
        --list-phases) LIST=1; shift ;;
        --scale)  SCALE=1; shift ;;
        --keep)   KEEP=1; shift ;;
        --remove) REMOVE=1; shift ;;
        -h|--help) sed -n '2,47p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "live-smoke.sh: unknown option $1" >&2; exit 2 ;;
    esac
done
NAME=${NAME:-$FLAVOR-smoke}
[ -f "$HERE/flavors/$FLAVOR.yaml" ] || { echo "no flavor $FLAVOR (vm/flavors/$FLAVOR.yaml)" >&2; exit 2; }
# The desktop, from the flavor's own `# vmctl-desktop:` header -- the same
# lookup vm/selftest.sh does, so a new flavor needs no edit here.
DESKTOP=$(sed -n 's/^#[[:space:]]*vmctl-desktop:[[:space:]]*//p' "$HERE/flavors/$FLAVOR.yaml" | head -1)
DESKTOP=${DESKTOP:-gnome}
[ -f "$STEPS/$DESKTOP.sh" ] || { echo "no step file $STEPS/$DESKTOP.sh for desktop $DESKTOP" >&2; exit 2; }

# ---------------------------------------------------------------- output
# `--list-phases` answers before any of this: it is a question about the step
# files, and a question should not leave a run directory behind.
if [ "$LIST" = 1 ]; then
    # shellcheck source=live-smoke.d/common.sh
    . "$STEPS/common.sh"
    # shellcheck source=/dev/null
    . "$STEPS/$DESKTOP.sh"
    echo "$FLAVOR ($DESKTOP): $SMOKE_PHASES"
    exit 0
fi
STAMP=$(date +%Y%m%d-%H%M%S)
# LIVE_SMOKE_OUT is for the offline self-test, which must not litter the
# real results directory with runs against a recording.
OUTDIR=${LIVE_SMOKE_OUT:-$HERE/live-smoke.out}
SHOTDIR=$OUTDIR/$FLAVOR-$STAMP-shots
LOG=$OUTDIR/$FLAVOR-$STAMP.log
mkdir -p "$SHOTDIR"
exec > >(tee -a "$LOG") 2>&1

T0=$(date +%s)
FAILS=0; PASSES=0; PHASE=-
elapsed() { echo $(( $(date +%s) - T0 )); }
step()  { echo; echo "== [$(elapsed)s] $*"; }
pass()  { PASSES=$((PASSES + 1)); echo "PASS $PHASE: $*"; }
fail()  { FAILS=$((FAILS + 1));  echo "FAIL $PHASE: $*"; }
note()  { echo "     $*"; }
# The evidence line for a failure: the bytes we saw, one line, clipped -- a
# GNOME extension error can be a screenful and the log is meant to be read.
ev()    { printf '%s' "$*" | tr '\n' '|' | cut -c1-400; }

# want <what> <extended-regex> <text...>  -- PASS when the text matches
want()    { local w=$1 re=$2; shift 2
            if printf '%s\n' "$*" | grep -Eq -- "$re"; then pass "$w"
            else fail "$w [want /$re/, got: $(ev "$*")]"; fi; }
# wantnot <what> <extended-regex> <text...>
wantnot() { local w=$1 re=$2; shift 2
            if printf '%s\n' "$*" | grep -Eq -- "$re"; then fail "$w [/$re/ present in: $(ev "$*")]"
            else pass "$w"; fi; }
# same <what> <expected> <actual>  -- byte equality, which is what most of the
# measurements are (`100,100 800x600`, `(true,)`, a typed string read back)
same()    { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 [want '$2', got '$(ev "$3")']"; fi; }
ok()      { if [ "$2" = 0 ]; then pass "$1"; else fail "$1 [exit $2]"; fi; }
# A check for behaviour a fix in flight is going to add: it counts as neither a
# pass nor a fail, so an unfinished fix cannot turn a smoke run red, and the day
# it lands the run says XPASS and the line here becomes a plain `want`.
xwant()   { local w=$1 re=$2; shift 2
            if printf '%s\n' "$*" | grep -Eq -- "$re"
            then echo "XPASS $PHASE: $w (the fix has landed: promote this to want)"
            else echo "XFAIL $PHASE: $w (not in the tree yet) [$(ev "$*")]"; fi; }

# ---------------------------------------------------------------- the guest
# Never let a guest command's own status abort the run: a FAIL is a result, not
# an accident.  Callers that care take the status from `$?` after `|| st=$?`.
guest()  { "$VM" user "$NAME" -- sh -c "$1" 2>&1; }          # as user test, session env
root()   { "$VM" ssh  "$NAME" -- sh -c "$1" 2>&1; }          # as root, no session env
guestq() { "$VM" user "$NAME" -- sh -c "$1" >/dev/null 2>&1; }
shot()   { "$VM" shot "$NAME" --all "$SHOTDIR/$1" >/dev/null 2>&1 && note "shots: $SHOTDIR/$1-*.png" || true; }

# ---------------------------------------------------------------- steps
# shellcheck source=live-smoke.d/common.sh
. "$STEPS/common.sh"
# shellcheck source=/dev/null
. "$STEPS/$DESKTOP.sh"

run_phases=$SMOKE_PHASES
[ -n "$ONLY" ] && run_phases=$(echo "$ONLY" | tr ',' ' ')
for s in $(echo "$SKIP" | tr ',' ' '); do
    run_phases=$(echo "$run_phases" | tr ' ' '\n' | grep -vx "$s" | tr '\n' ' ')
done

echo "live-smoke $FLAVOR ($DESKTOP) instance $NAME, $(if [ $REUSE = 1 ]; then echo reuse; else echo fresh; fi),"\
     "$(if [ $MODE = deb ]; then echo "$DEB only"; else echo 'working tree over the .deb'; fi)"
echo "phases: $run_phases"
echo "log:    $LOG"

# ---------------------------------------------------------------- boot
# `vmctl start` gives up when ONE ssh probe takes longer than 20 s (vmctl's
# wait_ssh caps each probe there and turns the timeout into a Fail), which on
# this host -- seven agents, two vCPU for the guest -- happens on a cold boot
# without the guest being in any trouble.  So a nonzero start is not the end of
# it: if QEMU is up, wait for ssh here and go on.
# The same again for `vmctl session`, whose probe is a 30 s ssh: a timeout there
# is the host being busy, not the desktop being absent, and `vmctl user -- true`
# answering is the proof of that.
wait_session() {
    local i
    for i in 1 2 3 4; do
        if "$VM" session "$NAME" --timeout 240; then return 0; fi
        if "$VM" user "$NAME" -- true >/dev/null 2>&1; then
            echo "     (vmctl session's probe timed out under load; the session answers, going on)"
            return 0
        fi
    done
    return 1
}
wait_ssh() {
    local i
    for i in $(seq 1 72); do          # 6 minutes
        if "$VM" ssh "$NAME" -- true >/dev/null 2>&1; then return 0; fi
        sleep 5
    done
    return 1
}
if [ "$REUSE" = 1 ]; then
    step "vmctl start $NAME (reuse: the overlay and whatever is installed in it)"
    "$VM" start "$NAME" --cpus "$CPUS" --mem "$MEM" >/dev/null || true
else
    step "vmctl start $NAME --flavor $FLAVOR --heads $HEADS --fresh"
    "$VM" start "$NAME" --flavor "$FLAVOR" --heads "$HEADS" --cpus "$CPUS" --mem "$MEM" --fresh >/dev/null || true
fi
wait_ssh || { echo "cannot reach $NAME over ssh"; exit 2; }
step "vmctl session $NAME (autologin; GDM's fires once per boot)"
wait_session || { echo "no graphical session"; exit 2; }
# The native display tool of this desktop, as a script the guest runs: every
# wxrandr apply below is checked against it and never against wxrandr itself.
# It is copied into the seated user's HOME (install_oracle, common.sh) rather
# than left in /tmp, because /tmp does not survive a reboot on 24.04 and this
# smoke reboots up to three times.
install_oracle
shot 00-session

cleanup() {
    if [ "$KEEP" = 1 ]; then
        echo "(--keep: $NAME left running -- vm/vmctl stop $NAME when you are done; ONE VM at a time on this host)"
    else
        "$VM" stop "$NAME" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

for p in $run_phases; do
    if ! declare -F "phase_$p" >/dev/null; then
        echo "live-smoke.sh: no phase_$p for $DESKTOP" >&2; FAILS=$((FAILS + 1)); continue
    fi
    PHASE=$p
    step "phase $p"
    "phase_$p" || note "(phase $p returned $? -- checks above stand)"
    shot "$p"
done
PHASE=-

# ------------------------------------------------------- the removal, last
# `apt remove` is the other half of the packaging claim and the half nobody
# ever runs: README.Debian says removing the package puts /dev/uinput back to
# root:root 0600 with no ACL, and until this existed that sentence had been
# checked by reading debian/fuckwayland.postrm.  It runs after every phase
# because it destroys the installation the phases measure, and only under
# --deb: the default mode puts the tree's zipapps in /usr/local/bin, which
# dpkg does not own and apt will not take away, so a removal there would
# report files left behind that were never the package's.
#
# What it asserts is what the hand smoke measured on resolute-gnome after
# `apt-get remove -y fuckwayland`: nothing of ours under /usr/lib/fuckwayland,
# no autostart symlink, no /var/lib/fuckwayland stamp, neither extension
# directory, /dev/uinput back to root:root 0600 with no ACL entry and no
# uaccess tag -- and, because the remove drops the stamp, a re-install printing
# the first-install banner all over again.
phase_remove() {
    local out st=0
    out=$(root "DEBIAN_FRONTEND=noninteractive apt-get remove -y fuckwayland 2>&1") || st=$?
    ok "apt-get remove -y fuckwayland" "$st"
    local left
    left=$(root "ls -d /usr/lib/fuckwayland /var/lib/fuckwayland \
                       /etc/xdg/autostart/fuckwayland-enable-bridge.desktop \
                       /usr/share/gnome-shell/extensions/fuckwayland-bridge@fuckwayland \
                       /usr/share/gnome-shell/extensions/fuckwayland-overlap@fuckwayland \
                       2>/dev/null | tr '\n' ' '")
    same "every path the package owned is gone" "" "$(printf '%s' "$left" | tr -d ' \r')"
    # The six names go with it: usr/bin is dpkg's, so `command -v` finding one
    # afterwards is a leftover (or a pip install nobody mentioned).
    same "no tool of ours is on PATH any more" "" \
         "$(guest 'for t in wdotool wwmctl wxprop wxrandr warandr wmirror; do command -v $t; done 2>/dev/null' \
              | tr -d ' \n\r')"
    # The node: postrm removes the ACL entry rather than masking it, because
    # udev's uaccess builtin only ever ADDS entries and a left-over one would
    # make the next install a no-op and leave input broken.
    same "/dev/uinput is back to root:root 0600" "root:root 600" \
         "$(root "stat -c '%U:%G %a' /dev/uinput 2>/dev/null" | tr -d '\r')"
    wantnot "no ACL entry is left on /dev/uinput" "^user:[^:]+:" \
            "$(root 'getfacl -p /dev/uinput 2>/dev/null' || true)"
    same "no uaccess tag is left for the node" "" \
         "$(root "grep -l uinput /run/udev/tags/uaccess/* 2>/dev/null" | tr -d ' \n\r')"
    # dh_python3 byte-compiles into dist-packages and nowhere else; a
    # __pycache__ under /usr/lib/fuckwayland or /usr/bin is py3compile having
    # been pointed at the wrong tree, and it outlives the remove because dpkg
    # does not own it.
    same "py3compile left no __pycache__ outside dist-packages" "" \
         "$(root "find /usr/lib/fuckwayland /usr/bin -name __pycache__ 2>/dev/null" | tr -d ' \n\r')"
    # A remove takes the stamp with it, so the next install is a first install
    # and owes the user the relogin sentence again.
    "$VM" scp "$NAME" "$DEB" "$NAME:/tmp/fw.deb" >/dev/null
    out=$(root "cd /tmp && apt-get install -y ./fw.deb 2>&1") || true
    want "installing again prints the first-install banner again" \
         "LOG OUT AND BACK IN ONCE" "$out"
}

if [ "$REMOVE" = 1 ]; then
    if [ "$MODE" != deb ]; then
        note "--remove needs --deb (the tree's zipapps in /usr/local/bin are not dpkg's); skipped"
    else
        PHASE=remove
        step "phase remove"
        phase_remove || note "(phase remove returned $? -- checks above stand)"
        shot remove
        PHASE=-
    fi
fi

step "done: $PASSES pass, $FAILS fail"
echo "log:  $LOG"
echo "shots: $SHOTDIR"
if [ "$FAILS" -gt 125 ]; then exit 125; fi
exit "$FAILS"
