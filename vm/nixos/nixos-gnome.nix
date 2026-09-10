# nixos-gnome: GNOME 50.4 through GDM, with the bridge on.
#
# `# vmctl-desktop: gnome` is the DESKTOPS row.  Two things about it are NixOS
# facts rather than Ubuntu ones and both are already handled by vm/vmctl:
#
#   * the display-manager unit is `display-manager.service`, not `gdm.service`
#     -- nixpkgs' gdm module sets systemd.services.gdm.enable = false
#     (gdm.nix:287) and defines display-manager instead [recon2/nixos §7.2];
#     the DESKTOPS `dm` field is used in cmd_session's failure hint only.
#   * gnome-shell here is a WRAPPED program, so its /proc/comm is
#     `.gnome-shell-wr` -- fifteen characters, truncated -- and both halves of
#     PAINT_SCRIPT's gnome branch are comm matches.  vm/vmctl's pg() builds
#     that name from the plain one and matches either [recon2/nixos §6.1].
#
# The bridge is not named here on purpose: programs.w11.gnomeBridge
# defaults to services.desktopManager.gnome.enable, so this flavor is also the
# rig's test of that default.
{ pkgs, lib, ... }:

{
  services.displayManager.gdm.enable = true;
  services.displayManager.gdm.autoLogin.delay = 0;
  services.displayManager.autoLogin = { enable = true; user = "test"; };
  services.desktopManager.gnome.enable = true;
  services.getty.autologinUser = lib.mkForce null;

  environment.systemPackages = with pkgs; [ gnome-console ];
}
