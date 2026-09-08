#!/usr/bin/env python3
"""Cross-document links, and the thirteen images three files describe.

Two kinds of claim, both of them the sort that decays without anybody noticing.

**Links.** Twenty-one markdown files link to each other and into their own
headings, 164 links in all, and a heading that is reworded takes every anchor
pointing at it with it -- silently, because a bad anchor renders as a link that
scrolls nowhere.  GitHub's slug rules are simple enough to reimplement exactly
(lowercase, backticks dropped, everything that is not a word character,
whitespace or `-` removed, whitespace to `-`, duplicates suffixed `-1`), so
every anchor is resolved against the headings of the file it points into.

**The rig's thirteen images.** `vm/flavors/*.yaml` is the fact; `vm/README.md`'s
table and README.md's support matrix are two descriptions of it, and the counts
in the prose ("thirteen flavors", "eleven are four desktops", "all nine") are a
third.  Each flavor's yaml carries `# vmctl-desktop:`, which `vm/vmctl` looks up
in its own `DESKTOPS` table, so a flavor naming a desktop vmctl does not have is
an image that cannot be built -- and would be discovered by building it, forty
minutes later.
"""

import os
import re
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

FLAVORS = os.path.join(ROOT, "vm", "flavors")
VMCTL = os.path.join(ROOT, "vm", "vmctl")

#: `](path#anchor)`, `](path)` and `](#anchor)`, which are the three shapes in
#: these documents.  Reference-style links and bare autolinks are not used.
LINK = re.compile(r"\]\(([^)\s#]+)?(?:#([^)\s]+))?\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.M)
#: ```-fenced blocks are dropped before HEADING runs: a console block in these
#: documents is full of shell comments, and thirteen `#` lines across the tree
#: read as headings that a rendered page does not offer.  They could only hide
#: a missing anchor, never invent one, but an anchor set that is not GitHub's
#: is not the thing this file claims to check.
FENCE = re.compile(r"^\s*(?:```|~~~)", re.M)

#: The counts vm/README.md writes in words.  Only the ones it uses.
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                "twelve": 12, "thirteen": 13}


def slugify(heading):
    """GitHub's anchor for a heading, without the duplicate suffix.

    The rules, in order, from GitHub's own `github-slugger`: strip, lowercase,
    remove everything that is not a word character, whitespace or a hyphen
    (which takes the backticks, the `*`, the `?` and the em dashes out), then
    whitespace to `-`.  Leading hyphens are kept, which is why
    `## \\`--dryrun\\`, and unrecognised builds` becomes
    `--dryrun-and-unrecognised-builds` and not `dryrun-and-unrecognised-builds`.
    """
    s = heading.strip().lower()
    s = s.replace("`", "")
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s+", "-", s)


def unfenced(text):
    """`text` with every fenced code block removed, lines kept in place.

    A fence is toggled by a line whose first non-blank token is ``` or ~~~;
    the closing fence toggles it back.  An unclosed fence swallows the rest of
    the file, which is what a renderer does too."""
    out, inside = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            out.append("")
            continue
        out.append("" if inside else line)
    return "\n".join(out)


def anchors(text):
    """Every anchor a rendered file offers, duplicates numbered as GitHub
    numbers them (`-1`, `-2`, ...)."""
    out, seen = set(), {}
    for _hashes, title in HEADING.findall(unfenced(text)):
        base = slugify(title)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.add(base if n == 0 else "%s-%d" % (base, n))
    return out


class TheSlugRules(unittest.TestCase):
    """The reimplementation, checked against headings whose slugs are known
    because other files link to them."""

    def test_a_heading_that_begins_with_an_option_keeps_its_dashes(self):
        self.assertEqual(slugify("`--dryrun`, and unrecognised builds"),
                         "--dryrun-and-unrecognised-builds")

    def test_backticks_go_and_the_words_stay(self):
        self.assertEqual(slugify("`wxrandr --persistent`"), "wxrandr---persistent")

    def test_punctuation_goes_and_spaces_become_hyphens(self):
        self.assertEqual(slugify("No authorization dialog"), "no-authorization-dialog")
        self.assertEqual(slugify("What's in it? (and what is not)"),
                         "whats-in-it-and-what-is-not")

    def test_a_repeated_heading_gets_a_numbered_anchor(self):
        text = "## One\n\n## One\n\n## One\n"
        self.assertEqual(anchors(text), {"one", "one-1", "one-2"})


