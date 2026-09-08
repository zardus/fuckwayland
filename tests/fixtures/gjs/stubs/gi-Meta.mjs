// gi://Meta: the enumerations, and a Meta.Window double built from one row of
// the bridge's own ListWindows JSON.
//
// `makeWindow(info, {shape})` turns a dict shaped like
// tests/test_backend_gnome.fixture_windows() back into the object the
// extension would have been handed, so a test can run _windowInfo() over it
// and compare the JSON that comes out with what MockBridge answers for the
// same row.  Every getter is recorded, and `shape` picks the Mutter API
// generation:
//
//   '46'     boolean maximized-horizontally/-vertically properties and
//            maximize(flags)/unmaximize(flags)  (mutter 46-48; measured on
//            noble-gnome, GNOME Shell 46.0)
//   '49'     set_maximize_flags()/set_unmaximize_flags(); maximize() takes no
//            argument and throws when given one (mutter 49+, the path GNOME
//            51.beta took on stonking-gnome)
//   'flags'  neither boolean property: only get_maximize_flags() and
//            is_maximized(), the shape maximizedFlags() falls back through
//   'hostile' every getter throws -- the axis where safe() has to answer
//
// The window type arrives as a name ('NORMAL', 'DESKTOP') because that is
// what the JSON carries; it is turned back into the number the enum uses,
// which is the index in WINDOW_TYPE_NAMES (mutter's own enum order).

import {record, tag} from './harness.mjs';

const WINDOW_TYPES = [
    'NORMAL', 'DESKTOP', 'DOCK', 'DIALOG', 'MODAL_DIALOG', 'TOOLBAR', 'MENU',
    'UTILITY', 'SPLASHSCREEN', 'DROPDOWN_MENU', 'POPUP_MENU', 'TOOLTIP',
    'NOTIFICATION', 'COMBO', 'DND', 'OVERRIDE_OTHER',
];

const WindowType = {};
WINDOW_TYPES.forEach((n, i) => {
    WindowType[n] = i;
});

// mutter's MetaWindowClientType: WAYLAND first, X11 second.
const WindowClientType = {WAYLAND: 0, X11: 1};
const MaximizeFlags = {HORIZONTAL: 1, VERTICAL: 2, BOTH: 3};

/** A Meta.Workspace double: `index()` is all the bridge asks of one. */
export function makeWorkspace(n) {
    const ws = tag({}, `Workspace(${n})`);
    ws.index = () => record('ws.index', [], n);
    ws.n = n;
    return ws;
}

const workspaces = new Map();

function workspaceFor(n) {
    if (!workspaces.has(n))
        workspaces.set(n, makeWorkspace(n));
    return workspaces.get(n);
}

function rect(r) {
    return {x: r.x | 0, y: r.y | 0, width: r.width | 0, height: r.height | 0};
}

/**
 * A Meta.Window double.  `info` is a ListWindows row; `opts.shape` is above;
 * `opts.transientFor` is another double, since the JSON only carries an id;
 * `opts.description` overrides the string isX11()'s fallback reads (X11
 * windows describe themselves as "0x400005 (title)" on 50).
 */
