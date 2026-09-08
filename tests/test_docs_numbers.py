#!/usr/bin/env python3
"""Every number the documents state, checked against the thing it counts.

The documents in this repository are unusually specific -- "2668 tests", "all
48 commands", "six checks", "a quarter of an hour" -- and specificity is only
worth anything while it is true.  Each of those is a count of something this
suite can produce, so each of them is produced here and compared, rather than
read by a human every few months.

What that caught at the time this file was written:

* four documents said 2668 tests; `unittest.defaultTestLoader.discover` said
  2675.  Nothing had gone wrong -- tests were added and the sentence was not --
  which is exactly why a person re-reading the documents never notices;
* README said the input daemon goes away "at once when its socket goes".  It
  does not: `wdotool/daemon.py` checks the socket once per `_CHECK_SECONDS`
  (15.0) from the accept loop, and only while no client is connected, so the
  honest bound is fifteen seconds;
* docs/Technical.md said `vm/selftest.sh` asserts "the right display manager".
  It asserts logind's session `Type`, `XDG_SESSION_TYPE`, autologin, the heads
  and that the compositor is painting.  There is no display-manager check in
  the script and there never was.

The test-count assertion is the one that goes red for an honest reason: adding
a test is what makes it fail, and the fix is to write the new number in the
places `THE_COUNT_LIVES_IN` names.  It is cheap to fix and it is the only thing
that keeps the number meaning anything.
"""

import os
import re
import subprocess
import sys
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` is a bare import: it resolves only with the tests directory
# on sys.path, which `python3 tests/<file>.py` gives for free and
# `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                    # noqa: E402
from fwcommon import VERSION                                      # noqa: E402

#: Where the collected test count is written down.  Four documents, and the
#: number is the same number in all four.
THE_COUNT_LIVES_IN = ("README.md", "CHANGELOG.md", "docs/Technical.md",
                      "docs/Blogpost.md")

FOUR_DIGIT_TESTS = re.compile(r"\b(\d{4}) tests\b")
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                "twelve": 12}
N_CHECKS = re.compile(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|"
                      r"eleven|twelve|\d+) checks\b", re.I)
#: The documents that count the overlap extension's checks, and the words that
#: say a paragraph is about it.  Both halves are needed: the count belongs to
#: one feature, and "three checks" written one day about the daemon, the
#: installer or the rig is not this number being wrong.
CHECK_DOCS = ("README.md", "docs/Technical.md", "docs/WXRANDR.md", "gnome/README.md")
ABOUT_OVERLAP = ("overlap", "libmutter", "Probe(", "--dryrun")
#: A markdown section: its heading line and everything up to the next heading.
SECTION = re.compile(r"^#{1,6} .*$", re.M)

OVERLAP_JS = os.path.join(ROOT, "gnome", "fuckwayland-overlap@fuckwayland",
                          "extension.js")


def collected():
    """What the loader collects, counted in a subprocess.

    A subprocess because the loader imports every test module in the suite,
    including this one, and doing that inside a running test would re-enter
    modules that have already registered cleanups and spawned nothing.  Loader
    errors are counted too: a module that fails to import becomes a synthetic
    `_FailedTest`, so a broken file would otherwise be *one more test* and the
    count would look healthy."""
    src = ("import unittest\n"
           "l = unittest.defaultTestLoader\n"
           "s = l.discover('tests', top_level_dir='tests')\n"
           "print('COUNT', s.countTestCases(), len(l.errors))\n")
    got = subprocess.run([sys.executable, "-c", src], cwd=ROOT, text=True,
                         capture_output=True, timeout=900)
    line = [ln for ln in got.stdout.splitlines() if ln.startswith("COUNT ")]
    if not line:
        raise AssertionError("discover printed no count:\n%s\n%s"
                             % (got.stdout[-2000:], got.stderr[-2000:]))
    _, count, errors = line[0].split()
    return int(count), int(errors)


