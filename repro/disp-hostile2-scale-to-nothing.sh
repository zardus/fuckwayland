#!/bin/bash
# hostile-2 reproducer: a --scale that truncates the logical size to 0 must be
# refused before anything is sent (the Screen line advertises a 16x16 minimum).
cd "$(dirname "$0")/.." || exit 1
export W11_PASSTHROUGH=never PYTHONPATH=$PWD
rm -f /tmp/fakewl-minsize.sock
setsid python3 tests/fixtures/fake_wlr.py /tmp/fakewl-minsize.sock normal </dev/null >/dev/null 2>&1 &
sleep 1
t() {
  out=$(WAYLAND_DISPLAY=/tmp/fakewl-minsize.sock timeout -s KILL 30 \
        python3 -m wxrandr --backend wlr "$@" 2>&1)
  printf 'rc=%-3s %-46s %s\n' "$?" "$*" "$(echo "$out" | head -1)"
}
t --output HEAD-1 --scale 99999
t --output HEAD-1 --scale 200
t --output HEAD-1 --scale-from 1x1
t --dryrun --output HEAD-1 --scale 99999
t --output HEAD-1 --scale 121          # 1920/121 = 15 -> below the minimum
t --output HEAD-1 --scale 120          # 1920/120 = 16 -> exactly the minimum
t --output HEAD-1 --scale 2            # ordinary, still fine
pkill -f 'tests/fixtures/fake_wlr.py' >/dev/null 2>&1
exit 0
