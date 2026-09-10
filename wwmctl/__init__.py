from w11common import VERSION as _RELEASE

#: the package identity, in the same shape as the other five; `-V` and
#: `--version` print cli.WMCTRL_VERSION, which is the oracle's number and
#: deliberately not this one.
VERSION = "wwmctl " + _RELEASE
