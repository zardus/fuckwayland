{
  description = "vm/: the NixOS rig flavors, built as qcow2 goldens instead of installed into one";

  # nixos-26.05, not the repo flake's nixos-unstable, and the two differ where
  # it matters: 26.05 ships xdotool 3.20211022.1 while unstable ships
  # 4.20260303.1, the generation tests/test_cli_parity.py is written against
  # [recon2/pkg-nix, recon2/nixos].  A rig flavor is a picture of what a NixOS
  # USER runs, so it takes the release; the parity oracle keeps unstable.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  # The repo itself, by relative path -- vm/nixos/ is two directories down --
  # with its nixpkgs made to follow this one, so the whole image is built from
  # exactly one nixpkgs and the module under test is the repo's own.
  inputs.fuckwayland.url = "path:../..";
  inputs.fuckwayland.inputs.nixpkgs.follows = "nixpkgs";

  outputs = { self, nixpkgs, fuckwayland }:
    let
      system = "x86_64-linux";
      flavor = name: nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          fuckwayland.nixosModules.default
          ./common.nix
          (./. + "/${name}.nix")
          { networking.hostName = name; }
        ];
      };
      flavors = [ "nixos-sway" "nixos-gnome" ];
    in
    {
      nixosConfigurations = nixpkgs.lib.genAttrs flavors flavor;

      # `vm/build-nixos-golden.sh <flavor>` builds this attribute and reads the
      # file name out of `config.image.filePath`: the image is called
      # nixos-image-qcow2-<label>-<system>.qcow2 and the label carries the
      # nixpkgs rev, so it changes every time the pin moves (the recon's was
      # nixos-image-qcow2-26.05.20260907.93108a5-x86_64-linux.qcow2, 5,191,303,168
      # bytes, ~10 min on 4 vCPU [recon2/nixos §7.4]).  Nothing may hard-code it.
      images = nixpkgs.lib.genAttrs flavors
        (name: self.nixosConfigurations.${name}.config.system.build.images.qemu);
    };
}
