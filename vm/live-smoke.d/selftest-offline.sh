#!/usr/bin/env bash
# live-smoke's own regression: the phases, with no VM.
#
# It runs vm/live-smoke.sh against vm/live-smoke.d/fake-vmctl, which replays
# tests/fixtures/live/noble-gnome-46.0-windows-wm-replay.txt -- the `windows`
# and `wm` phases' guest commands, recorded off the real GNOME 46.0 guest on
# 2026-09-08 in the order the phases ask them.  Five passes:
#
#   1. the recording as it is: every check has to PASS.  A check that cannot
#      pass against the desktop it was written from is a broken check.
#   2. the same recording with ONE answer replaced -- the geometry after
#      `wwmctl -b remove,maximized_vert,maximized_horz` becomes the maximized
#      one, which is what the tools did before b7a60f0 -- and exactly one check
#      has to go red, the one named for that fix.  A check that cannot fail is
#      not a check.
#
# Passes 3 and 4 are the recorder check (phase busrec, T65), which no recording
# off the real guest covers because it is about a process and a growing file
# rather than about bytes a tool printed.  They are two hand-written
# transcripts: a session where `pgrep -x dbus-monitor` names a pid and the log
# grows on one bus call has to PASS, and one where the pid is missing and the
# log stands still has to FAIL.  The check this replaced could not do the
# second: it matched `pgrep -f 'dbus-monitor --session'`, which counts the
# `sh -c` wrapper carrying that very string, so it printed PASS with no
# recorder anywhere.
#
# Pass 5 is the bookkeeping that keeps the other four honest as step files
# arrive: every vm/live-smoke.d/<token>.sh is either covered by a recording here
# or named in tests/fixtures/live/NOT-YET-RUN, never both and never neither.
# Without it a new step file could ship with no recording, no entry, and nothing
# saying so -- and its checks would have been run by nobody.
#
# Takes a couple of seconds and needs nothing but bash and python3.
#   usage: vm/live-smoke.d/selftest-offline.sh
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd)          # <repo>/vm/live-smoke.d
REPO=$(dirname "$(dirname "$HERE")")
CAP=$REPO/tests/fixtures/live/noble-gnome-46.0-windows-wm-replay.txt
[ -f "$CAP" ] || { echo "no capture at $CAP"; exit 2; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
fails=0
say() { echo "$*"; }
bad() { fails=$((fails + 1)); echo "SELFTEST FAIL: $*"; }

run_pass() {   # run_pass <label> <override-file-or-empty> [phases] [transcript] [flavor] -> log on stdout
    local label=$1 override=$2 phases=${3:-windows,wm} cap=${4:-$CAP} flavor=${5:-noble-gnome}
    rm -rf "$WORK/state-$label"; mkdir -p "$WORK/state-$label"
    LIVE_SMOKE_VMCTL=$HERE/fake-vmctl \
    FAKE_VMCTL_TRANSCRIPT=$cap \
    FAKE_VMCTL_OVERRIDE=$override \
    FAKE_VMCTL_STATE=$WORK/state-$label \
    LIVE_SMOKE_OUT=$WORK/out \
        "$REPO/vm/live-smoke.sh" "$flavor" --name fake-$label --reuse --keep \
            --phases "$phases" > "$WORK/$label.log" 2>&1 || true
    cat "$WORK/$label.log"
}

# <flavor> <phases> for a recording, read out of its NAME: strip `-replay.txt`, strip the
# longest flavor prefix, drop the version token that follows it, and the rest is the phase
# list with `-` for `,`.  So `resolute-hypr-0.53.3-windows-wm-replay.txt` is
# `resolute-hypr windows,wm` and `resolute-i3-4.25.1-i3ipc-replay.txt` is `resolute-i3 i3ipc`.
# The name IS the mapping -- tests/test_live_smoke.py:test_every_recording_resolves_to_a_flavor
# says the same thing about the first half of it.
replay_spec() {   # replay_spec <path> -> "<flavor> <phases>" or nothing
    local base fl best rest
    base=$(basename "$1" -replay.txt); best=""
    for fl in "$REPO"/vm/flavors/*.yaml; do
        fl=$(basename "$fl" .yaml)
        case $base in "$fl"-*) [ ${#fl} -gt ${#best} ] && best=$fl ;; esac
    done
    [ -n "$best" ] || return 0
    rest=${base#"$best"-}          # <version>-<phase>[-<phase>...]
    rest=${rest#*-}                # drop the version token
    [ -n "$rest" ] || return 0
    printf '%s %s\n' "$best" "$(printf '%s' "$rest" | tr - ,)"
}

say "== pass 1: the recording as it was captured"
run_pass good "" > "$WORK/good.out"
grep -E '^(PASS|FAIL) ' "$WORK/good.out" | sed 's/^/   /'
n_pass=$(grep -c '^PASS ' "$WORK/good.out" || true)
n_fail=$(grep -c '^FAIL ' "$WORK/good.out" || true)
[ "$n_pass" -ge 15 ] || bad "only $n_pass checks ran against the recording (expected 15 or more)"
[ "$n_fail" = 0 ] || bad "$n_fail check(s) failed against the desktop they were recorded from"

# The pre-b7a60f0 behaviour, in one section: the geometry read AFTER the pair is
# removed answers with the maximized geometry instead of 100,100 800x600.  The
# override table wins over the transcript for that command, and its two entries
# put it back in the same asking order the phase uses.
cat > "$WORK/regress.txt" <<'BREAK'
### wdotool getwindowgeometry 1950993699
Window 1950993699
  Position: 100,100 (screen: 0)
  Geometry: 800x600
### rc=0
### wdotool getwindowgeometry 1950993699
Window 1950993699
  Position: 100,100 (screen: 0)
  Geometry: 800x600
### rc=0
### wdotool getwindowgeometry 1950993699
Window 1950993699
  Position: 66,32 (screen: 0)
  Geometry: 1854x1048
### rc=0
### wdotool getwindowgeometry 1950993699
Window 1950993699
  Position: 66,32 (screen: 0)
  Geometry: 1854x1048
### rc=0
BREAK

say
say "== pass 2: the same recording with the b7a60f0 regression put back in"
run_pass regress "$WORK/regress.txt" > "$WORK/regress.out"
grep -E '^(PASS|FAIL) ' "$WORK/regress.out" | sed 's/^/   /'
if grep -q '^FAIL wm: b7a60f0' "$WORK/regress.out"; then
    say "   (the b7a60f0 check went red, which is the point of this pass)"
else
    bad "the b7a60f0 check did not fail when the regression was put back"
fi
r_fail=$(grep -c '^FAIL ' "$WORK/regress.out" || true)
[ "$r_fail" = 1 ] || bad "expected exactly one failing check in pass 2, got $r_fail"

# ---- the recorder (T65's phase busrec) ---------------------------------------
# A session where the recorder is there: a pid whose process NAME is
# dbus-monitor, and a log that grew from 12 to 57 lines while one deliberate
# bus call was made.  (`wc -l < ... || echo 0` and `pgrep -x dbus-monitor` are
# the two commands the phase asks, as fake-vmctl normalises them.)
cat > "$WORK/busrec-ok.txt" <<'REC'
### pgrep -x dbus-monitor
4242
### rc=0
### wc -l < $HOME/w11-bus.log || echo 0
12
### rc=0
### wc -l < $HOME/w11-bus.log || echo 0
57
### rc=0
REC
# And the session the old check could not tell apart from it: nothing is
# recording, so there is no dbus-monitor process and the log stands at 0.
cat > "$WORK/busrec-none.txt" <<'REC'
### pgrep -x dbus-monitor
### rc=1
### wc -l < $HOME/w11-bus.log || echo 0
0
### rc=0
### wc -l < $HOME/w11-bus.log || echo 0
0
### rc=0
REC

say
say "== pass 3: phase busrec with a recorder running"
run_pass busrec-ok "" busrec "$WORK/busrec-ok.txt" > "$WORK/busrec-ok.out"
grep -E '^(PASS|FAIL) ' "$WORK/busrec-ok.out" | sed 's/^/   /'
grep -q '^PASS busrec: dbus-monitor (pid 4242) is recording' "$WORK/busrec-ok.out" \
    || bad "phase busrec did not pass with a recorder running"

say
say "== pass 4: phase busrec with nothing recording"
run_pass busrec-none "" busrec "$WORK/busrec-none.txt" > "$WORK/busrec-none.out"
grep -E '^(PASS|FAIL) ' "$WORK/busrec-none.out" | sed 's/^/   /'
grep -q '^FAIL busrec: no session-bus recording' "$WORK/busrec-none.out" \
    || bad "phase busrec passed with no recorder: the check is matching itself again"

# ---- pass 5: every step file is recorded or declared not yet run ------------
say
say "== pass 5: every step file is either recorded here or listed in NOT-YET-RUN"
NYR=$REPO/tests/fixtures/live/NOT-YET-RUN
[ -f "$NYR" ] || bad "no $NYR"
# A recording's token is the flavor its name starts with, resolved to that
# flavor's `# vmctl-desktop:`.  Longest match first, so noble-gnome-iso wins
# over noble-gnome for a name that starts with it.
recorded_tokens() {
    local cap base fl best
    for cap in "$REPO"/tests/fixtures/live/*-replay.txt; do
        [ -e "$cap" ] || continue
        base=$(basename "$cap"); best=""
        for fl in "$REPO"/vm/flavors/*.yaml; do
            fl=$(basename "$fl" .yaml)
            case $base in "$fl"*) [ ${#fl} -gt ${#best} ] && best=$fl ;; esac
        done
        [ -n "$best" ] || { echo "?$base"; continue; }
        sed -n 's/^#[[:space:]]*vmctl-desktop:[[:space:]]*//p' "$REPO/vm/flavors/$best.yaml" | head -1
    done
}
rec=$(recorded_tokens | sort -u)
nyr=$(grep -v '^#' "$NYR" | grep . | sort -u)
for f in "$HERE"/*.sh; do
    tok=$(basename "$f" .sh)
    case $tok in common|selftest-offline|guest-*) continue ;; esac
    in_rec=$(printf '%s\n' "$rec" | grep -cx "$tok" || true)
    in_nyr=$(printf '%s\n' "$nyr" | grep -cx "$tok" || true)
    if [ "$in_rec" = 0 ] && [ "$in_nyr" = 0 ]; then
        bad "step file $tok.sh has no recording and is not in NOT-YET-RUN"
    elif [ "$in_rec" != 0 ] && [ "$in_nyr" != 0 ]; then
        bad "step file $tok.sh has a recording AND is still listed in NOT-YET-RUN"
    else
        say "   $tok: $(if [ "$in_rec" != 0 ]; then echo recorded; else echo 'not yet run'; fi)"
    fi
done

# ---- pass 6: every recording replays green against its own step file --------
# `CAP` above is one recording and its two phases; every OTHER recording in the
# directory used to be a file nothing ran.  Batch 16 checked its own by hand and
# said so in requests-batch-11.md item 3; this is that, in the tree, for all of
# them.  A recording is green or it is a claim about a desktop that nobody is
# checking any more.
say
say "== pass 6: every recording replays green against its own flavor's step file"
for cap in "$REPO"/tests/fixtures/live/*-replay.txt; do
    [ -e "$cap" ] || continue
    [ "$cap" = "$CAP" ] && continue          # pass 1 and 2 are this one, in more detail
    spec=$(replay_spec "$cap") || true
    if [ -z "$spec" ]; then
        bad "$(basename "$cap") does not resolve to a flavor and a phase list"
        continue
    fi
    set -- $spec
    lbl=$(basename "$cap" -replay.txt)
    run_pass "$lbl" "" "$2" "$cap" "$1" > "$WORK/$lbl.out"
    p=$(grep -c '^PASS ' "$WORK/$lbl.out" || true)
    f=$(grep -c '^FAIL ' "$WORK/$lbl.out" || true)
    say "   $1 [$2]: $p pass, $f fail"
    [ "$p" -ge 5 ] || bad "$(basename "$cap"): only $p checks ran against it"
    if [ "$f" != 0 ]; then
        grep '^FAIL ' "$WORK/$lbl.out" | sed 's/^/      /'
        bad "$(basename "$cap"): $f check(s) failed against the desktop they were recorded from"
    fi
done

say
if [ "$fails" = 0 ]; then
    say "selftest-offline: OK ($n_pass checks pass on the recording, 1 fails on the regression,"
    say "                  phase busrec passes with a recorder and fails without one, and every"
    say "                  step file is either recorded or declared not yet run)"
    exit 0
fi
say "selftest-offline: $fails problem(s)"
exit 1
