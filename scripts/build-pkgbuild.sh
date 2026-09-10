#!/bin/sh
# Build the w11 Arch package.  One command, from a clean clone, on
# Arch or in an archlinux:base-devel container:
#
#     sh scripts/build-pkgbuild.sh
#
# -> dist/w11-<version>-1-any.pkg.tar.zst
#    (never committed: site-packages is version-pinned on Arch, so a built
#    package belongs to one python and is a CI artifact, not a release file)
#
#   --no-deps     do not pacman anything; fail if a build tool is missing
#   --lint        run namcap on the recipe and on the package
#   --keep        keep the scratch build directory under dist/pkgbuild
#                 (which is where PKGBUILD.local and the tarball are)
#
# build-deb.sh's option shape, and build-deb.sh's first gate: pyproject.toml's
# version and the PKGBUILD's pkgver have to agree before anything runs.
#
# What is built is dist/pkgbuild/PKGBUILD.local: the shipped recipe with its
# source line pointing at the tarball `git archive` has just made and that
# tarball's real sha256, so nothing in what CI builds is SKIP.  makepkg
# refuses to run as root; in a container, be an ordinary user.
set -eu

cd "$(dirname "$0")/.."

DEPS=0; LINT=0; CLEAN=1
for a in "$@"; do
    case $a in
        --no-deps) DEPS=1 ;;
        --lint) LINT=1 ;;
        --keep) CLEAN=0 ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "build-pkgbuild.sh: unknown option: $a" >&2; exit 2 ;;
    esac
done

PKGBUILD=packaging/arch/PKGBUILD

# --- the SPDX id, from a LICENSE file ----------------------------------------
# HEAD 375d815 put a LICENSE at the root; its first line is
# "SPDX-License-Identifier: BSD-2-Clause", the "BSD 2-Clause License" heading
# is on line 3.  So the header wins before any heading table is consulted: it
# is the licence's own statement of its id, and reading it verbatim means a
# relicence needs no row here.
#
# Failing that, the heading, matched case-insensitively over the first TWO
# non-blank lines.  Two because the canonical GNU texts put the family on line
# 1 ("GNU GENERAL PUBLIC LICENSE") and the version on line 2 ("Version 3, 29
# June 2007"), so one line cannot tell GPL-2 from GPL-3; and case-insensitively
# because those headings are upper case in the FSF's own files and title case
# in GitHub's picker.
#
# A licence this cannot name stops the build.  That is deliberate and it is not
# a fallback to a LicenseRef: a package that ships a licence file under the
# wrong SPDX expression is worse than one that will not build.
spdx_id() {
    [ -f LICENSE ] || return 1
    # one sed and no grep: the trims run in order per line and the last -e drops
    # what is left blank, so nothing is added to what a build needs on PATH
    trimmed=$(sed -e 's/\r$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
                  -e '/^$/d' LICENSE)
    line=$(printf '%s\n' "$trimmed" | head -n 1)
    two=$(printf '%s\n' "$trimmed" | head -n 2 | tr '\n' ' ')
    case $line in
        SPDX-License-Identifier:*)
            id=$(printf '%s' "${line#SPDX-License-Identifier:}" \
                 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
            if [ -z "$id" ]; then
                echo "build-pkgbuild.sh: LICENSE's SPDX-License-Identifier header is empty" >&2
                exit 1
            fi
            echo "$id"
            return 0 ;;
    esac
    case $(printf '%s' "$two" | tr 'A-Z' 'a-z') in
        mit*|*"mit license"*)                     echo MIT ;;
        isc*|*"isc license"*)                     echo ISC ;;
        *apache*)                                 echo Apache-2.0 ;;
        *"bsd 2"*|*simplified\ bsd*)              echo BSD-2-Clause ;;
        *"bsd 3"*|*new\ bsd*)                     echo BSD-3-Clause ;;
        *affero*3*|*agpl*3*)                      echo AGPL-3.0-or-later ;;
        *lesser*3*|*lgpl*3*)                      echo LGPL-3.0-or-later ;;
        *general\ public\ license*3*|*gpl*3*)     echo GPL-3.0-or-later ;;
        *general\ public\ license*2*|*gpl*2*)     echo GPL-2.0-or-later ;;
        *mozilla*2*|*mpl*2*)                      echo MPL-2.0 ;;
        *)  echo "build-pkgbuild.sh: LICENSE names no SPDX id this table knows:" >&2
            echo "  $line" >&2
            echo "build-pkgbuild.sh: put an SPDX-License-Identifier header on line 1, or" >&2
            echo "build-pkgbuild.sh: add a row to spdx_id() in this script." >&2
            exit 1 ;;
    esac
}

# --- one version, in two places ----------------------------------------------
pver=$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' pyproject.toml | head -n 1)
aver=$(sed -n 's/^pkgver=\(.*\)$/\1/p' "$PKGBUILD" | head -n 1)
if [ -z "$pver" ] || [ -z "$aver" ]; then
    echo "build-pkgbuild.sh: cannot read the version from pyproject.toml / $PKGBUILD" >&2
    exit 1
