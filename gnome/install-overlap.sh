#!/bin/sh
# install-overlap.sh — install, enable, check or remove w11-overlap,
# the GNOME Shell extension behind `wxrandr --unsafe-gnome-overlap`.
#
#   install-overlap.sh [--system] [--no-enable]   install + enable
#   install-overlap.sh --check                    report status, and probe
#   install-overlap.sh --uninstall [--system]     disable + remove
#
# This is deliberately NOT part of install-bridge.sh.  The bridge is
# feature-detected JavaScript over public API and is meant to be installed and
# forgotten; this one ships a compiled type description pinned to the private
# layout of one libmutter generation and writes into gnome-shell's own memory.
# Two different kinds of thing, two installers, two enable steps, and nobody
# gets this one by accident.  The .deb carries the files (a route nobody can
# reach from the way almost everybody installs is not a route) and nothing that
# enables them.
#
# Exit status, the same as install-bridge.sh's: 0 when the extension is up and
# answering, 1 when it is installed and enabled but gnome-shell has not loaded
# it yet, which is every first install and is what the message then asks you to
# fix by logging out and back in.  A script that installs and then carries on
# has to expect that 1.
#
# It is safe to leave installed and enabled: the extension does nothing at
# login and nothing at all until wxrandr calls it.  --check proves that by
# asking it to run every guard and report, which writes nothing.
#
# POSIX sh, same conventions as install-bridge.sh: run it as the desktop user
# or through sudo (the user comes from $SUDO_USER / $PKEXEC_UID and every
# gsettings/gdbus call is made as that user on that user's session bus).
set -eu

UUID='w11-overlap@w11'
BUS_NAME='org.w11.Overlap'
OBJ_PATH='/org/w11/Overlap'
IFACE='org.w11.Overlap1'
SHELL_DEST='org.gnome.Shell'
SHELL_PATH='/org/gnome/Shell'
EXT_IFACE='org.gnome.Shell.Extensions'
SYSTEM_DIR='/usr/share/gnome-shell/extensions'

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/$UUID"

MODE=install
SYSTEM=0
DO_ENABLE=1

usage() {
    cat <<EOU
Usage: install-overlap.sh [--system] [--no-enable]
       install-overlap.sh --check
       install-overlap.sh --uninstall [--system]

  --system     install into $SYSTEM_DIR (via sudo)
  --no-enable  copy the files only
  --check      files, gsettings entry, extension state, $BUS_NAME, and a
               Probe: every guard run against the running libmutter, no write
  --uninstall  disable and delete the extension

Exit status of an install: 0 when the extension is up, 1 when it is installed
and waiting for the log out and back in that lets gnome-shell load it.

What this extension is for, and what it risks, is in gnome/README.md and in
docs/WXRANDR.md under "--unsafe-gnome-overlap".  Nothing else in w11
needs it: wdotool, wwmctl and wxprop need the *bridge*, which is a different
extension with a different installer.
EOU
}

while [ $# -gt 0 ]; do
    case $1 in
        --system) SYSTEM=1 ;;
        --check) MODE=check ;;
        --uninstall) MODE=uninstall ;;
        --no-enable) DO_ENABLE=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install-overlap.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

have() { command -v "$1" >/dev/null 2>&1; }

ME=$(id -u)
if [ "$ME" = 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    TARGET_USER=$SUDO_USER
elif [ "$ME" = 0 ] && [ -n "${PKEXEC_UID:-}" ] && [ "$PKEXEC_UID" != 0 ]; then
    TARGET_USER=$(id -un "$PKEXEC_UID")
else
    TARGET_USER=$(id -un)
fi
TARGET_UID=$(id -u "$TARGET_USER")
TARGET_HOME=$(getent passwd "$TARGET_USER" 2>/dev/null | cut -d: -f6 || true)
[ -n "$TARGET_HOME" ] || TARGET_HOME=$(eval echo "~$TARGET_USER")

if [ "$ME" = 0 ] && [ "$TARGET_UID" != 0 ]; then
    RUNTIME_DIR=/run/user/$TARGET_UID
    BUS_ADDR=unix:path=$RUNTIME_DIR/bus
else
    RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$TARGET_UID}
    BUS_ADDR=${DBUS_SESSION_BUS_ADDRESS:-unix:path=$RUNTIME_DIR/bus}
fi

if [ "$SYSTEM" = 1 ]; then
    DEST=$SYSTEM_DIR/$UUID
else
    DEST=$TARGET_HOME/.local/share/gnome-shell/extensions/$UUID
fi

