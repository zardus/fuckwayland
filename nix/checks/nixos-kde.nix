# Plasma 6.7.4, for one claim: `wxrandr` takes the KWin backend there.
#
# nixpkgs' locked kdePackages is kwin 6.7.4 [recon2/pkg-nix §1], and 6.7 is
# the generation where kde_output_management_v2 is advertised as a registry
# OBJECT rather than a plain global -- the discovery path wxrandr's KWin
# backend grew for stonking-kde (docs/WXRANDR.md, KWin backend).  Ubuntu 26.04
# ships Plasma 6.6, so outside the stonking probe images this is the repo's
# only Plasma 6.7 regression test, and it is the reason this check exists
# rather than a third copy of the sway one.
{ self, pkgs }:

pkgs.testers.runNixOSTest {
  name = "w11-nixos-kde";

  nodes.machine = { ... }: {
    imports = [ self.nixosModules.default ];

    virtualisation = { memorySize = 4096; cores = 2; diskSize = 8192; };

    users.users.alice = {
      isNormalUser = true;
      uid = 1000;
      extraGroups = [ "wheel" ];
      password = "alice";
    };

    services.displayManager.sddm = { enable = true; wayland.enable = true; };
    services.displayManager.autoLogin = { enable = true; user = "alice"; };
    services.desktopManager.plasma6.enable = true;

    programs.w11.enable = true;
    environment.systemPackages = [ pkgs.kdePackages.konsole pkgs.acl ];
  };

  testScript = ''
    uid = "1000"
    bus = f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus"

    def u(cmd):
        return machine.succeed(f"su - alice -c '{bus} {cmd}'")

    machine.wait_for_unit("display-manager.service")
    machine.wait_for_file(f"/run/user/{uid}/wayland-0")
    machine.wait_until_succeeds("pgrep -x kwin_wayland", timeout=180)

    backend = u("wxrandr --print-backend --verbose")
    assert backend.splitlines()[0].strip() == "kwin", backend
    assert "kde_output_management" in backend, backend

    # The head size is the NixOS test driver's, not a measurement of Plasma:
    # nothing in the recon ran a NixOS KDE guest.  So the claim made here is
    # the one that does not depend on it -- the two tools agree about the
    # screen -- and the backend line above is what this check exists for.
    query = u("wxrandr --query")
    assert "Virtual-1 connected" in query, query
    w, h = u("wdotool getdisplaygeometry").split()
    assert f"{w}x{h}+" in query, (w, h, query)

    # The udev rule again: KWin advertises no virtual-keyboard protocol to us
    # either, so /dev/uinput is the only input route on Plasma.
    acl = machine.succeed("getfacl -p /dev/uinput")
    assert "user:alice:rw-" in acl, acl
    typed = machine.succeed(f"su - alice -c '{bus} wdotool key a' 2>&1")
    assert "cannot create uinput devices" not in typed, typed
  '';
}
