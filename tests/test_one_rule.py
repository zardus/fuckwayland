#!/usr/bin/env python3
"""AGENTS.md's one rule, as a gate the suite runs.

The rule is in the tree as prose (AGENTS.md, landed 2d06ae3) and it names four
sentences that must never appear: "Wayland forbids it, so we don't", "Not
possible on Wayland", "By design", "The compositor is right to refuse this".
Prose is not a gate -- batch 16 found two copies of "impossible on Wayland" in
its own step files by grepping for them by hand, batch 14 found a third in
river.sh, and batch 20's sweep of 2026-09-09 found the fourth in a place no
grep of the rig would ever reach: `wdotool windowreparent` printed
"reparenting is not possible on Wayland; ignoring" to a user's stderr.  A
sentence that only a hand-grep catches comes back.

So the three unambiguous phrasings are banned tree-wide here, and the fourth
("by design", which has honest uses about our OWN design and about a library's
API shape) is allow-listed line by line: a new one has to be written into
ALLOWED_BY_DESIGN with its reason, which is the review this rule wants.

The second half is the positive form of the rule.  A refusal is allowed to
name what a protocol lacks -- that is a fact -- but it owes the user the route
that would close it and what that route costs.  The three refusals reworded on
2026-09-09 are pinned here by the thing that makes them right: each carries a
NOT-YET and a rung of AGENTS.md's ladder.
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wdotool import backend_cosmic, backend_wlr, window_cmds     # noqa: E402

#: the file suffixes that carry sentences a human reads -- code, shell, the
#: extension's JS, the rig's yaml and nix, and every document
SUFFIXES = (".py", ".sh", ".js", ".md", ".yaml", ".yml", ".nix", ".spec")

#: AGENTS.md and its CLAUDE.md symlink QUOTE the banned sentences, which is the
#: one place they belong; this file quotes them too.
EXEMPT = {"AGENTS.md", "CLAUDE.md", "test_one_rule.py"}

#: The three with no honest use.  Written as regexes because the wrong answer
#: is the claim, not the spelling: "not possible on Wayland" and "impossible on
#: Wayland" are the same sentence, and batch 16 found both spellings.
BANNED = (
    re.compile(r"(?:not +|im)possible on Wayland", re.I),
    re.compile(r"Wayland (?:forbids|does not (?:allow|permit)|disallows)", re.I),
    re.compile(r"the compositor is right", re.I),
)

#: "by design" is the fourth banned sentence and the only one with honest uses:
#: every line below is about OUR design or about a third party's API shape, and
#: none of them is an answer to a feature we do not have.  Stripped text, so a
#: line that only moves does not fail this.
ALLOWED_BY_DESIGN = {
    # our own security posture, in the README's own section about it
    "**What the tools do by design.** `wdotool` injects keystrokes and pointer events as a",
    # our state file's format
    '"""One of the store\'s sub-dicts, coerced.  The state file is a plain JSON file, hand-editable by design',
    # our lock's own contract
    "the other's load. The lock is best effort by design: a runtime dir that",
    # our deliberate difference from XWayland's fabricated CVT list, which is not
    # a refusal: wxrandr prints MORE than the oracle here, never less
    "geometry/rotation words, monitor listings — the mode TABLES differ by design:",
    # KWin's own JS API, described
    "return w.captionNormal;                   /* 6.x: no suffix, by design */",
    # our own choice of which pointer getmouselocation reports
    "// the daemon-tracked injected pointer by design; this is how the two are",
    "| `GetPointer` | `() → (iiu)` | the real pointer and Clutter modifier mask. Diagnostic only "
    "(`GnomeBackend.real_pointer()`, no command uses it): `getmouselocation` reports the daemon-tracked "
    "injected pointer by design, and this is how the two are checked against each other |",
    # wmirror's audience, which is a choice about the tool and not about a protocol
    "sway is the whole audience for this tool (wlroots only, by design), and",
}

BY_DESIGN = re.compile(r"by design", re.I)

#: A `want` pattern in a step file that pins one of these refusals, read out of
#: the file itself.  The three of them are the live oracle: `want` is `grep -Eq`
#: against the tool's own stderr.
STEP_REFUSAL = re.compile(r'"([^"\n]*is not supported by the (?:wlr|cosmic) backend[^"\n]*)"')

#: Lines that RESTATE the rule rather than lean on it.  README.md's Philosophy
#: section quotes AGENTS.md's second paragraph, which is the one place outside
#: AGENTS.md where a reader has to be shown the wrong answer to be told it is
#: wrong.  Stripped text, same shape as ALLOWED_BY_DESIGN.
QUOTING_THE_RULE = {
    "[AGENTS.md](AGENTS.md): **if X supports it, we support it.** What Wayland forbids is",
}


def tracked_files():
    """Every file git tracks with one of the suffixes above.  git, not a walk,
    so a scratch copy or a build tree in the working directory cannot fail the
    suite for a sentence nobody is shipping."""
    out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"],
                         stdout=subprocess.PIPE, check=True).stdout
    names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    return [n for n in names
            if n.endswith(SUFFIXES) and os.path.basename(n) not in EXEMPT]