# DEST is where an install of this shape *writes*.  FOUND is where the
# extension actually is, which is not the same thing on the machine almost
# everybody has: the .deb puts the files in $SYSTEM_DIR and enables nothing, so
# a plain --check there read "files: not installed, table: MISSING" about an
# extension gnome-shell had loaded, and a plain --uninstall printed "removed
# <a path in ~/.local that was never created>".  Both measured on a default
# 26.04 desktop that had taken the .deb.  Look in DEST first -- a user copy
# shadows the system one in gnome-shell too -- then in the system directory.
FOUND=
if [ -f "$DEST/extension.js" ]; then
    FOUND=$DEST
elif [ -f "$SYSTEM_DIR/$UUID/extension.js" ]; then
    FOUND=$SYSTEM_DIR/$UUID
fi

as_user() {
    if [ "$ME" = 0 ] && [ "$TARGET_UID" != 0 ]; then
        if have runuser; then
            runuser -u "$TARGET_USER" -- env DBUS_SESSION_BUS_ADDRESS="$BUS_ADDR" \
                XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"
        else
            sudo -u "$TARGET_USER" env DBUS_SESSION_BUS_ADDRESS="$BUS_ADDR" \
                XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"
        fi
    else
        env DBUS_SESSION_BUS_ADDRESS="$BUS_ADDR" XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"
    fi
}

gd() {
    gd_dest=$1; gd_path=$2; gd_method=$3; shift 3
    as_user gdbus call --session --dest "$gd_dest" --object-path "$gd_path" \
        --method "$gd_method" "$@"
}

shell_version() {
    have gdbus || return 1
    as_user gdbus call --session --dest "$SHELL_DEST" --object-path "$SHELL_PATH" \
        --method org.freedesktop.DBus.Properties.Get "$SHELL_DEST" ShellVersion \
        2>/dev/null | sed -n "s/^(<'\([^']*\)'>,)$/\1/p"
}

name_owned() {
    have gdbus || return 1
    gd org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner \
        "$BUS_NAME" 2>/dev/null | grep -q true
}

ext_loaded() {
    have gdbus || return 1
    gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.ListExtensions" 2>/dev/null | grep -q "'$UUID'"
}

ext_state() {
    have gdbus || { echo '-'; return; }
    gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.GetExtensionInfo" "$UUID" 2>/dev/null \
        | sed -n "s/.*'state': <\([0-9]*\)\(\.[0-9]*\)\{0,1\}>.*/\1/p" | head -n 1
}

setting_has() {
    have gsettings || return 1
    as_user gsettings get org.gnome.shell "$1" 2>/dev/null | grep -q "'$UUID'"
}

enable_setting() {
    if have gnome-extensions; then
        as_user gnome-extensions enable "$UUID" >/dev/null 2>&1 && setting_has enabled-extensions && return 0
    fi
    have gsettings || {
        echo "install-overlap.sh: add $UUID to org.gnome.shell enabled-extensions yourself" >&2
        return 1
    }
    cur=$(as_user gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo '[]')
    case $cur in
        *"'$UUID'"*) ;;
        '@as []'|'[]') as_user gsettings set org.gnome.shell enabled-extensions "['$UUID']" ;;
        *) as_user gsettings set org.gnome.shell enabled-extensions "${cur%]}, '$UUID']" ;;
    esac
    # disabled-extensions wins over enabled-extensions in gnome-shell, so a uuid
    # left in both lists is an extension that never loads -- the state anybody
    # who ran `--uninstall` (or the disable button) and then reinstalled is in.
    # install-bridge.sh has cleared it since 0.3; this one did not.
    cur=$(as_user gsettings get org.gnome.shell disabled-extensions 2>/dev/null || echo '[]')
    case $cur in
        *"'$UUID'"*)
            new=$(printf '%s' "$cur" | sed -e "s/, *'$UUID'//" -e "s/'$UUID', *//" -e "s/'$UUID'//")
            as_user gsettings set org.gnome.shell disabled-extensions "$new" ;;
    esac
}

disable_setting() {
    if have gnome-extensions; then
        as_user gnome-extensions disable "$UUID" >/dev/null 2>&1 && return 0
    fi
    have gsettings || return 0
    cur=$(as_user gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo '[]')
    case $cur in
        *"'$UUID'"*)
            new=$(printf '%s' "$cur" | sed -e "s/, *'$UUID'//" -e "s/'$UUID', *//" -e "s/'$UUID'//")
            as_user gsettings set org.gnome.shell enabled-extensions "$new" ;;
    esac
}

