# tests/fixtures/live — bytes off the real desktops on the QEMU rig

Captures taken with `vm/live-smoke.d/guest-capture.sh` and
`guest-capture-winwm.sh` inside a `vmctl` guest, in the transcript format the
smoke's stand-in reads:

    ### <the exact command>
    <its output, byte for byte>
    ### rc=<exit status>

Nothing in here is written by hand. They are the recorded half of
`vm/live-smoke.sh`: `vm/live-smoke.d/fake-vmctl` replays one of them so that the
smoke's own checks can be run — and shown to fail — without booting anything,
which `vm/live-smoke.d/selftest-offline.sh` does in about two seconds.

### `noble-gnome-46.0-capture.txt`

From instance `gnome46` (noble-gnome golden, GNOME Shell 46.0 on Ubuntu
24.04.4, `fuckwayland 0.4.0` out of `release/`, three heads), 2026-09-08 02:34
UTC. It holds the shapes every GNOME step asserts against: `getwindowgeometry`
before and after the maximize pair, `wwmctl -l` / `-l -G` / `-d`, `wxprop -id`
and `-root`, `wxrandr --listmonitors` / `--query` / `--backends` /
`--print-backend --verbose`, Mutter's `Logical monitors not adjacent` refusal
with the re-place hint, `--gnome-overlap-status`, and the six `overlap check`
lines of a `--dryrun --unsafe-gnome-overlap`.

### `noble-gnome-46.0-windows-wm-replay.txt`

The same instance, 2026-09-08 02:38 UTC: the `windows` and `wm` phases'
commands **in the order the phases ask them**, which is what `fake-vmctl`
needs. `wdotool getwindowgeometry` is asked four times across the two phases
and has to answer `100,100 800x600`, `100,100 800x600`, the maximized
`66,32 1854x1048`, and `100,100 800x600` again — the last of those is b7a60f0.

Two facts worth reading straight out of the first file, because prose elsewhere
in the tree paraphrases them: `WM_CLASS` on the GNOME text editor is
`org.gnome.TextEditor` (so `wdotool search --class gnome-text-editor` matching
nothing is X11 behaviour, not a bug), and `/dev/uinput` reads `root root 660`
while the package is installed — the mode is the ACL mask, the `user:test:rw-`
entry underneath is what udev's `uaccess` tag put there.

The KWin and sway equivalents are not here yet: each needs its own golden
booted, and on this host that is one VM at a time (`vm/live-smoke.d/guest-capture.sh
kde` / `sway` produces them).
