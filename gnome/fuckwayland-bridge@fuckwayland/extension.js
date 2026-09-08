// fuckwayland-bridge — GNOME Shell extension that exports Mutter's window,
// workspace and monitor facts (and the matching actions) over D-Bus so that
// wdotool / wwmctl / wxprop can drive a GNOME Wayland session.  wxrandr and
// warandr are not clients of it: they go straight to Mutter's own
// org.gnome.Mutter.DisplayConfig, which needs no extension.
//
// Why: GNOME has no window-management protocol, org.gnome.Shell.Eval is
// disabled outside "unsafe mode", org.gnome.Shell.Introspect is read-only and
// sender-allowlisted. The only supported way in is an extension.
//
// Targets GNOME Shell 45..51 (Ubuntu 24.04 = 46, Ubuntu 26.04 = 50, 26.10 = 51), ESM.
// Every Mutter API that drifted between those releases is feature-detected at
// runtime. Verified live on Ubuntu 24.04 (gnome-shell 46.0) and 26.04
// (gnome-shell 50.1); every method here has now been exercised on both, and
// nothing is left marked TODO-VERIFY (see gnome/README.md "Verified live").
//
// Wire: well-known name org.fuckwayland.Bridge (the object also answers under
// org.gnome.Shell because it lives on gnome-shell's own connection), object
// path /org/fuckwayland/Bridge, interface org.fuckwayland.Bridge1. Structured
// results travel as JSON strings; errors are org.fuckwayland.Bridge1.NotFound,
// .Unsupported, .InvalidArgs or .Failed. Every method body is guarded: a
// broken call yields a D-Bus error, never a shell crash.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.fuckwayland.Bridge';
const OBJECT_PATH = '/org/fuckwayland/Bridge';
const IFACE_NAME = 'org.fuckwayland.Bridge1';
// 2: SelectWindow picks the window under the pointer on the next button
// press (v1 waited for a focus change). An installed v1 has to be replaced --
// see gnome/README.md -- and the client says so instead of hanging.
// 3: SetState of MAXIMIZED under `toggle` follows the horizontal flag, as
// Mutter reads the two atoms of one _NET_WM_STATE message (v1/v2 asked for
// "both axes are already set"). Nothing gates on it: MAXIMIZED itself is as
// old as the bridge, and add/remove of the pair -- the corrupted restore
// size -- are fixed against an installed v2 too.
const VERSION = 3;

const ERR_NOT_FOUND = `${IFACE_NAME}.NotFound`;
const ERR_UNSUPPORTED = `${IFACE_NAME}.Unsupported`;
const ERR_INVALID = `${IFACE_NAME}.InvalidArgs`;
const ERR_FAILED = `${IFACE_NAME}.Failed`;
// SelectWindow only: Escape, the timeout, a caller that went away, the
// extension being disabled. Not a failure -- the client turns it into
// "selectwindow: <reason>" and a non-zero exit, as KWin's picker does.
const ERR_CANCELLED = `${IFACE_NAME}.Cancelled`;

// Hard cap on a window selection, and what timeout_ms 0 ("wait for the user")
// means. A pointer grab is the one thing here that can make a desktop feel
// broken, so it is never held indefinitely: thirty seconds is far longer than
// any click takes and short enough that a forgotten picker frees the session
// on its own. The client is also watched (see _beginSelect) and Escape
// cancels, so this is the last of three ways out, not the first.
const SELECT_MAX_MS = 30000;

// ...and the cap alone bounds each call, not the caller. Any process on the
// session bus may ask for a selection, and nothing stopped one from starting
// the next the microsecond the last ended: measured, that is a re-grab every
// 5 us, which is a session-wide input lock (no key, no button, no scroll
// reaches any application) held for as long as the process cares to. So a
// selection is followed by a quiet period as long as the grab it just held:
// an honest picker -- a click within a second or two -- is never delayed,
// while a loop can hold the input at most half the time, which always leaves
// the user a way to reach a terminal. Deliberately not keyed to the sender:
// a second bus connection would defeat that.
let selectCooldownUntil = 0;

// How long the grab is kept after the picking press, waiting for the matching
// release. The answer is already decided by then; this only keeps the release
// from reaching the application under the pointer as half a click it never saw
// the start of. A device that sends no release (a touch turned into a gesture,
// a synthetic press) must not delay the answer, so when this expires the
// answer is returned anyway.
const SELECT_RELEASE_MS = 300;

// Keep in sync with org.fuckwayland.Bridge1.xml next to this file.
const IFACE_XML = `<node>
  <interface name="org.fuckwayland.Bridge1">
    <!-- windows -->
    <method name="ListWindows">
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="GetWindow">
      <arg type="t" direction="in" name="id"/>
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="Activate"><arg type="t" direction="in" name="id"/></method>
    <method name="Focus"><arg type="t" direction="in" name="id"/></method>
    <method name="Close"><arg type="t" direction="in" name="id"/></method>
    <method name="Kill"><arg type="t" direction="in" name="id"/></method>
    <method name="Minimize"><arg type="t" direction="in" name="id"/></method>
    <method name="Unminimize"><arg type="t" direction="in" name="id"/></method>
    <method name="Raise"><arg type="t" direction="in" name="id"/></method>
    <method name="Lower"><arg type="t" direction="in" name="id"/></method>
    <method name="Move">
      <arg type="t" direction="in" name="id"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
    </method>
    <method name="Resize">
      <arg type="t" direction="in" name="id"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
    </method>
    <method name="MoveResize">
      <arg type="t" direction="in" name="id"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
    </method>
    <method name="SetState">
      <arg type="t" direction="in" name="id"/>
      <arg type="s" direction="in" name="state"/>
      <arg type="s" direction="in" name="action"/>
      <arg type="b" direction="out" name="applied"/>
    </method>
    <method name="MoveToWorkspace">
      <arg type="t" direction="in" name="id"/>
      <arg type="i" direction="in" name="index"/>
    </method>
    <method name="SelectWindow">
      <arg type="u" direction="in" name="timeout_ms"/>
      <arg type="t" direction="out" name="id"/>
    </method>
    <!-- workspaces -->
    <method name="GetActiveWorkspace"><arg type="i" direction="out" name="index"/></method>
    <method name="SetActiveWorkspace"><arg type="i" direction="in" name="index"/></method>
    <method name="GetNWorkspaces"><arg type="i" direction="out" name="count"/></method>
    <method name="SetNWorkspaces"><arg type="i" direction="in" name="count"/></method>
    <method name="ListWorkspaces"><arg type="s" direction="out" name="json"/></method>
    <method name="ShowDesktop"><arg type="b" direction="in" name="show"/></method>
    <!-- screen -->
    <method name="DisplaySize">
      <arg type="i" direction="out" name="width"/>
      <arg type="i" direction="out" name="height"/>
    </method>
    <method name="GetPointer">
      <arg type="i" direction="out" name="x"/>
      <arg type="i" direction="out" name="y"/>
      <arg type="u" direction="out" name="modifiers"/>
    </method>
    <method name="ListMonitors"><arg type="s" direction="out" name="json"/></method>
    <method name="XInfo">
      <arg type="s" direction="out" name="display"/>
      <arg type="s" direction="out" name="xauthority"/>
    </method>
    <method name="ConfirmDisplayChange">
      <arg type="b" direction="in" name="keep"/>
      <arg type="b" direction="out" name="handled"/>
    </method>
    <!-- misc -->
    <method name="GetVersion"><arg type="u" direction="out" name="version"/></method>
    <property name="Version" type="u" access="read"/>
    <!-- events -->
    <signal name="WindowEvent">
      <arg type="t" name="id"/>
      <arg type="s" name="change"/>
    </signal>
    <signal name="WorkspaceEvent">
      <arg type="s" name="change"/>
    </signal>
  </interface>
</node>`;

