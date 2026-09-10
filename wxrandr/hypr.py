"""The Hyprland display backend: `j/monitors` to read, `keyword monitor` to write.

Hyprland advertises `zwlr_output_manager_v1` version 4 and the generic wlroots backend reads it correctly --
but it cannot APPLY through it. Measured on 0.53.3, and again on 0.56.2 [M recon2/hyprland.md §4,
recon2/arch.md]:

* the FIRST apply of a fresh session with one head works (rc 0 in 0.25 s, the mode changed, `hyprctl monitors`
  agreed);
* every apply after it times out at 10 s with nothing changed and no `[COutputConfiguration] Applying
  configuration` line in Hyprland's own log -- and `wlr-randr`, the reference client, hangs for ever on the
  same request, so this is not our protocol code;
* with a second (headless) output present, even the first apply times out;
* and after a wedged apply, Hyprland's OWN `hyprctl keyword monitor` stops taking effect too (three times in a
  row on one session), while the same command on a fresh session applies at once.

That last line is what this file is: `keyword monitor NAME,WxH@Hz,XxY,SCALE` applied instantly and correctly
on a session that had never touched the protocol. So the route is Hyprland's own IPC, and the wlr path is left
for `--backend wlr` and told what it is walking into (wxrandr/core.py's timeout carries a Hyprland clause).

The transform numbering here is wlroots', not the spec-as-Mutter-reads-it: `--rotate left` over the protocol
put sway transform "270" on the wire and `hyprctl` reported `transform: 3` [M recon2/hyprland.md §4], which is
`core.WL_TRANSFORM`'s numbering (3 == 270 == xrandr `left`) and not `core.from_wl_spec_transform`'s (where 3 is
`right`). `snapshot_wlr` reads the same enum the same way."""

import json
import os
import re
import socket
import time

from w11common import session as wsession
from wxrandr import core
from wxrandr.core import Fatal, Mode, OutputState

#: `1920x1080@75.00Hz`, the shape of every string in `availableModes`
#: [M recon2/hyprland.md fixtures, tests/fixtures/hypr/monitors.json]
_MODE_RE = re.compile(r"^(\d+)x(\d+)@([0-9.]+)Hz$")

#: Deadline on connect and on the reply, the ten seconds every backend here arms.
IPC_TIMEOUT = 10.0
#: What probe() waits. `hypr` is second in AUTO_ORDER and `wxrandr --backends` runs every probe, so a socket
#: file whose compositor is gone must not cost ten seconds before the wlr fallback is even tried.
PROBE_TIMEOUT = 2.0


