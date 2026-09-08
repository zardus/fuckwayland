#!/bin/bash
# vmctl golden-image builder for the NixOS flavors -- the flavors whose yaml
# carries a `# vmctl-nix:` header instead of `# vmctl-base:` or `# vmctl-iso:`.
# The third builder, and the one that boots nothing of its own.
#
# `vmctl build` cannot make these and says so: it overlays a cloud image and
# lets cloud-init dress it up, and NixOS publishes no cloud image at all -- the
# 26.05 release directory holds two ISOs, nixexprs.tar.xz and nothing else, and
# NixOS runs no cloud-init, so the seed `vmctl start` attaches is read by
# nobody in the guest [recon2/nixos §7.1].  build-iso-golden.sh cannot either:
# there is no unattended installer to answer.  What there is instead is a
# configuration: vm/nixos/<flavor>.nix, built into a qcow2 by nixpkgs' own
# in-tree image module (`image.modules.qemu`), root key and desktop and
# autologin already in it.
#
#   sh vm/build-nixos-golden.sh nixos-sway
#
# The result is a plain qcow2 with no backing file, exactly like an ISO
# golden, and from there it is an ordinary golden:
#   vmctl start <name> --flavor nixos-sway --heads 3   /   vm/selftest.sh nixos-sway
#
# TWO THINGS ARE DIFFERENT FROM THE OTHER TWO BUILDERS, and both are why this
# file exists rather than a flag on one of them:
#
#   * The root ssh key is baked in, not seeded.  vm/nixos/common.nix reads it
#     with `builtins.getEnv "VMCTL_ROOT_PUBKEY"`, which is why every nix
#     command here passes --impure.  A committed flake cannot carry a key that
#     is generated per rig host; the recon's did carry one literally, which
#     works for exactly one machine [recon2/nixos/rig/flake.nix].
#   * nixpkgs' make-disk-image runs its OWN QEMU inside the nix sandbox
#     (measured: `sh -e /nix/store/...-vm-run` was the live process while
#     nixos.raw grew under /nix/var/nix/builds/ [recon2/nixos §7.4]).  So this
#     build IS starting a virtual machine: it needs kvm in nix's
#     system-features, and on a host with the one-VM-at-a-time rule it must
#     not run beside a rig instance.  Both are checked below.
#
# usage: vm/build-nixos-golden.sh [flavor] [--force] [--no-packages]
set -eu

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
VMDATA=${VMDATA:-$HOME/vm-data}
FLAVOR=nixos-sway
FORCE=; PACKAGES=1
while [ $# -gt 0 ]; do
    case $1 in
        --force) FORCE=1 ;;
        --no-packages) PACKAGES= ;;
        -h|--help) sed -n '2,37p' "$0"; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) FLAVOR=$1 ;;
    esac
    shift
done

die() { echo "build-nixos-golden: $*" >&2; exit 1; }
t0=$(date +%s)
say() { echo "build-nixos-golden: [$(( $(date +%s) - t0 ))s] $*" >&2; }

root_pubkey() {   # the rig root key, as one line, or a refusal naming the file
    # Every other flavor gets this key through cloud-init; here it is an input
    # to the nix build, so a missing key has to stop the build rather than
    # produce a 4.9 GiB image nobody can ssh into.  vmctl and
    # build-iso-golden.sh both generate it on first use, so the fix is to run
    # one of them once -- or ssh-keygen, which is what they do.
    key=$1/keys/id_ed25519.pub
    [ -f "$key" ] || {
        msg="build-nixos-golden: no rig root key at $key"
        msg="$msg (vmctl build, vm/build-iso-golden.sh, or"
        msg="$msg \`ssh-keygen -t ed25519 -N \"\" -f ${key%.pub}\` makes one)"
        echo "$msg" >&2
        return 1
    }
    tr -d '\r\n' < "$key"
}

store_names() {   # `nix path-info -r` on stdin -> sorted package names
    # /nix/store/<hash>-<name> -> <name>.  The hash is 32 characters of nix's
    # own base32, whose alphabet is 0-9 a-z MINUS e, o, t and u -- so the
    # character class below is exact and a name that happens to start with a
    # 32-character run of letters is not eaten by it.  Sorted and uniqued
    # because a closure lists the same name at several versions and the file
    # is read by eye and by grep, not by a package manager.
    sed -e 's|^/nix/store/||' -e 's|^[0-9a-df-np-sv-z]\{32\}-||' | LC_ALL=C sort -u
}

running_instances() {   # names of rig instances with a live qemu, one per line
    # The one-VM-at-a-time rule this host has: the nix build starts a QEMU of
    # its own, and a 12 GiB image build beside a running guest is what the
    # rule exists to prevent.
    for pidfile in "$1"/instances/*/qemu.pid; do
        [ -f "$pidfile" ] || continue
        pid=$(cat "$pidfile" 2>/dev/null) || continue
        kill -0 "$pid" 2>/dev/null || continue
        d=${pidfile%/qemu.pid}; echo "${d##*/}"
    done
}

