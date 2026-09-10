"""The command line.

    wmirror SOURCE --to TARGET [--region WxH+X+Y] [--scaling fit|cover|exact]
    wmirror --list | --stop TARGET | --stop-all | --check

This is its own command, not a wxrandr flag, and it is deliberate: see the
"Why a separate command" section of docs/WMIRROR.md. The short of it is that a
mirror here is a resident process, not a layout, so it cannot be spelled in
a saved layout script that has to keep running on a plain X11 box with the
real xrandr.
"""

import argparse
import shlex
import sys

from w11common import session, stdio

from . import VERSION, core, supervise


def _out(line: str = ""):
    sys.stdout.write(line + "\n")


def _err(lines):
    """One refusal: `wmirror: <first line>`, the rest indented under it."""
    if isinstance(lines, str):
        lines = [lines]
    for i, line in enumerate(lines):
        stdio.warn(("wmirror: " if i == 0 else "  ") + line + "\n")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wmirror", add_help=True,
        usage="%(prog)s SOURCE --to TARGET [options]\n"
              "       %(prog)s --list | --stop TARGET | --stop-all | --check",
        description="Mirror an output, or a region of one, onto another "
                    "output on wlroots compositors, by running wl-mirror "
                    "and owning its lifetime.",
        epilog="Only for what the layout cannot express: two outputs of the "
               "same size at the same position already mirror, byte for "
               "byte, with `wxrandr --output TARGET --same-as SOURCE`, and "
               "wmirror sends you there. A running mirror is a resident "
               "process that keeps the compositor compositing every frame; "
               "`--list` shows it, `--stop` ends it. wlroots only.")
    p.add_argument("source", nargs="?", metavar="SOURCE", help="the output to capture")
    p.add_argument("--to", metavar="TARGET", dest="to", help="the output to paint it on")
    p.add_argument("--region", metavar="WxH+X+Y",
                   help="capture only this rectangle of SOURCE, in layout "
                        "coordinates (the ones `wxrandr --query` and slurp "
                        "print)")
    p.add_argument("--scaling", choices=core.SCALINGS,
                   default=core.DEFAULT_SCALING,
                   help="how the picture meets the target: fit "
                        "(letterbox, default), cover (fill and crop), exact "
                        "(whole multiples only)")
    p.add_argument("--keep-layout", action="store_true",
                   help="mirror even where a shared position would do it, "
                        "so TARGET keeps its own place in the layout")
    p.add_argument("--replace", action="store_true", help="stop the mirror already running on TARGET first")
    p.add_argument("--dry-run", action="store_true", help="print the wl-mirror command line and stop")
    p.add_argument("--list", action="store_true",
                   help="what is mirroring now (verified, stale records "
                        "reaped)")
    p.add_argument("--stop", metavar="TARGET", help="stop the mirror on TARGET")
    p.add_argument("--stop-all", action="store_true", help="stop every mirror we started")
    p.add_argument("--check", action="store_true",
                   help="say whether this session can mirror at all, and "
                        "what is missing if it cannot")
    p.add_argument("--version", action="version", version=VERSION)
    return p


# -- the queries --------------------------------------------------------------

def _state():
    """(state, records, whether reaping changed them). Every command that reads the records verifies them first:
    the helper is invisible to output management, so this file is the only record there is, and a stale line in
    it would be a lie."""
    state = core.load_state()
    recs = core.records(state)
    return state, recs, supervise.reap(recs)


def _cmd_list() -> int:
    # under the lock like every other command that may write: a reap that dropped a record a concurrent start
    # had just replaced would take the new mirror out of the file and leave it running, unfindable.
    with core.state_lock():
        state, recs, changed = _state()
        if changed:
            state.save()
        lines = [core.fmt_record(t, recs[t]) for t in sorted(recs)]
    for line in lines:
        _out(line)
    return 0


def _cmd_stop(target: str) -> int:
    with core.state_lock():
        state, recs, changed = _state()
        rec = recs.pop(target, None)
        if changed or rec is not None:
            state.save()
        if rec is None:
            _err(["no mirror is running on %s" % target, "`wmirror --list` shows the ones that are"])
            return 1
        supervise.stop_record(rec)
    _out("stopped  %s" % core.fmt_record(target, rec))
    return 0


def _cmd_stop_all() -> int:
    with core.state_lock():
        state, recs, changed = _state()
        stopped = []
        for target in sorted(recs):
            rec = recs[target]
            supervise.stop_record(rec)
            stopped.append(core.fmt_record(target, rec))
        if recs or changed:
            recs.clear()
            state.save()
    for line in stopped:
        _out("stopped  %s" % line)
    return 0


def _cmd_check() -> int:
    ok = True
    helper = core.find_helper()
    if helper:
        version = core.helper_version(helper)
        _out("helper:   %s%s" % (helper, " (%s)" % version if version else ""))
    else:
        ok = False
        _out("helper:   not installed")
        for line in core.missing_helper_lines():
            _out("          " + line)
    hit = session.find_wayland_socket()
    _out("wayland:  %s" % (hit[2] if hit else "none"))
    try:
        conn = core.open_conn()
    except core.Refusal as e:
        for i, line in enumerate(e.lines):
            _out(("problem:  " if i == 0 else "          ") + line)
        return 1
    try:
        have = core.capture_support(conn)
        if have:
            _out("capture:  %s" % ", ".join("%s v%d" % (i, v) for i, v in have))
        else:
            ok = False
            for i, line in enumerate(core.no_capture_lines()):
                _out(("capture:  " if i == 0 else "          ") + line)
        try:
            outputs = core.read_outputs(conn)
            on = [o for o in outputs if o.active]
            off = [o.name for o in outputs if not o.active]
            _out("outputs:  %s" % (", ".join("%s %s" % (o.name, o.geom()) for o in on) or "none"))
            if off:
                _out("          off: %s" % ", ".join(off))
        except core.Refusal as e:
            ok = False
            for i, line in enumerate(e.lines):
                _out(("outputs:  " if i == 0 else "          ") + line)
    finally:
        conn.close()
    _, recs, _changed = _state()
    if recs:
        for i, target in enumerate(sorted(recs)):
            _out(("mirrors:  " if i == 0 else "          ") + core.fmt_record(target, recs[target]))
    else:
        _out("mirrors:  none")
    return 0 if ok else 1


