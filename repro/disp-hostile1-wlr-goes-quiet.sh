#!/bin/bash
# hostile-1 reproducer: a wlroots compositor that stops answering at three
# different moments.  Before the fix, "silent-apply" hangs forever (killed at
# 30 s, rc=137); after it, the CLI returns inside the 10 s guard.
cd "$(dirname "$0")/.." || exit 1
export W11_PASSTHROUGH=never PYTHONPATH=$PWD
for m in normal silent-start silent-apply mute-apply; do
  rm -f /tmp/fakewl-$m.sock
  setsid python3 tests/fixtures/fake_wlr.py /tmp/fakewl-$m.sock $m </dev/null >/dev/null 2>&1 &
done
sleep 1
t() {
  s=$1; l=$2; shift 2
  start=$(date +%s)
  out=$(WAYLAND_DISPLAY=$s timeout -s KILL 30 python3 -m wxrandr --backend wlr "$@" 2>&1)
  rc=$?; el=$(( $(date +%s) - start ))
  tag=ok
  case "$out" in *Traceback*) tag=TRACEBACK;; esac
  [ $rc -eq 137 ] && tag="HANG(killed at 30s)"
  printf '%-14s %-30s rc=%-4s %2ds  %s\n' "$l" "$*" "$rc" "$el" "$(echo "$out" | head -2 | tr '\n' '~')"
  [ "$tag" != ok ] && printf '   ^^^ %s\n' "$tag"
}
t /tmp/fakewl-normal.sock normal --query
t /tmp/fakewl-normal.sock normal --output HEAD-1 --pos 100x0
t /tmp/fakewl-silent-start.sock silent-start --output HEAD-1 --pos 100x0
t /tmp/fakewl-silent-apply.sock silent-apply --output HEAD-1 --pos 100x0
t /tmp/fakewl-mute-apply.sock mute-apply --output HEAD-1 --pos 100x0
pkill -f 'tests/fixtures/fake_wlr.py' >/dev/null 2>&1
exit 0
