"""Suite-wide safety guard: no test process ever hands itself over to the
real X11 tools.

`w11common.passthrough` replaces this process with `/usr/bin/xdotool` (and
friends) when it decides the session is X11 — which is exactly right for an
installed clone and exactly wrong inside a test runner: ~17 tests call
`cli.main([...])` in-process, and `tests/test_cli_parity.py` shells a shim
named `xdotool` while the real one is on PATH, so on an X11 development box
the parity oracle would compare the real xdotool against itself and pass
tautologically.

Two independent belts, both proven by `tests/test_passthrough.py`:

* this file, imported by pytest before collection, forces the documented
  escape hatch `W11_PASSTHROUGH=never` for the whole process *and*
  for everything it spawns;
* `maybe_exec_real(..., entry=False)`: a `main()` that was handed an explicit
  argv is being used as a library, and we never replace a library caller's
  process. That covers the in-process callers even when the suite is run
  file-by-file (`python3 tests/test_foo.py`), where pytest — and this file —
  never load.
"""

import os

os.environ["W11_PASSTHROUGH"] = "never"

# B13: the injection tests pin the *fixed US table* as the source of
# keycodes. Without this a developer running the suite inside a German or
# Dvorak session would have the daemon read that session's real keymap and
# type through it, and every keycode assertion here would be wrong.
os.environ.setdefault("WDOTOOL_LAYOUT", "us")
