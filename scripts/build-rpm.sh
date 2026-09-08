#!/bin/sh
# Build the fuckwayland rpms.  One command, from a clean clone, on Fedora 43
# or 44:
#
#     sh scripts/build-rpm.sh
#
# -> dist/fuckwayland-<version>-1.fc<n>.noarch.rpm and the two
#    gnome-shell-extension-fuckwayland-{bridge,overlap} rpms beside it
#    (nothing is committed: unlike the .deb, an rpm is one file per Fedora
#    release, so release/ carries none)
#
#   --no-deps     do not dnf anything; fail if a build tool is missing
#   --lint        run rpmlint with packaging/rpm/fuckwayland.rpmlintrc
#   --keep        keep the scratch topdir under dist/rpmbuild
#
# build-deb.sh's option shape, and build-deb.sh's first gate: the version in
# pyproject.toml, in the spec's Version: and in its top %changelog entry have
# to agree before anything runs.  That gate is what stopped a 0.3 package
# shipping in a 0.4 tree, and there is one more place for it to go wrong here.
#
# The %pyproject_* macros are Fedora-only (rpmbuild elsewhere dies at
# "%pyproject_buildrequires: not found"), so this refuses on a box without
# them rather than producing something that is not the package.
set -eu

cd "$(dirname "$0")/.."

DEPS=0; LINT=0; CLEAN=1
for a in "$@"; do
    case $a in
        --no-deps) DEPS=1 ;;
        --lint) LINT=1 ;;
        --keep) CLEAN=0 ;;
        -h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "build-rpm.sh: unknown option: $a" >&2; exit 2 ;;
    esac
done

SPEC=packaging/rpm/fuckwayland.spec
RPMLINTRC=packaging/rpm/fuckwayland.rpmlintrc

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
                echo "build-rpm.sh: LICENSE's SPDX-License-Identifier header is empty" >&2
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
        *)  echo "build-rpm.sh: LICENSE names no SPDX id this table knows:" >&2
            echo "  $line" >&2
            echo "build-rpm.sh: put an SPDX-License-Identifier header on line 1, or" >&2
            echo "build-rpm.sh: add a row to spdx_id() in this script." >&2
            exit 1 ;;
    esac
}

# --- one version, in three places --------------------------------------------
pver=$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' pyproject.toml | head -n 1)
sver=$(sed -n 's/^Version: *\([^ ]*\).*/\1/p' "$SPEC" | head -n 1)
cver=$(sed -n 's/^\* .* - \([0-9][^ ]*\)-[0-9]*$/\1/p' "$SPEC" | head -n 1)
if [ -z "$pver" ] || [ -z "$sver" ] || [ -z "$cver" ]; then
    echo "build-rpm.sh: cannot read the version from pyproject.toml / $SPEC" >&2
    exit 1
fi
if [ "$pver" != "$sver" ] || [ "$pver" != "$cver" ]; then
    cat >&2 <<EOM
build-rpm.sh: version mismatch
  pyproject.toml:      $pver
  spec Version:        $sver
  spec %changelog:     $cver
Edit $SPEC: set Version: to $pver and add a %changelog entry ending "- $pver-1".
EOM
    exit 1
fi
echo "build-rpm.sh: building fuckwayland $pver"

# --- the spec's licence tag, against the licence the tree ships --------------
# The spec carries `License: BSD-2-Clause` and %files carries `%license
# LICENSE`, so the tag is right in the file and not only in a build.  What this
# does is refuse to build when the two have drifted: a relicence that edits
# LICENSE and forgets the spec would otherwise ship a package whose tag names
# the old terms, which is the one packaging error nobody can see from outside.
# `set -e` carries spdx_id()'s own exit -- on a licence it cannot name -- with
# it.
if [ ! -f LICENSE ]; then
    echo "build-rpm.sh: no LICENSE at the root; Fedora requires one" >&2
    exit 1
