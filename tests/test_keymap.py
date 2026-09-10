"""Unit tests for wdotool.keymap and the generated wdotool.keysyms."""

import os
import string
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wdotool import keymap
from wdotool.keysyms import KEYSYM_TO_UNICODE, NAME_TO_KEYSYM

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

# B13: the injection tests pin the *fixed US table* as the source of
# keycodes. Without this a developer running the suite inside a German or
# Dvorak session would have the daemon read that session's real keymap and
# type through it, and every keycode assertion here would be wrong.
os.environ.setdefault("WDOTOOL_LAYOUT", "us")


class TestKeysyms(unittest.TestCase):
    def test_size(self):
        self.assertGreater(len(NAME_TO_KEYSYM), 2000)
        self.assertGreater(len(KEYSYM_TO_UNICODE), 1500)

    def test_known_values(self):
        self.assertEqual(NAME_TO_KEYSYM["space"], 0x20)
        self.assertEqual(NAME_TO_KEYSYM["dollar"], 0x24)
        self.assertEqual(NAME_TO_KEYSYM["a"], 0x61)
        self.assertEqual(NAME_TO_KEYSYM["A"], 0x41)
        self.assertEqual(NAME_TO_KEYSYM["Return"], 0xFF0D)
        self.assertEqual(NAME_TO_KEYSYM["BackSpace"], 0xFF08)
        self.assertEqual(NAME_TO_KEYSYM["Aacute"], 0xC1)
        self.assertEqual(NAME_TO_KEYSYM["F1"], 0xFFBE)
        self.assertEqual(NAME_TO_KEYSYM["F35"], 0xFFBE + 34)

    def test_unicode_mappings(self):
        self.assertEqual(KEYSYM_TO_UNICODE[0x20], 0x20)
        self.assertEqual(KEYSYM_TO_UNICODE[0xFF80], 0x20)  # KP_Space <U+0020>
        self.assertEqual(KEYSYM_TO_UNICODE[0xC1], 0xC1)  # Aacute

    def test_deprecated_aliases_present(self):
        # XStringToKeysym accepts deprecated names too
        self.assertIn("quoteleft", NAME_TO_KEYSYM)
        self.assertIn("L1", NAME_TO_KEYSYM)


class TestCharTable(unittest.TestCase):
    def test_all_printable_ascii_mapped(self):
        for ch in string.digits + string.ascii_letters + string.punctuation + " \t\n\r":
            self.assertIsNotNone(keymap.char_to_key(ch), f"unmapped char {ch!r}")

    def test_letters(self):
        self.assertEqual(keymap.char_to_key("a"), (30, False))
        self.assertEqual(keymap.char_to_key("A"), (30, True))
        self.assertEqual(keymap.char_to_key("z"), (44, False))
        self.assertEqual(keymap.char_to_key("Z"), (44, True))
        self.assertEqual(keymap.char_to_key("q"), (16, False))
        self.assertEqual(keymap.char_to_key("m"), (50, False))

    def test_digits_and_shifts(self):
        self.assertEqual(keymap.char_to_key("1"), (2, False))
        self.assertEqual(keymap.char_to_key("!"), (2, True))
        self.assertEqual(keymap.char_to_key("0"), (11, False))
        self.assertEqual(keymap.char_to_key(")"), (11, True))
        self.assertEqual(keymap.char_to_key("$"), (5, True))

    def test_punctuation(self):
        self.assertEqual(keymap.char_to_key("`"), (41, False))
        self.assertEqual(keymap.char_to_key("~"), (41, True))
        self.assertEqual(keymap.char_to_key("-"), (12, False))
        self.assertEqual(keymap.char_to_key("_"), (12, True))
        self.assertEqual(keymap.char_to_key("="), (13, False))
        self.assertEqual(keymap.char_to_key("+"), (13, True))
        self.assertEqual(keymap.char_to_key("["), (26, False))
        self.assertEqual(keymap.char_to_key("{"), (26, True))
        self.assertEqual(keymap.char_to_key("\\"), (43, False))
        self.assertEqual(keymap.char_to_key("|"), (43, True))
        self.assertEqual(keymap.char_to_key(";"), (39, False))
        self.assertEqual(keymap.char_to_key(":"), (39, True))
        self.assertEqual(keymap.char_to_key("'"), (40, False))
        self.assertEqual(keymap.char_to_key('"'), (40, True))
        self.assertEqual(keymap.char_to_key(","), (51, False))
        self.assertEqual(keymap.char_to_key("<"), (51, True))
        self.assertEqual(keymap.char_to_key("."), (52, False))
        self.assertEqual(keymap.char_to_key(">"), (52, True))
        self.assertEqual(keymap.char_to_key("/"), (53, False))
        self.assertEqual(keymap.char_to_key("?"), (53, True))

    def test_control_chars(self):
        self.assertEqual(keymap.char_to_key("\n"), (28, False))
        self.assertEqual(keymap.char_to_key("\r"), (28, False))
        self.assertEqual(keymap.char_to_key("\t"), (15, False))
        self.assertEqual(keymap.char_to_key(" "), (57, False))
        self.assertEqual(keymap.char_to_key("\b"), (14, False))
        self.assertEqual(keymap.char_to_key("\x1b"), (1, False))

    def test_unmapped(self):
        self.assertIsNone(keymap.char_to_key("é"))
        self.assertIsNone(keymap.char_to_key("\x00"))


