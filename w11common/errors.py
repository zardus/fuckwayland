"""The exception a failed command raises.

A module of its own rather than a corner of one of the others, because those four answer questions -- which
session is this, which original to hand over to, what did the bus say -- and this one only names the failure.
Every command in the tree catches it, the three display tools included, so it has to sit where nothing has to
come with it: this module imports nothing at all.

`wdotool/ctx.py` subclasses it twice, for what the window commands need to say on top of "it failed":
`SoftCmdError` (the compositor will not do this to *this* window) and `NoSessionError` (there is no session at
all, rc 2). Both are wdotool's own policy, and stay there.
"""


class CmdError(Exception):
    """A command failed. The driver prints str(self) and exits with
    `exit_code` (1 unless a subclass says otherwise)."""

    exit_code = 1
