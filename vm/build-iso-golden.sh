#!/bin/bash
# vmctl golden-image builder for the ISO flavors -- the flavors whose yaml carries a
# `# vmctl-iso:` header instead of a `# vmctl-base:` one.  `vmctl build` cannot make
# these: it overlays a cloud image and lets cloud-init dress it up, while this runs the
# real Ubuntu desktop installer off the release ISO and keeps what it produces.
#
# Two stages, both headless, no keyboard and no display needed anywhere:
#   1. INSTALL.  Boot the ISO's own kernel and initrd (isoinfo -x, no remastering) with
#      `autoinstall` on the kernel command line, the ISO attached as a CD-ROM and the
#      flavor yaml as a NoCloud seed.  subiquity finds the description in the seed
#      (cloud-config is source 4 of the 5 its select_autoinstall() looks at) and the
#      kernel argument is what stops the DESKTOP installer from parking on its "Ready to
#      install / Review your choices" page: with the config alone it renders the answers
#      and waits for a human to confirm them, forever, on a VM nobody can click in.
#      The installer powers the VM off when it is done (~12 min).
#   2. CONFIGURE.  Boot the installed disk once (no seed, no CD-ROM) and run
#      vm/build-iso-image.sh in it over ssh: update, GDM autologin, cloud-init back on.
#      Then power it off and keep the disk as $VMDATA/golden/<flavor>.qcow2.
# The result is a plain qcow2 with no backing file, so unlike the other goldens it does
# not depend on anything in $VMIMAGES afterwards.  From here on it is an ordinary golden:
#   vmctl start <name> --flavor <flavor> --heads 3   /   vm/selftest.sh <flavor>
#
# usage: vm/build-iso-golden.sh [flavor] [--force] [--keep] [--size 30G] [--mem 4G]
#                               [--cpus 2] [--timeout 3600] [--from-stage2]
# --from-stage2 skips the install and re-runs stage 2 on the disk a previous --keep run
# left in $VMDATA/build/<flavor>/ -- stage 2 is idempotent, stage 1 takes 12 minutes.
set -eu

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
VMDATA=${VMDATA:-$HOME/vm-data}
VMIMAGES=${VMIMAGES:-$HOME/images}
FLAVOR=resolute-gnome-iso
FORCE=; KEEP=; SIZE=30G; MEM=4G; CPUS=2; TIMEOUT=3600; STAGE2=
while [ $# -gt 0 ]; do
    case $1 in
        --force) FORCE=1 ;;
        --keep) KEEP=1 ;;
        --from-stage2) STAGE2=1; KEEP=1 ;;
        --size) SIZE=$2; shift ;;
        --mem) MEM=$2; shift ;;
        --cpus) CPUS=$2; shift ;;
        --timeout) TIMEOUT=$2; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 1 ;;
        *) FLAVOR=$1 ;;
    esac
    shift
done

die() { echo "build-iso-golden: $*" >&2; exit 1; }
t0=$(date +%s)
say() { echo "build-iso-golden: [$(( $(date +%s) - t0 ))s] $*" >&2; }

YAML=$HERE/flavors/$FLAVOR.yaml
[ -f "$YAML" ] || die "no flavor $FLAVOR ($YAML)"
hdr() { sed -n "s/^#[[:space:]]*$1:[[:space:]]*//p" "$YAML" | head -1; }
ISO_NAME=$(hdr vmctl-iso)
ISO_SHA=$(hdr vmctl-iso-sha256)
ISO_URL=$(hdr vmctl-iso-url)
DESKTOP=$(hdr vmctl-desktop); DESKTOP=${DESKTOP:-gnome}
[ -n "$ISO_NAME" ] || die "$YAML has no '# vmctl-iso: <image.iso>' header (is this an ISO flavor?)"
ISO=$VMIMAGES/$ISO_NAME
[ -f "$ISO" ] || die "$ISO not found; download it from ${ISO_URL:-releases.ubuntu.com} into \$VMIMAGES"
for t in qemu-system-x86_64 qemu-img cloud-localds isoinfo ssh scp ssh-keygen; do
    command -v "$t" >/dev/null || die "missing host tool: $t (isoinfo is in genisoimage)"