class TheTestCount(unittest.TestCase):
    """One number, four documents, and a loader that knows the answer."""

    @classmethod
    def setUpClass(cls):
        cls.count, cls.errors = collected()
        cls.docs = support.documents()

    def test_nothing_in_the_suite_fails_to_import(self):
        """The premise of the count.  A module that raises on import is
        collected as one `_FailedTest`, so a suite that has lost a whole file
        counts *higher* than it should, not lower."""
        self.assertEqual(self.errors, 0)

    def test_every_document_states_the_collected_count(self):
        """The number is `unittest.defaultTestLoader.discover` and nothing
        else: not what a run reports (skips and expected failures are tests
        that were collected), not what pytest says."""
        said = {}
        for name in THE_COUNT_LIVES_IN:
            for hit in FOUR_DIGIT_TESTS.findall(self.docs[name]):
                said.setdefault(int(hit), []).append(name)
        self.assertTrue(said, "no document states a test count any more")
        self.assertIn(self.count, said,
                      "the suite collects %d tests; the documents say %s. "
                      "Run `python3 -c \"import unittest; "
                      "print(unittest.defaultTestLoader.discover('tests', "
                      "top_level_dir='tests').countTestCases())\"` and write "
                      "that number in %s"
                      % (self.count, sorted(said), ", ".join(THE_COUNT_LIVES_IN)))

    def test_no_document_states_a_different_count(self):
        """A stanza updated in one file and not another is the failure this
        catches.  Only the current release is read: README's `## Releases`
        section carries one `<!-- release-notes: N -->` block per release and
        the CHANGELOG one stanza per release, and every count below the current
        one is history that is deliberately left where it is.  For README that
        means everything except the 0.3-and-older blocks, which run from their
        marker to `## Testing` -- and the count in `## Testing` itself, which
        comes after them, is a current one."""
        for name in THE_COUNT_LIVES_IN:
            text = self.docs[name]
            if name == "CHANGELOG.md":
                text = text.split("\n## ")[1] if "\n## " in text else text
            elif name == "README.md":
                old = text.index("<!-- release-notes: 0.3 -->")
                text = text[:old] + text[text.index("\n## Testing"):]
            with self.subTest(name):
                said = {int(h) for h in FOUR_DIGIT_TESTS.findall(text)}
                self.assertIn(self.count, said or {self.count}, (name, sorted(said)))
                self.assertEqual(said - {self.count}, set(), name)

    def test_the_changelog_says_what_it_grew_from(self):
        """`up from N` in one stanza is the `N tests` of the stanza below it,
        so the release notes form a chain that has to add up."""
        stanzas = self.docs["CHANGELOG.md"].split("\n## ")[1:]
        counts, ups = [], []
        for stanza in stanzas:
            found = FOUR_DIGIT_TESTS.findall(stanza)
            up = re.findall(r"tests\*\*, up from (\d{4})", stanza)
            if found:
                counts.append(int(found[0]))
                ups.append(int(up[0]) if up else None)
        self.assertGreaterEqual(len(counts), 2, counts)
        for i, up in enumerate(ups):
            if up is None or i + 1 >= len(counts):
                continue
            with self.subTest(counts[i]):
                self.assertEqual(up, counts[i + 1],
                                 "the %d-test stanza says it grew from %d, and "
                                 "the stanza below it is %d"
                                 % (counts[i], up, counts[i + 1]))


class TheDaemonsTwoTimeouts(unittest.TestCase):
    """The two numbers that decide when a forked daemon disappears."""

    @classmethod
    def setUpClass(cls):
        from wdotool import daemon
        cls.daemon = daemon
        cls.docs = support.documents()

    def test_the_constants_are_what_the_prose_says(self):
        self.assertEqual(self.daemon._CHECK_SECONDS, 15.0)
        self.assertEqual(self.daemon._IDLE_SECONDS, 900.0)

    def test_no_document_says_the_daemon_ends_at_once(self):
        """Finding F7.3.  A socket that has gone is noticed by the accept
        loop's timeout, not by an event: the check runs once per
        `_CHECK_SECONDS` and is skipped entirely while a client is connected.
        "at once" made a logout sound instantaneous and made
        `test_daemon_lifetime`'s fifteen-second waits look like slack."""
        for name, text in self.docs.items():
            with self.subTest(name):
                self.assertNotIn("at once when its socket goes", text)

    def test_the_fifteen_seconds_is_written_down_somewhere(self):
        """The bound has to be findable by somebody who is waiting for a
        daemon to go: the README sentence, docs/WDOTOOL.md's daemon section, or
        the CHANGELOG entry that introduced it."""
        spellings = ("fifteen seconds", "15 s", "15s", "t+15s", "15 seconds")
        found = [n for n, t in self.docs.items()
                 if any(s in t for s in spellings)]
        self.assertTrue(found, "the 15 s socket check is documented nowhere")
        self.assertIn("README.md", found)

    def test_the_quarter_of_an_hour_is_written_down_too(self):
        spellings = ("quarter of an hour", "fifteen minutes", "15 minutes")
        for name in ("README.md", "CHANGELOG.md", "docs/WDOTOOL.md"):
            with self.subTest(name):
                self.assertTrue(any(s in self.docs[name] for s in spellings), name)