class EveryLinkResolves(unittest.TestCase):
    """164 links today, none of them broken."""

    @classmethod
    def setUpClass(cls):
        cls.docs = support.documents()

    def links(self):
        for name, text in self.docs.items():
            base = os.path.dirname(name)
            for m in LINK.finditer(text):
                path, anchor = m.group(1), m.group(2)
                if path and re.match(r"[a-z]+:", path):
                    continue                    # http(s), mailto: not ours to check
                target = name if not path else os.path.normpath(
                    os.path.join(base, path))
                yield name, path, anchor, target

    def test_every_relative_path_names_a_file_that_exists(self):
        missing = [(n, p) for n, p, _a, t in self.links()
                   if p and not os.path.exists(os.path.join(ROOT, t))]
        self.assertEqual(missing, [])

    def test_every_anchor_names_a_heading_that_exists(self):
        """The half that rots: a heading is reworded and the link still
        renders, still looks like a link, and goes nowhere."""
        missing = []
        for name, path, anchor, target in self.links():
            if not anchor or target not in self.docs:
                continue
            if anchor not in anchors(self.docs[target]):
                missing.append("%s -> %s#%s" % (name, path or "(self)", anchor))
        self.assertEqual(missing, [])

    def test_there_are_links_to_check(self):
        """The premise: a regex that stopped matching would make both tests
        above pass over nothing at all."""
        found = list(self.links())
        self.assertGreater(len(found), 100, len(found))
        self.assertGreater(len([1 for _n, _p, a, _t in found if a]), 40)


