"""Backend runner: picks the RandR command (xrandr on X11, wxrandr on
Wayland), snapshots the screen, applies a layout.

Selection (first match wins):

1. an explicit choice -- ``warandr --backend NAME`` or the GUI's
   Layout ▸ Backend, `NAME` being ``auto``, ``x11`` or one of wxrandr's own
   backends (``sway``, ``hypr``/``hyprland``, ``wlr``, ``mutter``/``gnome``,
   ``cinnamon``/``muffin``, ``kwin``/``kde``).
   ``x11`` runs the real ``xrandr``; a Wayland backend runs wxrandr with
   ``--backend NAME``, which beats wxrandr's own ``$WXRANDR_BACKEND`` and
   its detection. ``auto`` is the default and means the rest of this list.
2. ``$WARANDR_XRANDR`` — a command line (shlex-split) to run instead;
   its kind is Wayland when it mentions ``wxrandr``, X11 otherwise, unless
   ``$WARANDR_BACKEND`` (``x11`` / ``wayland``) says so explicitly.
3. a Wayland session (``passthrough.session_kind()``: ``$WAYLAND_DISPLAY``
   with a live socket, logind, the runtime dirs -- a stale ``WAYLAND_DISPLAY``
   with no compositor behind it is *not* one) and the ``wxrandr`` package
   importable
   (repo checkout or the pyz that bundles it): the *same* interpreter with
   ``-m wxrandr``, PYTHONPATH pointing at wherever the package was found.
4. a Wayland session and ``wxrandr`` on PATH.
5. ``xrandr``.

warandr never hands its process over to the real tool (no ``execve``, see
``w11common.passthrough``): it *chooses* which one to run and runs it as a
child, which is what makes the choice switchable while the window is open.
"""

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys

from w11common import passthrough

from . import xrandr_parse
from .model import Layout

#: wxrandr's own backends, plus the two names that are not one of them. Kept in
#: step with `wxrandr/cli.py`'s tables and with `gui.BACKEND_ITEMS`: a name that
#: is missing here is dropped by `probe_backends()`'s parse, and the GUI then
#: shows its menu entry enabled with no availability behind it.
WAYLAND_BACKENDS = ("sway", "hypr", "wlr", "mutter", "cinnamon", "kwin")
BACKENDS = ("auto", "x11") + WAYLAND_BACKENDS
ALIASES = {"gnome": "mutter", "kde": "kwin",
           "muffin": "cinnamon", "hyprland": "hypr"}

#: What a *partial* overlap (two active outputs intersecting at different
#: origins) means per backend: whether the backend takes one, and the one
#: sentence the window says about it.  Measured on two 1920x1080 heads with
#: the second at x=960 -- see docs/WARANDR.md, "What an overlap means"; on X11,
#: KWin and wlroots the shared region came back byte-identical on both
#: heads, and Mutter answered every non-adjacent layout, overlap and gap
#: alike, with the message quoted here.
OVERLAP = {
    "x11": (True, "X11 draws both outputs from one framebuffer, so the "
                  "shared region shows the same pixels on both."),
    "sway": (True, "sway draws every output as a viewport onto one scene, so "
                   "the shared region shows the same pixels on both; only "
                   "which workspace a window opens on stays per-output."),
    "wlr": (True, "wlroots draws every output as a viewport onto one scene, "
                  "so the shared region shows the same pixels on both."),
    "kwin": (True, "KWin draws every output as a view onto one shared scene, "
                   "so the shared region shows the same pixels on both."),
    "mutter": (False, "GNOME's Mutter refuses monitors that are not "
                      "edge-adjacent, overlapping ones included "
                      "(\"Logical monitors not adjacent\")."),
}
#: a Wayland backend nobody has identified yet (and any future one)
OVERLAP_UNKNOWN = (True, "This backend has not been measured here; Apply "
                         "reports whatever the compositor makes of it.")

#: GNOME with a way round its own refusal: `wxrandr --gnome-overlap-status`
#: answered `agreed` or `available`, so the overlap extension is installed and
#: this is a build it has been measured on.  One sentence for both answers, on
#: purpose -- it is also the comment a saved script carries, and what a script
#: says about a layout must not depend on whether the person who saved it had
#: agreed to anything yet.
OVERLAP_MUTTER_EXT = (
    True, "GNOME's Mutter refuses monitors that are not edge-adjacent, so "
          "warandr places these through the w11-overlap extension "
          "instead; the layout is temporary and is gone at the next login.")

