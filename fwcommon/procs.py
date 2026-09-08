"""Detached children, and the /proc facts that outlive them.

Two of these tools start a process that must outlive the command that
started it: the gamma holder that keeps a zwlr_gamma_control alive for as
long as a brightness is set, and the supervisor that owns one screen-copy
helper. What they share is not a spawn but a protocol, because of what it
promises:

  * double-fork + setsid, so nothing is left in our process group and the
    child survives the shell -- and the terminal -- that started it;
  * the child writes `pid <pid> <starttime>` up a status pipe BEFORE it can
    fail, and the parent acts on that line the moment it arrives. Not when
    the start finishes: a start that hangs, or that the user interrupts
    halfway through, must still leave a record naming a process something
    later can stop. An orphan nobody can end is the one outcome this
    protocol exists to prevent, and buffering the lines would produce it;
  * liveness is (pid, starttime) read out of /proc, so a recycled pid is
    never mistaken for the process we started, and nothing is signalled
    that is not ours (the euid check);
  * every kill is bounded -- SIGTERM, wait, SIGKILL, confirm -- and never
    fire-and-forget.

What a status line means is the caller's business: this module knows only
which line ends the start. Standard library only, like everything under
fwcommon/.
"""

import os
import select
import signal
import time


# -- identity -----------------------------------------------------------------

def as_pid(value) -> int | None:
    """`value` as a pid, or None when it does not name a process at all.

    Every pid here arrives from a JSON state file that is plain text and
    hand-editable, so it can be a string, a float, a list or None. Only a
    positive int names a process: `"/proc/%d" % "4242"` raises TypeError, and
    that traceback came out of the callers' list, stop and stop-all commands
    alike as `%d format: a real number is required, not str` -- one mistyped
    line in the file and every command, including the ones that would have
    cleaned it up, refused to run. A bool is not a pid either: `True` is an
    int and would have been read as pid 1, which is init."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def proc_starttime(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat, the half of an identity that a pid alone does not give: pids are recycled,
    (pid, starttime) pairs are not. None when the process is gone (or /proc is not there to ask)."""
    pid = as_pid(pid)
    if pid is None:
        return None
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
        # field 22, counting from 1; comm (field 2) may contain spaces —
        # everything after the closing paren is space-separated.
        after = data[data.rindex(b")") + 2:].split()
        return after[19].decode()  # 22 - 2 (pid, comm)
    except (OSError, ValueError, IndexError):
        return None


def comm(pid: int) -> str | None:
    """/proc/<pid>/comm, the executable name, or None."""
    pid = as_pid(pid)
    if pid is None:
        return None
    try:
        with open("/proc/%d/comm" % pid) as f:
            return f.read().strip()
    except OSError:
        return None


def cmdline(pid: int) -> str | None:
    """/proc/<pid>/cmdline, the NULs turned back into spaces, or None.

    comm is the 15-character executable name, and every detached child in this
    tree is `python3` -- so a `'?'` record matched on comm alone claimed every
    python process on the box (measured: `python3 -c "import time;
    time.sleep(30)"`, a bystander, answered True for a supervisor record).
    The command line is where a caller's own name actually appears, in all
    three shapes these tools ship in: a console script, `python3 -m <pkg>`,
    and a test file run by path."""
    pid = as_pid(pid)
    if pid is None:
        return None
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return " ".join(raw.decode("utf-8", "replace").split("\0")).strip()


def zombie(pid: int) -> bool:
    """Has that process already exited, with only its exit status left?

    /proc still has the directory, the uid and the start time of a zombie, so every other test here says it is
    alive -- and a query would report a running child that had exited, a stop would report stopping it. The
    state letter is the only thing that tells them apart."""
    pid = as_pid(pid)
    if pid is None:
        return False
    try:
        with open("/proc/%d/stat" % pid) as f:
            data = f.read()
    except OSError:
        return False
    try:                    # comm is parenthesised and may contain spaces
        return data.rsplit(")", 1)[1].split()[0] == "Z"
    except IndexError:
        return False


def owned_by_us(pid: int) -> bool:
    """A process we started runs as us. Anything else is never signalled, whatever a state file claims. (Under
    sudo "us" is root, and what root forked is root too.)"""
    pid = as_pid(pid)
    if pid is None:
        return False
    try:
        return os.stat("/proc/%d" % pid).st_uid == os.geteuid()
    except OSError:
        return False


def alive(pid, start, comm_hint=None) -> bool:
    """Is that exact process still running?

    With a starttime the answer is exact. Without one (a '?' record, written when /proc could not be read) we
    fall back to the process NAME AND COMMAND LINE against `comm_hint` -- one string or several: never matching
    would strand a child that is holding something, with no way left to stop it. Both, because comm is the
    15-character executable name and every detached child here is `python3`: a hint naming the caller's own
    command only ever appears in the command line, and a hint of "python" against comm alone made every python
    process on the box answer True."""
    if as_pid(pid) is None:
        return False
    if not owned_by_us(pid):
        return False
    cur = proc_starttime(pid)
    if cur is None:
        return False
    if zombie(pid):
        return False
    if start and start != "?":
        return cur == start
    hints = ((comm_hint,) if isinstance(comm_hint, str)
             else tuple(comm_hint or ()))
    hay = " ".join(x for x in (comm(pid), cmdline(pid)) if x).lower()
    return bool(hay and any(h.lower() in hay for h in hints))