# -- starting one -------------------------------------------------------------

def _cmd_start(args) -> int:
    source, target = args.source, args.to
    region = core.parse_region(args.region) if args.region else None

    helper = core.find_helper()
    if helper is None:
        # on an X11 box the missing binary is not the point: there is no wl-mirror for X11 and never will be,
        # and `xrandr --same-as` is the answer. Say what is missing only where installing it would help.
        if session.find_wayland_socket() is None:
            raise core.Refusal(core.no_session_lines())
        raise core.Refusal(core.missing_helper_lines())

    conn = core.open_conn()
    try:
        core.require_capture(conn)
        outputs = core.read_outputs(conn)
    finally:
        conn.close()

    with core.state_lock():
        return _start_locked(args, source, target, region, outputs, helper)


def _start_locked(args, source, target, region, outputs, helper) -> int:
    """Decide and start with the records held still.

    The lock spans read-decide-start-write because every part of it reads the file: two starts that both read it
    empty would both spawn a helper, and the second write would drop the first record -- leaving a wl-mirror
    fullscreen on the target that nothing here could find or stop."""
    state, recs, changed = _state()
    # --replace must not be destructive on a refusal: decide FIRST, with the
    # record it would replace out of the way, and only then stop it.
    running = {k: v for k, v in recs.items() if not (args.replace and k == target)}
    decision = core.decide(outputs, source, target, region, args.keep_layout, running)
    if decision.verdict != core.RUN:
        if changed:
            state.save()                  # the reap, if it found anything
        _err(decision.lines)
        return 0 if decision.verdict == core.DONE else 1

    argv = core.build_argv(source, target, region, args.scaling, helper)
    if args.dry_run:
        if changed:
            state.save()
        _out(" ".join(shlex.quote(a) for a in argv))
        return 0
    if target in recs:                       # --replace, and it is going
        supervise.stop_record(recs.pop(target))

    hit = session.find_wayland_socket()
    src = core.by_name(outputs, source)
    try:
        err = supervise.start(recs, source, target, argv, region=region,
                              scaling=args.scaling,
                              wayland_socket=hit[2] if hit else None,
                              src_rect=src.rect() if src else None)
    finally:
        # even a Ctrl-C in the second this blocks for must leave the mirror written down: the supervisor names
        # itself before it can fail, and an unrecorded helper is one nobody can stop.
        state.save()
    if err:
        _err(err)
        return 1
    rec = recs[target]
    if not core.recorded(target, rec):
        supervise.stop_record(rec)
        recs.pop(target, None)
        state.save()
        _err(["started %s but could not write it down in %s"
              % (core.HELPER, core.state_path()),
              "stopped it again rather than leave a mirror nothing can end"])
        return 1
    _out(core.fmt_record(target, rec))
    return 0


# -- entry --------------------------------------------------------------------

def _run(args, p) -> int:
    queries = [bool(args.list), bool(args.stop), bool(args.stop_all), bool(args.check)]
    if sum(queries) > 1:
        p.error("--list, --stop, --stop-all and --check are one at a time")
    if any(queries):
        if args.source or args.to:
            p.error("--list, --stop, --stop-all and --check take no outputs")
        if args.list:
            return _cmd_list()
        if args.stop:
            return _cmd_stop(args.stop)
        if args.stop_all:
            return _cmd_stop_all()
        return _cmd_check()
    if not args.source or not args.to:
        p.error("name the output to capture and the one to paint it on: " "wmirror SOURCE --to TARGET")
    return _cmd_start(args)


def main(argv=None) -> int:
    """wmirror never hands over to an X11 original: it has none. (warandr is
    the other tool in this box with no original; see passthrough.py.)"""
    stdio.repair_std()      # fd 1 or 2 closed before Python started
    quiet = False
    try:
        p = parser()
        args = p.parse_args(sys.argv[1:] if argv is None else list(argv))
        code = _run(args, p)
    except SystemExit as e:
        # argparse's --help/--version and its usage errors: they used to leave main() with the help text still
        # buffered, so a full or closed stdout became exit 120 out of the interpreter's own exit-time flush.
        stdio.exit_after_flush("wmirror", e)
        raise               # unreachable; the line above raises
    except core.Refusal as e:
        _err(e.lines)
        code = 1
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        code = 1
    except Exception as e:
        # never a traceback: a compositor that drops the connection mid-query, an unreadable state file, a
        # helper that vanishes between the check and the signal -- one line, exit 1.
        _err(["%s" % e])
        # An OSError here is a write to stdout that failed (a full disk, a quota, `>/dev/full`): the flush below
        # is about to fail with the same errno, and the originals print one line, not two.
        quiet = isinstance(e, OSError)
        code = 1
    return code if stdio.flush_stdout("wmirror", quiet) else (code or 1)
