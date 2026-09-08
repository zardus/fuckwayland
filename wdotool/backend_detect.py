"""Backend detection.

Order: WDOTOOL_BACKEND override -> sway/i3 IPC socket -> Hyprland IPC socket -> KWin, GNOME or Cinnamon (one
ListNames on the session bus) -> Wayfire IPC socket -> one registry round trip, which picks wlr or COSMIC ->
clear error. The three D-Bus checks are ONE ListNames call over dbus_mini (no gdbus/busctl spawns); the
connection is kept for the process so the GNOME backend reuses it, and so is the registry.

Why each step sits where it does, all measured:

* Hyprland above the bus checks because a Hyprland session owns neither `org.kde.KWin` nor `org.gnome.Shell`
  [M recon2/hyprland.md §2, `busctl --user list`], so nothing below can shadow it and nothing it shadows can
  exist. It has to be above the registry probe, which would take it for a plain wlroots compositor -- honest
  for reading and wrong about ids, minimize and geometry [M hyprland.md §3].
* Cinnamon third among the bus names because a Cinnamon session owns neither of the other two
  [M recon2/cinnamon.md §2.2]. None of the three is ever swallowed: Mutter offers no foreign-toplevel protocol
  of either flavour (Muffin advertises 23 globals and neither one [M cinnamon.md §2.1]), and KWin implements
  neither zwlr_foreign_toplevel_manager_v1 nor ext_foreign_toplevel_list_v1 (checked against KWin 5.27 through
  6.6 and master), so the registry probe below could never succeed on any of them -- falling through would only
  replace a precise message (the GNOME bridge's install hint, a KWin failure) with "the compositor does not
  offer wlr-foreign-toplevel".
* Wayfire below the bus names -- a Plasma box with wayfire installed must not be misdetected, though Wayfire
  owns no bus name of its own [M recon2/wayfire.md §1.1] -- and above the registry probe, which would otherwise
  swallow it: Wayfire does advertise zwlr_foreign_toplevel_manager_v1, and the wlr floor is wrong there about
  ids, geometry, desktops and WM_CLASS [M wayfire.md §2.3, §2.4].
* COSMIC last, and only when the registry has both `ext_foreign_toplevel_list_v1` and
  `zcosmic_toplevel_info_v1` and no wlr manager: cosmic-comp publishes no zwlr_foreign_toplevel_manager_v1 at
  all, which is why every window command answered `no Wayland session found` there [M recon2/cosmic.md §2, §3].
  A compositor with both families keeps wlr, which is the older and better-tested path."""

import os

from fwcommon import session
from fwcommon.errors import CmdError
from wdotool.backend import program
from wdotool.ctx import NoSessionError

KWIN_NAME = "org.kde.KWin"
GNOME_NAME = "org.gnome.Shell"
CINNAMON_NAME = "org.Cinnamon"

#: the toplevel protocols the registry step chooses between
WLR_TOPLEVEL = "zwlr_foreign_toplevel_manager_v1"
EXT_TOPLEVEL = "ext_foreign_toplevel_list_v1"
COSMIC_TOPLEVEL = "zcosmic_toplevel_info_v1"

_bus = None          # dbus_mini.Bus for this process, once connected
_names = None        # cached ListNames result (None = no bus reachable)
_probed = False
_registry = None     # cached {interface: version} (None = no compositor reachable)
_registry_probed = False


def _sway():
    from wdotool.backend_sway import SwayBackend
    return SwayBackend()


def _not_built(exc, module, name):
    """Turn "the maker's own module is missing" into the refusal every arm of detect() already handles.

    A maker whose module is not in this install has to answer the way a maker whose protocol is not there
    answers -- with a CmdError -- because detect()'s arms and the WDOTOOL_BACKEND path are both written around
    that one exception, and a traceback out of `wdotool key a` on a real Hyprland session is not an answer.
    Only the maker's own module is swallowed: a ModuleNotFoundError from deeper inside a backend that IS
    installed is that backend's bug and has to stay visible."""
    if exc.name is not None and exc.name != module and not exc.name.startswith(module + "."):
        raise exc
    raise CmdError("%s backend: not built into this install" % name) from None


def _hypr():
    try:
        from wdotool.backend_hypr import HyprBackend
    except ModuleNotFoundError as exc:
        _not_built(exc, "wdotool.backend_hypr", "hypr")
    return HyprBackend()


def _wayfire():
    try:
        from wdotool.backend_wayfire import WayfireBackend
    except ModuleNotFoundError as exc:
        _not_built(exc, "wdotool.backend_wayfire", "wayfire")
    return WayfireBackend()


def _wlr():
    from wdotool.backend_wlr import WlrBackend
    return WlrBackend()


def _cosmic():
    try:
        from wdotool.backend_cosmic import CosmicBackend
    except ModuleNotFoundError as exc:
        _not_built(exc, "wdotool.backend_cosmic", "cosmic")
    return CosmicBackend()


def _kwin():
    from wdotool.backend_kwin import KwinBackend
    return KwinBackend(bus=session_bus(), names=session_names())


def _gnome():
    from wdotool.backend_gnome import GnomeBackend
    return GnomeBackend(bus=session_bus(), names=session_names())


