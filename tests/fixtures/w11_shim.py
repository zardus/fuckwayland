#!/usr/bin/env python3
"""Stands in for an *installed* copy of the clone in the passthrough tests.

Symlink it as ``xdotool``/``wmctrl``/``xprop``/``xrandr`` (or under our own
names) somewhere on PATH: the tool is picked from ``basename(argv[0])``, so
the tree looks exactly like the real install (`/usr/local/bin/xdotool` ->
w11, `/usr/bin/xdotool` -> the distribution's).

``$W11_SHIM_SEAMS`` (JSON) overrides ``w11common.passthrough``'s discovery
directories in this process, which is how a subprocess test describes a whole
session with a temporary directory (the module constants are the seams; a
child process cannot be monkeypatched).

``_RUN_USER_DIR`` is applied to ``w11common.session.RUN_USER_DIR`` as well, so
that the seamed tree describes the session for *backend* discovery too. Both
scan ``/run/user/*`` rather than trusting the environment, so without this a
wlroots or KWin session belonging to any user of the build machine answers
``--backend sway`` and the test asking for the refusal fails on that machine
and nowhere else.
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__))))
sys.path.insert(0, REPO)

MODULES = {
    "xdotool": "wdotool.cli", "wdotool": "wdotool.cli",
    "wmctrl": "wwmctl.cli", "wwmctl": "wwmctl.cli",
    "xprop": "wxprop.cli", "wxprop": "wxprop.cli",
    "xrandr": "wxrandr.cli", "wxrandr": "wxrandr.cli",
}


def main():
    seams = os.environ.get("W11_SHIM_SEAMS")
    if seams:
        from w11common import passthrough, session
        seams = json.loads(seams)
        for k, v in seams.items():
            setattr(passthrough, k, v)
        if "_RUN_USER_DIR" in seams:
            session.RUN_USER_DIR = seams["_RUN_USER_DIR"]
        passthrough.reset_cache()
    name = os.path.basename(sys.argv[0] or "")
    try:
        modname = MODULES[name]
    except KeyError:
        sys.stderr.write("w11_shim: don't know which tool %r is\n" % name)
        return 2
    mod = __import__(modname, fromlist=["main"])
    return mod.main()


if __name__ == "__main__":
    sys.exit(main())
