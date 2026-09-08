// resource:///org/gnome/shell/misc/config.js
//
// One string, and the whole compositor-version axis turns on it: the overlap
// extension refuses a shell whose major is not in generations.json, and the
// bridge prints it in its enabled line.  Measured values: '46.0' on
// noble-gnome, '50.1' on resolute-gnome, '51.beta' on stonking-gnome.
//
// It is a `let` with a setter because a namespace import (`import * as
// Config`) reads the live binding: a case calls setPackageVersion() and the
// extension sees the new value on its next read, with no module reloading.

export let PACKAGE_VERSION = '50.1';

export function setPackageVersion(v) {
    PACKAGE_VERSION = v;
}