#: what warandr adds to the Apply command line for an overlapping layout on
#: GNOME, and to nothing else
UNSAFE_OVERLAP = "--unsafe-gnome-overlap"
#: and the three that manage the agreement to it (see docs/WXRANDR.md)
OVERLAP_STATUS = "--gnome-overlap-status"
OVERLAP_ALLOW = "--gnome-overlap-allow"


class RandrError(Exception):
    pass


def canonical_backend(name):
    """The canonical spelling of a backend name (aliases resolved), or None
    when it is not one of ours."""
    v = (name or "").strip().lower()
    v = ALIASES.get(v, v)
    return v if v in BACKENDS else None


class Backend:
    def __init__(self, argv, wayland, env=None, source="auto", name=None, forced=None):
        self.argv = list(argv)
        self.wayland = wayland
        self.env = dict(env if env is not None else os.environ)
        if not self.wayland:
            # The X11 runner is a child, not a handover, so it does not go through passthrough.child_env() --
            # and without the same repair `warandr --command` / `--save` from a root shell or cron ran a bare
            # xrandr with no $DISPLAY and died with "Can't open display", alone among the five tools.
            # set_display() still overrides it.
            passthrough.repair_x_env(self.env)
        self.source = source
        #: warandr --unsafe-gnome-overlap: apply an overlapping layout without asking first
        self.overlap_never_ask = False
        #: the backend token: ``x11``, one of wxrandr's, or ``wayland``
        #: until ``identify()`` has asked which one wxrandr picked
        self.name = name or ("wayland" if wayland else "x11")
        #: the name that was forced, if any (None: auto)
        self.forced = forced
        #: ``wxrandr --print-backend --verbose``, once it has been asked
        self.info = []
        #: ``wxrandr --gnome-overlap-status`` as a dict, once it has been asked
        #: and only on GNOME: ``{"state": "agreed"|"available"|"unavailable",
        #: ...}``.  None until then, which reads as "no overlap route", so a
        #: backend nobody has asked about behaves exactly as it did before.
        self.overlap_info = None
        self._identified = False

    @property
    def word(self):
        """The command word written into layout scripts."""
        return "wxrandr" if self.wayland else "xrandr"

    @property
    def run_word(self):
        """The command word plus the flag Apply really passes, which is what the status bar promises to show.
        Scripts get ``word``: a saved layout must stay arandr's."""
        if self.forced and self.wayland:
            return "%s --backend %s" % (self.word, self.forced)
        return self.word

    @property
    def kind(self):
        return "Wayland" if self.wayland else "X11"

    @property
    def shown(self):
        """The name in the window's indicator: on Wayland the compositor
        backend once it is known, on X11 the tool itself."""
        if not self.wayland:
            return "xrandr"
        return "wxrandr" if self.name in (None, "wayland", "x11") else self.name

    @property
    def label(self):
        return "%s (%s)" % (self.shown, self.kind)

    def indicator(self):
        """The always-visible status-bar text."""
        return "backend: " + self.label

    def overlap_state(self):
        """``agreed`` / ``available`` / ``unavailable``, or None when nothing has
        asked yet.  Only ever non-None on GNOME."""
        return (self.overlap_info or {}).get("state")

    def overlap(self):
        """``(taken, sentence)`` for a partial overlap on this backend."""
        if self.name == "mutter" and self.overlap_state() in ("agreed", "available"):
            return OVERLAP_MUTTER_EXT
        return OVERLAP.get(self.name, OVERLAP_UNKNOWN)

    def overlap_note(self):
        """The one sentence about what an overlap does here."""
        return self.overlap()[1]

    def overlap_refusal(self):
        """The reason this backend refuses an overlap, or None when it takes one.  It is what ``Layout.check()``
        raises, so a refused drop is reported in the compositor's name and never in ours."""
        taken, why = self.overlap()
        return None if taken else why

    def overlap_needs_asking(self, layout):
        """True when applying *this* layout is the thing the user has to be
        asked about once: an overlap, on GNOME, through the extension, with no
        agreement recorded yet.  False the moment one is (``agreed``), which is
        the whole point of recording it.

        ``overlap_never_ask`` is warandr's ``--unsafe-gnome-overlap``: somebody
        who has already decided, starting the window from a hotkey or a desktop
        entry where a dialog is the whole interaction.  It waives the *asking*
        and nothing else -- the flag is still only added to a layout that really
        overlaps on a GNOME that really has the route, and every check the
        extension makes still runs on every apply.  It deliberately records no
        agreement: how a program was started should not change what it leaves
        behind on disk."""
        if self.overlap_never_ask:
            return False
        return bool(self.overlap_flag(layout)) and self.overlap_state() != "agreed"

    def overlap_flag(self, layout):
        """The flag Apply really adds for *this* layout, as a list: the unsafe
        one, and only when the layout genuinely overlaps, the backend genuinely
        is GNOME, and the route is genuinely there.  Every ordinary layout gets
        ``[]`` and the command line warandr runs is the one it always ran."""
        if (self.name == "mutter" and layout is not None
                and self.overlap_state() in ("agreed", "available")
                and layout.overlaps()):
            return [UNSAFE_OVERLAP]
        return []

    def run_word_for(self, layout):
        """``run_word`` plus that flag: what Apply will really run for this
        layout, which is what the status bar promises to show."""
        return " ".join([self.run_word] + self.overlap_flag(layout))

    def command(self):
        return " ".join(shlex.quote(a) for a in self.argv)

    def describe(self):
        return "%s (%s)" % (self.command(), self.kind)

    def info_lines(self):
        """The explanation behind the indicator, as lines: what runs, why warandr picked it, and what the tool
        on the other end says about itself.  A key the tool repeats is dropped -- its own ``chosen by:`` only
        ever restates ours (we are the one who passed ``--backend``), and two answers to one question in one
        paragraph is worse than none."""
        lines = ["kind: %s" % self.kind, "runs: %s" % self.command(), "chosen by: %s" % self.source]
        seen = set(ln.split(":", 1)[0] for ln in lines)
        for ln in self.info[1:]:
            if ln.strip() and ln.split(":", 1)[0] not in seen:
                lines.append(ln)
        # what a partial overlap means here: the window has to say it somewhere, and this paragraph is the one
        # that already explains the backend (indicator tooltip, About, Script Properties)
        lines.append("overlap: %s" % self.overlap_note())
        state = self.overlap_state()
        if state == "agreed":
            info = self.overlap_info or {}
            lines.append("overlapping layouts: agreed on %s (wxrandr --gnome-overlap-forget withdraws)"
                         % (info.get("agreed on") or "?"))
        elif state == "available":
            lines.append("overlapping layouts: Apply explains the risk and asks, once")
        return lines

    def detail(self):
        """The fuller explanation behind the indicator (the tooltip, the
        About dialog, Script Properties)."""
        return "\n".join(["backend: %s" % self.label] + self.info_lines()[1:])

    def report(self):
        """``warandr --print-backend --verbose``: the bare token first, for
        scripts, then the same explanation, spelled like wxrandr's own."""
        return [self.name] + self.info_lines()

    def script_note(self):
        """The single comment a saved layout script carries about the backend -- only when one was forced, and
        only ever a comment, so ``sh script.sh`` on a plain X11 box cannot care."""
        if not self.forced:
            return None
        return "warandr: backend %s forced (%s)" % (self.forced, self.run_word)

    def identify(self, timeout=15):
        """Ask which backend this really is: ``wxrandr --print-backend --verbose``, whose first line is the
        token.  The X11 runner is the real xrandr and has no such option, so its answer is composed here. Never
        raises, and asks only once: a wxrandr too old for the option just leaves the coarse name."""
        if self._identified:
            return self
        self._identified = True
        if not self.wayland:
            self.name = "x11"
            self.info = ["x11", "session: %s" % (
                passthrough.session_kind(env=self.env,
                                         respect_override=False) or "unknown")]
            return self
        try:
            rc, out, _err = self.run(["--print-backend", "--verbose"], timeout=timeout)
        except RandrError:
            return self
        lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
        if rc != 0 or not lines:
            return self
        name = canonical_backend(lines[0])
        if name and name != "auto":
            self.name = name
        self.info = lines
        # ...and, now that the backend has a name, whether an overlapping layout
        # can be applied on it at all.  Here rather than in snapshot(): it is one
        # more subprocess, it belongs on the same worker thread as the question
        # above, and the answer is a property of the session, not of a layout.
        self.read_overlap_status(timeout=timeout)
        return self

    def set_display(self, display):
        """arandr's --randr-display: talk to another display while the GUI
        stays on the one from the environment."""
        if display:
            self.env["WAYLAND_DISPLAY" if self.wayland else "DISPLAY"] = display

    def run(self, args, timeout=30):
        cmd = self.argv + list(args)
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=self.env,
                               timeout=timeout)
        except OSError as e:
            raise RandrError("cannot run %s: %s" % (cmd[0], e))
        except subprocess.TimeoutExpired:
            raise RandrError("%s did not finish within %ds" % (cmd[0], timeout))
        return (p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))

    def query(self, verbose=True):
        rc, out, err = self.run(["--verbose"] if verbose else ["--query"])
        if rc != 0:
            raise RandrError("%s failed (%d): %s" % (self.word, rc, err.strip() or out.strip()))
        return out

    def snapshot(self):
        """The current screen as a Layout."""
        text = self.query(verbose=True)
        try:
            screen = xrandr_parse.parse(text)
        except xrandr_parse.ParseError as e:
            raise RandrError("cannot parse %s output: %s" % (self.word, e))
        if not screen.outputs and "Screen" not in text:
            raise RandrError("no RandR output from %s" % self.word)
        return Layout.from_screen(screen, hidpi=self.wayland,
                                  command_word=self.word,
                                  overlap_refusal=self.overlap_refusal())

    def apply(self, layout):
        """Run the layout's command line; (rc, stdout, stderr)."""
        return self.run(self.overlap_flag(layout) + layout.args())

    def read_overlap_status(self, timeout=20):
        """Ask ``wxrandr --gnome-overlap-status`` and keep the answer.  GNOME
        only, never raises, and cheap by contract: the command reads the Shell's
        public version property and a bus name, and nothing out of gnome-shell
        itself, which is what makes it safe to run at startup."""
        if not self.wayland or self.name != "mutter":
            self.overlap_info = None
            return self.overlap_info
        info = {"state": "unavailable"}
        try:
            rc, out, _err = self.run([OVERLAP_STATUS], timeout=timeout)
        except RandrError:
            self.overlap_info = info
            return info
        lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
        # A wxrandr too old for the option exits non-zero with a usage error:
        # no route, and no complaint -- exactly the behaviour before it existed.
        if rc == 0 and lines and lines[0] in ("agreed", "available", "unavailable"):
            info["state"] = lines[0]
            for ln in lines[1:]:
                if ": " in ln:
                    k, v = ln.split(": ", 1)
                    info[k.strip()] = v.strip()
        self.overlap_info = info
        return info

    def allow_overlap(self, timeout=30):
        """``wxrandr --gnome-overlap-allow``: record the agreement, then re-read
        the status so the window is not left believing the old one.
        ``(ok, message)``; the message is worth showing when it is not ok."""
        try:
            rc, out, err = self.run([OVERLAP_ALLOW], timeout=timeout)
        except RandrError as e:
            return False, str(e)
        self.read_overlap_status()
        if rc != 0:
            return False, (err.strip() or out.strip()
                           or "%s exited %d" % (OVERLAP_ALLOW, rc))
        return self.overlap_state() == "agreed", err.strip()


