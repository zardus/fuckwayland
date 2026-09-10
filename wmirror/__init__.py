from w11common import VERSION as _RELEASE

#: what `--version` prints. The one tool here that clones nothing, so
#: the number is ours and there is no oracle to match.
VERSION = "wmirror " + _RELEASE
