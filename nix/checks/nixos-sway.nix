# `nix flake check` on a real sway session, with the module on.
#
# The recon wrote this test to find out whether the flake package works at all
# on NixOS; it printed everything and asserted nothing, and it passed in
# 46.98 s of test script [recon2/pkg-nix §2c, recon2/pkg-nix/nixostest.nix].
# Here the prints become assertions and the machine gains
# `programs.fuckwayland.enable`, which is the difference that matters: the
# recon's guest had /dev/uinput at `crw------- root root` and `wdotool key a`
# printed its fallback notice and typed through zwp_virtual_keyboard_v1.  With
# the module's udev rule the same command must take the device instead, and
# the absence of that notice is what this file checks.
{ self, pkgs }:

pkgs.testers.runNixOSTest {
  name = "fuckwayland-nixos-sway";

  nodes.machine = { lib, ... }: {
    imports = [ self.nixosModules.default ];

    virtualisation = { memorySize = 3072; cores = 2; diskSize = 8192; };
    # -vga none plus virtio-gpu-pci: what the recon booted sway on, and what
    # gives the guest a /dev/dri/card0 wlroots will scan out to.
    virtualisation.qemu.options = [ "-vga none -device virtio-gpu-pci" ];

    users.users.alice = {
      isNormalUser = true;
      uid = 1000;
      extraGroups = [ "wheel" ];
      password = "alice";
    };
    services.getty.autologinUser = "alice";

    programs.sway.enable = true;
    programs.fuckwayland = {
      enable = true;
      # The GUI is a package of its own and `warandr --command` is one of the
      # claims under test, so the option that installs it is under test too --
      # by hand would not prove the module.
      warandr.enable = true;
      x11Tools.enable = false;
    };
    environment.systemPackages = [ pkgs.foot pkgs.acl ];

    # A fixed socket path, so the test script does not have to guess the pid
    # in sway-ipc.<uid>.<pid>.sock; pixman because the VM has no GL.
    environment.variables = {
      SWAYSOCK = "/tmp/sway-ipc.sock";
      WLR_RENDERER = "pixman";
    };
    programs.bash.loginShellInit = lib.mkAfter ''
      if [ "$(tty)" = "/dev/tty1" ]; then
        sway > /tmp/sway.log 2>&1 &
      fi
    '';
  };

  testScript = ''
    machine.wait_for_unit("multi-user.target")
    machine.wait_for_file("/run/user/1000/wayland-1", timeout=60)
    machine.wait_for_file("/tmp/sway-ipc.sock", timeout=60)

    # A login shell of alice's, with the session's environment put back by
    # hand.  `su - alice` starts a shell that is not the sway session, so
    # WAYLAND_DISPLAY is not in it -- the tools find the compositor anyway
    # (SWAYSOCK is fixed above and the wayland socket is discovered under
    # XDG_RUNTIME_DIR), but `foot` cannot, and a client that never appears
    # would be a window test with no window in it.
    sock = machine.succeed(
        "ls /run/user/1000 | grep -o 'wayland-[0-9]*' | head -1").strip()
    env = f"export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY={sock};"

    def u(cmd):
        return machine.succeed(f"su - alice -c {env + ' ' + cmd!r}")

    backend = u("wxrandr --print-backend --verbose")
    assert backend.splitlines()[0].strip() == "sway", backend
    assert "protocol: sway IPC (i3-ipc)" in backend, backend

    # 1280x800 is the resolution the NixOS test driver gives a guest head, and
    # Virtual-1 is what the sway backend names it -- both measured in the
    # recon's run of this machine [recon2/pkg-nix §2c].
    query = u("wxrandr --query")
    assert "Virtual-1 connected 1280x800+0+0" in query, query
    geometry = u("wdotool getdisplaygeometry")
    assert geometry.split() == ["1280", "800"], geometry

    # `>/dev/null 2>&1` and not just `&`: the test driver reads a command's
    # output until the pipe closes, and a backgrounded client that keeps
    # stdout open holds it open -- measured, `foot -e sleep 600 &` "finished"
    # after 601.40 seconds, by which time the window was gone again.
    #
    # `cat > /tmp/typed` and not `sleep 600`: the window is the same window
    # either way, but this one also records what is typed into it, which is
    # the only way `wdotool type` can be asserted rather than watched for a
    # zero exit status.  `sleep` afterwards so the window outlives the cat.
    u("foot -e sh -c 'cat > /tmp/typed; sleep 600' >/dev/null 2>&1 &")
    machine.wait_until_succeeds(
        f"su - alice -c {env + ' wwmctl -l'!r} | grep -q foot", timeout=60)
    windows = u("wwmctl -lGx")
    assert "foot.foot" in windows, windows

    # warandr is the GTK package: this proves the wrapper's typelibs are on
    # $GI_TYPELIB_PATH inside a VM with no system PyGObject anywhere.
    command = u("warandr --command")
    assert command.strip() == (
        "wxrandr --output Virtual-1 --mode 1280x800 --pos 0x0 --rotate normal"), command

    # The udev rule, end to end: root:root 0600 plus one ACL entry for the
    # user at the seat, and group::--- -- the state the rule's own comment
    # describes and the one hardware.uinput.enable would NOT produce.
    node = machine.succeed("ls -l /dev/uinput")
    assert node.split()[0] == "crw-rw----+", node
    acl = machine.succeed("getfacl -p /dev/uinput")
    assert "user:alice:rw-" in acl, acl
    assert "group::---" in acl, acl

    # ... and therefore no fallback.  The notice wdotool prints when it cannot
    # open the node is one line naming /dev/uinput; on this machine it must
    # not appear at all.
    notice = machine.succeed(f"su - alice -c {env + ' wdotool key a'!r} 2>&1")
    assert "cannot create uinput devices" not in notice, notice

    # ... and the keystrokes land where the focus is.  A zero exit status from
    # `wdotool type` says only that the tool did not fall over; the claim is
    # that the compositor delivered the characters, so they are read back out
    # of the shell the foot window is running.  The `a` above went into the
    # same cat, so the line is "ahello" -- the substring is the assertion.
    u("wdotool type hello")
    u("wdotool key Return")
    machine.wait_until_succeeds("grep -q hello /tmp/typed", timeout=30)

    # The X11 handover has nothing to hand to here (x11Tools.enable = false),
    # and says so rather than pretending.
    backends = u("wxrandr --backends")
    # Read line by line, never by column: `--backends` pads the name column to
    # the longest backend name, so a literal "wlr    available" would break
    # the day a backend with a longer name is added to the table.
    rows = dict((ln.split()[0], ln.split()[1])
                for ln in backends.splitlines() if len(ln.split()) > 1)
    assert rows.get("wlr") == "available", backends
    assert rows.get("x11") == "unavailable", backends
    assert "no real xrandr on PATH" in backends, backends
  '';
}