class TheBannedSentences(unittest.TestCase):
    """The wrong answers, nowhere in the tree."""

    @classmethod
    def setUpClass(cls):
        cls.files = tracked_files()
        cls.assertTrue(cls.files, "git ls-files found nothing to scan")

    def hits(self, rx):
        found = []
        for name in self.files:
            with open(os.path.join(ROOT, name), encoding="utf-8", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    if rx.search(line) and line.strip() not in QUOTING_THE_RULE:
                        found.append("%s:%d: %s" % (name, n, line.strip()))
        return found

    def test_no_file_says_a_thing_is_not_possible_on_wayland(self):
        """The sentence AGENTS.md exists to keep out.  It was in
        wdotool/window_cmds.py's windowreparent warning until 2026-09-09 --
        user-visible stderr, not a comment -- and no rig grep could see it."""
        self.assertEqual(self.hits(BANNED[0]), [])

    def test_no_file_says_wayland_forbids_or_disallows_it(self):
        """One exemption, and it is the rule being quoted: README.md's
        Philosophy section repeats AGENTS.md's own sentence about what Wayland
        forbids being a cost.  QUOTING_THE_RULE holds it by its bytes, so a
        reworded Philosophy section has to come back through here."""
        self.assertEqual(self.hits(BANNED[1]), [])
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = [ln.strip() for ln in f]
        self.assertEqual(QUOTING_THE_RULE - set(readme), set(),
                         "the exemption outlived the line it exempts")

    def test_no_file_says_the_compositor_is_right_to_refuse(self):
        self.assertEqual(self.hits(BANNED[2]), [])

    def test_every_by_design_is_one_of_the_eight_that_earned_it(self):
        """Not a ban: a review gate.  Eight lines in the tree say "by design"
        and every one is about our own design or a documented third-party API,
        which is the opposite of citing a compositor's design as our reason.  A
        ninth has to be written down here with its sentence."""
        stripped = set()
        for hit in self.hits(BY_DESIGN):
            stripped.add(hit.split(": ", 1)[1])
        new = stripped - ALLOWED_BY_DESIGN
        self.assertEqual(new, set(), "a new 'by design': justify it in ALLOWED_BY_DESIGN or reword it")
        gone = ALLOWED_BY_DESIGN - stripped
        self.assertEqual(gone, set(), "ALLOWED_BY_DESIGN names a line that is no longer in the tree")


class ARefusalCarriesItsRoute(unittest.TestCase):
    """The positive half: naming what a protocol lacks is a fact and is
    allowed; stopping there is not.  Each of these three was a bare lack until
    2026-09-09 (requests-batch-8.md, batch 14's two items; the third found in
    the sweep), and each now ends in a rung of AGENTS.md's ladder."""

    def test_the_wlr_geometry_refusal_names_the_x_plane_and_a_patched_compositor(self):
        """Route 5 for an XWayland window (the X plane really does have a
        ConfigureWindow for it -- vm/live-smoke.d/labwc.sh:128 and river.sh:95
        already say so in their check names), route 6 for a native one."""
        s = backend_wlr.NO_GEOMETRY
        self.assertIn("zwlr_foreign_toplevel_management_v1 carries no geometry", s)
        self.assertIn("AGENTS.md route 5", s)
        self.assertIn("route 6", s)
        self.assertIn("not yet", s)

    def test_the_cosmic_geometry_refusal_names_a_patched_cosmic_comp(self):
        """cosmic-comp has no move request and no protocol above it that does,
        so route 6 is the lowest rung that reaches it."""
        s = backend_cosmic.NO_GEOMETRY
        self.assertIn("the COSMIC toplevel protocol has no move, resize, raise or lower", s)
        self.assertIn("AGENTS.md route 6", s)
        self.assertIn("not yet", s)

    def test_the_step_files_own_want_patterns_still_match_the_refusal(self):
        """The reason the route went at the END of each sentence and not after
        the colon: vm/live-smoke.d/labwc.sh and river.sh pin the protocol
        clause contiguous with `the wlr backend: `, and cosmic.sh pins the
        prefix alone.  `want` is `grep -Eq`, so a substring is the whole test
        -- and a reword that moved the clause would turn three live runs red
        without any unit test noticing.

        The patterns are READ out of the step files, not transcribed here: a
        transcription stays green on the day someone rewords labwc.sh's `want`
        line, which is the exact gap this test exists to close."""
        mods = {"wlr": backend_wlr, "cosmic": backend_cosmic}
        checked = []
        for name in ("labwc.sh", "river.sh", "cosmic.sh"):
            path = os.path.join(ROOT, "vm", "live-smoke.d", name)
            with open(path, encoding="utf-8") as f:
                pats = STEP_REFUSAL.findall(f.read())
            self.assertTrue(pats, "%s no longer pins one of these refusals; move this test with it" % name)
            for pat in pats:
                which = "cosmic" if "cosmic backend" in pat else "wlr"
                op = pat.split(" is not supported by the ", 1)[0]
                line = "%s is not supported by the %s backend: %s" % (op, which, mods[which].NO_GEOMETRY)
                self.assertTrue(re.search(pat, line),
                                "%s pins %r, which no longer matches %r" % (name, pat, line))
                checked.append((name, op))
        # labwc's two, river's one and cosmic's two -- so a step file that quietly lost its check
        # cannot leave this test asserting nothing
        self.assertEqual(len(checked), 5, checked)

    def test_windowreparent_warns_with_a_route_instead_of_a_verdict(self):
        """xdotool's windowreparent is one XReparentWindow and it succeeds, so
        the command stays a warn-and-succeed here (docs/WDOTOOL.md's own `warn
        and succeed` column).  What changed is the sentence: the old one told
        the user what Wayland allows, the new one tells them what would make it
        work.  Read out of the module rather than asserted as a literal so the
        claim is the shape and not the wording."""
        with open(os.path.join(ROOT, "wdotool", "window_cmds.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.index("windowreparent: ")
        warning = src[i:src.index('")', i)]
        self.assertIn("AGENTS.md route 5", warning)
        self.assertIn("route 6", warning)
        self.assertIn("ignoring", warning)
        self.assertTrue(hasattr(window_cmds, "cmd_windowreparent"))


if __name__ == "__main__":
    unittest.main()
