// resource:///org/gnome/shell/extensions/extension.js
//
// The GNOME 45+ base class both extensions extend.  `uuid` and `path` are
// getters onto the metadata, as they are in the shell, so
// `new W11Overlap({uuid, path: <the real extension directory>})` gives the
// extension the tree it will read generations.json and typelib/ out of.

import {record} from './harness.mjs';

export class ExtensionBase {
    constructor(metadata) {
        this.metadata = metadata || {};
    }

    get uuid() {
        return this.metadata.uuid;
    }

    get path() {
        return this.metadata.path;
    }

    getSettings(schema) {
        return record('extension.getSettings', [schema ?? null], null);
    }

    getLogger() {
        return console;
    }
}

export class Extension extends ExtensionBase {
    openPreferences() {
        record('extension.openPreferences', []);
    }
}

export class ExtensionPreferences extends ExtensionBase {}

export function gettext(s) {
    return s;
}