done

GOLDEN=$VMDATA/golden/$FLAVOR.qcow2
if [ -e "$GOLDEN" ] && [ -z "$FORCE" ]; then
    die "$GOLDEN exists; --force to rebuild (instances on it must then be restarted with --fresh)"
fi
KEY=$VMDATA/keys/id_ed25519
if [ ! -f "$KEY" ]; then
    mkdir -p "$VMDATA/keys"; chmod 700 "$VMDATA/keys"
    ssh-keygen -q -t ed25519 -N "" -C vmctl -f "$KEY"
    say "generated the rig root key $KEY"
fi
B=$VMDATA/build/$FLAVOR
if [ -d "$B" ] && [ -f "$B/qemu.pid" ] && kill -0 "$(cat "$B/qemu.pid")" 2>/dev/null; then
    die "a build of $FLAVOR is already running (pid $(cat "$B/qemu.pid"), $B)"
fi
if [ -n "$STAGE2" ]; then
    [ -f "$B/disk.qcow2" ] || die "--from-stage2 needs an installed disk in $B (run it once without --from-stage2, with --keep)"
else
    rm -rf "$B"
fi
mkdir -p "$B" "$VMDATA/golden"

if [ -n "$ISO_SHA" ] && [ -z "$STAGE2" ]; then
    say "verifying $ISO_NAME ($(du -h "$ISO" | cut -f1))"
    got=$(sha256sum "$ISO" | cut -d' ' -f1)
    [ "$got" = "$ISO_SHA" ] || die "sha256 mismatch on $ISO: got $got, flavor says $ISO_SHA"
    say "sha256 ok"
fi

# A free host port for the guest's sshd, outside vmctl's own 2400-2499 range so a build
# can never take an instance's port.
PORT=$(python3 - <<'PY'
import socket
for p in range(2500, 2600):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); print(p); break
    except OSError:
        pass
    finally:
        s.close()
PY
)
[ -n "$PORT" ] || die "no free port in 2500-2599"

SSH="ssh -p $PORT -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
     -o LogLevel=ERROR -o ConnectTimeout=5 -o BatchMode=yes test@127.0.0.1"
SCP="scp -P $PORT -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
     -o LogLevel=ERROR -o BatchMode=yes"

# ---------------------------------------------------------------- stage 1: install
if [ -n "$STAGE2" ]; then
say "--from-stage2: reusing the installed disk in $B ($(du -h "$B/disk.qcow2" | cut -f1)); skipping the install"
else
say "stage 1: unattended install of $FLAVOR from $ISO_NAME ($CPUS vCPU/$MEM, $SIZE disk, ssh port $PORT)"
sed "s|@@ROOT_PUBKEY@@|$(cat "$KEY.pub")|" "$YAML" > "$B/user-data"
printf 'instance-id: %s-install\nlocal-hostname: %s\n' "$FLAVOR" "$FLAVOR" > "$B/meta-data"
cloud-localds "$B/seed.img" "$B/user-data" "$B/meta-data"
# The ISO's own kernel and initrd, read out of the image without root or a remaster.
isoinfo -R -i "$ISO" -x /casper/vmlinuz > "$B/vmlinuz"
isoinfo -R -i "$ISO" -x /casper/initrd  > "$B/initrd"
if [ ! -s "$B/vmlinuz" ] || [ ! -s "$B/initrd" ]; then die "could not extract /casper/{vmlinuz,initrd}"; fi
say "extracted the ISO kernel ($(du -h "$B/vmlinuz" | cut -f1)) and initrd ($(du -h "$B/initrd" | cut -f1))"
qemu-img create -q -f qcow2 "$B/disk.qcow2" "$SIZE"