class TheThirteenImages(unittest.TestCase):
    """vm/flavors/ is the fact; two documents and one Python table describe
    it."""

    @classmethod
    def setUpClass(cls):
        cls.docs = support.documents()
        cls.names = sorted(n[:-5] for n in os.listdir(FLAVORS)
                           if n.endswith(".yaml"))
        cls.yaml = {}
        for name in cls.names:
            with open(os.path.join(FLAVORS, name + ".yaml"), encoding="utf-8") as fh:
                cls.yaml[name] = fh.read()
        with open(VMCTL, encoding="utf-8") as fh:
            vmctl = fh.read()
        block = vmctl.split("DESKTOPS = {", 1)[1].split("\n}", 1)[0]
        cls.desktops = re.findall(r'^\s*"([a-z0-9-]+)":\s*dict\(', block, re.M)
        cls.iso = sorted(n for n in cls.names
                         if re.search(r"^# vmctl-iso:", cls.yaml[n], re.M))
        cls.base = sorted(n for n in cls.names
                          if re.search(r"^# vmctl-base:", cls.yaml[n], re.M))
        # `stonking` is Ubuntu 26.10, a development release: two probe images
        # that are not part of the measured matrix (vm/README.md line 293,
        # README.md's "Four more images stand behind the table").
        cls.probes = sorted(n for n in cls.names if n.startswith("stonking-"))

    def test_there_are_thirteen_of_them(self):
        self.assertEqual(len(self.names), 13, self.names)

    def test_each_one_is_built_either_from_a_cloud_image_or_from_an_iso(self):
        """The two routes are different scripts (`vmctl build` and
        `vm/build-iso-golden.sh`), and the header is how each script recognises
        the flavors that are its own.  A yaml carrying both, or neither, is one
        no script will pick up."""
        for name, text in self.yaml.items():
            has_base = bool(re.search(r"^# vmctl-base:", text, re.M))
            has_iso = bool(re.search(r"^# vmctl-iso:", text, re.M))
            with self.subTest(name):
                self.assertEqual(has_base + has_iso, 1,
                                 "vmctl-base: %s, vmctl-iso: %s" % (has_base, has_iso))
        self.assertEqual(len(self.base), 11, self.base)
        self.assertEqual(len(self.iso), 2, self.iso)
        self.assertEqual(self.iso, ["noble-gnome-iso", "resolute-gnome-iso"])

    def test_every_flavor_names_a_desktop_vmctl_knows(self):
        """`vmctl` raises `unknown '# vmctl-desktop: x'` for anything else, and
        the flavor's whole session type, display manager and screenshot
        arrangement come out of that table."""
        # every desktop a flavor names is a DESKTOPS key; the set grows with the flavors (nineteen keys
        # since the new-desktops prelude), so the claim is the subset, not a fixed list
        vmctl = open(os.path.join(ROOT, "vm", "vmctl")).read()
        known = set(re.findall(r'^    "([a-z0-9-]+)":\s+dict\(', vmctl, re.M))
        self.assertTrue(set(self.desktops) <= known, sorted(set(self.desktops) - known))
        for name, text in self.yaml.items():
            with self.subTest(name):
                found = re.findall(r"^# vmctl-desktop:\s*(\S+)", text, re.M)
                self.assertEqual(len(found), 1, found)
                self.assertIn(found[0], self.desktops)

    def test_vm_readme_names_every_one_of_them(self):
        text = self.docs["vm/README.md"]
        for name in self.names:
            with self.subTest(name):
                self.assertIn("`%s`" % name, text)

    def test_vm_readme_names_no_flavor_that_does_not_exist(self):
        """The other direction: a flavor that was renamed or dropped leaves a
        row nobody can build."""
        text = self.docs["vm/README.md"]
        named = set(re.findall(r"`((?:noble|resolute|stonking)-[a-z0-9-]+)`", text))
        self.assertEqual(sorted(named - set(self.names)), [])

    def test_the_prose_counts_agree_with_the_directory(self):
        """Four number words in vm/README.md, every one of them read back out
        of vm/flavors/ rather than asserted as a string: `thirteen flavors` is
        the yaml count, `Eleven` is the cloud-image ones, `The other two` the
        ISO ones, and `the other nine` -- the count README.md's support matrix
        is measured on -- is what is left once the two ISO images and the two
        `stonking-*` (Ubuntu 26.10, a development release) probes come out.
        Adding a flavor changes three of the four, and this is what says so."""
        text = self.docs["vm/README.md"]
        m = re.search(r"\*\*(\w+) flavors\*\*\.\s+(\w+) are four desktops", text)
        self.assertTrue(m, "the counting sentence has been reworded")
        self.assertEqual(NUMBER_WORDS[m.group(1).lower()], len(self.names), self.names)
        self.assertEqual(NUMBER_WORDS[m.group(2).lower()], len(self.base), self.base)
        m = re.search(r"The other (\w+)\s*\n?\s*\(([^)]*)\)", text)
        self.assertTrue(m, "the ISO sentence has been reworded")
        self.assertEqual(NUMBER_WORDS[m.group(1).lower()], len(self.iso), self.iso)
        for name in self.iso:
            self.assertIn(name, m.group(2))
        m = re.search(r"the other\s+(\w+) are what the desktop-support matrix", text)
        self.assertTrue(m, "the matrix-count sentence has been reworded")
        self.assertEqual(NUMBER_WORDS[m.group(1).lower()],
                         len(self.names) - len(self.iso) - len(self.probes),
                         sorted(set(self.names) - set(self.iso) - set(self.probes)))

    def test_the_matrix_header_names_only_versions_a_flavor_provides(self):
        """README's support matrix is a summary of the flavor table, so every
        version number in its header has to be one some image actually runs.
        The header writes the pairs short -- `GNOME 46 / 50`, `Xfce 4.18 /
        4.20` -- so the numbers are compared, not the phrases."""
        header = [ln for ln in self.docs["README.md"].splitlines()
                  if ln.startswith("| | GNOME 4")]
        self.assertEqual(len(header), 1, header)
        # the lookbehind keeps `X11`'s 11 out: a version number here is a
        # whole token, never the tail of a word
        said = set(re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", header[0]))
        self.assertEqual(said, {"46", "50", "5.27", "6.6", "4.18", "4.20", "1.11"},
                         sorted(said))
        table = self.docs["vm/README.md"]
        for token in sorted(said):
            with self.subTest(token):
                self.assertIn(token, table)

    def test_the_two_newest_desktops_are_not_claimed_in_the_matrix(self):
        """Plasma 6.7 and GNOME Shell 51 are images that exist and are not what
        the matrix is measured on -- they stand behind it in the "Four more
        images" paragraph, which says so.  Promoting them into the header would
        be claiming nine flavors' worth of measurement for eleven."""
        header = [ln for ln in self.docs["README.md"].splitlines()
                  if ln.startswith("| | GNOME 4")][0]
        self.assertNotIn("6.7", header)
        self.assertNotIn("51", header)
        para = self.docs["README.md"].split("Four more images stand behind the table")[1]
        para = para.split("\n\n")[0]
        self.assertIn("Plasma 6.7", para)
        self.assertIn("GNOME 51", para)


if __name__ == "__main__":
    unittest.main()