// ---------------------------------------------------------------------------
// helpers

class BridgeError extends Error {
    constructor(name, message) {
        super(message);
        this.name = name;
    }
}

// debug(): G_LOG_LEVEL_DEBUG, hidden unless G_MESSAGES_DEBUG covers gjs.
// info(): G_LOG_LEVEL_MESSAGE, always in the journal (`journalctl --user
// _COMM=gnome-shell`); used for enable/disable, bus-name changes and
// unexpected (.Failed) errors only, so a polling client cannot flood it.
function debug(...args) {
    try {
        console.debug('[fuckwayland-bridge]', ...args);
    } catch (_e) {
        // logging must never be the thing that breaks
    }
}

function info(...args) {
    try {
        console.log('[fuckwayland-bridge]', ...args);
    } catch (_e) {
        // see above
    }
}

// Run fn(); return dflt when it throws or yields null/undefined.
function safe(fn, dflt = null) {
    try {
        const v = fn();
        return v === null || v === undefined ? dflt : v;
    } catch (_e) {
        return dflt;
    }
}

function isFn(obj, name) {
    return !!obj && typeof obj[name] === 'function';
}

// A Clutter.Grab that did not actually take the seat is worse than none: it
// answers no events and would have to be dismissed anyway.
function grabIsLive(grab) {
    if (!grab)
        return false;
    if (isFn(grab, 'get_seat_state')) {
        const none = safe(() => Clutter.GrabState.NONE, null);
        const state = safe(() => grab.get_seat_state(), null);
        if (none !== null && state !== null && state === none)
            return false;
    }
    return true;
}

// Grab all input for `actor`, feature-detected: the grab API is not the same
// across the releases this extension targets, and neither spelling may be
// assumed to exist.
//
//   Main.pushModal(actor, {actionMode}) -> Clutter.Grab, released with
//     Main.popModal(grab). The shell's own way in; it also puts the session
//     in a modal action mode, so no keybinding, panel button or hot corner
//     fires while the picker is up, and it restores key focus afterwards.
//   global.stage.grab(actor) -> Clutter.Grab, released with grab.dismiss().
//     The plain Clutter grab underneath it, for a shell whose pushModal is
//     missing altogether.
//
// pushModal *refusing* is a different thing from it being absent: the shell
// is already modal and will not share. A refusal is therefore final here --
// no reaching past it for a plain stage grab (see shellIsModal below for why).
//
// Returns {release} or null; a grab that came back dead is dismissed here, so
// a null answer never leaves anything held.
function takeGrab(actor) {
    if (isFn(Main, 'pushModal')) {
        const mode = safe(() => Shell.ActionMode.POPUP, 0);
        const grab = safe(() => Main.pushModal(actor, mode ? {actionMode: mode} : {}), null);
        if (grabIsLive(grab))
            return {release: () => Main.popModal(grab)};
        if (grab)
            safe(() => Main.popModal(grab));
        return null;
    }
    if (isFn(global.stage, 'grab')) {
        const grab = safe(() => global.stage.grab(actor), null);
        if (grabIsLive(grab))
            return {release: () => grab.dismiss()};
        if (grab)
            safe(() => grab.dismiss());
    }
    return null;
}

// Is the shell already modal -- the overview, Alt+F2, an open menu, a system
// dialog? A picker on top of that swallows the click and hit-tests it against
// the windows' *real frame rects*, which are not what is on screen then:
// measured with the overview up, a click on a visible thumbnail answered with
// a window nowhere near the pointer on GNOME 50, and with none at all on 46.
// So the picker refuses (.Unsupported) and leaves the shell's own modal alone.
//
// Asking pushModal is not enough: on some releases it nests happily on top of
// the overview and hands out a second grab. Every signal here is
// feature-detected and each is only trusted when it can be read; a shell that
// answers none of them gets the old behaviour, which is to go ahead.
function shellIsModal() {
    const normal = safe(() => Shell.ActionMode.NORMAL, null);
    const mode = safe(() => Main.actionMode, null);
    if (normal !== null && typeof mode === 'number' && mode !== normal)
        return true;
    const count = safe(() => Main.modalCount, null);
    if (typeof count === 'number' && count > 0)
        return true;
    return safe(() => Main.overview.visible, false) === true;
}

// A real timestamp keeps Mutter's focus-stealing prevention happy; 0
// (META_CURRENT_TIME) makes it guess. get_current_time_roundtrip() returns the
// event time when called from an event, else a monotonic ms time on Wayland.
function now() {
    const d = global.display;
    if (isFn(d, 'get_current_time_roundtrip')) {
        const t = safe(() => d.get_current_time_roundtrip(), 0);
        if (t)
            return t;
    }
    return safe(() => global.get_current_time(), 0);
}

function settings(schemaId) {
    try {
        const src = Gio.SettingsSchemaSource.get_default();
        if (!src || !src.lookup(schemaId, true))
            return null;
        return new Gio.Settings({schema_id: schemaId});
    } catch (_e) {
        return null;
    }
}

function rectToJson(r) {
    if (!r)
        return {x: 0, y: 0, width: 0, height: 0};
    return {x: r.x | 0, y: r.y | 0, width: r.width | 0, height: r.height | 0};
}

const WINDOW_TYPE_NAMES = [
    'NORMAL', 'DESKTOP', 'DOCK', 'DIALOG', 'MODAL_DIALOG', 'TOOLBAR', 'MENU',
    'UTILITY', 'SPLASHSCREEN', 'DROPDOWN_MENU', 'POPUP_MENU', 'TOOLTIP',
    'NOTIFICATION', 'COMBO', 'DND', 'OVERRIDE_OTHER',
];
let windowTypeNames = null;

function windowTypeName(t) {
    if (!windowTypeNames) {
        windowTypeNames = new Map();
        for (const n of WINDOW_TYPE_NAMES) {
            const v = Meta.WindowType[n];
            if (typeof v === 'number')
                windowTypeNames.set(v, n);
        }
    }
    return windowTypeNames.get(t) ?? String(t);
}

function isX11(w) {
    if (isFn(w, 'get_client_type') && Meta.WindowClientType) {
        const ct = safe(() => w.get_client_type(), -1);
        if (ct === Meta.WindowClientType.X11)
            return true;
        if (ct === Meta.WindowClientType.WAYLAND)
            return false;
    }
    // Fallback: X11 windows describe themselves as "0x<xid>" (46) or
    // "0x<xid> (title)" (50); Wayland ones as "W<n>" / "W<n> (title)".
    return /^0x[0-9a-f]+/i.test(safe(() => w.get_description(), ''));
}

