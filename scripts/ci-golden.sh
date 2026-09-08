#!/bin/bash
# The golden image of one flavor, from the cache or built here: what the VM jobs of
# .github/workflows/ci.yml run before vm/live-smoke.sh.
#
#     scripts/ci-golden.sh <flavor>
#
# A golden is keyed by the sha256 of everything that goes into building it (the flavor
# yaml, vm/vmctl, the three build scripts, and vm/golden-epoch, a file to bump by hand
# when the upstream cloud image or ISO has moved under an unchanged recipe) and kept as
# an OCI artifact at ghcr.io/<owner>/fuckwayland-golden/<flavor>:<key>, pushed with oras.
# On a hit it is pulled into $VMDATA/golden/<flavor>.qcow2 in about a minute; on a miss
# the base image or ISO is downloaded into $VMIMAGES, the golden is built the way
# vm/README.md says (vmctl build, or build-iso-golden.sh for an ISO flavor), flattened
# and compressed into a standalone qcow2 (no backing file, so the cache does not depend
# on a path), and pushed for the next run.
#
#   GOLDEN_REPO   the OCI repository prefix (default ghcr.io/<GITHUB_REPOSITORY_OWNER>/fuckwayland-golden)
#   GOLDEN_PUSH   set to 0 to never push (default 1 when a registry login exists)
#   VMDATA, VMIMAGES   as vm/vmctl reads them
set -eu
flavor=${1:?usage: scripts/ci-golden.sh <flavor>}
cd "$(dirname "$0")/.."
VMDATA=${VMDATA:-$HOME/vm-data}; VMIMAGES=${VMIMAGES:-$HOME/images}
export VMDATA VMIMAGES
yaml=vm/flavors/$flavor.yaml; [ -f "$yaml" ] || { echo "no flavor $flavor" >&2; exit 2; }
owner=$(printf '%s' "${GITHUB_REPOSITORY_OWNER:-$(git config --get remote.origin.url | sed 's|.*[:/]\([^/]*\)/[^/]*$|\1|')}" | tr 'A-Z' 'a-z')
repo=${GOLDEN_REPO:-ghcr.io/$owner/fuckwayland-golden}
key=$(cat "$yaml" vm/vmctl vm/build-image.sh vm/build-iso-golden.sh vm/build-iso-image.sh vm/golden-epoch | sha256sum | cut -c1-16)
ref=$repo/$flavor:$key
golden=$VMDATA/golden/$flavor.qcow2
mkdir -p "$VMDATA/golden" "$VMIMAGES"
say() { printf '\nci-golden %s: %s\n' "$flavor" "$*"; }

if [ -f "$golden" ]; then say "already at $golden"; exit 0; fi
if command -v oras >/dev/null && oras manifest fetch "$ref" >/dev/null 2>&1; then
    say "cache hit $ref"
    ( cd "$VMDATA/golden" && oras pull "$ref" >/dev/null )
    [ -f "$golden" ] || { echo "pulled $ref but $golden is not there" >&2; ls -la "$VMDATA/golden" >&2; exit 1; }
    qemu-img info "$golden" | sed 's/^/    /'; exit 0
fi
say "cache miss $ref: building"

# -- the base image or the ISO, from Ubuntu -------------------------------------------
fetch() {   # <file> -> $VMIMAGES/<file>
    local f=$1 url
    [ -s "$VMIMAGES/$f" ] && return 0
    case $f in
        noble-server-cloudimg-amd64.img)        url=https://cloud-images.ubuntu.com/noble/current/$f ;;
        ubuntu-26.04-server-cloudimg-amd64.img) url=https://cloud-images.ubuntu.com/releases/26.04/release/$f ;;
        stonking-server-cloudimg-amd64.img)     url=https://cloud-images.ubuntu.com/stonking/current/$f ;;
        ubuntu-24.04.*-desktop-amd64.iso)       url=https://releases.ubuntu.com/24.04/$f ;;
        ubuntu-26.04.*-desktop-amd64.iso)       url=https://releases.ubuntu.com/26.04/$f ;;
        *) echo "ci-golden: no download rule for $f" >&2; exit 2 ;;
    esac
    say "downloading $url"
    curl -fL --retry 3 -o "$VMIMAGES/$f.part" "$url" && mv "$VMIMAGES/$f.part" "$VMIMAGES/$f"
}
iso=$(sed -n 's/^#[[:space:]]*vmctl-iso:[[:space:]]*//p' "$yaml" | head -1)
base=$(sed -n 's/^#[[:space:]]*vmctl-base:[[:space:]]*//p' "$yaml" | head -1)
t0=$(date +%s)
if [ -n "$iso" ]; then
    fetch "$iso"; sh vm/build-iso-golden.sh "$flavor" --mem 4G
else
    fetch "$base"; vm/vmctl build "$flavor" --cpus 2 --mem 4G
fi
say "built in $(( ($(date +%s) - t0) / 60 )) min"

# -- flatten + compress, so the cached file stands alone ------------------------------
tmp=$golden.standalone; qemu-img convert -c -O qcow2 "$golden" "$tmp" && mv "$tmp" "$golden"
qemu-img info "$golden" | sed 's/^/    /'

if [ "${GOLDEN_PUSH:-1}" = 1 ] && command -v oras >/dev/null; then
    say "pushing $ref"
    ( cd "$VMDATA/golden" && oras push "$ref" --artifact-type application/vnd.fuckwayland.golden \
        "$flavor.qcow2:application/vnd.qemu.qcow2" ) || say "push failed (kept the local golden)"
fi
