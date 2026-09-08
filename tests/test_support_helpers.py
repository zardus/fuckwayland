#!/usr/bin/env python3
"""The shared doubles, tested as themselves.

`tests/support.py` is imported by two dozen files, so a double that lies
there is a whole afternoon of chasing the wrong bug.  Everything added for
the second round of tests is proved here before anything leans on it: the
markdown walker, the shell-script slicer, the fake GNOME command line the
install scripts are run against, the node harness that executes the two
GNOME Shell extensions, the sway IPC double, the session-leader stand-in and
the wl-mirror stub.

Where a helper stands in for a real thing, it is checked against that thing
rather than against itself: the sliced shell blocks are handed to `sh`, the
sway double is driven by wxrandr's own `SwayIPC` client over the real i3-ipc
framing, and the node harness runs the two `extension.js` files that ship --
by their real path, not a copy.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import support
from wxrandr.core import Fatal, SwayIPC


def _rmtree(path):
    shutil.rmtree(path, ignore_errors=True)


def _resolve_sleep():
    """Where `sleep` really lives, and whether it is a multicall binary.

    On Ubuntu 26.04 /usr/bin/sleep is a symlink into
    /usr/lib/cargo/bin/coreutils/, one binary that dispatches on argv[0]; on a
    GNU-coreutils host it is a program of its own.  Which one this is decides
    whether the test below has anything to assert."""
    path = shutil.which("sleep") or "/bin/sleep"
    real = os.path.realpath(path)
    return real, "coreutils" in real


_SLEEP_REAL, _SLEEP_IS_MULTICALL = _resolve_sleep()


class Documents(unittest.TestCase):
    """`documents()` -- the markdown walker scripts/check-docs.py,
    test_release_deb.py and the anchor checker each carried a copy of."""

    def test_it_finds_the_documents_by_their_path_relative_to_the_root(self):
        docs = support.documents()
        self.assertIn("README.md", docs)
        self.assertIn(os.path.join("docs", "Technical.md"), docs)
        self.assertIn("# fuckwayland", docs["README.md"])

    def test_it_agrees_with_the_walker_check_docs_carries(self):
        """The one that has to keep working: scripts/check-docs.py reports 0
        disagreements over exactly this set, and a walker that saw a different
        set would move that number without anybody editing a document."""
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        try:
            spec = importlib.util.spec_from_file_location(
                "check_docs_for_test", os.path.join(ROOT, "scripts", "check-docs.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.path.pop(0)
        self.assertEqual(set(mod.documents()), set(support.documents()))

    def test_it_walks_past_git_and_pycache(self):
        with tempfile.TemporaryDirectory() as tmp:
            for sub in (".git", "__pycache__", "node_modules", "docs"):
                os.makedirs(os.path.join(tmp, sub))
                with open(os.path.join(tmp, sub, "x.md"), "w") as fh:
                    fh.write("# x\n")
            self.assertEqual(set(support.documents(tmp)), {os.path.join("docs", "x.md")})

    def test_bytes_no_encoding_claims_do_not_stop_the_walk(self):
        """errors='replace' is not decoration: the walk covers media/ and
        vm/, where a note may hold anything."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "bad.md"), "wb") as fh:
                fh.write(b"# t\n\xff\xfe not utf-8\n")
            self.assertIn("not utf-8", support.documents(tmp)["bad.md"])


