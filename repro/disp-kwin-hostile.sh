#!/bin/bash
# hostile-2 (KWin half) and the rest of the hostile argument set, against the
# repo's wire-level FakeKWin driven through the real Session.
#   hostile_kwin.sh <tree>
T=${1:-$HOME/work/sd-fix}
export W11_PASSTHROUGH=never PYTHONPATH=$T
cd "$T" || exit 1
rm -f /tmp/kwin_path.txt
setsid python3 "$(dirname "$0")/fake_kwin_server.py" "$T" two \
    </dev/null >/tmp/kwin_path.txt 2>/dev/null &
for i in $(seq 1 40); do [ -s /tmp/kwin_path.txt ] && break; sleep 0.3; done
export WAYLAND_DISPLAY=$(head -1 /tmp/kwin_path.txt)
export XDG_RUNTIME_DIR=$(mktemp -d)
t() {
  start=$(date +%s)
  out=$(timeout -s KILL 25 python3 -m wxrandr --backend kwin "$@" 2>&1); rc=$?
  el=$(( $(date +%s) - start ))
  tag=ok; case "$out" in *Traceback*) tag=TRACEBACK;; esac
  [ $rc -eq 137 ] && tag=HANG
  printf '%-8s rc=%-4s %2ds %-42s | %s\n' "$tag" "$rc" "$el" "$*" \
         "$(echo "$out"|head -2|tr '\n' '~'|cut -c1-96)"
}
O=eDP-1; P=DP-1
echo "########## TREE=$T ##########"
t --output $O --scale 99999
t --output $O --scale 200
t --output $O --scale-from 1x1
t --output $O --scale inf
t --output $O --scale nan
t --output $O --rate 1e400
t --output $O --brightness nan
t --dpi 0 --verbose --output $O --auto
t --dryrun --dpi 0 --output $O --auto
t --dryrun --output $P --primary
t --newmode Z 0 0 0 0 0 0 0 0 0
t --output $O --pos 99999999999999999999x0
t --output $O --same-as $P --output $P --same-as $O
rm -rf "$XDG_RUNTIME_DIR"
pkill -f "fake_kwin_server.py $T" >/dev/null 2>&1
exit 0