copy_files() {
    if [ "$SYSTEM" = 1 ] && [ "$ME" != 0 ]; then
        if [ "$DO_ENABLE" = 0 ]; then
            exec sudo -- "$0" --system --no-enable
        fi
        exec sudo -- "$0" --system
    fi
    mkdir -p "$DEST/typelib"
    cp -f "$SRC/metadata.json" "$SRC/extension.js" "$SRC/rules.js" \
          "$SRC/generations.json" "$SRC/org.w11.Overlap1.xml" "$DEST/"
    # Typelibs are replaced by RENAME, never written in place.  gjs maps a
    # typelib into memory and keeps the mapping for the life of the process, so
    # rewriting the bytes of one a running gnome-shell has already loaded
    # changes the blob under it and the next call through the description aborts
    # the process -- which on Wayland is the session.  Measured on GNOME 51,
    # reinstalling over a session that had used the extension once.  A rename
    # leaves the old inode mapped and the running shell keeps working until the
    # next login, which is when it was going to read the new files anyway.
    for tl in "$SRC"/typelib/*.typelib; do
        cp -f "$tl" "$DEST/typelib/.$(basename "$tl").new"
        mv -f "$DEST/typelib/.$(basename "$tl").new" "$DEST/typelib/$(basename "$tl")"
    done
    if [ "$SYSTEM" = 0 ] && [ "$ME" = 0 ] && [ "$TARGET_UID" != 0 ]; then
        chown -R "$TARGET_USER" "$TARGET_HOME/.local/share/gnome-shell" 2>/dev/null || true
    fi
    echo "install-overlap.sh: installed into $DEST"
}

# gnome-shell will not load an extension whose metadata.json does not name the
# running Shell major, and metadata.json is generated from the table -- so on a
# GNOME nobody here has measured the extension is installed, enabled, and
# OUT_OF_DATE, the bus name is never taken, and `wxrandr
# --unsafe-gnome-overlap-unmeasured`, which exists for exactly that machine,
# cannot reach anything to force.  So: name the running major in the INSTALLED
# copy, never in the tree, and say so.
#
# This does not make the extension act on an unmeasured build.  Loading it and
# writing through it are two different things: it comes up idle, and its own
# `shell-version` check refuses every call on a build that is not in the table
# unless that call carries --unsafe-gnome-overlap-unmeasured with this machine's
# GNOME major in it.  What this buys is the honest refusal -- with the libmutter,
# the Meta typelib and the MetaMonitorsConfig size a maintainer needs, measured
# on the machine in front of them -- instead of "the extension is not running".
name_this_shell() {
    nts_major=${1%%.*}
    nts_file="$DEST/metadata.json"
    [ -f "$nts_file" ] || return 0
    # awk rather than sed, and over lines rather than over one line: gen-gir.py
    # writes the array on one line while it is short and over several once it is
    # not, and a rule that only understood one of those would quietly do nothing
    # on the day a third generation was added.  Insert before the first "]"
    # after the "shell-version" key, whichever line it is on.
    awk -v maj="$nts_major" '
        BEGIN { seen = 0; done = 0 }
        index($0, "\"shell-version\"") { seen = 1 }
        {
            if (seen && !done && index($0, "]")) {
                p = index($0, "]")
                if (index($0, "\"" maj "\"")) { done = 1; print; next }
                printf "%s, \"%s\"%s\n", substr($0, 1, p - 1), maj, substr($0, p)
                done = 1
                next
            }
            if (seen && !done && index($0, "\"" maj "\"")) { done = 1; seen = 0 }
            print
        }' "$nts_file" > "$nts_file.new" || { rm -f "$nts_file.new"; return 0; }
    # Only replace it, and only say so, if the file really gained the version.
    if cmp -s "$nts_file" "$nts_file.new"; then
        rm -f "$nts_file.new"
        return 0
    fi
    mv -f "$nts_file.new" "$nts_file"
    if [ "$SYSTEM" = 0 ] && [ "$ME" = 0 ] && [ "$TARGET_UID" != 0 ]; then
        chown "$TARGET_USER" "$nts_file" 2>/dev/null || true
    fi
    echo "install-overlap.sh: added \"$nts_major\" to the shell-version list of the" \
         "INSTALLED copy of metadata.json (the repository's copy is generated from" \
         "the table and is untouched), because gnome-shell will not load an" \
         "extension that does not name the running Shell major -- and an extension" \
         "it does not load cannot even tell you what your build is.  It comes up" \
         "idle and still refuses every call on this GNOME unless the call carries" \
         "wxrandr --unsafe-gnome-overlap-unmeasured $nts_major." >&2
}

# Everything version-specific comes out of the table, so this script has no
# list of its own to fall out of step with it.  Two greps over one file, which
# this project generates and keeps one record per line: the namespaces (one
# typelib each must be present) and the GNOME majors (which shells this has been
# measured on).
TABLE="$SRC/generations.json"

table_field() {
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",]*\).*/\1/p" "$TABLE"
}

