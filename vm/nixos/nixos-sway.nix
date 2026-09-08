# nixos-sway: sway 1.12 through greetd, the same shape as resolute-sway.
#
# `# vmctl-desktop: sway` is the DESKTOPS row this uses, and it fits without a
# change: the compositor's comm is `sway` (sway is not one of the nixpkgs
# programs that gets a wrapper, so PAINT_SCRIPT's scanout owner reads `sway`
# and pg()'s `.sway-wrapped` alternative is never needed), the shell process
# is swaybar, and greetd is the display manager on both distros
# [recon2/nixos §6.1, §7.2].
{ pkgs, lib, ... }:

{
  programs.sway = {
    enable = true;
    xwayland.enable = true;
  };

  # greetd's initial_session is autologin: the same mechanism
  # resolute-sway.yaml uses, and NixOS defaults services.greetd.restart to
  # false once initial_session is set (greetd.nix), so a session that exits
  # does not loop.  default_session is there for the second login of a smoke
  # run, where the first has already been consumed.
  services.greetd = {
    enable = true;
    settings.initial_session = { command = "sway"; user = "test"; };
    settings.default_session = { command = "sway"; user = "test"; };
  };
  services.getty.autologinUser = lib.mkForce null;

  environment.systemPackages = with pkgs; [ swaybg ];
}
