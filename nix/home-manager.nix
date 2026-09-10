# homeManagerModules.default -- the half of the NixOS module a user can do
# alone, and a warning naming the half they cannot.
#
# home-manager writes into $HOME and into the user's dconf; it does not write
# /etc, /run/current-system or a udev rule.  That splits this project cleanly
# in two: the extension and its dconf enable are per-user state and land here,
# the /dev/uinput grant is system state and does not.  Saying so in a module
# `warnings` entry is the whole point of the file -- the alternative is a user
# reaching for `gnome/install-bridge.sh --udev`, which writes
# /etc/udev/rules.d/60-w11-uinput.rules and
# /etc/modules-load.d/w11-uinput.conf (install-bridge.sh:35,37), both
# of which NixOS generates from the store [recon2/pkg-nix §3].
{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.programs.w11;
  w11pkgs = self.packages.${pkgs.stdenv.hostPlatform.system};
in
{
  options.programs.w11 = {
    enable = lib.mkEnableOption "the w11 tools in this user's profile";

    package = lib.mkOption {
      type = lib.types.package;
      default = w11pkgs.w11;
      defaultText = lib.literalExpression "w11.packages.\${system}.w11";
      description = "The stdlib CLI package (the six tools, no GTK).";
    };

    warandr.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Also install the GTK GUI and its desktop entry.  Separate because it
        is the package that carries gtk3 and pygobject3.
      '';
    };

    gnomeBridge.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Hand the bridge extension to {option}`programs.gnome-shell.extensions`,
        which defaults an entry's `id` to `package.passthru.extensionUuid`,
        expects the extension at
        `$out/share/gnome-shell/extensions/''${id}` and writes
        `disable-user-extensions = false` and `enabled-extensions` into the
        user's dconf (home-manager modules/programs/gnome-shell.nix).  That is
        exactly the shape this project's extension packages have.

        That module wraps its whole `config` in `mkIf cfg.enable`, so this
        option also turns {option}`programs.gnome-shell.enable` on with
        `mkDefault`.  Without that one line the entry below is inert, and
        inert silently: measured here on 2026-09-08 against home-manager
        2c0350c and this flake, a configuration whose only w11
        settings were `enable` and `gnomeBridge.enable` evaluated to
        `programs.gnome-shell.enable = false`,
        `dconf.settings."org/gnome/shell"` absent, and a `home.packages`
        holding `w11-0.4.0` and no extension at all.  With it, the
        same configuration answers `true`,
        `['w11-bridge@w11']` and a `home.packages` that has
        `w11-gnome-bridge-0.4.0` in it.
      '';
    };

    wlMirror.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Install `wl-mirror` 0.18.5, the helper `wmirror` drives.";
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages =
      [ cfg.package ]
      ++ lib.optional cfg.warandr.enable w11pkgs.warandr
      ++ lib.optional cfg.wlMirror.enable pkgs.wl-mirror;

    # mkDefault, and mkIf on the option and not on the value: a user who has
    # their own programs.gnome-shell block keeps it, and a user who has none
    # gets the module that reads the line below.
    programs.gnome-shell.enable = lib.mkIf cfg.gnomeBridge.enable (lib.mkDefault true);
    programs.gnome-shell.extensions =
      lib.optional cfg.gnomeBridge.enable { package = w11pkgs.gnome-bridge; };

    # Not an error and not silence: the input half of wdotool works on the
    # wlroots family with no grant at all (it falls back to the compositor's
    # zwp_virtual_keyboard_v1 / zwlr_virtual_pointer_v1, measured on a NixOS
    # sway session [recon2/pkg-nix §2c]), and needs the rule only on GNOME and
    # KDE, where no per-user configuration can supply it.
    warnings = lib.optional cfg.enable ''
      home-manager cannot grant /dev/uinput: the node is root:root 0600 on
      NixOS and the rule that changes that is system state.  On GNOME or KDE,
      wdotool's key/type/click commands will run as root or not at all until
      the NixOS module is used instead
      (programs.w11.enable = true, which sets uinput.enable by
      default).  On sway, Hyprland, labwc, river, Wayfire and COSMIC nothing
      is needed: the tools inject through the compositor's virtual-input
      protocols.  gnome/install-bridge.sh --udev is not a way around this --
      it writes /etc/udev/rules.d and /etc/modules-load.d, which NixOS
      generates from the store.
    '';
  };
}