case $MODE in
install)
    [ -d "$SRC" ] || { echo "install-overlap.sh: $SRC is missing" >&2; exit 1; }
    [ -f "$TABLE" ] || { echo "install-overlap.sh: $TABLE is missing" >&2; exit 1; }
    for ns in $(table_field namespace); do
        t="$SRC/typelib/$ns-1.0.typelib"
        [ -f "$t" ] || {
            echo "install-overlap.sh: $t is missing; run" \
                 "python3 gnome/overlap-typelib/gen-gir.py" >&2
            exit 1
        }
    done
    MAJORS=$(table_field shell_major | tr '\n' ' ')
    v=$(shell_version || true)
    if [ -z "${v:-}" ]; then
        echo "install-overlap.sh: no running GNOME Shell to ask; installing anyway" >&2
    else
        known=no
        for m in $MAJORS; do
            case ${v:-} in "$m"|"$m".*) known=yes ;; esac
        done
        [ "$known" = yes ] || \
            echo "install-overlap.sh: GNOME Shell $v: this extension has been" \
                 "measured on ${MAJORS% } only and refuses to write on anything" \
                 "else.  Installing it is harmless -- it does nothing until it is" \
                 "called -- but wxrandr --unsafe-gnome-overlap will say this" \
                 "compositor is not measured, and will print what a maintainer" \
                 "needs to add it." >&2
    fi
    copy_files
    [ -n "${v:-}" ] && [ "${known:-yes}" = no ] && name_this_shell "$v"
    if [ "$DO_ENABLE" = 1 ]; then
        enable_setting || true
        if ext_loaded; then
            gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.EnableExtension" "$UUID" >/dev/null 2>&1 || true
        fi
    fi
    if name_owned; then
        echo "install-overlap.sh: $BUS_NAME is up"
        exit 0
    fi
    cat >&2 <<'EOM'
install-overlap.sh: log out and back in once.  gnome-shell scans extension
directories only at login, and on Wayland it cannot be restarted in place.
The extension is already enabled, so it comes up by itself after the re-login
-- and comes up idle: it does nothing until wxrandr calls it.
(The files are installed: this exit status of 1 says the bus name is not up
yet, and after the re-login the same command exits 0.)
EOM
    exit 1
    ;;
check)
    echo "uuid:         $UUID"
    if [ -n "$FOUND" ] && [ "$FOUND" != "$DEST" ]; then
        echo "files:        $FOUND (the .deb's copy; this script installs into $DEST)"
    else
        echo "files:        ${FOUND:-not installed}"
    fi
    echo "table:        $([ -n "$FOUND" ] && [ -f "$FOUND/generations.json" ] && echo installed || echo MISSING)"
    echo "typelibs:     $([ -n "$FOUND" ] && ls "$FOUND/typelib" 2>/dev/null | tr '\n' ' ' || echo none)"
    echo "shell:        $(shell_version || echo unknown)"
    echo "enabled:      $(setting_has enabled-extensions && echo yes || echo no)"
    echo "loaded:       $(ext_loaded && echo yes || echo 'no (log out and back in)')"
    echo "state:        $(ext_state)   (1 ACTIVE 2 INACTIVE 3 ERROR 4 OUT_OF_DATE 6 INITIALIZED)"
    [ "$(ext_state)" = 4 ] && cat >&2 <<'EOM'
install-overlap.sh: OUT_OF_DATE means gnome-shell will not load it: the
installed metadata.json does not name this Shell major, so nothing here is
running and wxrandr will say the extension is not on the bus.  Run
install-overlap.sh again (no arguments) and log in again -- it names the
running major in the installed copy.  The extension still refuses every call
on a GNOME that is not in the table unless the call forces it.
EOM
    echo "$BUS_NAME: $(name_owned && echo owned || echo 'not owned')"
    if name_owned; then
        echo "probe:"
        gd "$BUS_NAME" "$OBJ_PATH" "$IFACE.Probe" '{}' 2>&1 | sed 's/^/  /'
    fi
    ;;
uninstall)
    disable_setting || true
    if ext_loaded; then
        gd "$SHELL_DEST" "$SHELL_PATH" "$EXT_IFACE.DisableExtension" "$UUID" >/dev/null 2>&1 || true
    fi
    if [ "$SYSTEM" = 1 ] && [ "$ME" != 0 ]; then
        exec sudo -- "$0" --system --uninstall
    fi
    if [ -e "$DEST" ]; then
        rm -rf "$DEST"
        echo "install-overlap.sh: removed $DEST"
    else
        echo "install-overlap.sh: nothing of this script's to remove at $DEST"
    fi
    # Disabled either way, which is the half that matters, but the .deb's files
    # are dpkg's and this script does not delete another package's payload.
    if [ "$SYSTEM" = 0 ] && [ -f "$SYSTEM_DIR/$UUID/extension.js" ]; then
        echo "install-overlap.sh: the extension is disabled, and its files are still" \
             "in $SYSTEM_DIR/$UUID, where the w11 package put them."
        echo "install-overlap.sh: to take the files too: sudo apt remove w11" \
             "(or, to delete just this extension, install-overlap.sh --system --uninstall)."
    fi
    ;;
esac
