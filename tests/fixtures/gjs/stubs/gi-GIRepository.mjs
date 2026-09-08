// gi://GIRepository, with every spelling of its moving API switchable.
//
// The overlap extension asks the same question four ways on purpose:
// `get_shared_library` (one comma-joined string) is glib <= 2.84,
// `get_shared_libraries` (an array) is after it, and each exists both as a
// method on the repository and as a static taking it -- and the size of a
// record moved between `info.get_size()`, `struct_info_get_size(info)` and
// `StructInfo.get_size(info)` between glib 2.80 and 2.88.  Which spellings
// exist is therefore the compositor-version axis for this file, so each one
// is a mode: 'ok' answers, 'missing' is not a property at all (an older or
// newer glib), 'throw' raises (gjs's answer for a call that does not apply),
// 'null' answers null.
//
// A case configures with GIRepository.configure({...}) BEFORE the extension
// runs: `version()` keeps the repository object it was handed, so a later
// change is not seen by a run already in progress.

import {record, tag} from './harness.mjs';

const state = {
    dupDefault: true,
    getVersion: true,
    metaVersion: '14',
    searchPaths: [],
    sharedLibraries: new Map(),   // ns -> array | string | null
    records: new Map(),           // "ns.Record" -> size, 0/absent = not found
    spelling: {},
};

const DEFAULT_SPELLING = {
    // glib 2.88 on Ubuntu 26.04: instance methods, arrays, info.get_size()
    instanceSharedLibraries: 'ok',
    instanceSharedLibrary: 'missing',
    staticSharedLibraries: 'missing',
    staticSharedLibrary: 'missing',
    instanceFindByName: 'ok',
    staticFindByName: 'missing',
    instancePrependSearchPath: 'ok',
    staticPrependSearchPath: 'ok',
    infoGetSize: 'ok',
    staticStructInfoGetSize: 'missing',
    staticStructInfoClassGetSize: 'missing',
};

function mode(which) {
    return state.spelling[which] ?? DEFAULT_SPELLING[which];
}

/** Build a function for `which`, or undefined when that spelling is absent. */
function spelled(which, name, fn) {
    const m = mode(which);
    if (m === 'missing')
        return undefined;
    return (...args) => {
        record(name, args);
        if (m === 'throw')
            throw new Error(`${name}: not available on this GIRepository`);
        if (m === 'null')
            return null;
        return fn(...args);
    };
}

function libsOf(ns) {
    const v = state.sharedLibraries.has(ns) ? state.sharedLibraries.get(ns) : [];
    return v;
}

function libStringOf(ns) {
    const v = libsOf(ns);
    return v === null ? null : (Array.isArray(v) ? v.join(',') : v);
}

function infoFor(ns, name) {
    const size = state.records.get(`${ns}.${name}`);
    if (!size)
        return null;
    const info = tag({}, `StructInfo(${ns}.${name})`);
    info.size = size;
    const get = spelled('infoGetSize', 'StructInfo.get_size', () => size);
    if (get)
        info.get_size = get;
    return info;
}

function makeRepository() {
    const repo = tag({}, 'GIRepository.Repository');
    if (state.getVersion)
        repo.get_version = ns => record('repo.get_version', [ns],
                                        ns === 'Meta' ? state.metaVersion : null);
    const prepend = spelled('instancePrependSearchPath', 'repo.prepend_search_path',
                            dir => void state.searchPaths.push(dir));
    if (prepend)
        repo.prepend_search_path = prepend;
    repo.require = (ns, version, flags) => record('repo.require', [ns, version, flags], null);

    const libs = spelled('instanceSharedLibraries', 'repo.get_shared_libraries', libsOf);
    if (libs)
        repo.get_shared_libraries = libs;
    const lib = spelled('instanceSharedLibrary', 'repo.get_shared_library', libStringOf);
    if (lib)
        repo.get_shared_library = lib;
    const find = spelled('instanceFindByName', 'repo.find_by_name', infoFor);
    if (find)
        repo.find_by_name = find;
    return repo;
}

let theRepo = makeRepository();

const GIRepository = {
    Repository: {},
    StructInfo: {},

    struct_info_get_size: undefined,

    // -- what a case turns -------------------------------------------------
    state,

    /** Merge `spec` into the configuration and rebuild the doubles. */
    configure(spec = {}) {
        if (spec.spelling)
            Object.assign(state.spelling, spec.spelling);
        for (const k of ['dupDefault', 'getVersion', 'metaVersion'])
            if (k in spec)
                state[k] = spec[k];
        if (spec.sharedLibraries)
            for (const [ns, v] of Object.entries(spec.sharedLibraries))
                state.sharedLibraries.set(ns, v);
        if (spec.records)
            for (const [k, v] of Object.entries(spec.records))
                state.records.set(k, v);
        GIRepository.rebuild();
    },

    rebuild() {
        theRepo = makeRepository();
        const R = {};
        if (state.dupDefault)
            R.dup_default = () => record('Repository.dup_default', [], theRepo);
        R.get_default = () => record('Repository.get_default', [], theRepo);
        const prepend = spelled('staticPrependSearchPath', 'Repository.prepend_search_path',
                                dir => void state.searchPaths.push(dir));
        if (prepend)
            R.prepend_search_path = prepend;
        const libs = spelled('staticSharedLibraries', 'Repository.get_shared_libraries',
                             (repo, ns) => libsOf(ns));
        if (libs)
            R.get_shared_libraries = libs;
        const lib = spelled('staticSharedLibrary', 'Repository.get_shared_library',
                            (repo, ns) => libStringOf(ns));
        if (lib)
            R.get_shared_library = lib;
        const find = spelled('staticFindByName', 'Repository.find_by_name',
                             (repo, ns, name) => infoFor(ns, name));
        if (find)
            R.find_by_name = find;
        GIRepository.Repository = R;

        GIRepository.struct_info_get_size = spelled(
            'staticStructInfoGetSize', 'GIRepository.struct_info_get_size',
            info => (info ? info.size : 0));
        const cls = spelled('staticStructInfoClassGetSize', 'StructInfo.get_size',
                            info => (info ? info.size : 0));
        GIRepository.StructInfo = cls ? {get_size: cls} : {};
    },

    /** The directories prepend_search_path was called with, in order. */
    get searchPaths() {
        return state.searchPaths.slice();
    },

    reset() {
        state.dupDefault = true;
        state.getVersion = true;
        state.metaVersion = '14';
        state.searchPaths.length = 0;
        state.sharedLibraries.clear();
        state.records.clear();
        state.spelling = {};
        GIRepository.rebuild();
    },
};

GIRepository.rebuild();

export default GIRepository;
