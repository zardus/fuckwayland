"""Desktop/workspace commands (cmd_get_desktop.c and friends). Desktops map to
compositor workspaces, 0-based like EWMH (sway workspace N -> desktop N-1)."""

import sys

from w11common.errors import CmdError
from wdotool.cli import _opts
from wdotool.cnum import atoi as _atoi, strtol as _strtol
from wdotool.window_cmds import _SEE_STACK, _out, _warn_noop, _window_arg


def cmd_set_num_desktops(ctx, args):
    cmd = getattr(ctx, "cmd_name", "set_num_desktops")
    usage = "Usage: %s num_desktops\n" % cmd
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    rest = args[nopts:]
    if not rest:
        raise CmdError(usage.rstrip("\n"))
    n = _strtol(rest[0])
    # B9: actually ask the compositor (the GNOME bridge has SetNWorkspaces and it works whenever Mutter's
    # dynamic-workspaces is off). Only a genuine capability gap -- a compositor whose workspace count is not
    # ours to choose -- degrades to a warning, like the other cosmetic no-ops.
    try:
        ctx.backend().set_num_desktops(n)
    except CmdError as e:
        if not getattr(e, "unsupported", False):
            raise
        _warn_noop("set_num_desktops: %s; ignoring" % e)
    return nopts + 1


def cmd_get_num_desktops(ctx, args):
    cmd = getattr(ctx, "cmd_name", "get_num_desktops")
    usage = "Usage: %s\n" % cmd
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    _out("%d" % ctx.backend().num_desktops())
    return nopts


def cmd_set_desktop(ctx, args):
    cmd = getattr(ctx, "cmd_name", "set_desktop")
    usage = (
        "Usage: %s desktop\n"
        "--relative    - Move relative to the current desktop. Negative values OK\n"
        % cmd
    )
    parsed = _opts(cmd, args, "h", [("help", False), ("relative", False)], usage)
    if parsed is None:
        return len(args)
    opts, nopts = parsed
    relative = any(n == "relative" for n, _ in opts)
    rest = args[nopts:]
    if not rest:
        raise CmdError(usage.rstrip("\n"))
    desktop = _strtol(rest[0])
    if relative:
        cur = ctx.backend().get_desktop()
        n = ctx.backend().num_desktops()
        if n > 0:
            desktop = (desktop + cur) % n
            if desktop < 0:
                desktop += n
    ctx.backend().set_desktop(desktop)
    return nopts + 1


def cmd_get_desktop(ctx, args):
    cmd = getattr(ctx, "cmd_name", "get_desktop")
    usage = "Usage: %s\n" % cmd
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    _out("%d" % ctx.backend().get_desktop())
    return nopts


def cmd_set_desktop_for_window(ctx, args):
    cmd = getattr(ctx, "cmd_name", "set_desktop_for_window")
    usage = "Usage: %s [window=%%1] <desktop>\n%s" % (cmd, _SEE_STACK)
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    rest = args[nopts:]
    warg, used = _window_arg(ctx, rest, 1, usage)
    desktop = _strtol(rest[used])
    for wid in ctx.resolve_windows(warg):
        try:
            ctx.backend().set_window_desktop(wid, desktop)
        except CmdError as e:
            sys.stderr.write("%s\n" % e)
            raise CmdError(
                "xdo_set_desktop_for_window on window %d, desktop %d failed"
                % (wid, desktop)
            ) from None
    return nopts + used + 1


def cmd_get_desktop_for_window(ctx, args):
    cmd = getattr(ctx, "cmd_name", "get_desktop_for_window")
    usage = "Usage: %s [window=%%1]\n%s" % (cmd, _SEE_STACK)
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    warg, used = _window_arg(ctx, args[nopts:], 0, usage)
    for wid in ctx.resolve_windows(warg):
        _out("%d" % ctx.backend().window_desktop(wid))
    return nopts + used


def cmd_get_desktop_viewport(ctx, args):
    cmd = getattr(ctx, "cmd_name", "get_desktop_viewport")
    usage = "Usage: %s\n" % cmd
    parsed = _opts(cmd, args, "h", [("help", False), ("shell", False)], usage)
    if parsed is None:
        return len(args)
    opts, nopts = parsed
    # Wayland compositors have no large-desktop viewport; it is always 0,0.
    if any(n == "shell" for n, _ in opts):
        _out("X=0")
        _out("Y=0")
    else:
        _out("0 0")
    return nopts


def cmd_set_desktop_viewport(ctx, args):
    cmd = getattr(ctx, "cmd_name", "set_desktop_viewport")
    usage = "Usage: %s x y\n" % cmd
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    rest = args[nopts:]
    if len(rest) < 2:
        raise CmdError("Not enough arguments given.\n%s" % usage.rstrip("\n"))
    _atoi(rest[0])
    _atoi(rest[1])
    _warn_noop("set_desktop_viewport: viewports do not exist on Wayland; ignoring")
    return nopts + 2