class TheSelftestParagraph(unittest.TestCase):
    """docs/Technical.md describes what `vm/selftest.sh` asserts.  It is a
    description of a script, so the script decides."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "vm", "selftest.sh"), encoding="utf-8") as fh:
            cls.sh = fh.read()
        cls.docs = support.documents()

    def paragraph(self):
        text = self.docs["docs/Technical.md"]
        return text.split("**`vm/selftest.sh` proves the rig")[1].split("\n\n")[0]

    def test_the_script_has_no_display_manager_check(self):
        """The premise: gdm, sddm and lightdm appear nowhere in it, and the
        `DESKTOPS` table it reads carries `logind_type` and `session_type` and
        no display manager at all."""
        for dm in ("gdm", "sddm", "lightdm", "display-manager"):
            with self.subTest(dm):
                self.assertNotIn(dm, self.sh)

    def test_the_paragraph_does_not_claim_one(self):
        """Finding F7.5(b)."""
        self.assertNotIn("display manager", self.paragraph())

    def test_the_paragraph_names_what_the_script_does_check(self):
        para = self.paragraph()
        self.assertIn("XDG_SESSION_TYPE", para)
        self.assertIn("XDG_SESSION_TYPE", self.sh)
        self.assertIn("autologin", para.lower())


class TheOverlapCheckCount(unittest.TestCase):
    """"six checks" appears in four documents.  The extension decides."""

    @classmethod
    def setUpClass(cls):
        with open(OVERLAP_JS, encoding="utf-8") as fh:
            cls.js = fh.read()
        cls.docs = support.documents()

    def passing_checks(self):
        """The names the extension appends to `checks` when a check *passes*.

        That is the list `wxrandr --dryrun --unsafe-gnome-overlap` and
        `install-overlap.sh --check` print, and it is what "six checks passed"
        counts.  `refuse()` names are a longer list -- one check refuses under
        three names (`struct-size`, `symbols`, `libmutter`) and two more
        (`read-back`, `positive-control`) exist only on the apply path and
        never report a pass at all."""
        return sorted(set(re.findall(r"_pass\('([a-z0-9-]+)'", self.js)))

    def test_the_extension_reports_six_passing_checks(self):
        """Measured on GNOME 46.0 (noble-gnome, package route): `wxrandr
        --dryrun --unsafe-gnome-overlap` passes all six with the shipped
        FwOverlap14 typelib."""
        names = self.passing_checks()
        self.assertEqual(names, ["bounded-read", "pending-dialog", "public-view",
                                 "sentinel", "shell-version", "typelib"])
        self.assertEqual(len(names), 6)

    def paragraphs_about_the_overlap(self):
        """Every `<number> checks` phrase in the four documents that count
        them, paired with the paragraph it sits in, kept only where that
        paragraph is about the overlap route.  Scoped both ways on purpose: a
        future sentence counting the daemon's checks or the rig's is not this
        number, and a test named for the overlap extension must not go red
        over it."""
        for name in CHECK_DOCS:
            text = self.docs[name]
            cuts = [m.start() for m in SECTION.finditer(text)] + [len(text)]
            for start, end in zip([0] + cuts, cuts):
                section = text[start:end]
                if not any(w in section for w in ABOUT_OVERLAP):
                    continue
                for word in N_CHECKS.findall(section):
                    yield name, section, word

    def test_every_document_says_six(self):
        n = len(self.passing_checks())
        found = list(self.paragraphs_about_the_overlap())
        # nine phrases across the four documents today (README 2, WXRANDR 2,
        # gnome/README 1, Technical 4).  The floor is the premise: a regex or
        # a scope that stopped matching would make the loop below pass over
        # nothing at all.
        self.assertGreaterEqual(len(found), 9, found)
        for name, _para, word in found:
            with self.subTest("%s: %s checks" % (name, word)):
                said = NUMBER_WORDS.get(word.lower(), None)
                said = said if said is not None else int(word)
                self.assertEqual(said, n)

    def test_the_apply_only_checks_never_report_a_pass(self):
        """The premise of the count, from the other side: `read-back` and
        `positive-control` run after the write and before the apply, so they
        appear in refusals and never in the passing list -- which is why
        counting `refuse()` names would give eleven and not six."""
        refused = set(re.findall(r"refuse\('([a-z0-9-]+)'", self.js))
        for name in ("read-back", "positive-control"):
            self.assertIn(name, refused)
            self.assertNotIn(name, self.passing_checks())


class TheCommandCount(unittest.TestCase):
    """"all 48 commands" is the size of `wdotool.commands.REGISTRY`."""

    def test_the_registry_is_the_number_in_the_documents(self):
        from wdotool import commands
        n = len(commands.REGISTRY)
        docs = support.documents()
        said = set()
        for text in docs.values():
            said |= {int(h) for h in re.findall(r"\b(\d+) commands\b", text)}
        self.assertTrue(said, "no document states a command count")
        self.assertEqual(said, {n}, (sorted(said), n))


class TheVersionBlock(unittest.TestCase):
    """README's "Check it worked" block is six commands and their output.  It
    is the first thing anybody runs after installing, so every byte of it is
    run here."""

    @classmethod
    def setUpClass(cls):
        text = support.documents()["README.md"]
        block = text.split("### Check it worked")[1].split("```console")[1]
        cls.block = block.split("```")[0].strip("\n")

    def pairs(self):
        """The block parsed into (argv, expected stdout lines)."""
        out, expected, cmd = [], [], None
        for line in self.block.splitlines():
            if line.startswith("$ "):
                if cmd is not None:
                    out.append((cmd, expected))
                cmd, expected = line[2:].split(), []
            else:
                expected.append(line)
        out.append((cmd, expected))
        return out

    def test_each_line_of_it_is_what_the_tool_prints(self):
        """Five of the six answer with no session at all.  `wxrandr --version`
        is the exception and it is xrandr's own behaviour, verbatim: xrandr
        1.5.4 prints its program version, then tries to open the display for
        the *server* line, and with none prints `Can't open display` and exits
        1 -- so off a desktop only the first line of that entry is produced,
        which is what is compared here."""
        env = dict(os.environ, PYTHONPATH=ROOT, FUCKWAYLAND_PASSTHROUGH="never",
                   WWMCTL_WMCTRL_GENERATION="1.07", LC_ALL="C")
        pairs = self.pairs()
        self.assertEqual(len(pairs), 6, pairs)
        for argv, want in pairs:
            with self.subTest(" ".join(argv)):
                got = subprocess.run([sys.executable, "-m", argv[0]] + argv[1:],
                                     cwd=ROOT, env=env, capture_output=True,
                                     text=True, timeout=120)
                lines = got.stdout.splitlines()
                if argv[0] == "wxrandr" and "Can't open display" in got.stderr:
                    self.assertEqual(lines, want[:1], want)
                    continue
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertEqual(lines, want)

    def test_the_block_is_the_six_tools_in_the_order_the_readme_lists_them(self):
        """`wxprop` is the one that takes a single-dash `-version`, which the
        prose above the block says out loud because it is the sort of thing
        somebody reports as a bug."""
        argvs = [tuple(a) for a, _ in self.pairs()]
        self.assertEqual(argvs, [("wdotool", "--version"), ("wwmctl", "--version"),
                                 ("wxprop", "-version"), ("wxrandr", "--version"),
                                 ("warandr", "--version"), ("wmirror", "--version")])

    def test_the_two_tools_of_our_own_print_this_release(self):
        """warandr and wmirror clone nothing, so their version is the
        project's -- and the README block is where it is written out in full."""
        self.assertIn("warandr %s" % VERSION, self.block)
        self.assertIn("wmirror %s" % VERSION, self.block)