class TestKeysymResolution(unittest.TestCase):
    def test_specials(self):
        self.assertEqual(keymap.keysym_to_key("Return"), (28, False))
        self.assertEqual(keymap.keysym_to_key("BackSpace"), (14, False))
        self.assertEqual(keymap.keysym_to_key("Escape"), (1, False))
        self.assertEqual(keymap.keysym_to_key("F1"), (59, False))
        self.assertEqual(keymap.keysym_to_key("F10"), (68, False))
        self.assertEqual(keymap.keysym_to_key("F11"), (87, False))
        self.assertEqual(keymap.keysym_to_key("F12"), (88, False))
        self.assertEqual(keymap.keysym_to_key("F13"), (183, False))
        self.assertEqual(keymap.keysym_to_key("F24"), (194, False))
        self.assertEqual(keymap.keysym_to_key("Left"), (105, False))
        self.assertEqual(keymap.keysym_to_key("Up"), (103, False))
        self.assertEqual(keymap.keysym_to_key("Right"), (106, False))
        self.assertEqual(keymap.keysym_to_key("Down"), (108, False))
        self.assertEqual(keymap.keysym_to_key("Prior"), (104, False))
        self.assertEqual(keymap.keysym_to_key("Page_Down"), (109, False))
        self.assertEqual(keymap.keysym_to_key("ISO_Left_Tab"), (15, True))

    def test_keypad(self):
        self.assertEqual(keymap.keysym_to_key("KP_0"), (82, False))
        self.assertEqual(keymap.keysym_to_key("KP_5"), (76, False))
        self.assertEqual(keymap.keysym_to_key("KP_9"), (73, False))
        self.assertEqual(keymap.keysym_to_key("KP_Enter"), (96, False))
        self.assertEqual(keymap.keysym_to_key("KP_Add"), (78, False))
        self.assertEqual(keymap.keysym_to_key("KP_Divide"), (98, False))

    def test_modifier_keysyms(self):
        self.assertEqual(keymap.keysym_to_key("Control_L"), (29, False))
        self.assertEqual(keymap.keysym_to_key("Control_R"), (97, False))
        self.assertEqual(keymap.keysym_to_key("Shift_L"), (42, False))
        self.assertEqual(keymap.keysym_to_key("Shift_R"), (54, False))
        self.assertEqual(keymap.keysym_to_key("Alt_L"), (56, False))
        self.assertEqual(keymap.keysym_to_key("Super_L"), (125, False))
        self.assertEqual(keymap.keysym_to_key("Caps_Lock"), (58, False))

    def test_via_unicode(self):
        self.assertEqual(keymap.keysym_to_key("dollar"), (5, True))
        self.assertEqual(keymap.keysym_to_key("exclam"), (2, True))
        self.assertEqual(keymap.keysym_to_key("asciitilde"), (41, True))
        self.assertEqual(keymap.keysym_to_key("space"), (57, False))
        self.assertEqual(keymap.keysym_to_key("a"), (30, False))
        self.assertEqual(keymap.keysym_to_key("A"), (30, True))

    def test_unreachable(self):
        self.assertIsNone(keymap.keysym_to_key("Aacute"))
        self.assertIsNone(keymap.keysym_to_key("nosuchname"))


class TestParseKeyseq(unittest.TestCase):
    def test_simple(self):
        keys, warns = keymap.parse_keyseq("ctrl+shift+t")
        self.assertEqual(keys, [(29, False), (42, False), (20, False)])
        self.assertEqual(warns, [])

    def test_aliases_case_insensitive(self):
        for spec in ("CTRL+T", "Ctrl+t", "ctrl+t"):
            keys, _ = keymap.parse_keyseq(spec)
            self.assertEqual(keys[0], (29, False))
        self.assertEqual(keymap.parse_keyseq("super+x")[0][0], (125, False))
        self.assertEqual(keymap.parse_keyseq("win+x")[0][0], (125, False))
        self.assertEqual(keymap.parse_keyseq("meta+x")[0][0], (56, False))
        self.assertEqual(keymap.parse_keyseq("enter")[0], [(28, False)])
        self.assertEqual(keymap.parse_keyseq("Return")[0], [(28, False)])

    def test_keysym_names_case_sensitive(self):
        # aliases match case-insensitively ("RETURN" -> alias "return"), but
        # plain keysym names do not ("BACKSPACE" is not "BackSpace")
        keys, warns = keymap.parse_keyseq("RETURN")
        self.assertEqual((keys, warns), ([(28, False)], []))
        _, warns = keymap.parse_keyseq("BACKSPACE")
        self.assertEqual(warns, ["(symbol) No such key name 'BACKSPACE'. Ignoring it."])

    def test_numeric_x_keycode(self):
        keys, warns = keymap.parse_keyseq("38")  # X keycode 38 == evdev 30 == 'a'
        self.assertEqual(keys, [(30, False)])
        self.assertEqual(warns, [])

    def test_unknown_token_warns_and_skips(self):
        keys, warns = keymap.parse_keyseq("ctrl+bogus+t")
        self.assertEqual(keys, [(29, False), (20, False)])
        self.assertEqual(warns, ["(symbol) No such key name 'bogus'. Ignoring it."])

    def test_invalid_sequence_chars(self):
        for bad in ("ctrl-x", "a b", "x[1]", "a\\b", "p|q", "a.b"):
            with self.assertRaises(ValueError):
                keymap.parse_keyseq(bad)

    def test_shifted_keysym(self):
        keys, _ = keymap.parse_keyseq("ctrl+A")
        self.assertEqual(keys, [(29, False), (30, True)])


class TestModifierTable(unittest.TestCase):
    def test_eight_modifiers(self):
        self.assertEqual(len(keymap.MODIFIER_KEYCODES), 8)
        self.assertEqual(
            set(keymap.MODIFIER_KEYCODES), {42, 54, 29, 97, 56, 100, 125, 126}
        )


if __name__ == "__main__":
    unittest.main()