// X11 client window id of an XWayland window, 0 for native ones.
// VERIFIED (50.1): the id equals the window's _NET_CLIENT_LIST entry and
// xprop's WM_CLASS/_NET_WM_PID agree with wm_class_instance/wm_class/pid.
//
// meta_x11_display_lookup_xwindow(MetaX11Display*, MetaWindow*) -> Window is
// META_EXPORT in mutter 46 and 50 (meta-x11-display.h) and is the only public
// window -> xid route (meta_window_get_xwindow does not exist; the private
// meta_window_x11_get_xwindow is not in the GIR). global.display
// .get_x11_display() is null while Xwayland is not running, and then there
// are no X11 windows either. In 50 the declaration moved from display.h to
// meta-x11-display.h but it is still a Meta.Display method.
//
// Fallback: Mutter's window description (window.c meta_window_update_desc)
// is "0x%lx" on 46 and "0x%lx (title)" on 50 for X11 clients ("W<n>" /
// "W<n> (title)" for Wayland ones); the leading hex number is the xid.
function xidOf(w) {
    if (!isX11(w))
        return 0;
    const x11 = safe(() => (isFn(global.display, 'get_x11_display')
        ? global.display.get_x11_display() : null), null);
    if (x11 && isFn(x11, 'lookup_xwindow')) {
        const xid = safe(() => Number(x11.lookup_xwindow(w)), 0);
        if (xid > 0)
            return xid;
    }
    const m = /^0x([0-9a-f]+)/i.exec(safe(() => w.get_description(), ''));
    return m ? (parseInt(m[1], 16) || 0) : 0;
}

// [horizontally, vertically]
function maximizedFlags(w) {
    // The boolean GObject properties maximized-horizontally/-vertically are
    // defined by window.c on 46 and 50 alike. Fallbacks: get_maximized()
    // (flags, 46-48), get_maximize_flags() (49+), is_maximized() (49+).
    const h = safe(() => w.maximized_horizontally, null);
    const v = safe(() => w.maximized_vertically, null);
    if (typeof h === 'boolean' && typeof v === 'boolean')
        return [h, v];
    const F = Meta.MaximizeFlags || {};
    const H = F.HORIZONTAL ?? 1;
    const V = F.VERTICAL ?? 2;
    for (const getter of ['get_maximized', 'get_maximize_flags']) {
        if (isFn(w, getter)) {
            const f = safe(() => w[getter](), 0);
            return [!!(f & H), !!(f & V)];
        }
    }
    const m = safe(() => w.is_maximized(), false);
    return [m, m];
}

// Maximize/unmaximize per axis. Partial maximization exists on every target
// release: mutter 46-48 take the directions in maximize(flags) /
// unmaximize(flags); mutter 49+ moved them to set_maximize_flags(flags) /
// set_unmaximize_flags(flags) and maximize() there is literally
// set_maximize_flags(BOTH). Both generations g_assert() that at least one
// direction is set -- an empty flag set would abort gnome-shell, hence the
// guard, and the flags are never 0 for the SetState callers anyway.
function setMaximized(w, horz, vert, on) {
    const F = Meta.MaximizeFlags || {};
    const flags = (horz ? (F.HORIZONTAL ?? 1) : 0) | (vert ? (F.VERTICAL ?? 2) : 0);
    if (!flags)
        throw new BridgeError(ERR_INVALID, 'no maximize direction given');
    if (isFn(w, 'set_maximize_flags') && isFn(w, 'set_unmaximize_flags')) {
        // mutter 49+
        if (on)
            w.set_maximize_flags(flags);
        else
            w.set_unmaximize_flags(flags);
    } else if (on) {
        // mutter 46-48
        w.maximize(flags);
    } else {
        w.unmaximize(flags);
    }
}

function workspaceName(i) {
    if (isFn(Meta, 'prefs_get_workspace_name')) {
        const n = safe(() => Meta.prefs_get_workspace_name(i), '');
        if (n)
            return n;
    }
    return `Workspace ${i + 1}`;
}

// Newest $XDG_RUNTIME_DIR/.mutter-Xwaylandauth.XXXXXX: the cookie file
// Mutter writes for Xwayland (meta-xwayland.c prepare_auth_file, mkstemp).
// Only used when the shell process has no XAUTHORITY in its environment.
function findXauthority() {
    const dir = safe(() => GLib.get_user_runtime_dir(), '');
    if (!dir)
        return '';
    let best = '';
    let bestMtime = -1;
    let d = null;
    try {
        d = GLib.Dir.open(dir, 0);
        let name;
        while ((name = d.read_name()) !== null) {
            if (!name.startsWith('.mutter-Xwaylandauth.'))
                continue;
            const path = GLib.build_filenamev([dir, name]);
            const st = safe(() => Gio.File.new_for_path(path).query_info(
                'time::modified', Gio.FileQueryInfoFlags.NONE, null), null);
            const mtime = st ? Number(safe(() => st.get_attribute_uint64('time::modified'), 0)) : 0;
            if (mtime > bestMtime) {
                best = path;
                bestMtime = mtime;
            }
        }
    } catch (e) {
        debug(`scanning ${dir} failed: ${e}`);
    } finally {
        if (d)
            safe(() => d.close());
    }
    return best;
}

// The "Keep these display settings?" dialog is a ModalDialog created in
// gnome-shell's windowManager.js and not stored anywhere public; find it by
// class name in the modal dialog group.
// VERIFIED (46.0 and 50.1, identical source on both): windowManager.js has
// `GObject.registerClass(class DisplayChangeDialog extends
// ModalDialog.ModalDialog)`, and ModalDialog._init ends in
// `Main.layoutManager.modalDialogGroup.add_child(this)` -- so the dialog is a
// direct child of the first group below, and Main.uiGroup is only insurance
// for a release that moves it. Registration keeps the JS name: on gjs 1.80.2
// and 1.88.0 `constructor.name` is 'DisplayChangeDialog' and
// `constructor.$gtype.name` is 'Gjs_DisplayChangeDialog', so either half of
// the pair below matches. `_onSuccess` and `_onFailure` are the `action:` of
// the "Keep Changes" and "Revert Settings" buttons, so calling one is what
// pressing that button does -- measured against a live dialog raised by
// `wxrandr --persistent` on both releases (gnome/README.md "Verified live").
function findDisplayChangeDialog() {
    const groups = [
        safe(() => Main.layoutManager.modalDialogGroup, null),
        safe(() => Main.uiGroup, null),
    ];
    for (const g of groups) {
        if (!g)
            continue;
        for (const c of safe(() => g.get_children(), [])) {
            const name = `${safe(() => c.constructor.name, '')} ${safe(() => c.constructor.$gtype.name, '')}`;
            if (/DisplayChangeDialog/.test(name) && isFn(c, '_onSuccess') && isFn(c, '_onFailure'))
                return c;
        }
    }
    return null;
}

const STATE_ACTIONS = ['add', 'remove', 'toggle'];

