# The unit-test image, one per Ubuntu release the package claims (24.04, 26.04, 26.10).
# Everything a test file can need on this host, so nothing skips for want of a tool:
# a real sway with Xwayland for the live files, GTK for warandr, Xvfb, the distro's
# own xdotool/wmctrl/xprop/xrandr (the parity oracle is a separate job, see ci.yml),
# g-ir-compiler for the typelib tests, dbus-run-session for the RealBus tests, the
# Debian build tools for scripts/build-deb.sh, and node 22 for tests/test_bridge_js.py.
# Built by the `image` job of .github/workflows/ci.yml and cached in GHCR by the hash
# of this file, so a push that does not touch it pays nothing for it.
ARG BASE=ubuntu:26.04
FROM ${BASE}
ARG NODE=v22.22.1
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-gi gir1.2-gtk-3.0 gir1.2-gtk-4.0 \
        sway xwayland xterm foot xvfb xauth x11-utils x11-xserver-utils xdotool wmctrl \
        dbus libglib2.0-bin libgirepository1.0-dev \
        dpkg-dev debhelper dh-python pybuild-plugin-pyproject python3-all python3-setuptools fakeroot \
        ca-certificates curl xz-utils git procps sudo \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL "https://nodejs.org/dist/${NODE}/node-${NODE}-linux-x64.tar.xz" | tar -xJ -C /opt \
    && ln -s "/opt/node-${NODE}-linux-x64/bin/node" /usr/local/bin/node && node --version
# The suite is not written to run as root (the root daemon listens on /run and a few
# tests skip themselves there), and uid 1001 is what the GitHub runner mounts the
# checkout as, so this user can write into it.
RUN useradd -m -u 1001 -s /bin/bash ci && install -d -m 0700 -o ci -g ci /tmp/xdg-ci
