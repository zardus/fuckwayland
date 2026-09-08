// The recording spine every gi:// stub writes to, and the knobs a case turns.
//
// One process runs one case, so one module-level log is the whole story: the
// stubs push a record for every call the extension makes, in order, and the
// case reads `calls` afterwards.  Arguments are tagged rather than kept --
// a Meta.Window double is not JSON -- so a record survives
// `JSON.stringify()`, which is how a case answers support.js_harness().

export const calls = [];

let nextTag = 0;

/** Give `obj` a stable short name that shows up in the call log. */
export function tag(obj, name) {
    if (obj && typeof obj === 'object')
        Object.defineProperty(obj, '__fw', {value: name, enumerable: false});
    return obj;
}

function tagOf(v) {
    if (v === null || v === undefined)
        return v ?? null;
    const t = typeof v;
    if (t === 'number' || t === 'string' || t === 'boolean')
        return v;
    if (t === 'function')
        return `[function ${v.name || 'anonymous'}]`;
    if (Array.isArray(v))
        return v.map(tagOf);
    if (v.__fw)
        return v.__fw;
    if (v instanceof Uint8Array)
        return `[${v.length} bytes]`;
    return `[${v.constructor ? v.constructor.name : 'object'}]`;
}

/** Record one call and return `ret` (so a stub body is a one-liner). */
export function record(what, args = [], ret = undefined) {
    calls.push({what, args: args.map(tagOf)});
    return ret;
}

/** Every recorded call's name, in order -- the usual assertion. */
export function names() {
    return calls.map(c => c.what);
}

/** The calls whose name starts with `prefix`, in order. */
export function callsTo(prefix) {
    return calls.filter(c => c.what.startsWith(prefix));
}

/** How many calls have been recorded so far -- a mark to compare against. */
export function mark() {
    return calls.length;
}

/** The calls recorded since `mark()`. */
export function since(m) {
    return calls.slice(m);
}

export function reset() {
    calls.length = 0;
}

/**
 * A configurable answer: `f()` returns `value`, unless `throws` is set, in
 * which case it throws it.  `absent` makes the property not exist at all,
 * which is how an older or newer API generation is spelled.
 */
export function answer(what, value) {
    const f = (...args) => {
        record(what, args);
        if (f.throws)
            throw f.throws instanceof Error ? f.throws : new Error(`${f.throws}`);
        return typeof f.value === 'function' ? f.value(...args) : f.value;
    };
    f.value = value;
    f.throws = null;
    return f;
}

/** A unique opaque handle a stub can hand out and a case can recognise. */
export function handle(name) {
    return tag({}, `${name}#${++nextTag}`);
}