// Method table: name -> [out signature, implementation]. Implementations
// return an array of out values (or nothing for "()"); they run with `this`
// bound to the extension. Exceptions become D-Bus errors in _invoke().
const METHODS = {
    // -- windows ----------------------------------------------------------
    ListWindows: ['(s)', function () {
        return [JSON.stringify(this._listWindows())];
    }],
    GetWindow: ['(s)', function (id) {
        return [JSON.stringify(this._windowInfo(this._find(id)))];
    }],
    Activate: ['()', function (id) {
        this._find(id).activate(now());
    }],
    Focus: ['()', function (id) {
        const w = this._find(id);
        const active = safe(() => global.workspace_manager.get_active_workspace(), null);
        const showing = safe(() => w.showing_on_its_workspace(), false) &&
            (!active || safe(() => w.located_on_workspace(active), false));
        // xdotool windowfocus = XSetInputFocus: focus without raising. Fall
        // back to activate for windows that are not visible right now.
        if (showing && isFn(w, 'focus'))
            w.focus(now());
        else
            w.activate(now());
    }],
    Close: ['()', function (id) {
        this._find(id).delete(now());
    }],
    Kill: ['()', function (id) {
        this._find(id).kill();
    }],
    Minimize: ['()', function (id) {
        this._find(id).minimize();
    }],
    Unminimize: ['()', function (id) {
        this._find(id).unminimize();
    }],
    Raise: ['()', function (id) {
        this._find(id).raise();
    }],
    Lower: ['()', function (id) {
        this._find(id).lower();
    }],
    Move: ['()', function (id, x, y) {
        // Frame coordinates, logical pixels. Mutter's constraints silently
        // keep maximized/fullscreen/tiled windows where they are (as on X11).
        this._find(id).move_frame(true, x, y);
    }],
    Resize: ['()', function (id, width, height) {
        const w = this._find(id);
        const r = w.get_frame_rect();
        w.move_resize_frame(true, r.x, r.y, width, height);
    }],
    MoveResize: ['()', function (id, x, y, width, height) {
        this._find(id).move_resize_frame(true, x, y, width, height);
    }],
    SetState: ['(b)', function (id, state, action) {
        return [this._setState(this._find(id), String(state), String(action))];
    }],
    MoveToWorkspace: ['()', function (id, index) {
        const w = this._find(id);
        if (index < 0) {
            // _NET_WM_DESKTOP 0xFFFFFFFF: all desktops.
            w.stick();
            return;
        }
        const ws = global.workspace_manager.get_workspace_by_index(index);
        if (!ws)
            throw new BridgeError(ERR_NOT_FOUND, `workspace ${index} not found`);
        if (safe(() => w.is_on_all_workspaces(), false))
            safe(() => w.unstick());
        w.change_workspace_by_index(index, false);
    }],
    // -- workspaces -------------------------------------------------------
    GetActiveWorkspace: ['(i)', function () {
        return [global.workspace_manager.get_active_workspace_index()];
    }],
    SetActiveWorkspace: ['()', function (index) {
        const ws = global.workspace_manager.get_workspace_by_index(index);
        if (!ws)
            throw new BridgeError(ERR_NOT_FOUND, `workspace ${index} not found`);
        ws.activate(now());
    }],
    GetNWorkspaces: ['(i)', function () {
        return [global.workspace_manager.get_n_workspaces()];
    }],
    SetNWorkspaces: ['()', function (count) {
        this._setNWorkspaces(count);
    }],
    ListWorkspaces: ['(s)', function () {
        return [JSON.stringify(this._listWorkspaces())];
    }],
    ShowDesktop: ['()', function (show) {
        this._showDesktop(!!show);
    }],
    // -- screen -----------------------------------------------------------
    DisplaySize: ['(ii)', function () {
        const [w, h] = global.display.get_size();
        return [w, h];
    }],
    // Diagnostic only (GnomeBackend.real_pointer()): getmouselocation reports
    // the daemon-tracked injected pointer by design; this is how the two are
    // checked against each other. No hit-test method on purpose -- the client
    // computes getmouselocation's window from ListWindows with the rule every
    // backend shares, so nothing can drift.
    GetPointer: ['(iiu)', function () {
        const [x, y, mods] = global.get_pointer();
        return [x, y, (mods >>> 0)];
    }],
    ListMonitors: ['(s)', function () {
        return [JSON.stringify(this._listMonitors())];
    }],
    XInfo: ['(ss)', function () {
        // Mutter g_setenv()s DISPLAY/XAUTHORITY in the shell process when it
        // binds the Xwayland sockets at startup (meta_xwayland_init), which
        // is before the lazy Xwayland spawn -- this is how a root caller
        // finds the X plane and its cookie file.
        // VERIFIED (46.0 and 50.1): both are set in gnome-shell's own
        // environment right after login (':0' and
        // $XDG_RUNTIME_DIR/.mutter-Xwaylandauth.XXXXXX). If DISPLAY ever
        // comes back empty the client falls back to /tmp/.X11-unix scanning
        // (fwcommon.session.find_x_display); the XAUTHORITY fallback below
        // scans the runtime dir for Mutter's cookie file.
        const display = GLib.getenv('DISPLAY') || '';
        const xauth = GLib.getenv('XAUTHORITY') || findXauthority();
        return [display, xauth];
    }],
    ConfirmDisplayChange: ['(b)', function (keep) {
        return [this._confirmDisplayChange(!!keep)];
    }],
    // -- misc -------------------------------------------------------------
    GetVersion: ['(u)', function () {
        return [VERSION];
    }],
};

// ---------------------------------------------------------------------------

export default class FuckwaylandBridge extends Extension {
    // D-Bus property `Version` (the method is GetVersion: GJS looks both
    // methods and properties up on this object, so they cannot share a name).
    get Version() {
        return VERSION;
    }

    enable() {
        this._wins = new Map();        // id -> Meta.Window
        this._handlers = new Map();    // Meta.Window -> {id, ids: [handler ids]}
        this._dead = new WeakSet();    // unmanaged windows whose actor lingers
        this._selects = new Set();     // pending SelectWindow finishers
        this._showDesktopWins = [];    // windows minimized by ShowDesktop(true)
        this._displayIds = [];
        this._wmIds = [];

        this._installMethods();

        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
            () => info(`acquired ${BUS_NAME}`),
            () => info(`lost ${BUS_NAME}`));

        const connect = (obj, list, sig, cb) => {
            try {
                list.push(obj.connect(sig, (...a) => {
                    try {
                        cb(...a);
                    } catch (e) {
                        debug(`handler ${sig} failed: ${e}`);
                    }
                }));
            } catch (e) {
                debug(`connect ${sig} failed: ${e}`);
            }
        };
        const display = global.display;
        connect(display, this._displayIds, 'window-created', (_d, w) => {
            this._track(w);
            this._emitWindow(w, 'new');
        });
        connect(display, this._displayIds, 'notify::focus-window', () => {
            const w = safe(() => display.focus_window, null);
            this._emit('WindowEvent', '(ts)', [w ? this._idOf(w) : 0, 'focus']);
        });
        const wm = global.workspace_manager;
        connect(wm, this._wmIds, 'active-workspace-changed', () => this._emit('WorkspaceEvent', '(s)', ['switch']));
        connect(wm, this._wmIds, 'workspace-added', () => this._emit('WorkspaceEvent', '(s)', ['add']));
        connect(wm, this._wmIds, 'workspace-removed', () => this._emit('WorkspaceEvent', '(s)', ['remove']));