# -- ending -------------------------------------------------------------------

def wait_gone(pid: int, start, tries: int = 50) -> bool:
    """Poll (bounded) until `pid` is gone / recycled. With a real starttime we detect recycle too; for a '?'
    record we can only watch for disappearance."""
    if as_pid(pid) is None:
        return True           # not a process: nothing to wait for
    for _ in range(tries):
        cur = proc_starttime(pid)
        if cur is None or (start != "?" and cur != start):
            return True
        time.sleep(0.02)
    return False


def kill_bounded(pid: int, start=None) -> bool:
    """SIGTERM, bounded wait, SIGKILL, confirm. Never fire-and-forget."""
    if as_pid(pid) is None:
        return False          # not a process: nothing was killed
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    if wait_gone(pid, start if start else "?"):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return True
    wait_gone(pid, start if start else "?")   # confirm the SIGKILL took
    return True


# -- the status pipe ----------------------------------------------------------

#: Status fds this process has already closed from `emit(..., close=True)`.
#: The fd number is reused by the next open, and a detached child does open
#: things after its verdict -- measured on a real headless sway, a mirror
#: supervisor's status pipe was (3, 4) and its fd table a moment later was
#: {3: its helper's stderr file, 4: the compositor socket}. A second emit on
#: that number would write a status line into somebody else's connection
#: and then close it. Cleared in the forked child of `spawn_detached`, whose
#: fd table is its own from that point on.
_CLOSED_STATUS_FDS = set()


def emit(status_fd, msg: str, close: bool = False):
    """One line from a detached child to whoever started it. Never raises: the reader is a command that may have
    gone away already, and a child that died of its own status pipe is exactly the orphan this module is here to
    prevent. Nothing is written after the line that closed the pipe -- see `_CLOSED_STATUS_FDS`."""
    if status_fd is None or status_fd in _CLOSED_STATUS_FDS:
        return
    try:
        os.write(status_fd, (msg + "\n").encode())
        if close:
            os.close(status_fd)
            _CLOSED_STATUS_FDS.add(status_fd)
    except OSError:
        pass


def spawn_detached(child_main, seconds: float, on_line) -> str | None:
    """Fork a detached grandchild running `child_main(status_fd)`, then read its status pipe for up to `seconds`
    and return the line that ended the start (None if none came in time, or the pipe closed first).

    Every line is handed to `on_line` as it arrives, and the first line `on_line` does not claim (does not
    return true for) is that terminal line. The caller's `on_line` is therefore where a child's identity is
    written down, while the start is still running -- see the module docstring: that timing is the whole
    point."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:                  # child: detach, the grandchild is the work
        try:
            os.close(r)
            os.setsid()
            if os.fork():
                os._exit(0)
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.close(devnull)
            _CLOSED_STATUS_FDS.clear()   # the parent's fd history is not ours
            child_main(w)
        except Exception as e:
            # The grandchild has no stderr (it was pointed at /dev/null two
            # lines up) and nobody waits for it, so an exception here used to
            # be invisible: the start read nothing, timed out after
            # STARTUP_SECONDS + 4 s, and said "did not report that it
            # started" -- while keeping a record of a supervisor that had
            # already died. Measured with tempfile.mkstemp raising EROFS,
            # which is what a read-only /tmp does to _Stderr(). The status
            # pipe is the only way out, and `failed ` is the prefix the
            # caller's on_line already reads as a verdict.
            #
            # `Exception`, deliberately, not `BaseException`: every ordinary
            # stop of a detached child -- `warandr`'s gamma release, and the
            # mirror supervisor's -- ends it by raising SystemExit(0) out of
            # its own SIGTERM handler, long after `emit("ok", close=True)`.
            # Caught, that called a finished job a failed start, and the emit
            # landed on a reused fd number -- the compositor socket, on sway.
            # SystemExit and KeyboardInterrupt fall through to the
            # `os._exit(0)` below, which is what they did before this clause.
            emit(w, "failed supervisor: %r" % e, close=True)
        finally:
            os._exit(0)
    os.close(w)
    os.waitpid(pid, 0)            # the middle process, already exiting
    deadline = time.monotonic() + seconds
    buf = b""
    status = None
    try:
        while status is None and time.monotonic() < deadline:
            ready, _, _ = select.select([r], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(r, 4096)
            except OSError:
                break
            if not chunk:         # the child closed it: no more lines coming
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.decode(errors="replace").strip()
                if not on_line(line):
                    status = line
                    break
    finally:
        os.close(r)               # also on the Ctrl-C that gets us here
    return status