export function makeWindow(info, opts = {}) {
    const shape = opts.shape || '46';
    const w = tag({}, `Window(${info.id})`);
    const hostile = shape === 'hostile';
    const state = {
        h: !!info.maximized_h,
        v: !!info.maximized_v,
        fullscreen: !!info.fullscreen,
        minimized: !!info.minimized,
        above: !!info.above,
        x11: info.client_type === 'x11',
    };
    w.state = state;

    const get = (name, value) => {
        w[name] = (...args) => {
            record(`w${info.id}.${name}`, args);
            if (hostile)
                throw new Error(`${name}: this compositor is hostile`);
            return typeof value === 'function' ? value(...args) : value;
        };
    };

    get('get_id', info.id);
    get('get_stable_sequence', info.stable_sequence ?? 0);
    get('get_title', info.title ?? '');
    get('get_wm_class', info.wm_class ?? '');
    get('get_wm_class_instance', info.wm_class_instance ?? '');
    get('get_gtk_application_id', info.gtk_app_id || null);
    get('get_sandboxed_app_id', info.sandboxed_app_id || null);
    get('get_role', info.role || null);
    get('get_pid', info.pid ?? 0);
    get('get_client_type', state.x11 ? WindowClientType.X11 : WindowClientType.WAYLAND);
    get('get_description', opts.description
        ?? (state.x11 ? `0x${(info.xid || 0).toString(16)} (${info.title})`
                      : `W${info.id} (${info.title})`));
    get('get_window_type', WindowType[info.window_type] ?? 0);
    get('get_frame_rect', () => rect(info));
    get('get_buffer_rect', () => rect(info.buffer_rect || info));
    get('get_monitor', info.monitor ?? -1);
    // A row with no `workspace` key at all is workspace 0, not undefined: a
    // Workspace double whose index() answered undefined came back out of the
    // bridge as JSON null where MockBridge answers a number.  Only a negative
    // workspace means "no workspace" (a dock, or a window being unmanaged).
    const wsIndex = info.workspace ?? 0;
    get('get_workspace', wsIndex < 0 ? null : workspaceFor(wsIndex));
    get('get_transient_for', opts.transientFor ?? null);
    get('has_focus', !!info.focused);
    get('showing_on_its_workspace', !info.hidden);
    get('is_on_all_workspaces', !!info.on_all_workspaces);
    get('located_on_workspace', ws => !!info.on_active_workspace && !!ws);
    get('is_override_redirect', !!opts.overrideRedirect);
    get('is_skip_taskbar', !!info.skip_taskbar);
    get('is_fullscreen', () => state.fullscreen);
    get('is_above', () => state.above);
    get('is_maximized', () => state.h && state.v);

    // The mutable properties: read straight off the double, as GObject
    // properties are, so `safe(() => w.minimized, false)` sees them.
    const prop = (name, read) => {
        Object.defineProperty(w, name, {
            enumerable: false,
            configurable: true,
            get() {
                record(`w${info.id}.get:${name}`, []);
                if (hostile)
                    throw new Error(`${name}: this compositor is hostile`);
                return read();
            },
        });
    };
    prop('minimized', () => state.minimized);
    prop('decorated', () => info.decorated !== false);
    prop('urgent', () => !!info.urgent);
    prop('demands_attention', () => false);
    prop('below', () => false);
    if (shape === '46' || shape === '49') {
        prop('maximized_horizontally', () => state.h);
        prop('maximized_vertically', () => state.v);
    }

    // The actions.  They move the double's own state, so a test can call
    // _setState twice and see where the window ended up.
    const act = (name, fn) => {
        w[name] = (...args) => {
            record(`w${info.id}.${name}`, args);
            if (hostile)
                throw new Error(`${name}: this compositor is hostile`);
            return fn(...args);
        };
    };
    act('make_fullscreen', () => {
        state.fullscreen = true;
    });
    act('unmake_fullscreen', () => {
        state.fullscreen = false;
    });
    act('minimize', () => {
        state.minimized = true;
    });
    act('unminimize', () => {
        state.minimized = false;
    });
    act('make_above', () => {
        state.above = true;
    });
    act('unmake_above', () => {
        state.above = false;
    });

    const setFlags = (flags, on) => {
        if (flags & MaximizeFlags.HORIZONTAL)
            state.h = on;
        if (flags & MaximizeFlags.VERTICAL)
            state.v = on;
    };
    if (shape === '49') {
        act('set_maximize_flags', f => setFlags(f, true));
        act('set_unmaximize_flags', f => setFlags(f, false));
        act('maximize', (...args) => {
            // mutter 49 dropped the argument: maximize(flags) is a
            // "Too many arguments" TypeError from gjs, not a no-op.
            if (args.length)
                throw new TypeError('Meta.Window.maximize: expected 0 arguments, got 1');
            setFlags(MaximizeFlags.BOTH, true);
        });
        act('unmaximize', (...args) => {
            if (args.length)
                throw new TypeError('Meta.Window.unmaximize: expected 0 arguments, got 1');
            setFlags(MaximizeFlags.BOTH, false);
        });
        act('get_maximize_flags', () => (state.h ? 1 : 0) | (state.v ? 2 : 0));
    } else {
        act('maximize', f => setFlags(f === undefined ? MaximizeFlags.BOTH : f, true));
        act('unmaximize', f => setFlags(f === undefined ? MaximizeFlags.BOTH : f, false));
        if (shape === 'flags')
            act('get_maximize_flags', () => (state.h ? 1 : 0) | (state.v ? 2 : 0));
    }

    // Signals: _track() connects nine of them and _untrack disconnects them.
    let nextHandler = 0;
    w.handlers = new Map();
    w.connect = (sig, cb) => {
        const id = ++nextHandler;
        record(`w${info.id}.connect`, [sig], id);
        if (hostile)
            throw new Error('connect: this compositor is hostile');
        w.handlers.set(id, {sig, cb});
        return id;
    };
    w.disconnect = id => {
        record(`w${info.id}.disconnect`, [id]);
        w.handlers.delete(id);
    };
    /** Fire every handler connected for `sig` -- how a test emits a change. */
    w.emit = sig => {
        for (const h of w.handlers.values()) {
            if (h.sig === sig)
                h.cb();
        }
    };
    return w;
}

