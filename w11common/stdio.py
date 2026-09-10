"""What a tool does about a standard output that is not there any more.

Three things go wrong with stdout, and each one used to end in a traceback,
in a status no original ever produces, or in silence where output was lost:

* the reader of a pipe leaves early (``wdotool search . | head -1``).  The
  originals die of SIGPIPE without a word.  Python turns SIGPIPE into
  BrokenPipeError, and then -- because the interpreter flushes stdout once
  more on the way out -- prints "Exception ignored in: <_io.TextIOWrapper
  name='<stdout>' ...>" after the tool has already finished.
* the file behind stdout cannot take what was written (``>/dev/full``, a
  full disk, a quota).  Nothing reached the reader, so exiting 0 is a lie,
  and the interpreter's exit-time flush turns whatever was returned into
  exit 120 -- a status none of the six originals has.
* fd 1 (or fd 2) was closed before the interpreter started (``>&-``, a
  daemon that closed its descriptors).  ``sys.stdout`` is then None and the
  first ``print()`` is an AttributeError.
* stderr cannot take the diagnostic either (``tool >/dev/full 2>&1``, a cron
  job whose log filled the disk).  The failed write escaped ``main()`` as a
  traceback, and the exit-time flush of the bytes still in that buffer made
  the status 120 as surely as stdout's does.

`repair_std()` answers the third at the top of ``main()``; `warn()` answers
the fourth wherever a tool reports; `flush_stdout()` answers the first two at
the bottom of ``main()``.  It CLOSES the stream on failure,
which is the part that matters: the interpreter skips a *closed* stdout when
it flushes the standard files at exit, and flushes -- and fails on, and
turns into exit 120 -- an open one.  So nothing may print after it.
"""

import os
import sys


def repair_std() -> None:
    """Give `main()` a stdout and a stderr to write to when fd 1 or fd 2 was closed before the interpreter
    started.

    /dev/null, not an error: the tools we clone are C programs whose `printf` to a closed fd fails quietly, and
    a tool that cannot say anything still has work to do (`wdotool key ctrl+c >&-` must press the keys).
    Idempotent, and never raises.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w"))
            except OSError:
                pass


def warn(text: str) -> None:
    """Write one diagnostic to stderr, and never raise or leave 120 behind.

    `main()`'s except-blocks are the last place a tool speaks, and `tool >/dev/full 2>&1` (a full disk, a quota,
    a cron job whose log filled up) is the case where that write itself fails: the OSError escaped `main()` as a
    traceback, and the interpreter's exit-time flush of the failed buffer turned the status into 120 -- the one
    the module docstring above says no original produces.  So the write is guarded, and on failure the stream is
    CLOSED, for the same reason `flush_stdout()` closes stdout: a closed stderr is skipped by that exit-time
    flush, an open one with unwritable bytes still in it is not.  Nothing may print after it, which is why this
    is for the diagnostic that ends the run.
    """
    err = getattr(sys, "stderr", None)
    if err is None:                       # `2>&-` without repair_std()
        return
    try:
        err.write(text)
        err.flush()
        return
    except (OSError, ValueError, AttributeError):
        pass
    try:
        err.close()
    except (OSError, ValueError, AttributeError):
        pass


def flush_stdout(prog: str, quiet: bool = False) -> bool:
    """Push out what was printed, and say whether it got there.

    Call it last in `main()`: on failure the stream is closed, which is what stops the interpreter flushing it
    again on the way out (see the module docstring), and a closed stdout raises on the next `print()`.

    Silent on a broken pipe -- xrandr, xprop, wmctrl and xdotool all say nothing when their reader leaves -- and
    otherwise one ``prog: message`` line on stderr, never a traceback.  `quiet` is for the caller that has
    already printed that line: a write big enough to overflow the buffer fails inside the command instead of
    here, and the same errno reported twice helps nobody.
    """
    out = getattr(sys, "stdout", None)
    if out is None:                       # `>&-` without repair_std()
        return True
    try:
        out.flush()
        return True
    except BrokenPipeError:
        pass
    except (OSError, ValueError, AttributeError) as e:
        if not quiet:
            warn("%s: %s\n" % (prog, e))   # itself guarded: stderr may be gone too
    try:
        out.close()                       # flushes again, and may fail again
    except (OSError, ValueError, AttributeError):
        pass                              # closed all the same: CPython marks
    return False                          # the file closed before re-raising


def exit_after_flush(prog: str, exc: SystemExit) -> None:
    """Flush on the way out through a SystemExit and re-raise it.

    argparse's ``--help``/``--version`` and its parse errors leave `main()` this way, which is how
    `--help >/dev/full` used to reach the exit-time flush with everything still buffered and exit 120.  The
    exception is re-raised rather than turned into a return value: callers that embed a tool as a library catch
    it, and `main()`'s own contract of *returning* the status is for the paths that get that far.  A lost stdout
    still turns a success into a failure.
    """
    code = exc.code
    if not isinstance(code, int):
        code = 0 if code is None else 1
    if flush_stdout(prog):
        raise exc
    raise SystemExit(code or 1) from None