qemu-system-x86_64 -name "isobuild-$FLAVOR" -enable-kvm -cpu host \
    -m "$MEM" -smp "$CPUS" -daemonize -pidfile "$B/qemu.pid" \
    -drive file="$B/disk.qcow2",if=virtio,format=qcow2 \
    -drive file="$B/seed.img",if=virtio,format=raw,readonly=on \
    -drive file="$ISO",media=cdrom,readonly=on \
    -device virtio-rng-pci \
    -netdev user,id=n0,hostfwd=tcp:127.0.0.1:$PORT-:22 \
    -device virtio-net-pci,netdev=n0 \
    -device virtio-vga,id=gpu0,max_outputs=4,edid=on \
    -display none \
    -serial file:"$B/install-serial.log" \
    -kernel "$B/vmlinuz" -initrd "$B/initrd" \
    -append "boot=casper autoinstall console=ttyS0,115200"
pid=$(cat "$B/qemu.pid")
say "installer running (QEMU pid $pid); serial log $B/install-serial.log"
# `autoinstall` on the command line is what makes the desktop installer non-interactive:
# subiquity-server then runs with interactive=False and installs on its own.
# Progress: the serial console goes quiet as soon as curtin starts unpacking (subiquity
# logs to the journal inside the live session, not to ttyS0), so what the installer is
# doing is best seen as the disk filling up.
while kill -0 "$pid" 2>/dev/null; do
    sleep 60
    say "  installing: $(du -m "$B/disk.qcow2" | cut -f1) MB written"
    if [ $(( $(date +%s) - t0 )) -gt "$TIMEOUT" ]; then
        kill "$pid" 2>/dev/null || true
        die "stage 1 timed out after ${TIMEOUT}s; see $B/install-serial.log"
    fi
done
rm -f "$B/qemu.pid"
qemu-img info "$B/disk.qcow2" | grep -q 'disk size' || die "no disk image after stage 1"
usedmb=$(du -m "$B/disk.qcow2" | cut -f1)
[ "$usedmb" -gt 2000 ] || die "stage 1 wrote almost nothing (${usedmb}M): the installer never installed; see $B/install-serial.log"
say "stage 1 done: installed system is $(du -h "$B/disk.qcow2" | cut -f1); installer log tail:"
sed 's/\r//g' "$B/install-serial.log" | tail -3 | sed 's/^/    /' >&2
fi

# ---------------------------------------------------------------- stage 2: configure
say "stage 2: booting the installed system to configure it for the rig"
rm -f "$B/boot-serial.log" "$B/qmp.sock"
qemu-system-x86_64 -name "isobuild2-$FLAVOR" -enable-kvm -cpu host \
    -m "$MEM" -smp "$CPUS" -daemonize -pidfile "$B/qemu.pid" \
    -drive file="$B/disk.qcow2",if=virtio,format=qcow2 \
    -device virtio-rng-pci \
    -netdev user,id=n0,hostfwd=tcp:127.0.0.1:$PORT-:22 \
    -device virtio-net-pci,netdev=n0 \
    -device virtio-vga,id=gpu0,max_outputs=4,edid=on \
    -display none \
    -qmp unix:"$B/qmp.sock",server,nowait \
    -serial file:"$B/boot-serial.log"
pid=$(cat "$B/qemu.pid")
# On any failure below, ask the guest to power down over QMP before killing QEMU: a VM
# cut off mid-boot loses whatever was not yet on disk (the ssh host keys the target
# generates on its first boot, for one) and leaves GRUB's recordfail set, so the next
# boot stops at the menu.  30 s of patience is cheaper than a 12-minute reinstall.
qmp() {   # qmp <json-command>
    python3 - "$B/qmp.sock" "$1" <<'PY' 2>/dev/null || true
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect(sys.argv[1])
f = s.makefile("rwb")
f.readline()                                   # QMP greeting
for cmd in ('{"execute": "qmp_capabilities"}', sys.argv[2]):
    f.write((cmd + "\n").encode()); f.flush(); f.readline()
PY
}
cleanup() {
    [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null || return 0
    say "shutting the build VM down (QMP system_powerdown)"
    qmp '{"execute": "system_powerdown"}'
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || return 0; sleep 1; done
    kill "$pid" 2>/dev/null || true
}
trap cleanup EXIT
# GRUB's recordfail: after a boot that did not finish -- a killed build VM, a host reboot --
# Ubuntu's GRUB shows its menu and waits, and a VM with no keyboard waits with it forever.
# Nothing reaches the guest's serial console while that happens, which is how it is spotted
# here; QEMU's QMP send-key is the one keystroke this rig can produce.
qmp_ret() { qmp '{"execute": "send-key", "arguments": {"keys": [{"type": "qcode", "data": "ret"}]}}'; }
for i in $(seq 1 90); do
    $SSH true 2>/dev/null && break
    case $i in 4|10|20|30)
        if [ ! -s "$B/boot-serial.log" ]; then
            say "  nothing on the guest console after $(( i * 5 ))s: pressing Return over QMP (GRUB waits at its menu after an unclean shutdown)"
            qmp_ret
        fi ;;
    esac
    [ "$i" = 90 ] && die "no ssh on port $PORT after 450s; see $B/boot-serial.log"
    sleep 5
