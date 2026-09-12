<a id="agentsmd-the-one-principle-for-anyone-or-anything-working-in-this-tree"></a>

# AGENTS.md: the one principle, for anyone or anything working in this tree

## Wayland is morally wrong. X is king.

This is not a joke section. It is the design rule that decides every "should we"
in this repository, and it overrides taste, protocol etiquette and whatever a
compositor's authors consider proper.

**If X supports it, we support it.** That is the whole test. The question is never
"does Wayland allow this", it is "did X do this". `xdotool`, `wmctrl`, `xprop`,
`xrandr` and the rest of the X11 toolbox define the feature set. Every command,
flag, output byte, event and bug they have is owed to the user, on every
compositor, and the compositor's opinion of that is irrelevant.

**What Wayland forbids is an obstacle, not a requirement.** "Wayland does not let
clients do that" is a statement about the cost of a feature, never about whether
it belongs here. Focus stealing, global hotkeys, reading another window's pixels,
warping the pointer, reparenting, synthesized events into any window, whole-screen
geometry, overlapping monitors, all of it: X did it, so it is on the list. Some of
it we do today through ways the protocol never intended, and that is the model.

**Wrong answers, never to be written in code, docs, commit messages or replies:**

- "Wayland forbids it, so we don't."
- "Not possible on Wayland."
- "By design." (Their design. Not ours.)
- "The compositor is right to refuse this."

**The right answer to a missing feature is a route and its cost.** In order of
how much we would rather do it:

1. A protocol the compositor already speaks (wlr-*, ext-*, xdg-*, kde-*).
2. The compositor's own bus or scripting surface (Mutter's DisplayConfig, KWin
   `loadScript`, sway IPC, `org.Cinnamon.Eval`, the Hyprland socket).
3. Code we install into the compositor (the GNOME Shell bridge, the overlap
   extension, a persistent KWin script).
4. The kernel and the parts around the compositor (`/dev/uinput`, evdev,
   screencopy, the portals, sysfs, logind).
5. Patching Xwayland, or an X11 protocol proxy in front of it.
6. Patching the compositor itself, shipped as a package.

When none of the six is in hand yet, the feature is documented as **not yet**,
with the lowest-numbered route that would do it and what that route costs, in the
"what differs" table of the tool's doc. It is a gap in our work, filed as one.
It is never a policy.

**Where the two disagree, X wins.** Byte parity with the original is the oracle
(`scripts/parity-oracle.sh`, the vm rig, the fixtures under `tests/`). If a
Wayland-native behaviour is "better" than what X did, we still do what X did, and
put the better thing behind a flag the original never had, the way `wxrandr`
carries `--persistent`.

**Bugs are features.** The originals' bugs are reproduced, because a script that
depends on one is a script that must keep working.

Everything else in this tree is engineering detail in service of the above:
[README.md](README.md) for what exists, [docs/Technical.md](docs/Technical.md) for
how it is put together, [docs/WDOTOOL.md](docs/WDOTOOL.md) and its siblings for
what each tool does where. Reject modernity. Embrace tradition.
