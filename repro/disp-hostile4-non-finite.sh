#!/bin/bash
# hostile-4: what a non-finite number does to each option that takes one.
cd "$(dirname "$0")/.." || exit 1
export W11_PASSTHROUGH=never PYTHONPATH=$PWD
rm -f /tmp/fakewl-nf.sock; rm -rf /tmp/nf-rt; mkdir -p /tmp/nf-rt
setsid python3 tests/fixtures/fake_wlr.py /tmp/fakewl-nf.sock normal </dev/null >/dev/null 2>&1 &
sleep 1
t() {
  out=$(XDG_RUNTIME_DIR=/tmp/nf-rt WAYLAND_DISPLAY=/tmp/fakewl-nf.sock \
        timeout -s KILL 20 python3 -m wxrandr --backend wlr "$@" 2>&1)
  printf 'rc=%-3s %-52s %s\n' "$?" "$*" "$(echo "$out" | head -1 | cut -c1-90)"
}
for v in nan inf -inf 1e400 nan0x; do
  t --output HEAD-1 --scale $v
done
for v in nan inf 1e400; do
  t --output HEAD-1 --rate $v
  t --dryrun --output HEAD-1 --brightness $v
  t --dryrun --output HEAD-1 --gamma $v:1:1
  t -s $v
  t --newmode nm $v 100 200 300 400 100 200 300 400
done
pkill -f 'tests/fixtures/fake_wlr.py' >/dev/null 2>&1
exit 0
