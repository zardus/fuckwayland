// gi://Shell.  The bridge asks it two things: which app owns a window
// (WindowTracker, for `desktop_id`) and what an action mode is worth when it
// takes a modal grab for `selectwindow`.
//
// `setApp(win, id)` is per window because that is the interesting case: a
// window with no app at all (an override-redirect surface, a DING desktop
// window on some builds) makes get_window_app() return null, and the bridge
// has to answer '' for it rather than throw.

import {record, tag} from './harness.mjs';

const apps = new Map();
let defaultApp = null;

const tracker = tag({}, 'WindowTracker');
tracker.get_window_app = w => {
    record('tracker.get_window_app', [w]);
    const id = apps.has(w) ? apps.get(w) : defaultApp;
    if (id === null || id === undefined)
        return null;
    const app = tag({}, `App(${id})`);
    app.get_id = () => record('app.get_id', [], id);
    return app;
};

const Shell = {
    // ShellActionMode, read out of Shell-14.typelib (gnome-shell 46.0-0ubuntu5,
    // noble) and Shell-18.typelib (gnome-shell 50.1-0ubuntu1.2, resolute):
    // identical on both, and POPUP is 1 << 7, not 1 << 5 -- 1 << 5 is
    // SYSTEM_MODAL.  The bridge only ever reads the two it names, but a case
    // that asserts what Main.pushModal() was handed compares numbers.
    ActionMode: {
        NONE: 0, NORMAL: 1 << 0, OVERVIEW: 1 << 1, LOCK_SCREEN: 1 << 2,
        UNLOCK_SCREEN: 1 << 3, LOGIN_SCREEN: 1 << 4, SYSTEM_MODAL: 1 << 5,
        LOOKING_GLASS: 1 << 6, POPUP: 1 << 7, ALL: -1,
    },

    WindowTracker: {
        get_default: () => record('WindowTracker.get_default', [], tracker),
    },

    // Named in the bridge's own header (Eval is disabled outside unsafe mode,
    // Introspect is read-only) and never called; present so a reference to
    // either is not a TypeError.
    Eval: class Eval {},
    Introspect: class Introspect {},
    WM: class WM {},

    // -- what a case turns -------------------------------------------------
    tracker,

    setApp(win, id) {
        apps.set(win, id);
    },

    setDefaultApp(id) {
        defaultApp = id;
    },

    reset() {
        apps.clear();
        defaultApp = null;
    },
};

export default Shell;
