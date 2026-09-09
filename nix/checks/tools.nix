# The unittest suite, in a derivation, with every tool it shells out to.
#
# `nix flake check` used to run zero tests: its whole output was the two
# package derivations and the devShell, then "all checks passed!"
# [recon2/pkg-nix §1 defect 5].  The suite is stdlib unittest and CI runs it
# one file per job (`python3 tests/<file>.py` under dbus-run-session,
# .github/workflows/ci.yml), so that is what this does -- the same command, in
# a loop, so a nix user gets the same coverage from one build.
#
# The input list is the CI image's package list translated to nixpkgs
# (.github/ci/Dockerfile): a real sway with Xwayland for the live files, GTK
# for warandr, Xvfb, xterm and foot for the compositor tests,
# g-ir-compiler for the typelib tests, dbus-run-session for the RealBus tests,
# node for tests/test_bridge_js.py.  The distro's own xdotool/wmctrl/xprop/
# xrandr are deliberately NOT here: FUCKWAYLAND_PASSTHROUGH=never is set for
# the whole run, the parity files are a job of their own against the pinned
# oracles (scripts/parity-oracle.sh), and a real xdotool on PATH inside a
# check would only make the handover tests answer differently than they do in
# CI.
{ pkgs, src, version }:

pkgs.stdenv.mkDerivation {
  pname = "fuckwayland-tests";
  inherit version src;

  # gobject-introspection is the setup HOOK that collects every input's
  # lib/girepository-1.0 into $GI_TYPELIB_PATH; the typelibs are buildInputs
  # and the hook is a nativeBuildInput, which is the difference between the
  # two lists here.  There is no `export GI_TYPELIB_PATH=` line any more, and
  # that line was a bug rather than a belt: an assignment REPLACES what the
  # hook collected, and `${gtk3}:${glib}` is two directories where the hook
  # collects nine.  Measured in a sandbox probe on this guest, with exactly
  # the old file's inputs and its export line, `from gi.repository import Gtk`
  # died with
  #
  #   gi.RepositoryError: Typelib file for namespace 'xlib', version '2.0'
  #   not found
  #
  # -- Gtk-3.0.typelib requires xlib-2.0, Pango-1.0, GdkPixbuf-2.0, Atk-1.0,
  # cairo-1.0 and HarfBuzz-0.0, and gtk+3-3.24.52's own girepository-1.0
  # carries none of them.  Without the export the same probe printed
  # `Gtk ok 3`, off a nine-entry path holding 37 typelibs: glib,
  # gobject-introspection and its wrapper, at-spi2-core, gdk-pixbuf,
  # gsettings-desktop-schemas, harfbuzz, pango, gtk+3 -- three of which
  # (harfbuzz, gsettings-desktop-schemas, glib) arrive by propagation and are
  # not named below.  The buildInputs list is the one that probe ran with,
  # verbatim; do not trim it to the directories that showed up.
  nativeBuildInputs = with pkgs; [
    python3
    python3Packages.pygobject3
    gobject-introspection
    sway
    xwayland
    foot
    xterm
    (pkgs.xauth or pkgs.xorg.xauth)
    (pkgs.xprop or pkgs.xorg.xprop)
    (pkgs.xrandr or pkgs.xorg.xrandr)
    xvfb-run
    dbus
    glib
    nodejs
    zstd
    git
    procps   # vm/vmctl's pg() is pgrep -x plus the wrapper rule; tests/test_vm_scripts.py runs it
  ];

  buildInputs = with pkgs; [ gtk3 pango gdk-pixbuf atk at-spi2-core cairo ];

  dontConfigure = true;
  dontBuild = true;

  # The suite is written to run from the repository root, with the tests
  # directory on sys.path -- which `python3 tests/<file>.py` gives and
  # `python3 -m unittest` does not (tests/test_passthrough.py's SuiteGuard
  # enforces both halves).  Nothing is installed: the output is the list of
  # files that ran, which is what makes a silently-empty run visible.
  installPhase = ''
    runHook preInstall
    export HOME=$TMPDIR/home
    export XDG_RUNTIME_DIR=$TMPDIR/run
    install -d -m 0700 "$HOME" "$XDG_RUNTIME_DIR"
    export FUCKWAYLAND_PASSTHROUGH=never
    # the rig's scripts and the fixtures' fake tools say #!/usr/bin/env, and the
    # sandbox has no /usr/bin; nothing the packages ship lives in these three trees
    patchShebangs vm scripts tests/fixtures
    : > "$out"
    fails=
    for f in tests/test_*.py; do
      case "$f" in
        # These six run the shipped shell scripts under the distro's own
        # `/usr/bin:/bin` (that is the claim: a maintainer script needs nothing
        # else), read /proc/<pid>/environ, or exec /usr/bin/install by path.  A
        # sandbox has none of that, so they are the per-distro lanes'
        # (Ubuntu, Fedora, Arch, each in its own container in CI), not this one's.
        tests/test_build_scripts.py|tests/test_debian_scripts.py|tests/test_install_scripts.py| \
        tests/test_passthrough_exec.py|tests/test_support_helpers.py|tests/test_vm_scripts.py)
          echo "== $f: not in the sandbox (pins the distro's /usr/bin or /proc; the distro lanes run it)"
          continue ;;
      esac
      echo "== $f"
      # `if`, not a bare command: with set -e the loop would stop at the first
      # failing file and a build log that names one file is a build log that
      # hides the other forty.  Every file runs; the summary is the last line.
      # the sandbox has no /etc/dbus-1, so the session bus takes its config from the dbus package
      if dbus-run-session --config-file=${pkgs.dbus}/share/dbus-1/session.conf -- python3 "$f"; then
        echo "$f" >> "$out"
      else
        fails="$fails $f"
      fi
    done
    [ -z "$fails" ] || { echo "FAILED:$fails" >&2; exit 1; }
    runHook postInstall
  '';
}
