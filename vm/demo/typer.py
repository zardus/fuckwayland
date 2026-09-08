#!/usr/bin/env python3
"""typer.py -- run a real interactive bash in this terminal and type into it.

Guest side of the README GIF recordings.  Everything on screen is a real shell
running real commands.  This only supplies the keystrokes, at a cadence a viewer
can read, and waits for the output that says the next beat can start.

    typer.py <script.tk>

The script file is a sequence of lines:
    #  ...        comment, ignored
    > COMMAND     type COMMAND, then Return
    ! TEXT        type TEXT with no Return (for a prompt answer, e.g. a password)
    ~ SECONDS     sleep
    @ REGEX SEC   wait until REGEX appears in the output (max SEC)
    ? RE\tSEC\tTEXT  type TEXT (and Return) only if RE appears within SEC
"""
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import fcntl

CPS = float(os.environ.get("TYPE_CPS", "22"))     # characters per second
JITTER = 0.35                                     # +/- fraction of the interval


def winsize(fd):
    try:
        r, c, _, _ = struct.unpack("HHHH", fcntl.ioctl(
            fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)))
        return r, c
    except Exception:
        return 24, 80


class Shell:
    def __init__(self):
        rows, cols = winsize(sys.stdout.fileno())
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.environ["PS1"] = r"\[\e[1;32m\]\u@\h\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]$ "
            os.environ["HISTFILE"] = "/dev/null"
            os.environ["PAGER"] = "cat"
            os.execvp("bash", ["bash", "--norc", "-i"])
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
        self.buf = ""

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                return
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            os.write(1, data)
            self.buf += data.decode("utf-8", "replace")
            self.buf = self.buf[-8000:]

    def wait_for(self, pattern, timeout):
        rx = re.compile(pattern)
        end = time.monotonic() + timeout
        if rx.search(self.buf):
            return True
        while time.monotonic() < end:
            self.pump(0.05)
            if rx.search(self.buf):
                return True
        return False

    def type(self, text, newline=True):
        import random
        self.buf = ""          # patterns are searched in what this command prints
        gap = 1.0 / CPS
        for ch in text:
            os.write(self.fd, ch.encode())
            self.pump(gap * random.uniform(1 - JITTER, 1 + JITTER))
        if newline:
            self.pump(0.18)
            os.write(self.fd, b"\r")
            self.pump(0.12)

    def close(self):
        try:
            os.write(self.fd, b"exit\r")
        except OSError:
            pass
        self.pump(0.5)
        try:
            os.kill(self.pid, signal.SIGHUP)
        except OSError:
            pass


def main():
    lines = open(sys.argv[1]).read().splitlines()
    sh = Shell()
    sh.pump(0.8)
    os.write(1, b"")
    t0 = time.monotonic()
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if os.environ.get("TYPE_TRACE"):
            sys.stderr.write("[%6.2f] %s\n" % (time.monotonic() - t0, line))
            sys.stderr.flush()
        op, _, rest = line.partition(" ")
        if op == ">":
            sh.type(rest)
        elif op == "!":
            sh.type(rest, newline=False)
        elif op == "$":          # type + Return, no echo expectation (answers)
            sh.type(rest)
        elif op == "~":
            sh.pump(float(rest))
        elif op == "@":
            pat, _, secs = rest.rpartition(" ")
            sh.wait_for(pat, float(secs))
        elif op == "?":
            # ? REGEX<TAB>SECONDS<TAB>TEXT -- type TEXT only if REGEX shows up
            pat, secs, text = rest.split("\t")
            if sh.wait_for(pat, float(secs)):
                sh.pump(0.45)
                sh.type(text)
        elif op == "%":          # raw keystrokes, no delay
            os.write(sh.fd, rest.encode().decode("unicode_escape").encode())
            sh.pump(0.2)
        else:
            raise SystemExit("typer: bad line: %r" % line)
    sh.close()


main()
