#!/bin/sh
# Append the running GNOME Shell major to the INSTALLED bridge metadata.json.
#
# On stonking-gnome (GNOME Shell 51.beta) the shipped extension declares
# shell-version ["45".."50"], so the shell marks it OUT OF DATE and every window
# tool prints "the fuckwayland bridge extension is marked out of date for this
# GNOME Shell"; with "51" appended and one reboot the bridge is ACTIVE and every
# window/desktop/input/display operation works (measured 2026-09-08, fresh
# instance, package route, nothing else touched).
#
# It edits /usr/share/gnome-shell/extensions/<uuid>/metadata.json -- the copy the
# .deb installed, inside a throw-away VM.  The repo's gnome/ is never touched by
# anything in this directory: that decision is not a smoke run's to make.
set -eu
M=/usr/share/gnome-shell/extensions/fuckwayland-bridge@fuckwayland/metadata.json
[ -f "$M" ] || { echo "no $M (is the package installed?)" >&2; exit 2; }
python3 - "$M" "${1:?usage: guest-gnome-meta51.sh <shell-major>}" <<'PY'
import json
import sys

path, major = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    meta = json.load(fh)
versions = meta.get("shell-version") or []
if major in versions:
    print("already there: %s" % " ".join(versions))
    raise SystemExit(0)
versions.append(major)
meta["shell-version"] = versions
with open(path, "w", encoding="utf-8") as fh:
    json.dump(meta, fh, indent=2)
    fh.write("\n")
print("added %s: %s" % (major, " ".join(versions)))
PY