fi
spdx=$(spdx_id)
stag=$(sed -n 's/^License: *\([^ ].*\)$/\1/p' "$SPEC" | head -n 1)
if [ "$spdx" != "$stag" ]; then
    cat >&2 <<EOM
build-rpm.sh: licence mismatch
  LICENSE says:        $spdx
  spec License:        $stag
Edit $SPEC: set License: to $spdx.
EOM
    exit 1
fi
echo "build-rpm.sh: LICENSE says $spdx"

# --- build tools -------------------------------------------------------------
# Two of the four are macro packages with no binary of their own, so they are
# checked by asking rpm to expand a macro each of them defines.  That is the
# exact failure the recon hit on a box with plain rpm: the spec parses and then
# dies at "%pyproject_buildrequires: not found" half way through the build.
DNF_BUILD_DEPS='rpm-build python3-devel pyproject-rpm-macros systemd-rpm-macros git'

need=''
command -v rpmbuild >/dev/null 2>&1 || need="$need rpm-build"
command -v git >/dev/null 2>&1 || need="$need git"
if command -v rpm >/dev/null 2>&1; then
    case $(rpm --eval '%{_udevrulesdir}' 2>/dev/null) in
        /*) ;;
        *) need="$need systemd-rpm-macros" ;;
    esac
    case $(rpm --eval '%{pyproject_wheel}' 2>/dev/null) in
        *'%{pyproject_wheel}'*|"") need="$need pyproject-rpm-macros python3-devel" ;;
    esac
else
    need="$need $DNF_BUILD_DEPS"
fi
if [ "$LINT" = 1 ]; then
    command -v rpmlint >/dev/null 2>&1 || need="$need rpmlint"
fi

if [ -n "$need" ]; then
    if [ "$DEPS" = 1 ]; then
        echo "build-rpm.sh: missing build tools:$need" >&2
        echo "build-rpm.sh: sudo dnf install -y$need" >&2
        exit 1
    fi
    echo "build-rpm.sh: installing build tools:$need"
    # shellcheck disable=SC2086
    sudo dnf install -y $need
fi

# --- the source tarball ------------------------------------------------------
# git archive, so the tarball is a commit and not a developer's scratch state;
# %autosetup unpacks fuckwayland-<version>/ and every path in %install is
# relative to it.
top=$(pwd)/dist/rpmbuild
rm -rf "$top"
mkdir -p "$top/SOURCES" "$top/SPECS" dist
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    echo "build-rpm.sh: the tree is dirty; the tarball is HEAD, not what is on disk" >&2
fi
git archive --format=tar.gz --prefix="fuckwayland-$pver/" \
    -o "$top/SOURCES/fuckwayland-$pver.tar.gz" HEAD
cp "$SPEC" "$top/SPECS/fuckwayland.spec"

# --- build -------------------------------------------------------------------
rpmbuild -bb --define "_topdir $top" "$top/SPECS/fuckwayland.spec"

built=''
for f in "$top"/RPMS/noarch/*.rpm; do
    [ -e "$f" ] || continue
    cp -f "$f" dist/
    built="$built $(basename "$f")"
done
[ -n "$built" ] || { echo "build-rpm.sh: rpmbuild produced no noarch rpm" >&2; exit 1; }
echo "build-rpm.sh: built$built into dist/"

if [ "$LINT" = 1 ]; then
    if command -v rpmlint >/dev/null 2>&1; then
        # The filter file is not shipped in the payload -- an rpmlint config
        # lives in the build, not on the user's machine.
        rpmlint -r "$RPMLINTRC" dist/*.rpm || true
    else
        echo "build-rpm.sh: rpmlint is not installed (sudo dnf install rpmlint)" >&2
    fi
fi

if [ "$CLEAN" = 1 ]; then
    rm -rf "$top"
fi

echo
echo "Install them with:  sudo dnf install ./dist/fuckwayland-$pver-*.rpm \\"
echo "                        ./dist/gnome-shell-extension-fuckwayland-*.rpm"
