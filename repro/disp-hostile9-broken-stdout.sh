#!/bin/bash
# hostile-9: a broken stdout must exit 1, never the interpreter's 120.
cd "$(dirname "$0")/.." || exit 1
export W11_PASSTHROUGH=never PYTHONPATH=$PWD
rm -f /tmp/fakewl-exit.sock; rm -rf /tmp/exit-rt; mkdir -p /tmp/exit-rt
setsid python3 tests/fixtures/fake_wlr.py /tmp/fakewl-exit.sock normal </dev/null >/dev/null 2>&1 &
sleep 1
run() { XDG_RUNTIME_DIR=/tmp/exit-rt WAYLAND_DISPLAY=/tmp/fakewl-exit.sock \
        python3 -m wxrandr --backend wlr "$@"; }
run --query >&- 2>/tmp/e1.txt; echo "closed stdout      rc=$? stderr=$(head -1 /tmp/e1.txt)"
run --query 2>/tmp/e2.txt | head -1 >/dev/null; echo "head -1 (SIGPIPE)  rc=${PIPESTATUS[0]} stderr=$(head -1 /tmp/e2.txt)"
run --query >/dev/full 2>/tmp/e3.txt; echo "/dev/full          rc=$? stderr=$(head -1 /tmp/e3.txt)"
pkill -f 'tests/fixtures/fake_wlr.py' >/dev/null 2>&1
exit 0