class Formatting(unittest.TestCase):
    """Three shapes that a renderer turns into something the author did not
    write.  Each was in the tree."""

    @classmethod
    def setUpClass(cls):
        cls.docs = support.documents()

    def test_no_bullet_is_glued_to_the_end_of_the_previous_one(self):
        """CHANGELOG.md had `...measured against.- **The documents were read`:
        a list item that begins in the middle of a line is not a list item, and
        the whole entry disappeared into the one above it."""
        for name, text in self.docs.items():
            for m in re.finditer(r"[^\s\-]\.- \*\*", text):
                line = text.count("\n", 0, m.start()) + 1
                self.fail("%s:%d: a bullet glued to the previous line: %r"
                          % (name, line, text[max(0, m.start() - 40):m.end() + 20]))

    def test_no_heading_is_repeated_within_eight_lines_of_itself(self):
        """repro/README.md carried "## The scaling pass" twice with one line
        between them -- a paste that duplicated the heading and its first
        sentence, one of the two copies spelling `wdotool`s for
        `wdotool`'s."""
        for name, text in self.docs.items():
            seen = {}
            for i, line in enumerate(text.splitlines(), 1):
                if not line.startswith("#"):
                    continue
                head = line.strip()
                if head in seen and i - seen[head] <= 8:
                    self.fail("%s:%d: %r is also at line %d"
                              % (name, i, head, seen[head]))
                seen[head] = i

    def test_no_tool_name_is_made_possessive_with_a_bare_s(self):
        """`` `wdotool`s pointer `` -- the apostrophe went inside the code span
        and then out of it.  Only the six tool names are checked: `` `a`s ``
        (a row of the letter a) and `` `execve`s `` are plurals and verbs, and
        both are correct."""
        for name, text in self.docs.items():
            for tool in ("wdotool", "wwmctl", "wxprop", "wxrandr", "warandr",
                         "wmirror"):
                with self.subTest("%s: %s" % (name, tool)):
                    self.assertNotIn("`%s`s " % tool, text)


if __name__ == "__main__":
    unittest.main()
