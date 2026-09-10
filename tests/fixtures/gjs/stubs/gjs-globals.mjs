// The two things gjs puts in scope that are not imports: `global` (Shell's
// own Global object) and the legacy `imports.gi` namespace loader.
//
// Both are installed onto node's globalThis rather than replacing it, so
// node's own globals stay where they are and `global.display` still reads
// the way the extension writes it.  Nothing here has a default worth
// trusting: a case builds the display, the workspace manager and the window
// actors it wants and hands them over, because what those answer IS the
// state under test.

import {record, tag} from './harness.mjs';

const INSTALLED = ['display', 'workspace_manager', 'stage', 'backend',
                   'window_manager', 'get_window_actors', 'get_pointer',
                   'get_current_time'];

/** A GObject-ish signal source: connect/disconnect recorded, emit() by hand. */
export function makeEmitter(name) {
    const o = tag({}, name);
    let next = 0;
    o.handlers = new Map();
    o.connect = (sig, cb) => {
        const id = ++next;
        record(`${name}.connect`, [sig], id);
        o.handlers.set(id, {sig, cb});
        return id;
    };
    o.disconnect = id => {
        record(`${name}.disconnect`, [id]);
        o.handlers.delete(id);
    };
    o.emit = (sig, ...args) => {
        for (const h of [...o.handlers.values()]) {
            if (h.sig === sig)
                h.cb(o, ...args);
        }
    };
    return o;
}

/**
 * Put `parts` on the global object.  Anything not given is left out rather
 * than guessed: a missing `global.backend` is a real GNOME 45 shape and the
 * extension's safe() is meant to survive it.  Returns globalThis.
 */
export function installGlobals(parts = {}) {
    for (const [k, v] of Object.entries(parts))
        globalThis[k] = v;
    globalThis.get_window_actors = parts.get_window_actors
        ?? (() => record('global.get_window_actors', [], []));
    globalThis.get_current_time = parts.get_current_time
        ?? (() => record('global.get_current_time', [], 12345));
    globalThis.get_pointer = parts.get_pointer
        ?? (() => record('global.get_pointer', [], [0, 0, 0]));
    return globalThis;
}

export function clearGlobals() {
    for (const k of INSTALLED)
        delete globalThis[k];
    delete globalThis.imports;
}

/** `imports.gi[ns]` -- how the overlap extension picks its typelib up. */
export function installImports(ns, lib) {
    globalThis.imports = globalThis.imports || {gi: {}};
    globalThis.imports.gi = globalThis.imports.gi || {};
    if (ns === null)
        return globalThis.imports;
    Object.defineProperty(globalThis.imports.gi, ns, {
        configurable: true,
        get() {
            record('imports.gi', [ns]);
            if (lib instanceof Error)
                throw lib;
            return lib;
        },
    });
    return globalThis.imports;
}

// The seventeen entry points the overlap extension refuses to run without.
// The list is the extension's own, in its own order; a case that wants one
// of them missing passes `{drop: ['wr']}`, which is the symbols check's
// failing case.
export const LIB_SYMBOLS = [
    'dup_cfg', 'dup_node', 'dup_lmc', 'dup_mc', 'dup_ms', 'strn', 'addr',
    'unref_addr', 'type_name', 'get_config_manager', 'get_current',
    'create_linear', 'get_switch_config', 'set_switch_config', 'verify',
    'apply', 'wr',
];

/**
 * The loaded W11Overlap namespace.  Every symbol records its arguments under
 * `lib.<name>`; `impl` supplies the ones whose answer matters, `drop` takes
 * symbols away, and `notCallable` leaves a non-function in their place.
 */
export function makeLib(opts = {}) {
    const {impl = {}, drop = [], notCallable = []} = opts;
    const lib = tag({}, 'W11Overlap');
    for (const name of LIB_SYMBOLS) {
        if (drop.includes(name))
            continue;
        if (notCallable.includes(name)) {
            lib[name] = 0;
            continue;
        }
        lib[name] = (...args) => {
            record(`lib.${name}`, args);
            const f = impl[name];
            if (f instanceof Error)
                throw f;
            return typeof f === 'function' ? f(...args) : f ?? null;
        };
    }
    return lib;
}
