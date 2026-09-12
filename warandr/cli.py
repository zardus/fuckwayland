"""warandr command line — arandr's (``warandr [savedfile]``, --version, --randr-display, --force-version) plus
non-GUI conveniences for scripts: ``--save FILE`` writes the current layout as a layout script, ``--command``
prints the command Apply would run, ``--backend NAME`` pins the backend for this run (the GUI's Layout ▸
Backend, spelled the same as wxrandr's own flag) and ``--print-backend`` prints the backend token and exits;
with ``--verbose`` it adds the whole of what the window's indicator explains, again spelled like wxrandr's own
``--print-backend --verbose``."""

import argparse
import os
import stat
import sys
import tempfile

from w11common import distro, stdio

from . import VERSION, randr
from .model import LayoutError

#: how each family is named in the line, and whether its installer wants sudo. nix-env is per-user and must
#: not be run as root, so NixOS gets the bare command [M recon2/nixos.md].
_GTK_WHERE = {"debian": ("Ubuntu/Debian", "sudo "), "fedora": ("Fedora", "sudo "),
              "arch": ("Arch", "sudo "), "nixos": ("NixOS", "")}
_GTK_LINE = "warandr: GTK 3 for Python is not available (%s) - on %s: %s%s\n"

#: the Debian rendering of the line, which is what every box got before the family was consulted (and what an
#: unidentifiable one still gets); a one-`%s` template, so `GTK_HINT % err` still reads the way it always did.
GTK_HINT = _GTK_LINE % ("%s", _GTK_WHERE["debian"][0], "sudo ",
                        distro.hint("gtk3-python", "debian"))


def gtk_hint(err, fam=None) -> str:
    """The "install GTK 3 for Python" line for this box's distribution.

    warandr is the one tool that never hands over, so this is the only thing a user with no python3-gi has to
    go on -- and it named a Debian package on Fedora, Arch and NixOS alike [M recon2/fedora.md, arch.md,
    nixos.md]."""
    fam = fam or distro.family()
    where, sudo = _GTK_WHERE.get(fam, _GTK_WHERE["debian"])
    return _GTK_LINE % (err, where, sudo, distro.hint("gtk3-python", fam))


def _parser():
    p = argparse.ArgumentParser(
        prog="warandr", usage="%(prog)s [options] [savedfile]",
        description="Another XRandR GUI - on Wayland through wxrandr, on X11 "
                    "through xrandr.")
    p.add_argument("savedfile", nargs="?", help="layout script to open")
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--randr-display", metavar="D",
                   help="Use D as display for xrandr/wxrandr (but still show "
                        "the GUI on the display from the environment; e.g. "
                        "`localhost:10.0` or `wayland-1`)")
    p.add_argument("--force-version", action="store_true",
                   help="Even run with untested XRandR versions (accepted for "
                        "arandr compatibility; warandr never refuses one)")
    p.add_argument("--save", metavar="FILE",
                   help="write the current layout (or SAVEDFILE re-based on "
                        "the current outputs) as a layout script and exit; "
                        "no GUI")
    p.add_argument("--command", action="store_true",
                   help="print the command Apply would run and exit; no GUI")
    p.add_argument("--backend", metavar="NAME",
                   help="force the backend for this run: %s (aliases gnome, "
                        "kde, hyprland, muffin); auto detects the session. "
                        "Beats $WXRANDR_BACKEND, which beats detection"
                        % ", ".join(randr.BACKENDS))
    p.add_argument("--print-backend", action="store_true",
                   help="print the backend token (x11, sway, hypr, wlr, "
                        "mutter, cinnamon, kwin) and exit; no GUI")
    p.add_argument("--verbose", action="store_true",
                   help="with --print-backend: add what runs, why it was "
                        "picked, and what that tool says about the session")
    p.add_argument("--unsafe-gnome-overlap", action="store_true",
                   help="apply overlapping layouts on GNOME without asking "
                        "first, for a window started from a hotkey or a "
                        "desktop entry. Waives the question, not the checks, "
                        "and records no agreement. To agree once instead, "
                        "use wxrandr --gnome-overlap-allow")
    return p