YAML=$HERE/flavors/$FLAVOR.yaml
[ -f "$YAML" ] || die "no flavor $FLAVOR ($YAML)"
hdr() { sed -n "s/^#[[:space:]]*$1:[[:space:]]*//p" "$YAML" | head -1; }
ATTR=$(hdr vmctl-nix)
[ -n "$ATTR" ] || die "$YAML has no '# vmctl-nix: <attr>' header (is this a NixOS flavor?)"

command -v nix >/dev/null || die "no nix on PATH (see vm/SETUP.md)"
case $(nix config show system-features 2>/dev/null || true) in
    *kvm*) ;;
    *) die "nix's system-features has no kvm: make-disk-image boots a VM inside" \
           "the sandbox and cannot build without it" ;;
esac
busy=$(running_instances "$VMDATA")
[ -z "$busy" ] || die "rig instance(s) running: $busy -- this build starts its own VM;" \
                      "stop them first (vmctl stop <name>)"

GOLDEN=$VMDATA/golden/$FLAVOR.qcow2
if [ -e "$GOLDEN" ] && [ -z "$FORCE" ]; then
    die "$GOLDEN exists; --force to rebuild (instances on it must then be restarted with --fresh)"
fi
VMCTL_ROOT_PUBKEY=$(root_pubkey "$VMDATA") || exit 1
export VMCTL_ROOT_PUBKEY
mkdir -p "$VMDATA/golden"

# A bare directory inside a git work tree is a git flake ref, so nix builds
# what is COMMITTED: an untracked vm/nixos/*.nix is refused by name ("is not
# tracked by Git"), and an uncommitted edit to a tracked one is ignored with a
# "Git tree is dirty" warning.  That is the right default for a golden -- an
# image nobody can reproduce is not a measurement -- but it is why editing a
# flavor and rebuilding appears to change nothing until the edit is committed.
# `path:` is not the alternative: vm/nixos/flake.nix takes the repo as
# `path:../..`, and a path: copy resolves that relative input against the
# store root and fails.
FLAKE=$HERE/nixos
say "building $FLAVOR from $FLAKE#images.$ATTR (a QEMU runs inside the nix sandbox; ~10 min cold)"
out=$(nix build --impure --no-link --print-out-paths "$FLAKE#images.$ATTR")

# The file inside $out is named for the nixpkgs label and the system, so it
# changes whenever the pin moves.  `image.filePath` is the option that says
# what it is called (nixos/modules/image/file-options.nix: "Path of the image,
# relative to $out"), and images.nix hangs it on the derivation as
# `passthru.filePath` -- not as a plain attribute, because it merges the
# attrset in with lib.recursiveUpdate and nothing hoists it.  Reading it is
# the difference between a builder that survives a channel bump and one that
# does not: against the pin in vm/nixos/flake.lock it evaluates to a name
# ending in `-26.05.20260907.93108a5-x86_64-linux.qcow2`, and the rev in the
# middle of that is the whole point.
rel=$(nix eval --impure --raw "$FLAKE#images.$ATTR.passthru.filePath")
[ -n "$rel" ] || die "$FLAKE#images.$ATTR has no filePath attribute"
[ -f "$out/$rel" ] || die "$out/$rel is not there (filePath says $rel; \`ls $out\`)"
say "image is $out/$rel ($(du -h "$out/$rel" | cut -f1))"

# Copied, not linked: nix outputs are read-only and `vmctl start` makes a qcow2
# OVERLAY on the golden, which a read-only backing file would survive -- but
# build-iso-golden.sh's golden is writable and `vmctl build --force` expects to
# be able to replace this one, so match them.
rm -f "$GOLDEN"
cp --reflink=auto "$out/$rel" "$GOLDEN"
chmod u+w "$GOLDEN"
say "golden: $GOLDEN ($(du -h "$GOLDEN" | cut -f1))"

if [ -n "$PACKAGES" ]; then
    # images.<flavor>.passthru.config, not nixosConfigurations.<flavor>.config:
    # the image module is an extendModules layer that adds the root filesystem
    # and the bootloader, so the plain configuration does not even evaluate
    # ("The 'fileSystems' option does not specify your root file system").
    # The closure wanted here is the one that is IN the image.
    top=$(nix build --impure --no-link --print-out-paths \
              "$FLAKE#images.$ATTR.passthru.config.system.build.toplevel")
    {
        echo "# nix path-info -r /run/current-system, hashes stripped and sorted --"
        echo "# the store closure of this image's system, NOT a dpkg list: these are"
        echo "# store path names (<name>-<version>), one per derivation in the"
        echo "# closure, and one Debian package can be several of them or none."
        nix path-info -r "$top" | store_names
    } > "$VMDATA/golden/$FLAVOR-packages.txt"
    say "$(( $(wc -l < "$VMDATA/golden/$FLAVOR-packages.txt") - 4 )) store paths in the closure"
fi

say "done -- vm/selftest.sh $FLAVOR"