def _package_root(name):
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        return None
    pkgdir = list(spec.submodule_search_locations)[0]
    return os.path.dirname(pkgdir.rstrip(os.sep))


def _wxrandr_runner(env):
    """``(argv, source)`` for running wxrandr — the *same* interpreter with ``-m wxrandr`` when the package is
    importable (a zipapp path works: zipimport), else ``wxrandr`` on PATH.  ``(None, None)`` when there is
    neither.  ``env`` gains the PYTHONPATH that makes the first one work."""
    root = _package_root("wxrandr")
    # `sys.executable` is "" for an interpreter that cannot work out its own path -- which is what happens under
    # `env -i`, with no PATH to search. Handing execve an empty argv[0] is "Permission denied: ''", so fall back
    # to a named python3 and, failing that, to the PATH branch below.
    exe = sys.executable or shutil.which("python3") or shutil.which("python")
    if root is not None and exe:
        old = env.get("PYTHONPATH")
        env["PYTHONPATH"] = root + (os.pathsep + old if old else "")
        return [exe, "-m", "wxrandr"], "wxrandr package at %s" % root
    if shutil.which("wxrandr"):
        return ["wxrandr"], "wxrandr on PATH"
    return None, None


def choose(env=None, forced=None):
    """The backend to run.  `forced` (``--backend NAME``, the GUI menu) beats everything else and never falls
    back silently: a Wayland backend with no wxrandr to run it is an error, not plain xrandr."""
    env = dict(os.environ if env is None else env)
    want = canonical_backend(forced)
    if forced and want is None:
        raise RandrError("unknown backend %r (valid: %s)" % (forced, ", ".join(BACKENDS)))
    if want == "auto":
        want = None
    kind = env.get("WARANDR_BACKEND", "").strip().lower()
    override = env.get("WARANDR_XRANDR", "").strip()
    base = None
    if override:
        base = shlex.split(override)
        if not base:
            raise RandrError("WARANDR_XRANDR is empty")
    if want == "x11":
        # the real xrandr, run as a child (warandr never hands over)
        return Backend(base or ["xrandr"], False, env=env, name="x11",
                       forced=want,
                       source="--backend x11" + (" ($WARANDR_XRANDR)"
                                                 if base else ""))
    if want:
        src = "$WARANDR_XRANDR"
        if base is None:
            base, src = _wxrandr_runner(env)
            if base is None:
                raise RandrError(
                    "the %s backend needs wxrandr, which is not here (no "
                    "wxrandr package importable, none on PATH)" % want)
        return Backend(base + ["--backend", want], True, env=env, name=want,
                       forced=want, source="--backend %s (%s)" % (want, src))
    if base is not None:
        argv = base
        wayland = any("wxrandr" in os.path.basename(a) for a in argv)
        source = "WARANDR_XRANDR"
    # respect_override=False: $W11_PASSTHROUGH says what to do about *handing over to the original*, and
    # warandr never hands over -- it only picks a command word. Honouring `never` here would answer "wayland" on
    # an X11 box and select wxrandr, i.e. break warandr for exactly the developers the variable is documented
    # for.
    elif passthrough.session_kind(env=env, respect_override=False) == "wayland":
        argv, source = _wxrandr_runner(env)
        if argv is None:
            argv = ["xrandr"]
            source = "xrandr (no wxrandr found; XWayland view)"
        wayland = argv[-1] == "wxrandr"
    else:
        argv = ["xrandr"]
        wayland = False
        source = "xrandr (X11 session)"
    if kind in ("x11", "wayland"):
        wayland = kind == "wayland"
    return Backend(argv, wayland, env=env, source=source)


