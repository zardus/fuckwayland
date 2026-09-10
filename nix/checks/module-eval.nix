# The module's option set, evaluated -- and nothing built.
#
# A NixOS VM test costs about 25 minutes cold and needs KVM; a typo in an
# option name, a `lib.hiPrio` on something that is not a package or an
# assertion that refers to an option nixpkgs renamed costs nothing to catch,
# and this is where it is caught.  `toplevel.drvPath` forces the whole module
# system -- every option this file sets and every option the module sets from
# them -- to a derivation path, which is a string.
#
# `builtins.unsafeDiscardStringContext` is not decoration.  A drvPath carries a
# string CONTEXT of the `=` kind, and interpolating it into a derivation makes
# nix add that .drv AND every output of its closure as inputs.  Written
# without the discard, this very file built the whole system: `nix derivation
# show` of the check named 3993 input derivations and 4614 input sources,
# among them nixos-system-nixos-26.11.20260831.34ab990.drv, a kernel tarball
# and the initrd units, and `nix build` of it went off realising the lot --
# one such run was still going 21 minutes later, when it was killed (all
# measured here on 2026-09-08, by putting the plain interpolation back and
# reading `inputs.drvs` -- nix 2.34 spells it that way, not `inputDrvs`).
# With the discard the same command has TWO input derivations -- bash and
# stdenv-linux-no-cc, which is a runCommand with nothing in it -- and two
# input sources, both stdenv's own shell files.  The string is still the
# drvPath and forcing it is still the whole module system evaluated; nothing
# behind it is a build input.  `nix build` of the check then costs an
# evaluation and no realisation: 8.6 s with a warm eval cache on this guest,
# against the tens of minutes the three VM tests beside it cost.
{ self, nixpkgs, pkgs }:

let
  system = pkgs.stdenv.hostPlatform.system;

  # A machine with everything the module can turn on, including the four
  # options that are off by default: shadowOriginals (lib.hiPrio over
  # xdotool/wmctrl/xprop/xrandr/arandr), gnomeOverlap and warandr, whose
  # packages are otherwise never referenced by any evaluation, and x11Tools,
  # which is the one that reads `pkgs.xprop or pkgs.xorg.xprop`.
  everything = nixpkgs.lib.nixosSystem {
    inherit system;
    modules = [
      self.nixosModules.default
      ({ ... }: {
        boot.loader.grub.device = "nodev";
        fileSystems."/" = { device = "/dev/vda1"; fsType = "ext4"; };
        system.stateVersion = "25.11";
        programs.w11 = {
          enable = true;
          warandr.enable = true;
          gnomeBridge.enable = true;
          gnomeOverlap.enable = true;
          x11Tools.enable = true;
          shadowOriginals = true;
          wlMirror.enable = true;
        };
      })
    ];
  };
in
pkgs.runCommand "w11-module-eval" { } ''
  echo ${builtins.unsafeDiscardStringContext
    everything.config.system.build.toplevel.drvPath} > "$out"
''