class ShellSlices(unittest.TestCase):
    """`sh_function()` and `sh_block()` -- the installers' branches are only
    reachable inside a GNOME session, so the block itself is run instead."""

    OVERLAP = os.path.join(ROOT, "gnome", "install-overlap.sh")
    BRIDGE = os.path.join(ROOT, "gnome", "install-bridge.sh")

    def test_a_function_comes_out_whole_and_sh_accepts_it(self):
        src = support.sh_function(self.OVERLAP, "enable_setting")
        self.assertTrue(src.startswith("enable_setting() {"), src[:40])
        self.assertTrue(src.endswith("}\n"), src[-40:])
        self.assertIn("enabled-extensions", src)
        r = subprocess.run(["sh", "-n"], input=src, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_every_function_both_installers_define_can_be_sliced(self):
        """Not one name written down here: whatever the scripts define is what
        gets sliced, so a rename cannot quietly stop being covered."""
        for path in (self.OVERLAP, self.BRIDGE):
            with open(path, encoding="utf-8") as fh:
                names = re.findall(r"^(\w+)\(\) \{$", fh.read(), re.M)
            self.assertGreater(len(names), 5, path)
            for name in names:
                src = support.sh_function(path, name)
                self.assertTrue(src.startswith("%s() {" % name), (path, name))
                r = subprocess.run(["sh", "-n"], input=src, capture_output=True,
                                   text=True, timeout=30)
                self.assertEqual(r.returncode, 0, (path, name, r.stderr))

    def test_a_function_that_is_not_there_says_so(self):
        with self.assertRaises(AssertionError) as cm:
            support.sh_function(self.OVERLAP, "no_such_function")
        self.assertIn("no function no_such_function()", str(cm.exception))

    def test_a_block_is_inclusive_of_both_markers(self):
        block = support.sh_block(self.OVERLAP, "FOUND=\n", 'FOUND=$SYSTEM_DIR/$UUID\nfi')
        self.assertTrue(block.startswith("FOUND=\n"))
        self.assertTrue(block.endswith("FOUND=$SYSTEM_DIR/$UUID\nfi"))
        self.assertIn("$DEST", block)

    def test_a_marker_that_no_longer_matches_is_a_failure_not_an_empty_slice(self):
        """A slice that silently came back empty would be a test that passes
        over a script it never read."""
        with self.assertRaises(ValueError):
            support.sh_block(self.OVERLAP, "FOUND=\n", "this text is not in the script")


class FakeGnomeCommandLine(unittest.TestCase):
    """`fake_gnome_bin()` -- gsettings, gnome-extensions, gdbus and the rest,
    over one state file.  Every branch of debian/enable-bridge is chosen by
    what these answer, and the real ones would write into the runner's dconf."""

    UUID = "fuckwayland-bridge@fuckwayland"
    KEY = "org.gnome.shell/enabled-extensions"
    DISABLED = "org.gnome.shell/disabled-extensions"
    OVERLAP = os.path.join(ROOT, "gnome", "install-overlap.sh")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fake-gnome-")
        self.addCleanup(_rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        self.log = os.path.join(self.tmp, "log")
        self.bin = support.fake_gnome_bin(os.path.join(self.tmp, "bin"), self.state)

    def run_tool(self, *argv, **env):
        e = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                 FAKE_LOG=self.log)
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run(list(argv), capture_output=True, text=True,
                              env=e, timeout=30)

    def logged(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [ln.rstrip("\n") for ln in fh]

    def test_list_schemas_is_what_decides_this_is_a_gnome_session(self):
        """`gsettings list-schemas | grep -qx org.gnome.shell` is the line in
        debian/enable-bridge that makes it a no-op on Plasma or sway."""
        support.fake_gnome_state(self.state, schemas=("org.gnome.shell",
                                                      "org.gnome.desktop.interface"))
        r = self.run_tool("gsettings", "list-schemas")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(r.stdout.split()),
                         ["org.gnome.desktop.interface", "org.gnome.shell"])
        support.fake_gnome_state(self.state, schemas=())
        self.assertEqual(self.run_tool("gsettings", "list-schemas").stdout, "")

    def test_get_returns_the_variant_text_verbatim(self):
        """`@as []` and `[]` are different strings to these scripts -- an empty
        list reads back as `@as []` from a real gsettings, and the case arm in
        enable-bridge names both -- so the double must not normalise either."""
        for value in ("@as []", "[]", "['a@x']"):
            support.fake_gnome_state(self.state, settings={self.KEY: value})
            r = self.run_tool("gsettings", "get", "org.gnome.shell", "enabled-extensions")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), value)

    def test_an_unknown_schema_or_key_fails_the_way_gsettings_does(self):
        support.fake_gnome_state(self.state, schemas=("org.gnome.shell",))
        r = self.run_tool("gsettings", "get", "org.example.nope", "k")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No such schema", r.stderr)
        r = self.run_tool("gsettings", "get", "org.gnome.shell", "never-set")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No such key", r.stderr)

    def test_set_is_read_back_and_logged(self):
        support.fake_gnome_state(self.state, settings={self.KEY: "@as []"})
        r = self.run_tool("gsettings", "set", "org.gnome.shell",
                          "enabled-extensions", "['%s']" % self.UUID)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(support.gnome_list(self.state, self.KEY), (self.UUID,))
        self.assertIn("gsettings set org.gnome.shell enabled-extensions ['%s']" % self.UUID,
                      self.logged())

    def test_set_can_be_made_to_fail(self):
        """A dconf that will not commit is the branch enable-bridge is silent
        about today; a test needs it to be reachable."""
        support.fake_gnome_state(self.state, settings={self.KEY: "@as []"})
        r = self.run_tool("gsettings", "set", "org.gnome.shell", "enabled-extensions",
                          "['x']", FAKE_GSETTINGS_SET_FAILS="failed to commit changes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("failed to commit changes", r.stderr)
        self.assertEqual(support.gnome_setting(self.state, self.KEY), "@as []")

    def test_an_unseeded_key_reads_back_the_schema_default(self):
        """A fresh desktop has never written enabled-extensions, and gsettings
        answers the schema default for it -- `@as []`, exit 0 -- not `No such
        key`.  It matters because every caller reads with `|| echo '[]'`
        (install-bridge.sh:208 for disabled-extensions): a double that failed
        here steered every unseeded run into the `[]` arm of enable-bridge's
        case and the `@as []` arm a real GNOME produces was never taken.
        Defaults read out of org.gnome.shell.gschema.xml in gnome-shell-common
        50.1-0ubuntu1.2."""
        support.fake_gnome_state(self.state, settings={})
        for key, default in (("enabled-extensions", "@as []"),
                             ("disabled-extensions", "@as []"),
                             ("disable-user-extensions", "false")):
            r = self.run_tool("gsettings", "get", "org.gnome.shell", key)
            self.assertEqual(r.returncode, 0, (key, r.stderr))
            self.assertEqual(r.stdout.strip(), default, key)

    def test_a_key_the_schema_does_not_have_is_still_an_error(self):
        """The default is per key, not a blanket "anything reads empty": a
        misspelled key has to fail the way gsettings fails, or a test would
        pass over a typo in the script it is checking."""
        support.fake_gnome_state(self.state, settings={})
        r = self.run_tool("gsettings", "get", "org.gnome.shell", "enabled-extension")
        self.assertEqual(r.returncode, 1)
        self.assertIn('No such key "enabled-extension"', r.stderr)
        self.assertEqual(r.stdout, "")

    def test_gnome_extensions_refuses_a_uuid_the_shell_has_not_seen(self):
        """The refusal that sends every caller down its gsettings fallback: on
        a fresh install, before the relogin, the running shell has never
        scanned the directory the package wrote into."""
        support.fake_gnome_state(self.state, settings={self.KEY: "@as []"})
        r = self.run_tool("gnome-extensions", "enable", self.UUID)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr.strip(), 'Extension "%s" does not exist' % self.UUID)
        self.assertEqual(support.gnome_list(self.state, self.KEY), ())

    def test_enabling_a_loaded_uuid_moves_it_between_the_two_lists(self):
        """What the real front end does: EnableExtension adds to
        enabled-extensions AND removes from disabled-extensions.  A uuid left
        in the second list is an extension that never loads."""
        support.fake_gnome_state(self.state, loaded=(self.UUID,),
                                 settings={self.KEY: "['other@x']",
                                           self.DISABLED: "['%s']" % self.UUID})
        r = self.run_tool("gnome-extensions", "enable", self.UUID)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(support.gnome_list(self.state, self.KEY),
                         ("other@x", self.UUID))
        self.assertEqual(support.gnome_list(self.state, self.DISABLED), ())
        r = self.run_tool("gnome-extensions", "disable", self.UUID)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(support.gnome_list(self.state, self.KEY), ("other@x",))
        self.assertEqual(support.gnome_list(self.state, self.DISABLED), (self.UUID,))

    def test_an_emptied_list_renders_as_gsettings_renders_it(self):
        support.fake_gnome_state(self.state, loaded=(self.UUID,),
                                 settings={self.KEY: "['%s']" % self.UUID})
        self.run_tool("gnome-extensions", "disable", self.UUID)
        self.assertEqual(support.gnome_setting(self.state, self.KEY), "@as []")

    #: install-overlap.sh's own gdbus parsers, and the three helpers they are
    #: built on.  Sliced rather than re-typed: a test that carried its own copy
    #: of the script's sed would keep passing while the script changed.
    PARSERS = ("have", "as_user", "gd", "shell_version", "name_owned",
               "ext_loaded", "ext_state")

    def run_parsers(self, body, **env):
        """Run `body` after install-overlap.sh's own gdbus functions, with the
        fake command line on PATH and the non-root arm of as_user taken."""
        src = [support.sh_block(self.OVERLAP, "UUID='fuckwayland-overlap@fuckwayland'",
                                "EXT_IFACE='org.gnome.Shell.Extensions'"),
               "ME=%d\nTARGET_UID=%d\nTARGET_USER=nobody\nBUS_ADDR=\nRUNTIME_DIR=\n"
               % (os.getuid(), os.getuid())]
        src += [support.sh_function(self.OVERLAP, name) for name in self.PARSERS]
        src.append(body)
        e = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                 FAKE_LOG=self.log)
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run(["sh", "-s"], input="\n".join(src), capture_output=True,
                              text=True, env=e, timeout=60)

    def test_gdbus_answers_what_the_installers_own_parsers_read(self):
        """The installers parse gdbus with sed, so the shape matters as much
        as the value -- `(<'51.beta'>,)` for the ShellVersion property,
        `(true,)` for NameHasOwner, `'state': <4>` for GetExtensionInfo.  The
        parsers here are gnome/install-overlap.sh:146, :153, :159 and :164
        themselves, sliced out and run over the fake, so the fake is checked
        against the script rather than against a copy of its sed."""
        support.fake_gnome_state(self.state)
        r = self.run_parsers("shell_version", FAKE_SHELL_VERSION="51.beta")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "51.beta")
        r = self.run_parsers("name_owned && echo owned || echo free")
        self.assertEqual(r.stdout.strip(), "free")
        r = self.run_parsers("name_owned && echo owned || echo free",
                             FAKE_NAME_OWNED="true")
        self.assertEqual(r.stdout.strip(), "owned")
        r = self.run_parsers("ext_state", FAKE_EXT_STATE="4", FAKE_EXT_LOADED=self.UUID)
        self.assertEqual(r.stdout.strip(), "4")
        r = self.run_parsers("ext_loaded && echo yes || echo no",
                             FAKE_EXT_LOADED="fuckwayland-overlap@fuckwayland")
        self.assertEqual(r.stdout.strip(), "yes")
        r = self.run_parsers("ext_loaded && echo yes || echo no",
                             FAKE_EXT_LOADED="somebody-else@x")
        self.assertEqual(r.stdout.strip(), "no")

    def test_the_installers_own_parsers_read_nothing_when_no_shell_answers(self):
        """No shell on the bus is gdbus exiting 1 with nothing on stdout, and
        both parsers then print nothing at all: install-overlap.sh's `-` arm
        belongs to a host with no gdbus, not to a host whose gdbus fails, so a
        double that printed an error there would send --check down the wrong
        branch."""
        support.fake_gnome_state(self.state)
        self.assertEqual(self.run_parsers("ext_state").stdout, "")
        self.assertEqual(self.run_parsers("shell_version").stdout, "")
        r = self.run_parsers("v=$(shell_version); echo \"[$v]\"")
        self.assertEqual(r.stdout.strip(), "[]")

    def test_no_shell_on_the_bus_is_gdbus_exiting_one(self):
        support.fake_gnome_state(self.state)
        r = self.run_tool("gdbus", "call", "--method",
                          "org.freedesktop.DBus.Properties.Get", "ShellVersion")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")

    def test_dpkg_S_answers_who_owns_a_path(self):
        """`--system` refuses to write over dpkg's payload; the test of that
        needs a dpkg that says the package owns the file, and one that does
        not know it."""
        support.fake_gnome_state(self.state)
        r = self.run_tool("dpkg", "-S", "/usr/share/gnome-shell/extensions/x",
                          FAKE_DPKG_S="fuckwayland")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(),
                         "fuckwayland: /usr/share/gnome-shell/extensions/x")
        r = self.run_tool("dpkg", "-S", "/tmp/x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no path found", r.stderr)

    def test_sudo_and_runuser_run_the_command_here(self):
        """The TARGET_USER/RUNTIME_DIR/BUS_ADDR block is what is under test in
        those scripts, not privilege: `sudo -u u env A=B cmd` has to reach cmd
        with A=B and no password prompt."""
        support.fake_gnome_state(self.state)
        r = self.run_tool("sudo", "-u", "someone", "env", "FOO=bar", "sh", "-c",
                          'printf %s "$FOO"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "bar")
        r = self.run_tool("runuser", "-u", "someone", "--", "sh", "-c", "echo hi")
        self.assertEqual(r.stdout.strip(), "hi")
        self.assertTrue([ln for ln in self.logged() if ln.startswith("sudo -u someone")])

    def test_id_and_getent_answer_for_a_user_that_does_not_exist_here(self):
        support.fake_gnome_state(self.state)
        self.assertEqual(self.run_tool("id", "-u", "root").stdout.strip(), "0")
        self.assertEqual(self.run_tool("id", "-u", "someone", FAKE_UID=1234).stdout.strip(),
                         "1234")
        self.assertEqual(self.run_tool("id", "-un", "someone",
                                       FAKE_USER="tester").stdout.strip(), "tester")
        r = self.run_tool("id", "-u", "ghost", FAKE_ID_UNKNOWN="ghost")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no such user", r.stderr)
        r = self.run_tool("getent", "passwd", "someone",
                          FAKE_PASSWD="someone:x:1234:1234::/home/someone:/bin/sh")
        self.assertEqual(r.stdout.strip(),
                         "someone:x:1234:1234::/home/someone:/bin/sh")
        self.assertEqual(self.run_tool("getent", "passwd", "ghost").returncode, 2)

    def test_every_call_is_logged_so_no_call_at_all_can_be_asserted(self):
        support.fake_gnome_state(self.state, settings={self.KEY: "@as []"})
        self.assertEqual(self.logged(), [])
        self.run_tool("gsettings", "get", "org.gnome.shell", "enabled-extensions")
        self.assertEqual(self.logged(),
                         ["gsettings get org.gnome.shell enabled-extensions"])

    def test_the_tools_are_shell_scripts_sh_itself_accepts(self):
        for name in support.FAKE_GNOME_TOOLS:
            path = os.path.join(self.bin, name)
            self.assertTrue(os.access(path, os.X_OK), path)
            r = subprocess.run(["sh", "-n", path], capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(r.returncode, 0, (name, r.stderr))

    def test_a_subset_leaves_the_real_tools_alone(self):
        d = support.fake_gnome_bin(os.path.join(self.tmp, "few"), self.state,
                                   ["gsettings"])
        self.assertEqual(sorted(n for n in os.listdir(d) if not n.startswith("_")),
                         ["gsettings"])


@support.skip_without_node
class NodeHarness(unittest.TestCase):
    """`js_harness()` -- the loader that lets node run the two extensions.

    Neither extension.js had ever been executed by anything but a GNOME
    session: they import `gi://Meta` and
    `resource:///org/gnome/shell/ui/main.js`, which node refuses before
    reading a line.  Proven on node v22.22.1 on this host."""

    def test_a_case_gets_its_argument_and_its_answer_comes_back_parsed(self):
        got = support.js_harness(support.BRIDGE_EXT, """
            const input = JSON.parse(process.argv[2]);
            console.log(JSON.stringify({doubled: input.n * 2, kind: typeof input}));
        """, {"n": 21})
        self.assertEqual(got, {"doubled": 42, "kind": "object"})

    def test_the_four_path_tokens_are_substituted(self):
        got = support.js_harness(support.OVERLAP_EXT, """
            console.log(JSON.stringify({module: '@MODULE@', stubs: '@STUBS@',
                                        gjs: '@GJS@', root: '@ROOT@'}));
        """)
        self.assertEqual(got, {"module": support.OVERLAP_EXT, "stubs": support.GJS_STUBS,
                               "gjs": support.GJS_DIR, "root": ROOT})

    def test_the_shipped_bridge_evaluates_and_constructs(self):
        """The shipped file, at its own path -- not a copy with the imports
        cut out, which is what the older static tests had to do."""
        got = support.js_harness(support.BRIDGE_EXT, """
            import Ext from '@MODULE@/extension.js';
            const e = new Ext({uuid: 'fuckwayland-bridge@fuckwayland'});
            console.log(JSON.stringify({
                name: Ext.name,
                methods: Object.getOwnPropertyNames(Object.getPrototypeOf(e)),
            }));
        """)
        self.assertEqual(got["name"], "FuckwaylandBridge")
        for m in ("enable", "disable", "_setState", "_windowInfo", "_allWindows"):
            self.assertIn(m, got["methods"])

    def test_the_shipped_overlap_evaluates_and_keeps_the_path_it_was_given(self):
        """`this.path` is where generations.json and typelib/ are read from, so
        an extension constructed with the real directory reads the real table."""
        got = support.js_harness(support.OVERLAP_EXT, """
            import Ext from '@MODULE@/extension.js';
            const e = new Ext({uuid: 'fuckwayland-overlap@fuckwayland', path: '@MODULE@'});
            console.log(JSON.stringify({name: Ext.name, path: e.path,
                methods: Object.getOwnPropertyNames(Object.getPrototypeOf(e))}));
        """)
        self.assertEqual(got["name"], "FwOverlap")
        self.assertEqual(got["path"], support.OVERLAP_EXT)
        for m in ("Probe", "ApplyOverlap", "Version", "_apply"):
            self.assertIn(m, got["methods"])

    def test_a_case_that_throws_carries_the_stack_into_the_failure(self):
        """A JS exception with no stack is an afternoon; the harness must not
        swallow node's stderr."""
        with self.assertRaises(AssertionError) as cm:
            support.js_harness(support.BRIDGE_EXT,
                               "throw new Error('deliberate: the case broke');")
        self.assertIn("deliberate: the case broke", str(cm.exception))
        self.assertIn("node exited", str(cm.exception))

    def test_a_case_that_prints_nothing_says_so_rather_than_raising_valueerror(self):
        with self.assertRaises(AssertionError) as cm:
            support.js_harness(support.BRIDGE_EXT, "console.error('not json');")
        self.assertIn("printed no JSON", str(cm.exception))
        self.assertIn("not json", str(cm.exception))


@support.skip_without_node
class GjsStubs(unittest.TestCase):
    """tests/fixtures/gjs/stubs -- recording doubles, not a swallowing Proxy.

    Each one is configured through the same specifier the extension imports,
    so `import Meta from 'gi://Meta'` in a case is the object the extension
    got, and what it asked for is readable afterwards."""

    def case(self, body, arg=None, module=None):
        return support.js_harness(module or support.BRIDGE_EXT, body, arg)

    def test_a_window_double_answers_a_listwindows_row_back(self):
        """The row is the bridge's own JSON shape (test_backend_gnome
        .fixture_windows()): the xterm, XWayland, focused, at 100,80 640x480."""
        got = self.case("""
            import Meta from 'gi://Meta';
            const row = JSON.parse(process.argv[2]);
            const w = Meta.makeWindow(row);
            console.log(JSON.stringify({
                title: w.get_title(), cls: w.get_wm_class(), pid: w.get_pid(),
                type: w.get_window_type(), rect: w.get_frame_rect(),
                client: w.get_client_type(), desc: w.get_description(),
                focus: w.has_focus(), ws: w.get_workspace().index(),
            }));
        """, {"id": 1, "title": "test@vm: ~", "wm_class": "XTerm", "pid": 1201,
              "window_type": "NORMAL", "x": 100, "y": 80, "width": 640, "height": 480,
              "client_type": "x11", "xid": 0x400005, "focused": True, "workspace": 0,
              "maximized_h": False, "maximized_v": False})
        self.assertEqual(got["title"], "test@vm: ~")
        self.assertEqual(got["cls"], "XTerm")
        self.assertEqual(got["pid"], 1201)
        self.assertEqual(got["type"], 0)          # Meta.WindowType.NORMAL
        self.assertEqual(got["rect"], {"x": 100, "y": 80, "width": 640, "height": 480})
        self.assertEqual(got["client"], 1)        # WindowClientType.X11
        self.assertTrue(got["desc"].startswith("0x400005 "))
        self.assertTrue(got["focus"])
        self.assertEqual(got["ws"], 0)

    def test_a_row_with_no_workspace_at_all_is_workspace_zero(self):
        """`fixture_windows()` rows carry `workspace`, but the hostile and
        minimal rows a case writes by hand do not, and a Workspace double whose
        index() answered `undefined` came back out of the bridge as JSON null
        where MockBridge answers a number.  Only a negative workspace is "no
        workspace" -- what a dock, or a window being unmanaged, has."""
        got = self.case("""
            import Meta from 'gi://Meta';
            const base = {id: 7, title: 't', wm_class: 'c', window_type: 'NORMAL',
                          x: 0, y: 0, width: 10, height: 10, client_type: 'wayland'};
            const none = Meta.makeWindow(base).get_workspace();
            const neg = Meta.makeWindow({...base, workspace: -1}).get_workspace();
            const two = Meta.makeWindow({...base, workspace: 2}).get_workspace();
            console.log(JSON.stringify({
                none: none === null ? null : none.index(),
                neg: neg === null ? null : neg.index(),
                two: two === null ? null : two.index(),
            }));
        """)
        self.assertEqual(got, {"none": 0, "neg": None, "two": 2})

    def test_the_enum_numbers_are_the_ones_the_typelibs_carry(self):
        """Both extensions read these symbolically today, so no product code
        notices a wrong number -- but a case that asserts what Main.pushModal()
        was handed, or that builds an event by type number, compares against
        them, and a guessed number would make that case agree with nothing.
        Read out of the shipped typelibs here on 2026-09-08: Shell-14.typelib
        (gnome-shell 46.0-0ubuntu5, noble) and Shell-18.typelib (50.1-0ubuntu1.2,
        resolute) both put ActionMode.POPUP at 128 and SYSTEM_MODAL at 32;
        Clutter-14/-18 (gir1.2-mutter-14 46.0-1ubuntu9, gir1.2-mutter-18
        50.1-0ubuntu2.2) put BUTTON_PRESS at 6 and BUTTON_RELEASE at 7, because
        ENTER and LEAVE sit between MOTION and the buttons; Meta-14 has
        Cursor.CROSSHAIR 17 (Meta-18 has no Cursor enum at all -- mutter 18
        moved those names to Clutter.CursorType, where CROSSHAIR is 9); and
        this host's own GLib answers FileTest.EXISTS 16, IS_REGULAR 1."""
        got = self.case("""
            import Shell from 'gi://Shell';
            import Clutter from 'gi://Clutter';
            import Meta from 'gi://Meta';
            import GLib from 'gi://GLib';
            console.log(JSON.stringify({
                popup: Shell.ActionMode.POPUP,
                system_modal: Shell.ActionMode.SYSTEM_MODAL,
                normal: Shell.ActionMode.NORMAL,
                press: Clutter.EventType.BUTTON_PRESS,
                release: Clutter.EventType.BUTTON_RELEASE,
                touch_begin: Clutter.EventType.TOUCH_BEGIN,
                pad_press: Clutter.EventType.PAD_BUTTON_PRESS,
                crosshair: Meta.Cursor.CROSSHAIR,
                cursor_default: Meta.Cursor.DEFAULT,
                css_crosshair: Clutter.CursorType.CROSSHAIR,
                exists: GLib.FileTest.EXISTS,
                is_regular: GLib.FileTest.IS_REGULAR,
                sha256: GLib.ChecksumType.SHA256,
            }));
        """)
        self.assertEqual(got, {"popup": 128, "system_modal": 32, "normal": 1,
                               "press": 6, "release": 7, "touch_begin": 9,
                               "pad_press": 18, "crosshair": 17, "cursor_default": 1,
                               "css_crosshair": 9, "exists": 16, "is_regular": 1,
                               "sha256": 2})

    def test_the_window_shapes_are_the_three_mutter_api_generations(self):
        """46-48 take maximize(flags); 49+ moved the directions to
        set_maximize_flags() and maximize() there takes no argument at all;
        a window with neither boolean property still answers through
        get_maximize_flags().  Which one is in front of the bridge is the
        compositor-version axis, and it is `shape` here."""
        got = self.case("""
            import Meta from 'gi://Meta';
            const row = {id: 5, title: 't', wm_class: 'c', window_type: 'NORMAL',
                         x: 0, y: 0, width: 10, height: 10, client_type: 'wayland',
                         maximized_h: false, maximized_v: false};
            const out = {};
            for (const shape of ['46', '49', 'flags', 'hostile']) {
                const w = Meta.makeWindow(row, {shape});
                out[shape] = {
                    props: 'maximized_horizontally' in w,
                    setters: typeof w.set_maximize_flags === 'function',
                    flags: typeof w.get_maximize_flags === 'function',
                    throws: (() => { try { w.get_title(); return false; }
                                     catch (e) { return true; } })(),
                };
            }
            console.log(JSON.stringify(out));
        """)
        self.assertEqual(got["46"], {"props": True, "setters": False, "flags": False,
                                     "throws": False})
        self.assertEqual(got["49"], {"props": True, "setters": True, "flags": True,
                                     "throws": False})
        self.assertEqual(got["flags"], {"props": False, "setters": False, "flags": True,
                                        "throws": False})
        self.assertTrue(got["hostile"]["throws"])

    def test_the_window_double_records_every_call_in_order(self):
        got = self.case("""
            import Meta from 'gi://Meta';
            import * as H from '@STUBS@/harness.mjs';
            const w = Meta.makeWindow({id: 9, window_type: 'NORMAL', x: 0, y: 0,
                width: 1, height: 1, client_type: 'wayland',
                maximized_h: false, maximized_v: true});
            w.get_title();
            w.maximize(1);
            const before = w.minimized;
            console.log(JSON.stringify({names: H.names(),
                                        state: [w.state.h, w.state.v], before}));
        """)
        self.assertEqual(got["names"], ["w9.get_title", "w9.maximize", "w9.get:minimized"])
        self.assertEqual(got["state"], [True, True])
        self.assertFalse(got["before"])

    def test_gobject_type_query_answers_the_size_a_case_registers(self):
        """72 bytes for MetaMonitorsConfig is what GNOME 46.0's registry
        reports on noble-gnome (libmutter-14, build 9e23feb34618); 80 on 50
        and 51.  An unregistered type is a type_from_name of 0, which is the
        `MetaMonitorsConfig is not a registered GType` refusal."""
        got = self.case("""
            import GObject from 'gi://GObject';
            GObject.setType('MetaMonitorsConfig', 72);
            const t = GObject.type_from_name('MetaMonitorsConfig');
            const n = GObject.type_query(t).instance_size;
            const missing = GObject.type_from_name('MetaNotAType');
            console.log(JSON.stringify({n, missing}));
        """)
        self.assertEqual(got, {"n": 72, "missing": 0})

    def test_the_shell_version_and_the_modal_count_are_live_bindings(self):
        """Both are read through a namespace import in the extensions, so a
        setter is enough -- no module has to be reloaded between cases."""
        got = self.case("""
            import * as Config from 'resource:///org/gnome/shell/misc/config.js';
            import * as Main from 'resource:///org/gnome/shell/ui/main.js';
            const before = [Config.PACKAGE_VERSION, Main.modalCount];
            Config.setPackageVersion('51.beta');
            Main.setModalCount(1);
            console.log(JSON.stringify({before,
                after: [Config.PACKAGE_VERSION, Main.modalCount]}));
        """)
        self.assertEqual(got["before"], ["50.1", 0])
        self.assertEqual(got["after"], ["51.beta", 1])

    def test_girepository_spellings_can_be_taken_away_one_at_a_time(self):
        """`get_shared_library` is glib <= 2.84, `get_shared_libraries` after
        it, and each exists both on the repository and as a static.  The
        overlap extension asks all four because it must not care; the double
        has to be able to answer as any one of those glibs."""
        got = self.case("""
            import GIRepository from 'gi://GIRepository';
            const out = {};
            GIRepository.configure({sharedLibraries: {FwOverlap14: ['libmutter-14.so.0']}});
            const repo = GIRepository.Repository.dup_default();
            out.modern = [typeof repo.get_shared_libraries, typeof repo.get_shared_library,
                          repo.get_shared_libraries('FwOverlap14')];
            GIRepository.configure({spelling: {instanceSharedLibraries: 'missing',
                                               instanceSharedLibrary: 'ok'}});
            const old = GIRepository.Repository.dup_default();
            out.old = [typeof old.get_shared_libraries, old.get_shared_library('FwOverlap14')];
            GIRepository.configure({spelling: {instanceSharedLibrary: 'throw'}});
            const hostile = GIRepository.Repository.dup_default();
            out.hostile = (() => { try { hostile.get_shared_library('FwOverlap14');
                                         return 'answered'; }
                                   catch (e) { return 'threw'; } })();
            console.log(JSON.stringify(out));
        """)
        self.assertEqual(got["modern"], ["function", "undefined", ["libmutter-14.so.0"]])
        self.assertEqual(got["old"], ["undefined", "libmutter-14.so.0"])
        self.assertEqual(got["hostile"], "threw")

    def test_glib_reads_the_files_a_case_puts_there_and_hashes_them_for_real(self):
        """The digest is the overlap extension's whole "did monitors.xml move
        under us" check, so a counter for a hash would make that comparison
        meaningless.  sha256 of "x" is 2d711642...b6e2fd4f."""
        got = self.case("""
            import GLib from 'gi://GLib';
            GLib.setFile('/proc/self/maps', 'abc');
            const [ok, bytes] = GLib.file_get_contents('/proc/self/maps');
            const [gone] = GLib.file_get_contents('/nowhere');
            console.log(JSON.stringify({
                ok, text: new TextDecoder().decode(bytes), gone,
                exists: GLib.file_test('/proc/self/maps', GLib.FileTest.EXISTS),
                sha: GLib.compute_checksum_for_data(GLib.ChecksumType.SHA256,
                                                    new TextEncoder().encode('x')),
                joined: GLib.build_filenamev(['a', 'b', 'c']),
            }));
        """)
        self.assertTrue(got["ok"])
        self.assertEqual(got["text"], "abc")
        self.assertFalse(got["gone"])
        self.assertTrue(got["exists"])
        self.assertEqual(
            got["sha"],
            "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881")
        self.assertEqual(got["joined"], "a/b/c")

    def test_the_overlap_extension_runs_far_enough_to_refuse_an_unknown_shell(self):
        """End to end through the stubs, and the answer is checked against the
        rig: on GNOME 46.0 the table says libmutter-14.so.0, FwOverlap14 and
        MetaMonitorsConfig 72 bytes, and a shell major nobody measured is the
        one refusal `--unsafe-gnome-overlap-unmeasured` can get past."""
        got = self.case("""
            import fs from 'node:fs';
            import GLib from 'gi://GLib';
            import GObject from 'gi://GObject';
            import * as Config from 'resource:///org/gnome/shell/misc/config.js';
            const Ext = (await import('@MODULE@/extension.js')).default;
            GLib.setFile('@MODULE@/generations.json',
                         fs.readFileSync('@MODULE@/generations.json', 'utf8'));
            GLib.setFile('/proc/self/maps',
                '55d0c0000000-55d0c0100000 r--p 0 00:1b 1 /usr/bin/gnome-shell\\n' +
                '7f1200000000-7f1200400000 r-xp 0 00:1b 2 /usr/lib/libmutter-14.so.0\\n');
            GObject.setType('MetaMonitorsConfig', 72);
            Config.setPackageVersion('52.0');
            const e = new Ext({uuid: 'u', path: '@MODULE@'});
            console.log(e.Probe(JSON.stringify({})));
        """, module=support.OVERLAP_EXT)
        self.assertFalse(got["ok"])
        self.assertEqual(got["check"], "shell-version")
        self.assertTrue(got["forceable"])
        self.assertEqual(got["checks"], [])
        self.assertEqual(got["found"]["instance_size"], 72)
        self.assertEqual(got["found"]["sonames"], ["libmutter-14.so.0"])

    def test_the_lib_double_carries_every_symbol_the_extension_demands(self):
        """The seventeen entry points the `symbols` check refuses without --
        the list is the extension's own, read back out of the shipped file so
        the double cannot drift from it."""
        got = self.case("""
            import {makeLib, LIB_SYMBOLS} from '@STUBS@/gjs-globals.mjs';
            import * as H from '@STUBS@/harness.mjs';
            const lib = makeLib({impl: {type_name: () => 'MetaMonitorsConfig'},
                                 drop: ['wr']});
            const name = lib.type_name(4096);
            lib.strn(0, 0);
            console.log(JSON.stringify({symbols: LIB_SYMBOLS, name,
                dropped: typeof lib.wr, calls: H.calls}));
        """)
        with open(os.path.join(support.OVERLAP_EXT, "extension.js"), encoding="utf-8") as fh:
            src = fh.read()
        block = src[src.index("for (const fn of ["):src.index("'verify', 'apply', 'wr']")]
        declared = re.findall(r"'(\w+)'", block + "'verify', 'apply', 'wr'")
        self.assertEqual(got["symbols"], declared)
        self.assertEqual(got["name"], "MetaMonitorsConfig")
        self.assertEqual(got["dropped"], "undefined")
        self.assertEqual([c["what"] for c in got["calls"]], ["lib.type_name", "lib.strn"])
        self.assertEqual(got["calls"][1]["args"], [0, 0])


class SwayDouble(unittest.TestCase):
    """`FakeSway` -- one sway IPC socket, six ways of behaving badly.

    Driven by wxrandr's own SwayIPC client rather than by a hand-rolled
    reader, so what is proved is that a real client sees what the mode
    promises."""

    def sway(self, mode, **kw):
        srv = support.FakeSway(mode, **kw)
        self.addCleanup(srv.close)
        return srv

    def client(self, srv):
        c = SwayIPC(sockpath=srv.path)
        self.addCleanup(c.close)
        return c

    def test_ok_answers_get_outputs_as_sway_1_11_does(self):
        """The two headless outputs a `swaymsg create_output` session has on
        this host: HEADLESS-1 at 0,0 1280x720 and HEADLESS-2 beside it."""
        c = self.client(self.sway("ok"))
        outs = c.get_outputs()
        self.assertEqual([o["name"] for o in outs], ["HEADLESS-1", "HEADLESS-2"])
        self.assertEqual(outs[1]["rect"], {"x": 1280, "y": 0, "width": 1280,
                                           "height": 1024})
        self.assertEqual(c.run("output HEADLESS-2 pos 1280 0"), [{"success": True}])

    def test_refuse_is_the_error_sway_gives_for_a_layout_it_will_not_take(self):
        srv = self.sway("refuse")
        c = self.client(srv)
        with self.assertRaises(Fatal) as cm:
            c.run("output HEADLESS-2 pos 640 0")
        self.assertIn("Cannot apply output configuration", str(cm.exception))
        self.assertEqual(srv.requests, [(support.RUN_COMMAND,
                                         "output HEADLESS-2 pos 640 0")])

    def test_partial_leaves_the_rect_out_entirely(self):
        outs = self.client(self.sway("partial")).get_outputs()
        self.assertEqual(len(outs), 2)
        for o in outs:
            self.assertNotIn("rect", o)
            self.assertIn("name", o)

    def test_badjson_is_a_well_framed_reply_with_a_payload_that_is_not_json(self):
        """The framing has to be right, or what is being tested is the frame
        reader and not the parser above it."""
        with self.assertRaises(json.JSONDecodeError):
            self.client(self.sway("badjson")).get_outputs()

    def test_gone_answers_once_and_then_the_connection_is_closed(self):
        """A peer that has gone gives ECONNRESET on the read and EPIPE on the
        next write -- not a clean EOF -- which is what made this the failure
        that used to reach the user as a traceback.  Either shape counts here:
        which one a client sees depends on whether its own read was already
        posted when the socket went."""
        c = self.client(self.sway("gone"))
        self.assertEqual([o["name"] for o in c.get_outputs()],
                         ["HEADLESS-1", "HEADLESS-2"])
        with self.assertRaises((ConnectionResetError, BrokenPipeError, Fatal)):
            for _ in range(3):
                c.get_outputs()

    def test_wedged_accepts_the_connection_and_never_answers(self):
        """The kernel accepts for a compositor stuck in its own event loop, so
        connecting proves nothing and only the client's deadline ends it."""
        c = self.client(self.sway("wedged"))
        c.sock.settimeout(0.4)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            c.get_outputs()
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_close_ends_the_wedged_handler_instead_of_leaking_it(self):
        """A wedged handler has nothing to answer, so it sleeps -- on the
        server's own stop event, not on the clock.  Sleeping on the clock is
        what the double did where it was lifted from: close() shuts the
        sockets but a `while True: sleep(0.5)` thread lives to the end of the
        interpreter, and T32 opens several wedged sockets per run.  The accept
        loop is counted here too: closing a listening socket does not wake a
        thread blocked in accept() on Linux, so it polls on the same event."""
        base = threading.active_count()
        srv = support.FakeSway("wedged")
        c = SwayIPC(sockpath=srv.path)
        c.sock.settimeout(0.2)
        with self.assertRaises(TimeoutError):
            c.get_outputs()
        for _ in range(200):                       # the handler thread may not have started yet under load
            if threading.active_count() > base:
                break
            time.sleep(0.01)
        self.assertGreater(threading.active_count(), base, "no handler thread ran")
        c.close()
        srv.close()
        for _ in range(200):                       # 2 s, generous for a wakeup
            if threading.active_count() <= base:
                break
            time.sleep(0.01)
        self.assertLessEqual(threading.active_count(), base,
                             "the wedged handler outlived its server")

    def test_the_outputs_can_be_replaced_wholesale(self):
        outs = self.client(self.sway("ok", outputs=[{"name": "Virtual-1"}])).get_outputs()
        self.assertEqual(outs, [{"name": "Virtual-1"}])

    def test_it_records_what_was_asked_of_it(self):
        srv = self.sway("ok")
        c = self.client(srv)
        c.get_outputs()
        c.run("output HEADLESS-1 disable")
        self.assertEqual(srv.requests, [(support.GET_OUTPUTS, ""),
                                        (support.RUN_COMMAND, "output HEADLESS-1 disable")])


class SessionLeader(unittest.TestCase):
    """`leader_process` -- a real process with a session leader's name and a
    session leader's environment, which is the only way to test the /proc walk
    in fwcommon.session."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leader-check-")
        self.addCleanup(_rmtree, self.tmp)

    def test_the_stand_in_reaches_the_name_we_look_for(self):
        """`startplasma-x11` is exactly the 15 characters the kernel keeps
        (TASK_COMM_LEN is 16 with the NUL), so it arrives whole."""
        with support.leader_process("startplasma-x11", {"DISPLAY": ":7"}) as leader:
            self.assertEqual(leader.comm(), "startplasma-x11")

    def test_a_longer_name_is_truncated_the_way_the_kernel_truncates_it(self):
        """The other half of the same fact, and the one that can be wrong:
        the walk in fwcommon.session matches on /proc/<pid>/comm, which the
        kernel cuts at 15 characters.  `gnome-shell-calendar-server` -- a real
        name on every GNOME session here -- reaches /proc as
        `gnome-shell-cal`, so the helper must not wait for more than that."""
        name = "gnome-shell-calendar-server"
        with support.leader_process(name, {}) as leader:
            self.assertEqual(leader.comm(), "gnome-shell-cal")
            self.assertEqual(len(leader.comm()), 15)

    def test_the_environment_is_readable_out_of_proc(self):
        """/proc/<pid>/environ is the environment at execve, which is exactly
        what session.find_x_display reads."""
        cookie = "/tmp/xauth_sddm_test"
        with support.leader_process("kwin_wayland",
                                    {"DISPLAY": ":1", "XAUTHORITY": cookie}) as leader:
            with open("/proc/%d/environ" % leader.pid, "rb") as fh:
                env = fh.read().decode().split("\0")
            self.assertIn("DISPLAY=:1", env)
            self.assertIn("XAUTHORITY=" + cookie, env)

    def test_an_empty_environment_is_an_empty_environment(self):
        """The Plasma Wayland ranking turns on this: kwin_wayland is started
        with nothing in it, and a helper that leaked the runner's DISPLAY in
        would make the test that ranks it pass for the wrong reason."""
        with support.leader_process("kwin_wayland", {}) as leader:
            with open("/proc/%d/environ" % leader.pid, "rb") as fh:
                raw = fh.read()
            self.assertEqual(raw.strip(b"\0"), b"")

    @unittest.skipUnless(_SLEEP_IS_MULTICALL,
                         "this host's sleep is not the uutils multicall binary")
    def test_a_copy_of_sleep_is_not_a_stand_in_on_this_distribution(self):
        """Ubuntu 26.04 ships uutils coreutils as ONE multicall binary
        (/usr/bin/sleep resolves to /usr/lib/cargo/bin/coreutils here) which
        dispatches on argv[0]: a copy named `startplasma-x11` exits 1 with
        "coreutils: unknown program 'startplasma-x11'".  It still leaves a
        corpse whose /proc/<pid>/comm reads the name, so a helper built on
        that copy looks like it worked and hands out a pid with an empty
        environ -- which is why leader_process is a blocked /bin/sh and waits
        for the environment too.  (Measured here: tests/test_backend_gnome.py's
        own copy of the sleep stand-in fails when that file is run on its own,
        `find_xauthority` answering None.)  The skip guard is the precondition,
        not a way out: on a GNU-coreutils host this name is not a true claim
        and there is nothing here to assert."""
        copy = os.path.join(self.tmp, "startplasma-x11")
        shutil.copy(_SLEEP_REAL, copy)
        r = subprocess.run([copy, "0.1"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("coreutils: unknown program", r.stderr)
        self.assertIn("startplasma-x11", r.stderr)

    def test_the_stand_in_is_alive_and_carries_its_environment(self):
        """What the copy of sleep above could not give: a process still
        running when the caller gets the pid, with the variables readable."""
        with support.leader_process("startplasma-x11", {"DISPLAY": ":7"}) as leader:
            self.assertIsNone(leader.proc.poll(), "the stand-in has to be alive")
            self.assertEqual(leader.environ()["DISPLAY"], ":7")
            self.assertEqual(leader.comm(), "startplasma-x11")

    def test_stop_leaves_nothing_running_and_nothing_behind(self):
        leader = support.leader_process("gnome-shell", {})
        path = leader.path
        leader.stop()
        self.assertFalse(os.path.exists(path))
        for _ in range(250):
            if leader.comm() is None:
                break
            time.sleep(0.02)
        self.assertIsNone(leader.comm())


class WlMirrorStub(unittest.TestCase):
    """`WL_MIRROR_STUB` -- wl-mirror's stand-in, so wmirror's supervisor can
    be tested with no compositor and no GPU."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wl-mirror-stub-")
        self.addCleanup(_rmtree, self.tmp)
        self.log = os.path.join(self.tmp, "log")
        self.stub = support.write_wl_mirror_stub(self.tmp)

    def run_stub(self, *argv, **env):
        e = dict(os.environ, WMIRROR_STUB_LOG=self.log, WMIRROR_STUB_LIFE="0.1",
                 WAYLAND_DISPLAY="wayland-9")
        e.update(env)
        return subprocess.run([self.stub, *argv], capture_output=True, text=True,
                              env=e, timeout=30)

    def test_it_logs_its_arguments_and_the_display_it_saw(self):
        """Which WAYLAND_DISPLAY the helper was started with is the thing
        wmirror gets wrong when it forks: the log is how that is read back."""
        r = self.run_stub("--fullscreen", "HEADLESS-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.log) as fh:
            self.assertEqual(fh.read().strip(),
                             "--fullscreen HEADLESS-1 wayland=wayland-9")

    def test_the_libegl_warning_is_on_stderr_of_a_run_that_succeeded(self):
        """Real wl-mirror prints it on a headless box, and wmirror must not
        read a warning as a failure."""
        r = self.run_stub("HEADLESS-1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("libEGL warning", r.stderr)

    def test_the_failure_knob_is_wl_mirrors_own_wording(self):
        r = self.run_stub("NOPE", WMIRROR_STUB_FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("options::find_output(): output NOPE not found", r.stderr)

    def test_it_stays_up_for_the_life_it_is_given(self):
        started = time.monotonic()
        self.run_stub("HEADLESS-1", WMIRROR_STUB_LIFE="0.5")
        self.assertGreaterEqual(time.monotonic() - started, 0.45)

    def test_it_is_the_same_text_test_wmirror_lifetime_runs(self):
        """The stub moved here; a second copy that drifted would be two
        different wl-mirrors under one name.  Either that file still carries
        its own copy, in which case the two texts have to be equal, or T52's
        owner has replaced it with the one here, in which case the file has to
        say so -- there is no third state where nothing is checked."""
        with open(os.path.join(ROOT, "tests", "test_wmirror_lifetime.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r'^STUB = """(.*?)"""$', src, re.S | re.M)
        if m is not None:
            self.assertEqual(eval('"""%s"""' % m.group(1)), support.WL_MIRROR_STUB)
            return
        self.assertRegex(src, r"support\.(WL_MIRROR_STUB|write_wl_mirror_stub)",
                         "the local copy is gone and support's is not used")


if __name__ == "__main__":
    unittest.main()
