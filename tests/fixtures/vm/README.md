# tests/fixtures/vm -- what the rig's parsers are sliced against

Every file here is the verbatim output of a real tool, or is derived from one by a
substitution this file names.  The tests in `tests/test_vm_scripts.py` cut the parser
out of the shipped `vm/*.sh` and feed it these bytes, so a parser that drifts from
what the rig runs fails here rather than on a forty-minute build.

| file | what it is |
|---|---|
| `wlr-randr-0.4.1-3heads.txt` | `wlr-randr` (0.4.1-1build1, Ubuntu 26.04 universe) against headless sway 1.11 with `WLR_HEADLESS_OUTPUTS=3`, recorded in this dsb guest on 2026-09-08 after `wlr-randr --output HEADLESS-N --pos ...`.  Note the enumeration order the rig's layout helper exists to correct: `HEADLESS-3` is listed first. |
| `wlr-randr-0.4.1-3heads-one-disabled.txt` | the same session after `wlr-randr --output HEADLESS-3 --off`: a disabled head prints `Enabled: no` and **no** `Position:` line at all, which is the whole reason the parser is a state machine and not a `grep`. |
| `wlr-randr-0.4.1-virtual-3heads-one-disabled.txt` | derived: the file above with `HEADLESS-` substituted by `Virtual-`, which is what virtio-vga names its connectors in the rig.  Nothing else is changed. Read twice: by selftest.sh's `wlr` parser and by `/usr/local/bin/vmctl-wlr-layout`, which the same file exercises -- its heads are 1280 wide, so a layout helper that stepped by a constant 1920 cannot pass over it. |
| `hyprctl-monitors-2heads.json` | `hyprctl -j monitors` recorded in the Hyprland recon's VM (`recon2/hyprland/fixtures/hyprctl-monitors.json`): `Virtual-1` at 0,0 and a runtime headless head at 1920,0 whose `scale` came up **2.00**, which is why the flavor pins `monitor = , preferred, auto, 1`. |
| `cosmic-randr-winit-1head.kdl` | `cosmic-randr list --kdl` recorded against the COSMIC recon's nested session (`recon2/cosmic/cosmic-randr.kdl`): one `WINIT-0` output, `enabled=#true`, `transform "flipped180"`. |
| `cosmic-randr-2heads-one-disabled.kdl` | derived from the capture above and **not** a recording: the same document shape with the rig's `Virtual-N` names, a second head at `position 1920 0` with a `physical 480 270` that is not its position (so a parser reading the wrong key is visible), and a third with `enabled=#false` and no `position` node at all. |
| `serial-packages-*.txt` | synthetic `vmctl build` serial logs -- three package-list shapes (dpkg, rpm with the two uppercase names, pacman normalised to names) and one that is too short for the sanity floor.  Synthetic on purpose: the block's *shape* is the contract between `pkg_manifest` and `cmd_build`, and a real 1200-line list would say nothing more. |
