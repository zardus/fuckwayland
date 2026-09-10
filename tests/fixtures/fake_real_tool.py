#!/usr/bin/env python3
"""Stand-in for the real xdotool/wmctrl/xprop/xrandr in the passthrough tests.

Appends one JSON line per invocation to ``$FAKE_REAL_LOG``::

    {"argv0": ..., "argv": [...], "pid": ..., "ppid": ..., "cwd": ...,
     "env": {"DISPLAY": ..., "XAUTHORITY": ..., "_W11_PASSTHROUGH": ...}}

then prints a marker on stdout and exits ``$FAKE_REAL_RC`` (default 0).

* ``FAKE_REAL_SIGNAL=15`` — kill itself with that signal instead of exiting
  (so the caller can prove signal death survives the handover).
Two things this stand-in deliberately cannot show, because it is a *script*
and the originals are compiled binaries -- the tests use /bin/sh for both:

* ``argv0``: when the kernel follows a shebang it replaces argv[0] with the
  script's path, so what is logged here names the file that was exec'd, not
  the argv[0] we chose;
* the inherited signal dispositions: CPython installs SIG_IGN for SIGPIPE
  itself during startup.

NOTE: this file must not carry the clone's build stamp (``passthrough.STAMP``)
or import one of its packages in its first 4 KiB, or the head-sniff guard would
take it for one of ours and refuse to hand over to it.
"""

import json
import os
import signal
import sys


def main():
    rec = {
        "argv0": sys.argv[0],
        "argv": sys.argv[1:],
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "cwd": os.getcwd(),
        "env": {k: os.environ.get(k) for k in
                ("DISPLAY", "XAUTHORITY", "_W11_PASSTHROUGH",
                 "WAYLAND_DISPLAY", "XDG_SESSION_TYPE")},
    }
    log = os.environ.get("FAKE_REAL_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(rec) + "\n")
    sys.stdout.write("fake-real-tool %s\n" % os.path.basename(sys.argv[0]))
    sys.stdout.flush()

    sig = os.environ.get("FAKE_REAL_SIGNAL")
    if sig:
        n = int(sig)
        signal.signal(n, signal.SIG_DFL)
        os.kill(os.getpid(), n)
        os.pause()
    return int(os.environ.get("FAKE_REAL_RC") or 0)


if __name__ == "__main__":
    sys.exit(main())
