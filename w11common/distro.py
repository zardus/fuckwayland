"""Which distribution family this box is, and the install line that goes with it.

Every "install the original and I will hand over to it" message this project prints named a Debian package and
`apt`, on every distribution. That is wrong twice over on three of the four families we now ship for, and it was
measured wrong on all three:

* Fedora 44 has no `x11-utils` and no `x11-xserver-utils`, and no `xorg-x11-utils`/`xorg-x11-server-utils`
  either: the binaries live in packages literally called `xprop` (1.2.8-5.fc44) and `xrandr` (1.5.3-4.fc44)
  [M recon2/fedora.md].
* Arch calls them `xorg-xprop` (1.2.8-1) and `xorg-xrandr` (1.5.4-1), and warandr's GTK pair is
  `python-gobject` + `gtk3` rather than `python3-gi` + `gir1.2-gtk-3.0` [M recon2/arch.md].
* NixOS printed `apt install xdotool` on a box that has no apt at all [M recon2/nixos.md].

So the hint is table-driven, keyed on `/etc/os-release`. Debian's bytes are the ones every existing test pins
and are also the answer when the family cannot be told, so an unknown distribution keeps today's behaviour
rather than getting a worse guess.

Two seams, both module constants rather than environment variables, because tests plant files and production
never overrides these: `OS_RELEASE` and `NIXOS_MARKER`."""

import os

#: test seam (production value): where the ID/ID_LIKE fields are read from
OS_RELEASE = "/etc/os-release"
#: test seam: NixOS's own marker file. os-release says ID=nixos as well, but this
#: file is there on a system whose /etc/os-release has been shadowed, and it is
#: what nixos-rebuild itself checks for.
NIXOS_MARKER = "/etc/NIXOS"

#: the families with a table of their own; everything else falls back to `debian`
FAMILIES = ("debian", "fedora", "arch", "nixos")

#: distribution IDs (from os-release ID and ID_LIKE) that are not their own family's name
_ID_FAMILY = {
    "ubuntu": "debian", "linuxmint": "debian", "pop": "debian", "raspbian": "debian",
    "rhel": "fedora", "centos": "fedora", "rocky": "fedora", "almalinux": "fedora",
    "ol": "fedora", "fedora-asahi-remix": "fedora",
    "manjaro": "arch", "endeavouros": "arch", "cachyos": "arch", "garuda": "arch",
}

#: what a caller may ask for. `gtk3-python` is warandr's pair, which is two
#: packages everywhere and therefore has no single name.
WHAT = ("xdotool", "wmctrl", "xprop", "xrandr", "wl-mirror", "gtk3-python")

_INSTALL = {
    "debian": "apt install %s",
    "fedora": "dnf install %s",
    "arch": "pacman -S %s",
    "nixos": "nix-env -iA %s",
}

#: family -> what -> package name(s). Debian's four tool rows are today's bytes
#: (w11common/passthrough.py:_PACKAGE), which is why no existing test moves.
_PACKAGES = {
    "debian": {
        "xdotool": "xdotool",
        "wmctrl": "wmctrl",
        "xprop": "x11-utils",
        "xrandr": "x11-xserver-utils",
        "wl-mirror": "wl-mirror",
        "gtk3-python": "python3-gi gir1.2-gtk-3.0",
    },
    "fedora": {
        "xdotool": "xdotool",
        "wmctrl": "wmctrl",
        "xprop": "xprop",
        "xrandr": "xrandr",
        "wl-mirror": "wl-mirror",
        "gtk3-python": "python3-gobject gtk3",
    },
    "arch": {
        "xdotool": "xdotool",
        "wmctrl": "wmctrl",
        "xprop": "xorg-xprop",
        "xrandr": "xorg-xrandr",
        "wl-mirror": "wl-mirror",
        "gtk3-python": "python-gobject gtk3",
    },
    "nixos": {
        "xdotool": "nixpkgs.xdotool",
        "wmctrl": "nixpkgs.wmctrl",
        "xprop": "nixpkgs.xorg.xprop",
        "xrandr": "nixpkgs.xorg.xrandr",
        "wl-mirror": "nixpkgs.wl-mirror",
        "gtk3-python": "nixpkgs.python3Packages.pygobject3 nixpkgs.gtk3",
    },
}

#: how a name this table does not carry is spelled for the family's installer
_DEFAULT_PACKAGE = {"debian": "%s", "fedora": "%s", "arch": "%s", "nixos": "nixpkgs.%s"}


def _os_release(path: str | None = None) -> dict[str, str]:
    """`/etc/os-release` as a dict, {} when it is not readable.

    Values may be quoted (`ID_LIKE="rhel centos fedora"`) or bare (`ID=fedora`); both spellings are in the
    files this was measured against, so both are unquoted here."""
    out: dict[str, str] = {}
    try:
        with open(OS_RELEASE if path is None else path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _sep, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                out[key.strip()] = value
    except OSError:
        return {}
    return out


def family() -> str | None:
    """`debian`, `fedora`, `arch`, `nixos`, or None when os-release says nothing we know.

    `ID` first, then each word of `ID_LIKE` in the order the file gives them -- Ubuntu is `ID=ubuntu
    ID_LIKE=debian`, Rocky is `ID=rocky ID_LIKE="rhel centos fedora"`, and both have to land on the family
    whose package manager they actually ship."""
    if os.path.exists(NIXOS_MARKER):
        return "nixos"
    fields = _os_release()
    ids = [fields.get("ID", "").strip().lower()]
    ids += fields.get("ID_LIKE", "").lower().split()
    for name in ids:
        if name in FAMILIES:
            return name
        if name in _ID_FAMILY:
            return _ID_FAMILY[name]
    return None


def package(what: str, fam: str | None = None) -> str:
    """The package name(s) providing `what` on `fam` (this box's family by default). A family with no table --
    including None -- gets Debian's, which is what every message printed before this module existed."""
    if fam is None:
        fam = family()
    if fam not in _PACKAGES:
        fam = "debian"
    table = _PACKAGES[fam]
    return table.get(what) or _DEFAULT_PACKAGE[fam] % what


def hint(what: str, fam: str | None = None) -> str:
    """The install command for `what`, with no `sudo` and no trailing punctuation: `apt install x11-utils`,
    `dnf install xprop`, `pacman -S xorg-xprop`, `nix-env -iA nixpkgs.xorg.xprop`. Callers own the sentence
    around it, so the same string serves exit 127's parenthesis and the mirror tool's `on <distro>: sudo ...` line."""
    if fam is None:
        fam = family()
    if fam not in _INSTALL:
        fam = "debian"
    return _INSTALL[fam] % package(what, fam)