def probe_backends(env=None, timeout=20):
    """What ``wxrandr --backends`` says about this session:
    ``{name: {"available": bool, "reason": str, "auto": bool}}``, which is what the GUI greys its Backend menu
    with.  Never raises: without wxrandr (or with one too old to know the option) the Wayland backends are
    unavailable *for that reason* and x11 is judged here, from PATH."""
    env = dict(os.environ if env is None else env)
    override = env.get("WARANDR_XRANDR", "").strip()
    argv = shlex.split(override) if override else None
    if not argv:
        argv, _src = _wxrandr_runner(env)
    out = ""
    if argv:
        try:
            p = subprocess.run(argv + ["--backends"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=env,
                               timeout=timeout)
            if p.returncode == 0:
                out = p.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired):
            out = ""
    info = {}
    for line in out.splitlines():
        if len(line) < 2 or line[0] not in " *":
            continue
        parts = line[1:].split(None, 2)
        if len(parts) < 2 or parts[0] not in BACKENDS:
            continue
        info[parts[0]] = {"available": parts[1] == "available",
                          "reason": parts[2].strip() if len(parts) > 2 else "",
                          "auto": line[0] == "*"}
    if not info:
        why = ("wxrandr is not installed" if not argv else "wxrandr does not report its backends")
        info = {n: {"available": False, "reason": why, "auto": False} for n in WAYLAND_BACKENDS}
        has = bool(shutil.which("xrandr"))
        info["x11"] = {"available": has, "auto": False, "reason": "" if has else "no xrandr on PATH"}
    return info
