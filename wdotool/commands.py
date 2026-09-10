"""Command registry: name -> (module, "cmd_<name>") in the real xdotool dispatch[] order (which is also the
`help` output order).

Modules are imported lazily at dispatch time so partially-implemented command modules still load, import cycles
are impossible, and a missing cmd_* function resolves to a stub that raises CmdError."""

import importlib

from w11common.errors import CmdError

_MISC = "wdotool.misc_cmds"
_INPUT = "wdotool.input_cmds"
_WINDOW = "wdotool.window_cmds"
_DESKTOP = "wdotool.desktop_cmds"
_CLI = "wdotool.cli"

REGISTRY: dict[str, tuple[str, str]] = {
    name: (module, "cmd_" + name)
    for module, names in (
        # Query functions
        (_WINDOW, ["getactivewindow", "getwindowfocus", "getwindowname",
                   "getwindowclassname", "getwindowpid", "getwindowgeometry"]),
        (_MISC, ["getdisplaygeometry"]),
        (_WINDOW, ["search", "selectwindow"]),
        # Help me!
        (_CLI, ["help", "version"]),
        # Action functions
        (_WINDOW, ["behave"]),
        (_INPUT, ["behave_screen_edge", "click", "getmouselocation", "key",
                  "keydown", "keyup", "mousedown", "mousemove",
                  "mousemove_relative", "mouseup"]),
        (_WINDOW, ["set_window"]),
        (_INPUT, ["type"]),
        (_WINDOW, ["windowactivate", "windowfocus", "windowkill", "windowclose",
                   "windowquit", "windowmap", "windowminimize", "windowmove",
                   "windowraise", "windowlower", "windowreparent", "windowsize",
                   "windowstate", "windowunmap"]),
        (_DESKTOP, ["set_num_desktops", "get_num_desktops", "set_desktop",
                    "get_desktop", "set_desktop_for_window",
                    "get_desktop_for_window", "get_desktop_viewport",
                    "set_desktop_viewport"]),
        (_MISC, ["exec", "sleep"]),
    )
    for name in names
}
assert len(REGISTRY) == 48


def is_command(name: str) -> bool:
    """Case-insensitive, like xdotool's is_command()/strcasecmp."""
    return name.lower() in REGISTRY


def lookup(name: str):
    """Resolve a command name (case-insensitively) to its function, or None if it is not a command.
    Not-yet-implemented commands resolve to a CmdError stub so the chain errors cleanly."""
    entry = REGISTRY.get(name.lower())
    if entry is None:
        return None
    module, func = entry
    fn = getattr(importlib.import_module(module), func, None)
    if fn is None:

        def fn(ctx, args, _name=name.lower()):
            raise CmdError("%s: not implemented" % _name)

    return fn