fi
if [ "$pver" != "$aver" ]; then
    cat >&2 <<EOM
build-pkgbuild.sh: version mismatch
  pyproject.toml:    $pver
  PKGBUILD pkgver:   $aver
Edit $PKGBUILD: set pkgver=$pver (and reset pkgrel=1).
EOM
    exit 1
fi
echo "build-pkgbuild.sh: building w11 $pver"

# The licence tag, against the licence the tree ships.  The recipe carries
# license=('BSD-2-Clause') and package() installs LICENSE under
# /usr/share/licenses/w11/, so the tag is right in the file; this
# refuses the build when a relicence has edited one and not the other.  `set
# -e` carries spdx_id()'s own exit -- on a licence it cannot name -- with it.
if [ ! -f LICENSE ]; then
    echo "build-pkgbuild.sh: no LICENSE at the root; namcap requires one" >&2
    exit 1
fi
spdx=$(spdx_id)
atag=$(sed -n "s/^license=('\([^']*\)').*/\1/p" "$PKGBUILD" | head -n 1)
if [ "$spdx" != "$atag" ]; then
    cat >&2 <<EOM
build-pkgbuild.sh: licence mismatch
  LICENSE says:       $spdx
  PKGBUILD license:   $atag
Edit $PKGBUILD: set license=('$spdx').
EOM
    exit 1
fi
echo "build-pkgbuild.sh: LICENSE says $spdx"

# --- build tools -------------------------------------------------------------
PACMAN_BUILD_DEPS='base-devel git namcap python-build python-installer python-wheel'
PACMAN_BUILD_DEPS="$PACMAN_BUILD_DEPS python-setuptools gobject-introspection python-gobject"

need=''
for c in makepkg git python; do
    command -v "$c" >/dev/null 2>&1 || need="$need $c"
done
if [ "$LINT" = 1 ]; then
    command -v namcap >/dev/null 2>&1 || need="$need namcap"
fi

if [ -n "$need" ]; then
    if [ "$DEPS" = 1 ]; then
        echo "build-pkgbuild.sh: missing build tools:$need" >&2
        echo "build-pkgbuild.sh: sudo pacman -S --needed --noconfirm $PACMAN_BUILD_DEPS" >&2
        exit 1
    fi
    echo "build-pkgbuild.sh: installing build tools:$need"
    # shellcheck disable=SC2086
    sudo pacman -S --needed --noconfirm $PACMAN_BUILD_DEPS
fi

# --- the source tarball, and PKGBUILD.local ----------------------------------
build=$(pwd)/dist/pkgbuild
rm -rf "$build"
mkdir -p "$build" dist
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    echo "build-pkgbuild.sh: the tree is dirty; the tarball is HEAD, not what is on disk" >&2
fi
tarball="w11-$pver.tar.gz"
git archive --format=tar.gz --prefix="w11-$pver/" -o "$build/$tarball" HEAD
sum=$(sha256sum "$build/$tarball" | cut -d' ' -f1)

# The three lines that differ from the shipped recipe, and nothing else: a
# local source, its real checksum, and the directory `git archive` wrote (the
# shipped one names upstream's two-component tag, which unpacks to
# w11-0.4/ -- see the comment on _tag in the recipe).  It is written
# into the scratch directory and not into packaging/arch/, so a build leaves
# nothing behind that a `git status` has to explain.
sed -e "s|^source=(.*|source=(\"$tarball\")|" \
    -e "s|^sha256sums=(.*|sha256sums=('$sum')|" \
    -e "s|^_srcdir=.*|_srcdir=\"\$pkgname-\$pkgver\"|" \
    "$PKGBUILD" > "$build/PKGBUILD.local"

cp packaging/arch/w11.install "$build/w11.install"

# --- build -------------------------------------------------------------------
( cd "$build" && makepkg -p PKGBUILD.local -f --noconfirm )

built=''
for f in "$build"/*.pkg.tar.*; do
    [ -e "$f" ] || continue
    cp -f "$f" dist/
    built="$built $(basename "$f")"
done
[ -n "$built" ] || { echo "build-pkgbuild.sh: makepkg produced no package" >&2; exit 1; }
echo "build-pkgbuild.sh: built$built into dist/"

if [ "$LINT" = 1 ]; then
    if command -v namcap >/dev/null 2>&1; then
        # The recipe has to be clean; the package's accepted findings are named
        # one by one in packaging/arch/namcap.expected, which
        # tests/test_release_pkgbuild.py compares the output against.
        namcap "$build/PKGBUILD.local"
        namcap dist/w11-"$pver"-*.pkg.tar.* || true
    else
        echo "build-pkgbuild.sh: namcap is not installed (sudo pacman -S namcap)" >&2
    fi
fi

if [ "$CLEAN" = 1 ]; then
    rm -rf "$build"
fi

echo
echo "Install it with:  sudo pacman -U ./dist/w11-$pver-1-any.pkg.tar.zst"