class HyprIPC:
    """Hyprland's request socket: send the request as text, read the reply to EOF, close.

    That is the whole protocol -- one connection per request, proved against a live 0.53.3 with a ten-line
    AF_UNIX client [M recon2/hyprland.md §2] -- and this is the display half of it: connect, `j/<name>`,
    `keyword <text>`.

    It is a second copy of `wdotool/hypr_ipc.py`'s reader, and deliberately, for the reason
    `wdotool/layoutbox.py` carries its own copy of Mutter's logical-size rule: the zipapp install route builds
    `dist/wxrandr` out of `w11common` and `wxrandr` alone (scripts/build-pyz.sh, pinned by
    tests/test_build_scripts.py:TheZipapps -- "no display tool carries the input stack", 680 kB off each of
    the three), so a `from wdotool...` here would work from the .deb and quietly not from the zipapp -- and
    "quietly" on Hyprland means falling back to a wlr path that cannot apply. The price is this class; what
    keeps the two from drifting is tests/test_wxrandr_hypr.py:TheTwoClients, which drives both against one
    double and insists on the same bytes and the same sentences.

    `wxrandr` speaks Fatal, so every failure is one here rather than the CmdError the wdotool side raises."""

    def __init__(self, sockpath: "str | None" = None, timeout: float = IPC_TIMEOUT):
        self.sockpath = sockpath or wsession.find_hypr_socket()
        if not self.sockpath:
            raise Fatal("hypr backend: no Hyprland IPC socket found ($HYPRLAND_INSTANCE_SIGNATURE unset "
                        "and no hypr/*/.socket.sock in any runtime dir)\n")
        self.timeout = timeout

    def close(self):
        """Nothing to close -- there is no held connection. It exists so this object can be a probe's handle,
        which is closed once by the probe sweep and again by the Session that reused it."""

    @property
    def events_path(self) -> str:
        """`.socket2.sock` beside `.socket.sock`. Nothing here reads it; it is the one line that keeps the
        two clients' idea of the instance directory identical."""
        return os.path.join(os.path.dirname(self.sockpath), ".socket2.sock")

    def _connect(self) -> socket.socket:
        """A connected socket. The connect gets its own deadline because a compositor wedged inside its own
        event loop still has a LISTENING socket -- the kernel queues connections for it without waking it --
        so connecting looks fine right up to the moment the backlog fills."""
        deadline = time.monotonic() + self.timeout
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                s.connect(self.sockpath)
                break
            except (TimeoutError, BlockingIOError):
                # both are OSError subclasses: this arm has to come first
                s.close()
                if time.monotonic() >= deadline:
                    raise self._wedged() from None
                time.sleep(0.01)
            except OSError as e:
                s.close()
                raise Fatal("hypr backend: cannot connect to %s: %s\n" % (self.sockpath, e)) from None
        s.settimeout(self.timeout)
        return s

    def _wedged(self) -> Fatal:
        return Fatal("hypr backend: no answer from the compositor within %gs "
                     "(it is not responding)\n" % self.timeout)

    def request(self, text: str) -> bytes:
        s = self._connect()
        try:
            try:
                s.sendall(text.encode())
            except TimeoutError:
                raise self._wedged() from None
            except OSError as e:
                raise Fatal("hypr backend: lost the connection to the compositor (%s)\n" % e) from None
            out = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except TimeoutError:
                    # TimeoutError is an OSError: this arm has to come first, or a compositor that is merely
                    # wedged reads as one that has gone.
                    raise self._wedged() from None
                except OSError as e:
                    raise Fatal("hypr backend: lost the connection to the compositor (%s)\n" % e) from None
                if not chunk:
                    break
                out += chunk
        finally:
            s.close()
        if not out:
            raise Fatal("hypr backend: the compositor closed the IPC socket without answering `%s`\n" % text)
        return out

    def json(self, name: str):
        raw = self.request("j/" + name)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as e:
            raise Fatal("hypr backend: the compositor's answer to j/%s is not JSON (%s)\n"
                        % (name, e)) from None

    def keyword(self, text: str) -> str:
        """`keyword <text>` -- a live config assignment, which is how a layout is applied. Hyprland answers
        `ok`, or its own words, which are relayed whole and in its name."""
        reply = self.request("keyword " + text).decode("utf-8", "replace").strip()
        if reply != "ok":
            raise Fatal("hypr: %s\n" % reply)
        return reply


#: `mirrorOf` when an output mirrors nothing
NO_MIRROR = "none"

PERSIST_NOTE = ("--persistent: Hyprland keeps its layout in hyprland.conf; saving there is not done yet "
                "-- the route is a `monitor=` line in a snippet that file sources (AGENTS.md route 2), at "
                "the cost of owning a file the user hand-edits; this layout lasts as long as the "
                "session\n")


def _mode_of(text: str) -> "Mode | None":
    """One `availableModes` string as a Mode, or None for a shape this has never seen."""
    m = _MODE_RE.match(text.strip())
    if not m:
        return None
    return Mode(w=int(m.group(1)), h=int(m.group(2)),
                refresh_mhz=int(round(float(m.group(3)) * 1000)))


def _rate_text(mhz: int) -> str:
    """The refresh a `keyword monitor` line carries: `%g` of the Hz, so 75000 is `75` and 60020 is `60.02`.

    Hyprland parses the field with its own float reader and matches the nearest mode, so an exact
    three-decimal rate is neither needed nor harmful; what matters is that a whole number does not arrive as
    `75.000000`, which is what `%f` would send."""
    return "%g" % (mhz / 1000.0)


