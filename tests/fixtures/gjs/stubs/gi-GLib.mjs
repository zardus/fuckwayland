// gi://GLib, as much of it as the two extensions touch.
//
// The file calls are backed by a dictionary a case fills in
// (`GLib.setFile(path, text)`), because both extensions read files whose
// content is the thing under test: `/proc/self/maps` decides every address
// bound, `monitors.xml` decides the digest, `generations.json` is the table.
// A path nobody set answers `[false, empty]` -- which is what the extension
// treats as "not there" -- unless `GLib.throwOnMissing` is turned on, which
// is gjs's real behaviour and the hostile-compositor axis.
//
// Checksums are the real thing (node:crypto), not a counter: the overlap
// extension compares a digest it computed before an apply with one it
// computes after, and a fake hash would make that comparison meaningless.

import {createHash} from 'node:crypto';
import {record, answer} from './harness.mjs';

const enc = new TextEncoder();

class GLibError extends Error {
    constructor(msg) {
        super(msg);
        this.name = 'GLib.Error';
        this.domain = 'g-file-error-quark';
        this.code = 4;
    }
}

const files = new Map();

const GLib = {
    Error: GLibError,

    PRIORITY_DEFAULT: 0,
    PRIORITY_DEFAULT_IDLE: 200,
    SOURCE_REMOVE: false,
    SOURCE_CONTINUE: true,

    // GFileTest, read back from this host's own GLib (python3-gi,
    // GLib.FileTest.EXISTS == 16): EXISTS is 1 << 4, not 1 << 0 -- 1 << 0 is
    // IS_REGULAR.  The overlap tests a path with FileTest.EXISTS twice
    // (extension.js:324 and :528).
    FileTest: {IS_REGULAR: 1 << 0, IS_SYMLINK: 1 << 1, IS_DIR: 1 << 2,
               IS_EXECUTABLE: 1 << 3, EXISTS: 1 << 4},
    ChecksumType: {MD5: 0, SHA1: 1, SHA256: 2, SHA512: 3, SHA384: 4},

    // -- files -------------------------------------------------------------
    file_get_contents(path) {
        record('GLib.file_get_contents', [path]);
        if (!files.has(path)) {
            if (GLib.throwOnMissing)
                throw new GLibError(`Failed to open file ${path}: No such file or directory`);
            return [false, new Uint8Array(0)];
        }
        return [true, files.get(path)];
    },

    file_test(path, flags) {
        record('GLib.file_test', [path, flags]);
        return files.has(path);
    },

    build_filenamev(parts) {
        return parts.join('/');
    },

    get_user_config_dir() {
        return record('GLib.get_user_config_dir', [], GLib.userConfigDir);
    },

    get_user_runtime_dir() {
        return record('GLib.get_user_runtime_dir', [], GLib.userRuntimeDir);
    },

    getenv(name) {
        return record('GLib.getenv', [name], GLib.environ[name] ?? null);
    },

    compute_checksum_for_data(type, bytes) {
        record('GLib.compute_checksum_for_data', [type]);
        const algo = {0: 'md5', 1: 'sha1', 2: 'sha256', 3: 'sha512', 4: 'sha384'}[type];
        return createHash(algo || 'sha256').update(Buffer.from(bytes)).digest('hex');
    },

    // -- the main loop -----------------------------------------------------
    //
    // Nothing runs by itself: a case that wants the callback fired calls
    // GLib.runTimeouts(), so "the bridge armed a 250 ms timer" and "the timer
    // went off" are two assertions and not one race.
    timeout_add(priority, interval, fn) {
        const id = ++GLib._nextSource;
        record('GLib.timeout_add', [priority, interval], id);
        GLib.timeouts.set(id, {interval, fn});
        return id;
    },

    source_remove(id) {
        record('GLib.source_remove', [id]);
        return GLib.timeouts.delete(id);
    },

    Variant: class Variant {
        constructor(sig, value) {
            this.signature = sig;
            this.value = value;
            record('GLib.Variant', [sig]);
        }

        deepUnpack() {
            return this.value;
        }

        unpack() {
            return this.value;
        }
    },

    Dir: {
        open: answer('GLib.Dir.open', null),
    },

    // -- what a case turns -------------------------------------------------
    files,
    throwOnMissing: false,
    userConfigDir: '/home/test/.config',
    userRuntimeDir: '/run/user/1000',
    environ: {},
    timeouts: new Map(),
    _nextSource: 0,

    /** Put `body` (a string or bytes) at `path`; null removes it. */
    setFile(path, body) {
        if (body === null || body === undefined)
            files.delete(path);
        else
            files.set(path, typeof body === 'string' ? enc.encode(body) : body);
        return path;
    },

    /** Fire every armed timeout once, oldest first; drop the ones that say so. */
    runTimeouts() {
        for (const [id, t] of [...GLib.timeouts]) {
            if (t.fn() !== true)
                GLib.timeouts.delete(id);
        }
    },

    reset() {
        files.clear();
        GLib.timeouts.clear();
        GLib.throwOnMissing = false;
        GLib.environ = {};
    },
};

export default GLib;
