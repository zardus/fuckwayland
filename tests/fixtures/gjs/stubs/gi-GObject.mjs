// gi://GObject: the GType registry the overlap extension asks for the size of
// MetaMonitorsConfig.
//
// That number is the whole struct-size check -- 72 bytes on GNOME 46
// (libmutter-14, measured on the rig), and a build whose registry says
// anything else must be refused -- so it is settable per case, and so is
// "the type is not registered at all", which is a `type_from_name` of 0.

import {record} from './harness.mjs';

const types = new Map();

const GObject = {
    TYPE_INVALID: 0,

    type_from_name(name) {
        const t = types.has(name) ? `gtype:${name}` : 0;
        return record('GObject.type_from_name', [name], t);
    },

    type_query(gtype) {
        record('GObject.type_query', [gtype]);
        const name = typeof gtype === 'string' ? gtype.replace(/^gtype:/, '') : null;
        const entry = name === null ? undefined : types.get(name);
        if (entry === undefined)
            throw new Error(`type_query: ${gtype} is not a registered type`);
        if (entry.throws)
            throw new Error(entry.throws);
        return {type: gtype, type_name: name, class_size: entry.classSize ?? 0,
                instance_size: entry.instanceSize};
    },

    // -- what a case turns -------------------------------------------------
    types,

    /**
     * Register `name` with the instance size the compositor's registry would
     * report.  `opts.throws` makes type_query() throw for it instead, which
     * is the hostile-compositor axis; `opts.classSize` is recorded for
     * completeness and nothing reads it.
     */
    setType(name, instanceSize, opts = {}) {
        types.set(name, {instanceSize, ...opts});
    },

    unsetType(name) {
        types.delete(name);
    },

    reset() {
        types.clear();
    },
};

export default GObject;