class HyprOutputs:
    """Snapshot + per-output `keyword monitor` apply over Hyprland's IPC, in the five-method backend shape.

    Holds no connection (`HyprIPC` opens one per request, which is the protocol), so `close()` is a no-op and
    the same object can be a probe's handle and then the Session's backend."""

    name = "hypr"

    def __init__(self, sock: "str | None" = None, ipc: "HyprIPC | None" = None):
        self.ipc = ipc if ipc is not None else HyprIPC(sock)
        #: {name: the output it mirrors}, from the last snapshot, so an apply that does not mention a mirror
        #: does not silently break one -- a `keyword monitor` line with no `mirror` field clears it.
        self.mirrors: "dict[str, str]" = {}
        #: {name: the row j/monitors gave}, for the verify step's error line
        self.rows: "dict[str, dict]" = {}
        self._said_persistent = False

    @property
    def sockpath(self) -> str:
        return self.ipc.sockpath

    def close(self):
        """Nothing to close: every request is its own connection. Idempotent by construction, which is what
        the probe's handle contract asks for."""

    # -- query ---------------------------------------------------------------

    #: `all`, and not the plain `j/monitors`, because a head Hyprland has disabled is not in the plain
    #: answer at all -- the row VANISHES rather than gaining `disabled: true`.  Measured live on
    #: resolute-hypr (Hyprland 0.53.3) 2026-09-09, after `wxrandr --output Virtual-3 --off`:
    #:
    #:     j/monitors      -> [('Virtual-1', False), ('Virtual-2', False)]
    #:     j/monitors all  -> [('Virtual-1', False), ('Virtual-2', False), ('Virtual-3', True)]
    #:
    #: and the disabled row is complete: the same keys as an enabled one and all 26 `availableModes`.  X is
    #: the oracle and `xrandr --output X --off` keeps X in the listing with no mode, so the plain form was
    #: the wrong question: it made `--off` refuse ("Hyprland accepted the configuration for Virtual-3 and
    #: then stopped listing it") and left `--auto`, `--right-of` and `--below` warning `output Virtual-3 not
    #: found; ignoring` on an output that was there the whole time.
    MONITORS = "monitors all"

    def monitors(self) -> list:
        rows = self.ipc.json(self.MONITORS)
        if not isinstance(rows, list):
            raise Fatal("the compositor's j/monitors answer is not a list of monitors\n")
        return rows

    def snapshot(self, state: "core.State | None" = None) -> list:
        """OutputState per `j/monitors` row.

        `width`/`height` are the mode's pixels and `x`/`y` are already logical, so the logical rectangle is
        core.logical_size() of the two -- the recorded second head is a 1920x1080 HEADLESS at scale 2.0 sitting
        at +1920+0, which is 960x540 of layout."""
        outs = []
        self.mirrors = {}
        self.rows = {}
        rows = self.monitors()
        # `mirrorOf` comes back as the mirrored monitor's numeric `id` AS A STRING, never its name --
        # measured live on resolute-hypr 2026-09-09: `keyword monitor Virtual-3,...,mirror,Virtual-1`
        # answers ok and `j/monitors all` then reports `mirrorOf: "0"`, Virtual-1's id.  Both spellings are
        # accepted on the way in (`,mirror,0` and `,mirror,Virtual-2` both applied), so this is only about
        # what we remember: an id is a position in Hyprland's own list and moves when a head is plugged,
        # where a name does not, so the id is translated back here and the name is what a later
        # `keyword monitor` re-emits.
        by_id = {str(m.get("id")): str(m.get("name") or "?") for m in rows}
        for i, m in enumerate(rows):
            name = str(m.get("name") or "?")
            self.rows[name] = m
            mirror = str(m.get("mirrorOf") or NO_MIRROR)
            if mirror != NO_MIRROR:
                self.mirrors[name] = by_id.get(mirror, mirror)
            st = OutputState(
                name=name, active=not m.get("disabled"), ident=i + 1,
                mm_w=int(m.get("physicalWidth") or 0), mm_h=int(m.get("physicalHeight") or 0),
                make=str(m.get("make") or "") or "Unknown",
                model=str(m.get("model") or "") or "Unknown",
                serial=str(m.get("serial") or "") or "Unknown",
            )
            for text in m.get("availableModes") or []:
                mode = _mode_of(str(text))
                if mode is not None:
                    st.modes.append(mode)
            st.virtual_modes = not st.modes
            if st.active:
                st.x, st.y = int(m.get("x") or 0), int(m.get("y") or 0)
                st.scale = float(m.get("scale") or 1.0) or 1.0
                st.transform = core.WL_TRANSFORM_NAME.get(int(m.get("transform") or 0), "normal")
                st.current = self._current_mode(st, m)
                if st.current is not None:
                    st.w, st.h = core.logical_size(st.current.w, st.current.h, st.transform, st.scale)
            core.finish_modes(st, [] if state is None else state.modes_for_output(name))
            outs.append(st)
        return outs

    @staticmethod
    def _current_mode(st: OutputState, m: dict) -> "Mode | None":
        """Which of the advertised modes is running.

        `width`/`height`/`refreshRate` are the live mode and `availableModes` is the list, and the two do not
        agree to the digit: the recorded head runs 74.998 Hz and advertises `1920x1080@75.00Hz`. So the match
        is on the size and then on the nearest rate -- which is also what keeps the query one row (a mode
        synthesized beside the list would print `1920x1080` twice, both reading `75.00`). A size that is in no
        advertised mode at all (a headless output driven off-list) gets a Mode of its own, prepended, because
        the alternative is a `--query` that does not say what is running."""
        px_w, px_h = int(m.get("width") or 0), int(m.get("height") or 0)
        hz = float(m.get("refreshRate") or 0.0)
        cands = [x for x in st.modes if (x.w, x.h) == (px_w, px_h)]
        if cands:
            return min(cands, key=lambda x: abs(x.refresh_hz - hz))
        if not px_w or not px_h:
            return st.modes[0] if st.modes else None
        live = Mode(w=px_w, h=px_h, refresh_mhz=int(round(hz * 1000)))
        st.modes.insert(0, live)
        return live

    # -- planning ------------------------------------------------------------

    def predicted_dims(self, t: "core.Target", state: "core.State") -> tuple:
        """`keyword monitor` carries the scale as text in a config line, exactly as the sway IPC does, so it
        lands on the 120th the decimal was printed to rather than on the wl_fixed one the protocol would
        have."""
        return core.predicted_dims(t, state, wire="text")

    def verify(self, state: "core.State", targets: list):
        """--dryrun: Hyprland's IPC has no validating call yet. `keyword` IS the apply, and running it would
        be the thing a dryrun must not do, so this hook sends nothing and the plan is printed unchecked.

        The route is a validating call in that same IPC (AGENTS.md route 6, Hyprland's own code): its cost
        is a patched compositor for a flag that reports rather than acts, which is why nothing here waits
        for it."""

    # -- apply ---------------------------------------------------------------

    def monitor_line(self, t: "core.Target", pos: dict) -> str:
        """The `keyword monitor` argument for one target, byte for byte.

        `NAME,disable` for an output going off; otherwise `NAME,WxH@Hz,XxY,SCALE` with `,transform,N` and
        `,mirror,OTHER` appended in that order. Hyprland's parser reads the first four fields positionally and
        the rest as keyword pairs [R the config's `monitor=` grammar; the four-field form is the one measured
        applying instantly, `keyword monitor "Virtual-1,1280x1024@60,0x0,1"`, M recon2/hyprland.md §2]."""
        name = t.name
        if not t.enabled:
            return "%s,disable" % name
        mode = t.mode if t.mode is not None else t.output.current
        if mode is None:
            size = "preferred"
        elif mode.refresh_mhz:
            size = "%dx%d@%s" % (mode.w, mode.h, _rate_text(mode.refresh_mhz))
        else:
            size = "%dx%d" % (mode.w, mode.h)
        x, y = pos.get(name, (t.output.x, t.output.y))
        line = "%s,%s,%dx%d,%g" % (name, size, x, y, t.scale)
        if t.sway_tf != "normal":
            line += ",transform,%d" % core.WL_TRANSFORM[t.sway_tf]
        mirror = self._mirror_for(t)
        if mirror:
            line += ",mirror,%s" % mirror
        return line

    def _mirror_for(self, t: "core.Target") -> str:
        """The output `t` should mirror after this run: the `--same-as` it was given, else whatever it is
        mirroring now (a line with no `mirror` field clears one, so an unrelated `--pos` on a mirrored output
        would silently un-mirror it)."""
        s = t.stanza
        if s is not None and s.relation is not None:
            kind, other = s.relation
            return other if kind == "same-as" else ""
        return self.mirrors.get(t.name, "")

    def apply(self, state: "core.State", targets: list, persistent: bool = False) -> list:
        """One `keyword monitor` per touched output, then a `j/monitors` re-read that has to agree.

        The re-read is not decoration. Hyprland answers `ok` to a `keyword monitor` it then ignores -- measured
        three times in a row on a session whose protocol apply had wedged [M recon2/hyprland.md §4] -- so a
        run that only trusted the `ok` would report a layout that is not on the screen. `--persistent` is said
        once and does nothing: the file that would keep this is hyprland.conf."""
        if persistent and not self._said_persistent:
            self._said_persistent = True
            core.warn(PERSIST_NOTE)
        core.record_lastmodes(state, targets)
        dims = {}
        for t in targets:
            if t.enabled:
                dims[t.name] = self.predicted_dims(t, state)
        pos = core.resolve_positions(targets, dims)
        touched = [t for t in targets if t.changed]
        for t in touched:
            self.ipc.keyword("monitor " + self.monitor_line(t, pos))
        fresh = self.snapshot(state)
        self._verify_applied(touched, pos, {o.name: o for o in fresh})
        return fresh

    def _verify_applied(self, touched: list, pos: dict, fresh: dict):
        """One line naming the first output Hyprland accepted and did not change.

        Position, mode size and transform are checked and the scale is not: those three are what §4 measured
        standing still, and Hyprland adjusts a scale it cannot divide the head by (so a mismatch there would
        be its arithmetic rather than a refusal)."""
        for t in touched:
            o = fresh.get(t.name)
            if o is None:
                raise Fatal("Hyprland accepted the configuration for %s and then stopped listing it\n" % t.name)
            if o.active != t.enabled:
                raise Fatal("Hyprland accepted %s %s and did not apply it (it is still %s)\n"
                            % (t.name, "on" if t.enabled else "off",
                               "on" if o.active else "off"))
            if not t.enabled:
                continue
            want_pos = pos.get(t.name)
            if want_pos is not None and (o.x, o.y) != tuple(want_pos):
                raise Fatal("Hyprland accepted the position %dx%d for %s and did not apply it "
                            "(it reports %dx%d)\n" % (want_pos[0], want_pos[1], t.name, o.x, o.y))
            cur = o.current
            if t.mode is not None and cur is not None and (cur.w, cur.h) != (t.mode.w, t.mode.h):
                raise Fatal("Hyprland accepted the mode %dx%d for %s and did not apply it "
                            "(it reports %dx%d)\n"
                            % (t.mode.w, t.mode.h, t.name, cur.w, cur.h))
            if o.transform != t.sway_tf:
                raise Fatal("Hyprland accepted the transform %s for %s and did not apply it "
                            "(it reports %s)\n" % (t.sway_tf, t.name, o.transform))