def _cinnamon():
    from wdotool.backend_cinnamon import CinnamonBackend
    return CinnamonBackend(bus=session_bus(), names=session_names())


#: WDOTOOL_BACKEND's accepted spellings. `i3` is an alias of `sway` and buys
#: nothing but the spelling -- the dialect is read off GET_VERSION, not off the
#: variable -- so the refusal below names the eight canonical tokens.
_MAKERS = {"sway": _sway, "i3": _sway, "hypr": _hypr, "wayfire": _wayfire,
           "wlr": _wlr, "cosmic": _cosmic, "kwin": _kwin, "gnome": _gnome,
           "cinnamon": _cinnamon}
_FORCED_NAMES = "sway, hypr, wayfire, wlr, cosmic, kwin, gnome, cinnamon"


def reset():
    """Forget the cached bus/names/registry (tests re-detect against a fresh session)."""
    global _bus, _names, _probed, _registry, _registry_probed
    if _bus is not None:
        try:
            _bus.close()
        except Exception:  # best effort on teardown
            pass
    _bus, _names, _probed = None, None, False
    _registry, _registry_probed = None, False


def session_bus():
    """This process's connection to the graphical session's bus, or None
    when no bus can be found/joined. Connected once, cached."""
    global _bus, _probed, _names
    if _probed:
        return _bus
    _probed = True
    from fwcommon.dbus_mini import Bus, DBusError
    try:
        _bus = Bus()
        _names = _bus.list_names()
    except DBusError:
        if _bus is not None:
            _bus.close()
        _bus, _names = None, None
    return _bus


def session_names() -> list[str] | None:
    """Names on the session bus (one ListNames per process), or None when
    there is no bus."""
    session_bus()
    return _names


def session_registry() -> "dict[str, int] | None":
    """{interface: version} the session's compositor advertises, or None when there is no Wayland socket or it
    cannot be spoken to. One connection per process, opened, read and closed here.

    Detection used to pay this round trip inside `_wlr()` and throw the registry away, so a compositor that has
    ext-foreign-toplevel and no wlr manager -- which is exactly COSMIC -- could only be reported as "does not
    offer wlr-foreign-toplevel" [M recon2/cosmic.md §3]. Reading it once and choosing from it costs the same
    round trip. The chosen backend opens its own connection afterwards; threading this one through to it is
    wdotool/backend_wlr.py's and backend_cosmic.py's to do."""
    global _registry, _registry_probed
    if _registry_probed:
        return _registry
    _registry_probed = True
    from fwcommon.wayland_mini import WlConn
    hit = session.find_wayland_socket()
    if hit is None:
        return None
    conn = None
    try:
        conn = WlConn(hit[2])
        out: dict[str, int] = {}
        for iface, ver in conn.get_registry().values():
            out[iface] = max(ver, out.get(iface, 0))
        _registry = out
    except (OSError, RuntimeError, ValueError):
        _registry = None
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
    return _registry


def detect():
    forced = os.environ.get("WDOTOOL_BACKEND")
    if forced:
        maker = _MAKERS.get(forced.strip().lower())
        if maker is None:
            raise CmdError("WDOTOOL_BACKEND=%s is not one of: %s" % (forced, _FORCED_NAMES))
        return maker()
    if session.find_sway_socket():
        try:
            return _sway()
        except CmdError:
            pass
    if session.find_hypr_socket():
        try:
            return _hypr()
        except CmdError:
            pass
    names = session_names() or []
    if KWIN_NAME in names:
        # Not swallowed either (see the module docstring): KWin offers no
        # foreign-toplevel protocol, so nothing below this could work here.
        return _kwin()
    if GNOME_NAME in names:
        # Not swallowed: the GNOME error carries the bridge install hint and
        # nothing below it can work under Mutter.
        return _gnome()
    if CINNAMON_NAME in names:
        # Not swallowed: Muffin advertises no foreign-toplevel protocol of
        # either flavour, so the registry step below could not answer here.
        return _cinnamon()
    if session.find_wayfire_socket():
        try:
            return _wayfire()
        except CmdError:
            pass
    reg = session_registry() or {}
    # No swallow on these two arms, unlike the socket arms above: the registry we just read IS the evidence
    # that the protocol is there, so whatever the backend says on the way up is a better answer than the
    # sentence below, which would claim the compositor offers neither family and be wrong.
    if WLR_TOPLEVEL in reg:
        return _wlr()
    if EXT_TOPLEVEL in reg and COSMIC_TOPLEVEL in reg:
        return _cosmic()
    bus_note = ("no session D-Bus reachable" if session_names() is None
                else "no KWin, GNOME Shell or Cinnamon on the session D-Bus")
    # rc 2, not 1: "there is no session to talk to" is a different answer to
    # a script than "the session is up and nothing matched" (B5).
    raise NoSessionError(
        "%s: no Wayland session found: no sway/i3, Hyprland or Wayfire IPC socket, %s, and "
        "the compositor offers neither wlr-foreign-toplevel nor the COSMIC toplevel protocols. "
        "Set WDOTOOL_BACKEND=sway|hypr|wayfire|wlr|cosmic|kwin|gnome|cinnamon to force one."
        % (program(), bus_note)
    )