done
say "ssh is up (user test, the account the installer created)"
printf 'FLAVOR=%s\nDESKTOP=%s\n' "$FLAVOR" "$DESKTOP" > "$B/vmctl-build.env"
$SCP "$B/vmctl-build.env" "$HERE/build-iso-image.sh" "test@[127.0.0.1]:/tmp/" >/dev/null
# The installer's user is in the sudo group with a password, exactly as on a default
# install; no NOPASSWD rule is added anywhere, so the password goes in over stdin here.
$SSH 'sudo -S -p "" install -m 0644 /tmp/vmctl-build.env /etc/vmctl-build.env' <<<test
set +e
$SSH 'sudo -S -p "" bash /tmp/build-iso-image.sh' <<<test 2>&1 | tee "$B/stage2.log" | sed 's/^/    /' >&2
# PIPESTATUS[0], not $?: $? here is sed's, which is 0 whatever ssh did, so a stage 2
# that died -- ssh dropped, sudo refused the password, the guest rebooted under it --
# was reported as "rc 0" in the die message below and the real status was lost.
rc=${PIPESTATUS[0]}
set -e
grep -q VMCTL-BUILD-OK "$B/stage2.log" || die "stage 2 did not finish (rc $rc); see $B/stage2.log"
# The guest's ERR trap can fire inside a command substitution, where its exit only ends the
# subshell -- the script then carries on to VMCTL-BUILD-OK.  Treat any FAIL marker as a failure.
if grep -q VMCTL-BUILD-FAIL "$B/stage2.log"; then die "stage 2 reported a failure; see $B/stage2.log"; fi
$SCP "test@[127.0.0.1]:/var/lib/vmctl/packages.txt" "$B/packages.txt" >/dev/null
$SCP "test@[127.0.0.1]:/var/lib/vmctl/snaps.txt" "$B/snaps.txt" >/dev/null
n=$(wc -l < "$B/packages.txt")
[ "$n" -gt 800 ] || die "package list looks wrong ($n entries)"
say "powering the VM off"
$SSH 'sudo -S -p "" systemctl poweroff' <<<test 2>/dev/null || true
for i in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 5; done
if kill -0 "$pid" 2>/dev/null; then say "guest did not power off in 300s; killing QEMU"; kill "$pid"; sleep 3; fi
trap - EXIT
rm -f "$B/qemu.pid"

# ---------------------------------------------------------------- keep it
mv -f "$B/disk.qcow2" "$GOLDEN"
cp -f "$B/packages.txt" "$VMDATA/golden/$FLAVOR-packages.txt"
cp -f "$B/snaps.txt" "$VMDATA/golden/$FLAVOR-snaps.txt"
cat "$B/install-serial.log" "$B/stage2.log" > "$VMDATA/golden/$FLAVOR.build.log" 2>/dev/null || true
[ -n "$KEEP" ] || rm -rf "$B"
say "done in $(( ($(date +%s) - t0) / 60 )) min -> $GOLDEN ($(du -h "$GOLDEN" | cut -f1), $n packages, $(wc -l < "$VMDATA/golden/$FLAVOR-snaps.txt") snaps)"
say "next: vm/selftest.sh $FLAVOR"
echo "$GOLDEN"