def probe(sock: str, verbose: bool = False):
    """`wxrandr --backends` / `--print-backend`'s row for `hypr`.

    The caller (wxrandr/cli.py:_probe_hypr) has already found the socket, so what is left to establish is that
    something on the other end really is Hyprland: `j/version` parsing is the whole test, and it is also where
    the version in `compositor: Hyprland 0.53.3` comes from. `verbose` is taken and ignored -- _probe_sway
    pays for its version line only when asked, and here the same request is the availability test, so there is
    nothing left to defer. A probe never raises, and it never waits the backend's ten seconds either: a socket
    file whose compositor is gone must not cost that much before the wlr fallback is even tried."""
    from wxrandr.cli import Probe
    try:
        ipc = HyprIPC(sock, timeout=PROBE_TIMEOUT)
        ver = ipc.json("version")
    except Fatal as e:
        return Probe("hypr", False, str(e).rstrip("\n"))
    if not isinstance(ver, dict) or not ver.get("version"):
        return Probe("hypr", False, "the socket at %s did not answer j/version like Hyprland" % sock)
    # the backend that comes out of this probe gets the ordinary deadline: a probe may not wait ten seconds,
    # an apply may
    return Probe("hypr", True, detail="IPC socket %s" % sock,
                 compositor="Hyprland %s" % ver["version"],
                 protocol="Hyprland IPC (hyprctl)",
                 handle=HyprIPC(sock))
