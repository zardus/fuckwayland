// The ESM loader that lets node run gnome-shell's two extensions.
//
// gjs resolves `gi://Meta` through its own importer and
// `resource:///org/gnome/shell/ui/main.js` out of the shell's GResource
// bundle; node knows neither scheme and refuses the specifier before the
// module is ever read.  This hook answers both with a file beside it under
// stubs/, so `extension.js` -- the shipped file, byte for byte, imported by
// its real path -- evaluates in a plain node 22.
//
// Registered as `node --experimental-loader <this file>`: that form is what
// node 22.22.1 on this host accepts (the `module.register()` form needs a
// second entry file and buys nothing here).  Everything else, `./rules.js`
// included, goes to `next()` -- node's own detection reads that one as ESM.
//
// The stubs are recording doubles, not a swallowing Proxy: a test configures
// them through the same specifiers the extension imports (`gi://GObject` in
// the case file is the same module object the extension got), so what the
// extension asked for is readable afterwards.  See stubs/harness.mjs.

import {fileURLToPath} from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const stubs = path.join(here, 'stubs');
const RESOURCE = 'resource:///org/gnome/shell/';

function stub(name) {
    return {url: 'file://' + path.join(stubs, name), shortCircuit: true};
}

export async function resolve(spec, ctx, next) {
    if (spec.startsWith('gi://')) {
        // `gi://Meta?version=13` is legal gjs; the query is not part of the name.
        const ns = spec.slice(5).split('?')[0];
        return stub('gi-' + ns + '.mjs');
    }
    if (spec.startsWith(RESOURCE))
        return stub('shell-' + spec.slice(RESOURCE.length).replace(/\//g, '_') + '.mjs');
    return next(spec, ctx);
}