def read_script(path):
    """A layout script as text, or a LayoutError saying why not.

    Scripts are read as UTF-8 and written back byte for byte, so a file that is not text at all -- an image the
    file chooser was pointed at, a layout somebody saved in latin-1 -- has to be refused rather than mangled.
    `errors="replace"` is not the fix: it turns every undecodable byte into U+FFFD, and Save would then write
    that back over the user's own file (the round trip is pinned byte for byte in tests/test_warandr_model.py).

    Refusing is also what keeps the GUI alive: the reader thread used to die on the UnicodeDecodeError before it
    could hand anything back, so the window stayed busy and Apply, Open and New became silent no-ops."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError as e:
        raise LayoutError("Not a text file: %s" % e) from None


def load_layout(backend, savedfile):
    layout = backend.snapshot()
    if savedfile:
        layout.load_script(read_script(savedfile))
    return layout


def script_notes(layout, backend):
    """The comment header a saved layout carries: the forced backend, if one was forced, and — when the layout
    has a partial overlap — what that overlap means on the backend that wrote it.  Comments only: the script
    still runs anywhere."""
    notes = []
    forced = backend.script_note()
    if forced:
        notes.append(forced)
    pairs = layout.overlaps()
    if pairs:
        # neither output is "over" the other -- on every backend that takes an overlap both draw the shared
        # region, which is the whole point -- so the note names the pair symmetrically and says which rectangle
        # they share, in xrandr's own WxH+X+Y spelling
        shared = []
        for a, b in pairs:
            x, y, w, h = layout.shared_region(a, b)
            shared.append("%s and %s share %dx%d at +%d+%d" % (a, b, w, h, x, y))
        notes.append("warandr: partial overlap (%s)" % "; ".join(shared))
        notes.append(backend.overlap_note())
    return notes


def write_script(layout, path, word=None, notes=None):
    """Write the layout as a script -- all of it, or none of it.

    `open(path, "w")` truncates first, so a disk that filled up or a
    quota that ran out halfway through left a *runnable* half-layout
    where a working one had been: the file warandr exists to keep, now
    naming three outputs out of four.  A sibling temporary renamed over
    the target cannot do that -- a reader sees the old file or the new
    one, and a failure leaves the old one exactly as it was.

    Details that matter:

    * `realpath` first, and the rename goes to *that*: `~/.screenlayout/
      desk.sh` is often a symlink into a dotfiles repo, and os.replace
      replaces the name it is given -- it would leave a regular file
      where the link was and never touch the file the user keeps.
    * `fchmod` on the descriptor, because mkstemp makes 0600 and
      arandr's scripts are 0700, and doing it before the rename means
      the file is never briefly visible with the wrong mode.
    * the temporary is removed on any failure, and `e.filename` is set
      to the name the user typed: "Cannot save: [Errno 28] No space left
      on device: '/tmp/.desk.sh.7f3x'" names a file they never asked
      for and cannot find.

    The one thing this gives up: writing needs permission on the
    *directory*, not just on the file.  A read-only directory holding a
    writable script used to save and now refuses -- which is the usual
    price of an atomic save, and cheap next to a truncated layout."""
    if not path.endswith(".sh"):
        path += ".sh"
    text = layout.to_script(word, notes)
    real = os.path.realpath(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real) or ".", prefix=".%s." % os.path.basename(real)[:64])
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), stat.S_IRWXU)
            f.write(text)
        os.replace(tmp, real)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        e.filename = path
        raise
    return path


def _main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    args = _parser().parse_args(argv)
    try:
        backend = randr.choose(forced=args.backend)
        # Set before anything asks: --command and --save go through the
        # same backend, and the flag they print must match what Apply
        # would really run.
        backend.overlap_never_ask = args.unsafe_gnome_overlap
        backend.set_display(args.randr_display)
        if args.print_backend:
            backend.identify()
            for line in (backend.report() if args.verbose else [backend.name]):
                print(line)
            return 0
        if args.save or args.command:
            # which backend this really is decides whether an overlapping layout is refused and in whose name,
            # so ask before reading one; `auto` on Wayland is only "wxrandr" until it has.  (The window asks off
            # the main loop instead, and patches the layout when the answer lands.)
            backend.identify()
            layout = load_layout(backend, args.savedfile)
            if args.command:
                # run_word_for: on GNOME an overlapping layout really is applied
                # with --unsafe-gnome-overlap, and --command's whole job is to
                # print the line Apply would run
                print(layout.command_line(backend.run_word_for(layout)))
            if args.save:
                # ~/.screenlayout is where arandr puts these and where the GUI's Save As already creates on
                # demand; --save is the same recipe without a window, so it should not fail on a fresh account
                # for want of one directory.
                parent = os.path.dirname(os.path.abspath(args.save))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                write_script(layout, args.save, backend.word, script_notes(layout, backend))
            return 0
    except (randr.RandrError, LayoutError, OSError) as e:
        sys.stderr.write("warandr: %s\n" % e)
        return 1
    try:
        from . import gui
    except (ImportError, ValueError, AttributeError) as e:
        sys.stderr.write(gtk_hint(e))
        return 1
    return gui.run(backend, args.savedfile, args.randr_display)


def main(argv=None):
    """`_main()` with the guard every one of these tools needs.

    `--version`/`--help` leave argparse through SystemExit, which used to walk straight past `main()` with the
    text still buffered: the interpreter's own exit-time flush then failed on a full or closed stdout and turned
    exit 0 into exit 120, with an "Exception ignored" block nobody can act on.  Everything else -- Ctrl-C on the
    way to the window, a reader that left, an unexpected error out of GTK -- becomes one `warandr: ...` line
    (w11common/stdio.py)."""
    stdio.repair_std()
    quiet = False
    try:
        code = _main(argv)
    except SystemExit as e:
        stdio.exit_after_flush("warandr", e)
        raise                       # unreachable; the line above raises
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        code = 1
    except Exception as e:
        stdio.warn("warandr: %s\n" % e)
        # An OSError here is a write to stdout that failed (a full disk, a quota, `>/dev/full`): the flush below
        # is about to fail with the same errno, and the originals print one line, not two.
        quiet = isinstance(e, OSError)
        code = 1
    return code if stdio.flush_stdout("warandr", quiet) else (code or 1)