/** A MetaWindowActor double: `meta_window` is the only field the bridge reads. */
export function makeActor(win) {
    const a = tag({}, `Actor(${win.__fw})`);
    a.meta_window = win;
    a.get_meta_window = () => record('actor.get_meta_window', [], win);
    return a;
}

const Meta = {
    MaximizeFlags,
    WindowType,
    WindowClientType,
    // MetaCursor, read out of Meta-14.typelib (gir1.2-mutter-14
    // 46.0-1ubuntu9, the noble-gnome golden's mutter): CROSSHAIR is 17.  The
    // enum is gone in Meta-18 (gir1.2-mutter-18 50.1-0ubuntu2.2) -- mutter 18
    // moved the names to Clutter.CursorType -- so on GNOME 50 the bridge's
    // `safe(() => global.display.set_cursor(Meta.Cursor.CROSSHAIR))`
    // (extension.js:882) reads undefined and sets no cursor at all.  A case
    // that wants that generation deletes this table: `delete Meta.Cursor`.
    Cursor: {
        NONE: 0, DEFAULT: 1, NORTH_RESIZE: 2, SOUTH_RESIZE: 3, WEST_RESIZE: 4,
        EAST_RESIZE: 5, SE_RESIZE: 6, SW_RESIZE: 7, NE_RESIZE: 8, NW_RESIZE: 9,
        MOVE_OR_RESIZE_WINDOW: 10, BUSY: 11, DND_IN_DRAG: 12, DND_MOVE: 13,
        DND_COPY: 14, DND_UNSUPPORTED_TARGET: 15, POINTING_HAND: 16,
        CROSSHAIR: 17, IBEAM: 18, BLANK: 19, LAST: 20,
    },
    Display: class Display {},
    Window: class Window {},
    Monitor: {get_connector: m => record('Meta.Monitor.get_connector', [m], null)},

    prefs_get_workspace_name(i) {
        return record('Meta.prefs_get_workspace_name', [i],
                      Meta.workspaceNames[i] ?? `Workspace ${i + 1}`);
    },

    // -- what a case turns -------------------------------------------------
    workspaceNames: [],
    makeWindow,
    makeActor,
    makeWorkspace,

    reset() {
        Meta.workspaceNames = [];
        workspaces.clear();
    },
};

export default Meta;
