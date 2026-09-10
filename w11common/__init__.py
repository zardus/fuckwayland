"""What every command here shares: which session this is, how to hand a command over to the X11 original,
the two wire clients, and the three small things a command does whatever it is for -- fail, print, and start
something that outlives it.

A package of its own rather than a corner of `wdotool`, because this is the whole of what the *display* tools
use of that package: they find a session, talk D-Bus and talk Wayland, and they never type a key, never open
a window backend and never start the input daemon.

- `session`      -- which Wayland/X11 session this is, and where its sockets,
                    cookies and runtime directory are, from any uid.
- `passthrough`  -- on an X11 session, execve the real xdotool/wmctrl/xprop/
                    xrandr with argv untouched, and never ourselves.
- `dbus_mini`    -- a D-Bus client: SASL, marshalling, calls, signals.
- `wayland_mini` -- a Wayland client: the registry, roundtrips, fd passing.
- `errors`       -- `CmdError`, the exception every command catches.
- `stdio`        -- what a tool does about a standard output that is gone.
- `procs`        -- detached children, and the /proc facts that outlive them.

Nothing here imports anything outside it: the package is closed, which is what lets a zipapp of a display
tool carry it and nothing else.
"""

#: The release. This is the constant the packages build their own VERSION
#: from; pyproject.toml, debian/changelog and flake.nix state the same number
#: for their own build systems, and scripts/build-deb.sh refuses to build
#: when the first two disagree.
VERSION = "0.4.0"