        for (const w of this._allWindows())
            this._track(w);
        info(`enabled (bridge v${VERSION}, gnome-shell ${safe(() => Config.PACKAGE_VERSION, '?')})`);
    }

    disable() {
        for (const finish of Array.from(this._selects ?? []))
            safe(() => finish(0, ERR_CANCELLED, 'the bridge extension was disabled'));
        this._selects = null;

        for (const w of Array.from(this._handlers?.keys() ?? []))
            this._untrack(w);
        for (const h of this._displayIds ?? [])
            safe(() => global.display.disconnect(h));
        for (const h of this._wmIds ?? [])
            safe(() => global.workspace_manager.disconnect(h));
        this._displayIds = [];
        this._wmIds = [];

        if (this._nameId) {
            safe(() => Gio.bus_unown_name(this._nameId));
            this._nameId = 0;
        }
        if (this._dbus) {
            safe(() => this._dbus.flush());
            safe(() => this._dbus.unexport());
            this._dbus = null;
        }
        this._wins = null;
        this._handlers = null;
        this._dead = null;
        this._showDesktopWins = [];
        info('disabled');
    }

    // -- D-Bus plumbing -----------------------------------------------------

    // GJS' wrapJSObject calls `<Name>Async(params, invocation)` when no sync
    // `<Name>` exists. Going async for everything keeps error names, logging
    // and return packing under our control (a sync handler that throws makes
    // GJS logError() and invent an org.gnome.gjs.JSError.* name).
    _installMethods() {
        for (const [name, [sig, fn]] of Object.entries(METHODS)) {
            this[`${name}Async`] = (params, invocation) =>
                this._invoke(name, sig, fn, params, invocation);
        }
    }

    _invoke(name, sig, fn, params, invocation) {
        let out;
        try {
            out = fn.apply(this, Array.isArray(params) ? params : []);
        } catch (e) {
            this._returnError(invocation, name, e);
            return;
        }
        try {
            invocation.return_value(new GLib.Variant(sig, out ?? []));
        } catch (e) {
            this._returnError(invocation, name, new BridgeError(ERR_FAILED, `cannot pack reply ${sig}: ${e}`));
        }
    }

    _returnError(invocation, name, e) {
        const errName = (e && typeof e.name === 'string' && e.name.includes('.')) ? e.name : ERR_FAILED;
        const message = e && e.message ? String(e.message) : String(e);
        // Expected errors (NotFound, InvalidArgs, Unsupported) stay at debug
        // level; anything else is a bug worth a journal line.
        (errName === ERR_FAILED ? info : debug)(`${name}: ${errName}: ${message}`);
        try {
            invocation.return_dbus_error(errName, message);
        } catch (e2) {
            debug(`return_dbus_error failed: ${e2}`);
        }
    }

    _emit(signal, sig, values) {
        if (!this._dbus)
            return;
        try {
            this._dbus.emit_signal(signal, new GLib.Variant(sig, values));
        } catch (e) {
            debug(`emit ${signal} failed: ${e}`);
        }
    }

    _emitWindow(w, change) {
        const id = safe(() => this._idOf(w), 0);
        this._emit('WindowEvent', '(ts)', [id, change]);
    }

    // -- selectwindow -------------------------------------------------------

    // SelectWindow(u timeout_ms) -> t: the window under the pointer at the
    // NEXT BUTTON PRESS, which is what `xdotool selectwindow` means and what
    // KWin's own picker does. v1 waited for the compositor's focus signal
    // instead, so clicking the window that already had focus never returned
    // at all and every other click answered only after the focus had moved.
    //
    // The shape: take a grab (takeGrab above), swallow the next press, hit-test
    // the window under it, let go. Escape cancels; timeout_ms 0 means "wait
    // for the user" and is still bounded by SELECT_MAX_MS.
    //
    // A grab that outlives the call would leave the session unable to click
    // anything, which is far worse than a slow picker -- so every way out of
    // this method goes through the one teardown, _endSelect():
    //   * the button press and Escape, from the event handler;
    //   * the timeout, whose source is installed BEFORE the grab is taken, so
    //     even a setup that throws half way is already bounded;
    //   * the caller vanishing (Ctrl-C on `wdotool selectwindow`: the pending
    //     method call would otherwise simply never be answered), watched on
    //     the sender's unique bus name;
    //   * disable(), which cancels every pending selection;
    //   * a throw anywhere in setup or in the handler, both of which end in
    //     _endSelect() too.
    // _endSelect() releases whatever was taken, in any order, and is a no-op
    // the second time -- two of those racing is expected, not exceptional.
    SelectWindowAsync(params, invocation) {
        let sel = null;
        try {
            // One at a time. Two stage grabs coexist happily, but only the
            // first captured-event handler sees each event (it answers with
            // EVENT_STOP), so the second picker would sit there grabbing --
            // and then eat the user's next click, for up to SELECT_MAX_MS.
            if ((this._selects?.size ?? 0) > 0) {
                throw new BridgeError(ERR_UNSUPPORTED,
                    'another window selection is already in progress');
            }
            const quiet = selectCooldownUntil - Date.now();
            if (quiet > 0) {
                throw new BridgeError(ERR_UNSUPPORTED,
                    `a window selection just held the input grab; the next ` +
                    `one is refused for another ${Math.ceil(quiet / 1000)} s`);
            }
            if (shellIsModal()) {
                throw new BridgeError(ERR_UNSUPPORTED,
                    'the shell is already modal (the overview, a menu or a ' +
                    'dialog has the input); dismiss it and pick again');
            }
            const asked = Number(params?.[0] ?? 0) >>> 0;
            const ms = Math.min(asked || SELECT_MAX_MS, SELECT_MAX_MS);
            sel = {done: false, invocation, handlers: [], timerId: 0,
                   watchId: 0, grab: null, pick: null, startedAt: Date.now()};
            sel.finish = (id, errName, errMsg) =>
                this._endSelect(sel, id, errName, errMsg);
            this._selects.add(sel.finish);
            // Bound the grab before there is one: nothing below can throw
            // past this without the timeout still being armed.
            sel.timerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
                sel.timerId = 0;
                sel.finish(0, ERR_CANCELLED,
                    `no window picked within ${ms} ms`);
                return GLib.SOURCE_REMOVE;
            });
            this._beginSelect(sel);
        } catch (e) {
            // Keep a BridgeError's own name (a shell that will not grant a
            // grab is .Unsupported, not a bug of ours); anything else is
            // .Failed and gets a journal line from _endSelect.
            const name = (e && typeof e.name === 'string' && e.name.includes('.'))
                ? e.name : ERR_FAILED;
            const message = e && e.message ? String(e.message) : String(e);
            if (sel)
                sel.finish(0, name, message);
            else
                this._returnError(invocation, 'SelectWindow', e);
        }
    }

    _beginSelect(sel) {
        // The stage is the grab actor: no picker chrome to create, nothing
        // covering the screen, nothing to leak if a teardown step fails.
        const stage = global.stage;
        sel.grab = takeGrab(stage);
        if (!sel.grab) {
            throw new BridgeError(ERR_UNSUPPORTED,
                'the shell would not grant an input grab (something else is ' +
                'modal: the overview, a menu, a dialog)');
        }
        sel.handlers.push([stage, stage.connect('captured-event',
            (_a, event) => this._onSelectEvent(sel, event))]);
        // Cosmetic, and best-effort on both counts: a client's own cursor
        // wins over the display's while the pointer is over its window.
        safe(() => global.display.set_cursor(Meta.Cursor.CROSSHAIR));
        const sender = safe(() => sel.invocation.get_sender(), '');
        if (sender) {
            sel.watchId = safe(() => Gio.bus_watch_name_on_connection(
                Gio.DBus.session, sender, Gio.BusNameWatcherFlags.NONE, null,
                () => sel.finish(0, ERR_CANCELLED, 'the caller went away')), 0);
        }
    }

    _onSelectEvent(sel, event) {
        try {
            const T = Clutter.EventType;
            const type = safe(() => event.type(), null);
            if (type === T.BUTTON_PRESS || type === T.TOUCH_BEGIN) {
                if (sel.pick === null)
                    this._pickAt(sel, event);
                return Clutter.EVENT_STOP;
            }
            if (type === T.BUTTON_RELEASE || type === T.TOUCH_END) {
                if (sel.pick !== null)
                    sel.finish(sel.pick, null, null);
                return Clutter.EVENT_STOP;
            }
            if (type === T.KEY_PRESS) {
                const sym = safe(() => event.get_key_symbol(), 0);
                // Once the click has happened the answer is settled; Escape
                // is then just another key to swallow.
                if (sym === Clutter.KEY_Escape && sel.pick === null)
                    sel.finish(0, ERR_CANCELLED, 'cancelled with Escape');
                return Clutter.EVENT_STOP;
            }
            // Everything else the grab can see and an application would
            // otherwise act on: swallowed, so that nothing aimed at the
            // picker lands in the window under it. Motion, enter and leave
            // are deliberately let through -- hover feedback while aiming is
            // wanted and changes nothing about the answer.
            if (type === T.KEY_RELEASE || type === T.SCROLL ||
                type === T.TOUCH_UPDATE || type === T.TOUCH_CANCEL ||
                type === T.PAD_BUTTON_PRESS || type === T.PAD_BUTTON_RELEASE)
                return Clutter.EVENT_STOP;
        } catch (e) {
            // A handler that throws must not be the reason a grab is kept.
            sel.finish(0, ERR_FAILED, `picker failed: ${e}`);
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    // The press decides the answer. The grab is held a moment longer, for the
    // matching release, so the application does not receive a button-release
    // whose press it never saw; SELECT_RELEASE_MS bounds that wait.
    //
    // The deadline is *replaced*, never added to: exactly one timer is armed
    // at any moment, which is what makes _endSelect's single source_remove
    // enough. Every failure here still ends in an answer -- a throw goes to
    // the handler's catch, and a timeout source that cannot be created
    // finishes on the spot rather than leaving the grab unbounded.
    _pickAt(sel, event) {
        let x = 0, y = 0;
        const at = safe(() => event.get_coords(), null);
        if (at)
            [x, y] = at;
        else
            [x, y] = safe(() => global.get_pointer(), [0, 0]);
        sel.pick = this._windowUnderPointer(x, y);
        if (sel.timerId) {
            safe(() => GLib.source_remove(sel.timerId));
            sel.timerId = 0;
        }
        const id = safe(() => GLib.timeout_add(
            GLib.PRIORITY_DEFAULT, SELECT_RELEASE_MS, () => {
                sel.timerId = 0;
                sel.finish(sel.pick, null, null);
                return GLib.SOURCE_REMOVE;
            }), 0);
        if (id)
            sel.timerId = id;
        else
            sel.finish(sel.pick, null, null);
    }

    // The one teardown. Idempotent, order-independent, and it never lets an
    // exception skip the steps after it.
    _endSelect(sel, id, errName, errMsg) {
        if (sel.done)
            return;
        sel.done = true;
        // However this one ended, the next may not start until the shell has
        // had the input to itself for as long as this call took it away.
        selectCooldownUntil = Date.now() +
            Math.min(Date.now() - (sel.startedAt || Date.now()), SELECT_MAX_MS);
        this._selects?.delete(sel.finish);
        for (const [obj, hid] of sel.handlers)
            safe(() => obj.disconnect(hid));
        sel.handlers = [];
        if (sel.timerId) {
            safe(() => GLib.source_remove(sel.timerId));
            sel.timerId = 0;
        }
        if (sel.watchId) {
            safe(() => Gio.bus_unwatch_name(sel.watchId));
            sel.watchId = 0;
        }
        if (sel.grab) {
            safe(() => sel.grab.release());
            sel.grab = null;
        }
        safe(() => global.display.set_cursor(Meta.Cursor.DEFAULT));
        if (errName === ERR_FAILED)
            info(`SelectWindow: ${errName}: ${errMsg}`);
        try {
            if (errName)
                sel.invocation.return_dbus_error(errName, errMsg || errName);
            else
                sel.invocation.return_value(new GLib.Variant('(t)', [Number(id) || 0]));
        } catch (e) {
            debug(`SelectWindow reply failed: ${e}`);
        }
    }

    // Topmost window containing (x, y). NOT exported on the bus:
    // getmouselocation's hit-test stays the client-side one over ListWindows
    // (there must be exactly one of those). This one answers a different
    // question -- what the click that just happened would have landed on, at
    // the instant it happened -- which only the shell can know.
    //
    // The candidates are literally what ListWindows reports, so the picker
    // can never name a window no other command knows: same order (actors,
    // bottom-to-top), same exclusions (override-redirect surfaces, windows
    // whose info cannot be read). Measured live: hit-testing Mutter's raw
    // window list instead answered with an untitled surface that ListWindows
    // does not carry, for every click.
    //
    // DESKTOP and DOCK layers are looked through, as the client-side rule
    // does; hidden windows and other workspaces are not hits. The LAST hit
    // wins -- unlike getmouselocation's tie-break, a focused window does not
    // win over one stacked above it: for a picker the answer is what the
    // click would have gone to. 0 = the press landed on no window; on
    // Wayland there is no root window to name.
    _windowUnderPointer(x, y) {
        let hit = 0;
        for (const d of this._listWindows()) {
            if (d.window_type === 'DESKTOP' || d.window_type === 'DOCK')
                continue;
            if (d.minimized || d.hidden)
                continue;
            if (!(d.on_active_workspace || d.on_all_workspaces))
                continue;
            if (d.width <= 0 || d.height <= 0)
                continue;
            if (x >= d.x && x < d.x + d.width && y >= d.y && y < d.y + d.height)
                hit = d.id;
        }
        return hit;
    }

    // -- window bookkeeping -------------------------------------------------

    _idOf(w) {
        // get_id() is the 64-bit id Introspect uses; stable for the shell's
        // lifetime. Older typelibs without it fall back to the creation counter.
        if (isFn(w, 'get_id'))
            return Number(w.get_id());
        return Number(w.get_stable_sequence());
    }

    // Every managed window: actors first (bottom-to-top stacking), then
    // anything list_all_windows() knows that has no actor yet.
    _allWindows() {
        const seen = new Set();
        const out = [];
        const add = w => {
            if (w && !seen.has(w) && !this._dead.has(w)) {
                seen.add(w);
                out.push(w);
            }
        };
        for (const a of safe(() => global.get_window_actors(), []))
            add(safe(() => a.meta_window, null));
        for (const w of safe(() => global.display.list_all_windows(), []))
            add(w);
        return out;
    }

    _find(id) {
        id = Number(id);
        const cached = this._wins.get(id);
        if (cached && !this._dead.has(cached))
            return cached;
        for (const w of this._allWindows()) {
            if (safe(() => this._idOf(w), -1) === id) {
                this._track(w);
                return w;
            }
        }
        throw new BridgeError(ERR_NOT_FOUND, `window ${id} not found`);
    }

    _track(w) {
        if (!w || !this._wins || this._dead.has(w))
            return;
        const id = safe(() => this._idOf(w), null);
        if (id === null)
            return;
        this._wins.set(id, w);
        if (this._handlers.has(w))
            return;
        const ids = [];
        const on = (sig, cb) => {
            try {
                ids.push(w.connect(sig, () => {
                    try {
                        cb();
                    } catch (e) {
                        debug(`window ${sig} handler failed: ${e}`);
                    }
                }));
            } catch (e) {
                debug(`connect ${sig} failed: ${e}`);
            }
        };
        on('unmanaged', () => this._onUnmanaged(w));
        on('notify::title', () => this._emitWindow(w, 'title'));
        on('position-changed', () => this._emitWindow(w, 'move'));
        on('size-changed', () => this._emitWindow(w, 'move'));
        on('notify::fullscreen', () => this._emitWindow(w, 'fullscreen_mode'));
        on('notify::demands-attention', () => this._emitWindow(w, 'urgent'));
        on('notify::urgent', () => this._emitWindow(w, 'urgent'));
        on('workspace-changed', () => this._emitWindow(w, 'workspace'));
        on('notify::on-all-workspaces', () => this._emitWindow(w, 'workspace'));
        on('notify::minimized', () => this._emitWindow(w, 'minimized'));
        this._handlers.set(w, {id, ids});
    }

    _untrack(w) {
        const entry = this._handlers?.get(w);
        if (!entry)
            return;
        for (const h of entry.ids)
            safe(() => w.disconnect(h));
        this._handlers.delete(w);
        if (this._wins?.get(entry.id) === w)
            this._wins.delete(entry.id);
    }

    _onUnmanaged(w) {
        this._dead.add(w);
        this._emitWindow(w, 'close');
        this._untrack(w);
    }

    // -- window facts -------------------------------------------------------

    _listWindows() {
        const out = [];
        for (const a of safe(() => global.get_window_actors(), [])) {
            const w = safe(() => a.meta_window, null);
            if (!w || this._dead.has(w))
                continue;
            if (safe(() => w.is_override_redirect(), false))
                continue;
            this._track(w);
            try {
                out.push(this._windowInfo(w));
            } catch (e) {
                debug(`skipping window: ${e}`);
            }
        }
        return out;
    }

    _windowInfo(w) {
        const active = safe(() => global.workspace_manager.get_active_workspace(), null);
        const ws = safe(() => w.get_workspace(), null);
        const sticky = safe(() => w.is_on_all_workspaces(), false);
        const [maxH, maxV] = maximizedFlags(w);
        const transient = safe(() => w.get_transient_for(), null);
        const x11 = isX11(w);
        const frame = rectToJson(safe(() => w.get_frame_rect(), null));
        // No is_urgent() in either release; `urgent` and `demands-attention`
        // are GObject properties on both.
        const urgent = !!safe(() => w.urgent, false) || !!safe(() => w.demands_attention, false);
        return {
            id: this._idOf(w),
            xid: x11 ? xidOf(w) : 0,
            title: safe(() => w.get_title(), ''),
            wm_class: safe(() => w.get_wm_class(), ''),
            wm_class_instance: safe(() => w.get_wm_class_instance(), ''),
            gtk_app_id: safe(() => w.get_gtk_application_id(), ''),
            sandboxed_app_id: safe(() => w.get_sandboxed_app_id(), ''),
            desktop_id: safe(() => Shell.WindowTracker.get_default().get_window_app(w).get_id(), ''),
            role: safe(() => w.get_role(), ''),
            pid: safe(() => Number(w.get_pid()), 0) | 0,
            client_type: x11 ? 'x11' : 'wayland',
            window_type: windowTypeName(safe(() => w.get_window_type(), -1)),
            x: frame.x,
            y: frame.y,
            width: frame.width,
            height: frame.height,
            buffer_rect: rectToJson(safe(() => w.get_buffer_rect(), null)),
            focused: safe(() => w.has_focus(), false),
            minimized: !!safe(() => w.minimized, false),
            hidden: !safe(() => w.showing_on_its_workspace(), true),
            on_all_workspaces: sticky,
            workspace: sticky ? -1 : safe(() => ws.index(), -1),
            on_active_workspace: sticky || (!!active && safe(() => w.located_on_workspace(active), false)),
            monitor: safe(() => w.get_monitor(), -1),
            fullscreen: safe(() => w.is_fullscreen(), false),
            maximized_h: maxH,
            maximized_v: maxV,
            above: safe(() => w.is_above(), false),
            urgent,
            skip_taskbar: safe(() => w.is_skip_taskbar(), false),
            transient_for: transient ? safe(() => this._idOf(transient), 0) : 0,
            decorated: !!safe(() => w.decorated, true),
            stable_sequence: safe(() => w.get_stable_sequence(), 0),
        };
    }

    // Returns true when the state was changed (or already as requested),
    // false for states Mutter cannot set. Never throws for unknown states.
    _setState(w, state, action) {
        const S = state.toUpperCase();
        const A = action.toLowerCase();
        if (!STATE_ACTIONS.includes(A))
            throw new BridgeError(ERR_INVALID, `action must be add|remove|toggle, got ${action}`);
        const want = cur => (A === 'add' ? true : A === 'remove' ? false : !cur);

        switch (S) {
        case 'FULLSCREEN': {
            if (want(safe(() => w.is_fullscreen(), false)))
                w.make_fullscreen();
            else
                w.unmake_fullscreen();
            return true;
        }
        case 'MAXIMIZED_HORZ':
        case 'MAXIMIZED_VERT':
        case 'MAXIMIZED': {
            const [h, v] = maximizedFlags(w);
            const horz = S !== 'MAXIMIZED_VERT';
            const vert = S !== 'MAXIMIZED_HORZ';
            // MAXIMIZED is the pair in ONE call, and a client that wants
            // both axes has to send it rather than the two axis names in a
            // row. Mutter unmaximizes to the window's *current* frame rect
            // and takes only the axis it is unmaximizing from the saved
            // rectangle (meta_window_set_unmaximize_flags, window.c), so a
            // second single-axis call that arrives before the Wayland
            // client has answered the first configure carries the still
            // maximized half into its target -- and once both flags are
            // clear Mutter saves that rectangle as the restore size
            // (maybe_save_rect). Measured on GNOME 46 and 50; see
            // wwmctl.core._state_steps.
            //
            // Which way a *toggle* of the pair goes is Mutter's own rule
            // for the two atoms of one _NET_WM_STATE message: the
            // horizontal flag decides (window-x11.c, `max = action ==
            // _NET_WM_STATE_ADD || (action == _NET_WM_STATE_TOGGLE &&
            // !...is_maximized_horizontally (...))`). Before bridge v3
            // this said `h && v`, which disagreed with wmctrl on X for a
            // window maximized on one axis only.
            const cur = S === 'MAXIMIZED_VERT' ? v : h;
            setMaximized(w, horz, vert, want(cur));
            return true;
        }
        case 'HIDDEN': {
            if (want(!!safe(() => w.minimized, false)))
                w.minimize();
            else
                w.unminimize();
            return true;
        }
        case 'ABOVE': {
            if (want(safe(() => w.is_above(), false)))
                w.make_above();
            else
                w.unmake_above();
            return true;
        }
        case 'BELOW': {
            // Neither 46 nor 50 exports make_below(); wm_state_below is only
            // reachable through _NET_WM_STATE on X11 windows. Probed for free
            // in case a release adds one; otherwise "not applied".
            if (isFn(w, 'make_below') && isFn(w, 'unmake_below')) {
                if (want(!!safe(() => w.below, false)))
                    w.make_below();
                else
                    w.unmake_below();
                return true;
            }
            return false;
        }
        case 'STICKY': {
            if (want(safe(() => w.is_on_all_workspaces(), false)))
                w.stick();
            else
                w.unstick();
            return true;
        }
        case 'DEMANDS_ATTENTION': {
            if (want(!!safe(() => w.demands_attention, false)))
                w.set_demands_attention();
            else
                w.unset_demands_attention();
            return true;
        }
        case 'SHADED':
        case 'SKIP_TASKBAR':
        case 'SKIP_PAGER':
        case 'MODAL':
        default:
            return false;
        }
    }

    // -- workspaces ---------------------------------------------------------

    _listWorkspaces() {
        const wm = global.workspace_manager;
        const n = wm.get_n_workspaces();
        const active = wm.get_active_workspace_index();
        const out = [];
        for (let i = 0; i < n; i++) {
            const ws = wm.get_workspace_by_index(i);
            out.push({
                index: i,
                name: workspaceName(i),
                active: i === active,
                work_area: rectToJson(safe(() => ws.get_work_area_all_monitors(), null)),
                viewport: {x: 0, y: 0},
            });
        }
        return out;
    }

    _setNWorkspaces(count) {
        count = count | 0;
        if (count < 1 || count > 36)
            throw new BridgeError(ERR_INVALID, `workspace count must be 1..36, got ${count}`);
        const mutter = settings('org.gnome.mutter');
        if (mutter && safe(() => mutter.get_boolean('dynamic-workspaces'), false)) {
            throw new BridgeError(ERR_UNSUPPORTED,
                'dynamic workspaces are enabled (org.gnome.mutter dynamic-workspaces); ' +
                'the workspace count is managed by the shell');
        }
        const wm = global.workspace_manager;
        const ts = now();
        for (let guard = 0; guard < 64 && wm.get_n_workspaces() < count; guard++)
            wm.append_new_workspace(false, ts);
        for (let guard = 0; guard < 64 && wm.get_n_workspaces() > count; guard++) {
            const ws = wm.get_workspace_by_index(wm.get_n_workspaces() - 1);
            if (!ws)
                break;
            wm.remove_workspace(ws, ts);
        }
        // Keep the preference in step so Mutter's own handler agrees with us.
        const prefs = settings('org.gnome.desktop.wm.preferences');
        if (prefs)
            safe(() => prefs.set_int('num-workspaces', count));
        if (wm.get_n_workspaces() !== count)
            throw new BridgeError(ERR_FAILED, `workspace count is ${wm.get_n_workspaces()}, wanted ${count}`);
    }

    // wmctrl -k: Mutter's real show-desktop mode is not reachable from JS
    // (meta_workspace_manager_show_desktop is private), so minimize every
    // normal window on the active workspace and remember them for -k off.
    //
    // ShowDesktop(true) is IDEMPOTENT: a second one must not rescan, because
    // the scan skips already-minimized windows and would then store an empty
    // restore set, making every later ShowDesktop(false) a no-op (the mode
    // is a latch, not a stack -- `wmctrl -k on` twice then `-k off` restores
    // the desktop on every real WM).
    _showDesktop(show) {
        if (!show) {
            for (const w of this._showDesktopWins) {
                if (!this._dead.has(w) && safe(() => w.minimized, false))
                    safe(() => w.unminimize());
            }
            this._showDesktopWins = [];
            return;
        }
        if (this._showDesktopWins.length)
            return;                    // already on: keep the restore set
        const ws = global.workspace_manager.get_active_workspace();
        const done = [];
        for (const w of safe(() => ws.list_windows(), [])) {
            if (this._dead.has(w) || safe(() => w.is_override_redirect(), false))
                continue;
            if (safe(() => w.minimized, false))
                continue;
            const t = safe(() => w.get_window_type(), -1);
            if (t === Meta.WindowType.DESKTOP || t === Meta.WindowType.DOCK)
                continue;
            if (isFn(w, 'can_minimize') && !safe(() => w.can_minimize(), true))
                continue;
            try {
                w.minimize();
                done.push(w);
            } catch (e) {
                debug(`minimize failed: ${e}`);
            }
        }
        this._showDesktopWins = done;
    }

    // -- monitors -----------------------------------------------------------

    _listMonitors() {
        const mons = safe(() => Main.layoutManager.monitors, []);
        const primary = safe(() => Main.layoutManager.primaryIndex, -1);
        const connectors = this._connectorMap();
        return mons.map((m, i) => {
            const index = typeof m.index === 'number' ? m.index : i;
            return {
                index,
                x: m.x | 0,
                y: m.y | 0,
                width: m.width | 0,
                height: m.height | 0,
                scale: Number(m.geometry_scale ?? 1) || 1,
                primary: index === primary,
                connector: connectors.get(index) ?? '',
            };
        });
    }

    // index -> connector name, best-effort. MonitorManager
    // .get_monitor_for_connector(connector) (META_EXPORT on 46 and 50) returns
    // the logical monitor's number, i.e. the Main.layoutManager.monitors index;
    // MonitorManager.get_monitors() and Meta.Monitor.get_connector() (49+/50)
    // supply the connector names to feed it. GNOME 46 exports no way to
    // enumerate connectors from JS, so the map stays empty there and a client
    // matches DisplayConfig.GetCurrentState's logical monitors on x/y instead.
    // Mirrored monitors share a logical monitor: the first connector wins.
    _connectorMap() {
        const map = new Map();
        const mm = safe(() => global.backend.get_monitor_manager(), null);
        if (!mm)
            return map;
        // VERIFIED (50.1): get_monitors() yields Meta.Monitor objects with
        // get_connector() (mutter-18 typelib); ListMonitors reports
        // connector "Virtual-1" in the QEMU rig. On 46 this route is absent
        // and the connector stays "" (verified).
        if (isFn(mm, 'get_monitors') && isFn(mm, 'get_monitor_for_connector')) {
            for (const m of safe(() => mm.get_monitors(), [])) {
                const c = safe(() => m.get_connector(), '');
                const idx = c ? safe(() => mm.get_monitor_for_connector(c), -1) : -1;
                if (idx >= 0 && !map.has(idx))
                    map.set(idx, c);
            }
            if (map.size)
                return map;
        }
        // Fallback only (never needed on 50.1, where the route above already
        // filled the map): gjs.guide's 49 porting notes list
        // MonitorManager.get_logical_monitors(), LogicalMonitor.get_number()
        // and LogicalMonitor.get_monitors(); only get_logical_monitors() is in
        // the on-disk 50 header, so every call is probed.
        if (isFn(mm, 'get_logical_monitors')) {
            for (const lm of safe(() => mm.get_logical_monitors(), [])) {
                const idx = safe(() => lm.get_number(), -1);
                const first = safe(() => lm.get_monitors(), [])[0];
                const c = first ? safe(() => first.get_connector(), '') : '';
                if (idx >= 0 && c && !map.has(idx))
                    map.set(idx, c);
            }
        }
        return map;
    }

    // Best-effort: dismiss gnome-shell's "Keep these display settings?" dialog
    // (opened from Shell.WM 'confirm-display-change' after a persistent
    // ApplyMonitorsConfig). Returns true when a dialog was found and its
    // Keep/Revert action invoked. When none is found the verdict is still
    // forwarded to Mutter via global.window_manager.complete_display_change()
    // (a no-op when nothing is pending) and false is returned.
    _confirmDisplayChange(keep) {
        // Keeping a configuration with no enabled monitor is the one outcome
        // the dialog exists to prevent: nobody can press Revert on a screen
        // that is not there, and the 20-second self-revert is the only way
        // back. wxrandr never wants that state, so a caller asking for it was
        // not helping the user. An unreadable monitor list (-1) is not a
        // refusal -- this must not break a confirm on a shell we cannot read.
        const monitors = safe(() => Main.layoutManager.monitors.length, -1);
        if (keep && monitors === 0) {
            throw new BridgeError(ERR_UNSUPPORTED,
                'refusing to keep a display configuration with no enabled ' +
                'monitor (nothing could press Revert afterwards)');
        }
        const dialog = findDisplayChangeDialog();
        if (dialog) {
            if (keep)
                dialog._onSuccess();
            else
                dialog._onFailure();
            return true;
        }
        // VERIFIED (46.0 and 50.1): Shell.WM.complete_display_change(ok) is
        // what the dialog's _onSuccess/_onFailure call (shell-wm.c ->
        // meta_plugin_complete_display_change), and it is in the Shell-14 /
        // Shell-18 typelib on both -- the probe passes, so the call below is
        // really made rather than skipped. Harmless with nothing pending:
        // seven calls per release with no dialog on screen, and two more
        // straight after a keep had been confirmed, left the layout,
        // ~/.config/monitors.xml and the shell exactly as they were (the
        // confirmed layout was still up 20 s later). Still probed, so a shell
        // without the method just means "not handled".
        if (isFn(global.window_manager, 'complete_display_change'))
            safe(() => global.window_manager.complete_display_change(keep));
        return false;
    }
}
