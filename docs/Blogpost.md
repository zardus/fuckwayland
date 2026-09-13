# Six tools that should not exist

Consider a shell script that finds a terminal window, raises it, types a command and
presses Return. That script may have worked unchanged since `xdotool` appeared in
2007. Then the desktop moves to Wayland, the terminal becomes a native Wayland
client, and the script can no longer find it.

The usual explanation is that this is intentional. An X11 client could inspect other
windows, read their properties and inject input into them. Wayland moved those powers
into the compositor so that ordinary clients could no longer use them against one
another. That change closed a broad security hole, but it also removed the common
interface used by automation tools.

Jordan Sissel described the result in
[xdotool and exploring Wayland fragmentation](https://www.semicomplete.com/blog/xdotool-and-exploring-wayland-fragmentation/).
The missing capability was only half of the problem. Its replacements were spread
across compositor-specific protocols, D-Bus methods, scripting interfaces, portals
and kernel devices. A program that once spoke to one X server now needed a different
answer for every desktop.

This raised a narrower and more useful question: had the capability disappeared, or
had its address changed?

The answer was that its address had changed. GNOME, KWin, sway and the other
compositors still know where every window is, which output it occupies and which
window has focus. They expose different portions of that state through different
interfaces. Rebuilding the old tools therefore meant finding those interfaces,
measuring their behavior and hiding the differences behind the command lines that
existing scripts already use.

That work became w11.

## The compatibility target

w11 contains six tools:

```text
wdotool   xdotool, including all 48 commands
wwmctl    wmctrl, with native Wayland and XWayland windows in one list
wxprop    xprop, using real X properties or synthesized Wayland properties
wxrandr   xrandr, including multi-output layout changes
warandr   arandr, with a drag-and-drop display editor
wmirror   region and irregular-output mirroring on wlroots compositors
```

The first four aim to reproduce the original command-line interfaces: the same
arguments, exit status and output bytes, including behavior that would be considered
a bug in a new interface. Existing scripts are the compatibility target.

Most of the implementation uses only the Python standard library. The repository
contains small D-Bus, Wayland and X11 clients rather than depending on full protocol
stacks. `warandr` uses the system GTK 3 bindings, and `wmirror` invokes `wl-mirror`
for capture and presentation.

This post is about the parts that required measurement. The command reference and
current support matrix live in the [README](../README.md); the implementation details
live in [Technical.md](Technical.md).

## Where the old X11 boundary went

X11 put windows, properties, input and displays behind one server connection. That
made the old tools small and independent of the window manager:

* `xdotool` sent XTEST input and window-manager messages.
* `wmctrl` sent EWMH messages such as `_NET_CLOSE_WINDOW`.
* `xprop` read properties stored by the X server.
* `xrandr` configured outputs through the RandR extension.

Any client that could open the display could use these interfaces. This was unsafe,
but it was also a stable automation boundary.

Wayland moved that boundary into the compositor. There is no standard protocol that
provides the full replacement. Window listing, input injection and display control
may each require a different interface on the same desktop. The table below shows
the main routes used by w11; it is a map of implementations rather than an exhaustive
support matrix.

| Desktop | Window management | Input | Display configuration |
|---|---|---|---|
| GNOME | bundled Shell bridge to Mutter | `/dev/uinput` | Mutter DisplayConfig on D-Bus |
| KDE Plasma | temporary KWin scripts | `/dev/uinput` | KDE output-management protocol |
| sway | i3-compatible IPC | virtual-keyboard and virtual-pointer protocols | sway IPC |
| Hyprland | Hyprland IPC | virtual-keyboard and virtual-pointer protocols | Hyprland IPC |
| Wayfire | optional Wayfire IPC plugins, then generic protocols | virtual-keyboard and virtual-pointer protocols | wlr output management |
| Cinnamon | `org.Cinnamon.Eval` | `/dev/uinput` | Muffin DisplayConfig on D-Bus |
| labwc, Budgie, Xfce Wayland, LXQt Wayland and river | generic toplevel protocols | virtual-keyboard and virtual-pointer protocols | wlr output management |
| COSMIC | COSMIC toplevel protocols | virtual keyboard; `/dev/uinput` for the pointer | wlr output management |
| X11 sessions | the installed X11 tools | the installed X11 tools | the installed X11 tools |

The implementation selects one of these routes at runtime. On an X11 session it gets
out of the way and executes the original program. On Wayland it presents the same
interface over whichever route the compositor supplies.

Finding an interface was usually straightforward. The interesting part was learning
when its apparently simple answer could not be trusted.

## APIs that almost tell the truth

Compositor APIs tend to describe their own internal model accurately. Problems begin
when that model is translated into an X11 interface with different assumptions.

### The workspace that exists only to remain empty

GNOME uses dynamic workspaces by default. Mutter always keeps an empty workspace at
the end of the list so that the user has somewhere to move a window. Its
`get_n_workspaces()` method includes that workspace.

On a fresh session, `wmctrl -d` therefore reports two workspaces even when the
overview appears to show only one in use. Mutter also refuses a request such as
`wdotool set_num_desktops 4`, because a fixed count is incompatible with dynamic
workspaces.

Neither result is a transport error. The count is Mutter's real count, and the setter
has no honest implementation while dynamic workspaces are enabled. w11 reports both
facts rather than inventing a second workspace model.

### The window ID that KWin does not have

X11 tools pass numeric window IDs from one command to another. KWin's scripting API
uses UUID strings and exposes no equivalent number. w11 therefore creates a stable
32-bit value for each native KWin window:

```text
0x40000000 | 30 bits derived from KWin's internal ID
```

The range is outside the IDs normally assigned to XWayland clients, so a native
window cannot be mistaken for an X window in a mixed listing. If two UUIDs collide in
the 30-bit space, the second receives another value rather than disappearing from the
list.

XWayland windows are harder. Plasma 6 removed the scriptable X11 properties that once
exposed their X IDs. `wwmctl` still needs to print those real IDs because users pass
them to `xprop` and other X11 programs. w11 reads `_NET_CLIENT_LIST` from XWayland and
matches it against KWin's window list using process ID and `WM_CLASS`, followed by
title and geometry.

The first matcher looked convincing until it met an adversarial case: one X client
created two top-level windows with the same process ID, class, title and rectangle.
Only `WM_WINDOW_ROLE`, which the matcher deliberately did not use, distinguished
them. The matcher swapped the IDs in six of eight runs.

The fix was a refusal rule rather than a more elaborate score. A pair must agree on
process ID or class before it can receive an X ID. If the available evidence cannot
separate two candidates, both retain ID 0. An unknown ID is safer and more useful
than a plausible ID for the wrong window.

### A display change that is always permanent

The xrandr model separates applying a layout from saving it. A layout change is
temporary until another component records it.

KWin does not offer that distinction. It writes every accepted layout to
`~/.config/kwinoutputconfig.json`, often before the command that requested the change
has returned. Deleting the file does not make the change temporary because KWin
writes it again at shutdown.

`wxrandr` cannot reproduce temporary changes on Plasma, so it states the difference
and prints the exact command that restores the previous layout. The additional
`--persistent` option is accepted there, but it cannot make an already persistent
operation more permanent.

Plasma 6.7 introduced a separate discovery problem. Earlier releases published each
output as a `kde_output_device_v2` global in the Wayland registry. Starting with
6.7.0, those globals disappeared and the objects moved behind
`kde_output_device_registry_v2`. A client using the old path sees no outputs at all.
`wxrandr` now supports both discovery mechanisms, with a wire-level KWin test double
covering each one.

## Four failures found by running the tools

Specifications established which requests existed. They did not establish how a
complete desktop behaved while those requests were in flight. The following failures
came from real sessions, screenshots and deliberately hostile test cases.

### 1. Mirroring turned both displays black

`wmirror` captures a region of one output and displays it in a fullscreen
`wl-mirror` window on another. This is safe while the two outputs occupy separate
rectangles.

Now place both outputs at the same coordinates, as is commonly done to mirror a
wlroots layout. The fullscreen target window occupies pixels that also belong to the
source. The next captured frame therefore contains the window displaying the previous
captured frame.

The expected failure was a video-feedback tunnel. On a live sway session, both
screens instead became completely black and stayed that way until `wl-mirror` was
killed from another tty.

For this reason, `wmirror` does not merely warn when source and target overlap. It
refuses to start, identifies the conflicting geometry and prints the `wxrandr`
command for output-level mirroring when that is the operation the user intended.

### 2. The valid X cookie belonged to the wrong display

w11 supports commands invoked through `sudo`, root SSH sessions and cron. On an X11
desktop, that requires recovering the active user's `DISPLAY` and `XAUTHORITY` before
executing the original tool.

The obvious search checks the caller's environment, the runtime directory and
`~/.Xauthority`. On a Plasma session started by SDDM 0.20, that search found a real
cookie and the X server still rejected it:

```text
Authorization required, but no authorization protocol specified
```

SDDM had written the active cookie to `/tmp/xauth_<random>` and recorded its path only
in the environment of the session leader. The cookie in `~/.Xauthority` belonged to
an older display and authorized nothing in the current session.

The reliable source is therefore `/proc/<pid>/environ` for the session leader,
provided that the process has the expected user ID. w11 checks that source before
falling back to conventional paths. This also avoids a related error in which the
lowest-numbered directory under `/run/user` belongs to the display manager's greeter
rather than the logged-in user.

### 3. Two maximize requests raced each other

On X11, this command is one client message containing two state atoms:

```sh
wmctrl -b add,maximized_vert,maximized_horz
```

The window manager can apply both states together. On Wayland, maximizing a window is
a negotiation. The compositor sends a configure event, the client acknowledges it,
and the client draws at the new size. Reading the state immediately after the request
may return the old value because the client has not responded yet.

Sending separate vertical and horizontal requests made the second request race the
first. The inverse operation exposed a worse GNOME failure:
`wwmctl -b remove,maximized_vert,maximized_horz` removed only one axis and damaged
the saved restore rectangle. An explicit resize was then required to recover the
window's previous shape.

The GNOME bridge now sends one combined unmaximize operation, matching Mutter's own
handling of the two atoms. On KDE, the injected script waits for the window's state
change signal and uses a bounded timer as a fallback. The command returns only after
the state has settled enough for the next command to observe it.

### 4. A matching algorithm was confidently wrong

The KWin X-ID case revealed a broader testing lesson. Normal applications supplied
enough distinguishing information that the original matcher appeared exact. Only a
client designed to create indistinguishable windows exposed the ambiguity.

The useful assertion was not that the matcher usually found an ID. It was that every
ID it returned had enough evidence behind it. Once the test was phrased that way, ID
0 became a successful result for an ambiguous pair rather than a failure to be hidden.

This pattern appears throughout w11: a refusal with a specific reason is preferable
to an operation that reports success while acting on the wrong object.

## Input injection is a keyboard-layout problem

The portable input path is `/dev/uinput`. wdotool creates a virtual keyboard, a
relative mouse and an absolute tablet. Compositors process those devices like physical
hardware, so the path works on GNOME, KDE and desktops without a virtual-input
protocol. It requires root or a udev rule. Device creation also causes roughly 600
milliseconds of hotplug settling, so a small daemon owns the devices and later
commands connect to it.

Many wlroots compositors provide a better route for an unprivileged client.
`zwp_virtual_keyboard_v1` accepts an uploaded keymap and key events, while
`zwlr_virtual_pointer_v1` accepts motion, buttons and scrolling. sway advertises both
interfaces to every client on its Wayland socket. wdotool uses them when the kernel
device is unavailable.

The protocol path avoids one class of layout errors because the compositor interprets
keycodes through the keymap supplied by wdotool. That keymap is currently US, so this
path cannot produce every character from layouts such as German. The kernel path has
the opposite problem: it must send keycodes that the desktop interprets through the
user's active XKB layout. A fixed US table types `z` when a German user asks for `y`.

wdotool solves this by reading the compositor's keymap and constructing the mapping
in reverse: character to keycode plus modifier mask. AltGr is discovered from the
keymap rather than assumed to be Right Alt, and dead-key characters become two key
presses that the application composes.

The active layout introduces another problem. Wayland sends the active XKB group to
the focused client, but an input injector is never that client. wdotool asks each
desktop through the best interface it provides: KWin's keyboard-layout method,
GNOME's input-source setting, Hyprland's device state, Wayfire's keyboard state and
Cinnamon's input-source setting. sway and COSMIC provide the required state on the
Wayland connection itself. Where no source exists, wdotool states which layout it
has assumed.

One kernel detail placed a hard limit on `--clearmodifiers`. Linux discards a key-up
event when the emitting device does not hold that key. A virtual keyboard therefore
cannot release Shift or Control held on a physical keyboard. Trying to fake the
release can leave the modifier stuck because Mutter and KWin combine key state across
the seat's devices. wdotool clears only modifiers held by its own device and reports
the physical modifier when it can identify one.

## What the test environment had to model

The repository currently contains 4302 tests. The count matters less than the kinds
of disagreement they are designed to expose.

Parity tests run the real `xdotool`, `wmctrl`, `xprop` and `xrandr`, then compare
their output and exit status with the corresponding w11 tool. Other tests use small
servers that speak the real KWin, Mutter, X11 and generic Wayland wire formats. One
of the X servers is deliberately hostile and returns malformed or misleading data.
Live tests run against headless compositors, and the VM rig covers complete desktop
sessions with outputs that can be connected, resized and removed by the host.

The rig includes cloud images as well as Ubuntu systems installed from release ISOs
with the installer's default choices. That distinction found documentation defects:
a cloud image plus a desktop metapackage does not have the same packages, services or
defaults as an installed desktop. Claims about a default Ubuntu installation are now
checked on the latter.

The test suite also became a tool for reducing the implementation. Shared argument
parsing, X-style integer conversion, window hit testing, detached-process handling
and display transforms moved into `w11common`. The simulated protocol servers now
share the code that encodes and decodes messages, while keeping compositor behavior
separate. This made the production code smaller while expanding the cases that could
be tested.

More importantly, the suite checks statements in the documentation. It derives the
test count, VM inventory and other numeric claims from the files that define them.
Documentation drift then becomes a CI failure rather than an error waiting for a
reader to notice it.

## What long-lived desktops found

Disposable VMs are good at reproducing installation and protocol behavior. They are
bad at reproducing a desktop that logs out, sits idle, changes keyboard layouts and
survives upgrades. Running w11 that way produced several failures that short-lived
tests had missed.

The input daemon originally lived until the machine shut down. Logout removed its
socket but left the process and lock alive, so no client could reach the old daemon
and no new daemon could start. One test host accumulated 161 abandoned processes
using about three gigabytes of memory. The daemon now compares the inode of its socket
every fifteen seconds and exits if the path disappears or is replaced. It also exits
after fifteen minutes without a client.

A Greek keyboard layout exposed a silent input error. When a requested chord was not
available in the active layout, the old code used the same physical position as a US
keyboard. `key ctrl+s` therefore produced Control plus sigma in Kate, saved nothing
and returned success. The active layout is now authoritative. If it cannot produce a
requested chord, wdotool refuses the chord and explains why.

GNOME's saved display configuration produced a different kind of long-term failure.
Mutter validates all of `~/.config/monitors.xml` at startup and discards the entire
file if one entry is invalid. A layout saved while fractional scaling is disabled can
become invalid when scaling is enabled because the stored positions are interpreted
in a different coordinate space. Before saving a persistent layout, `wxrandr` now
checks whether Mutter has already discarded the file, warns about layouts vulnerable
to this change and keeps a copy of the file it replaces.

The most invasive feature is deliberately separate. GNOME rejects overlapping
monitor rectangles in every public configuration route. w11 can bypass that check
through an optional Shell extension that writes into a private Mutter structure. The
extension is disabled by default and guarded by six checks tied to measured GNOME
builds. Tests install twelve deliberately incorrect structure descriptions and
verify that every one is rejected before a write occurs. This reduces the risk; it
does not make the private interface stable. The README documents the route and its
cost before showing how to enable it.

## The security boundary is part of the interface

These tools exist to give scripts powers that ordinary Wayland clients do not have.
That fact should be visible in the design rather than buried in an installation note.

On GNOME, installing the bridge lets processes on the user's session bus list and
control windows. Installing the udev rule lets the user at the active seat inject
keyboard and pointer input through `/dev/uinput`. On Plasma, KWin's script-loading
method is already available to session-bus clients. On sway, the virtual-input
protocols are already available to clients on the Wayland socket. w11 uses these
interfaces; it does not narrow the access they already grant.

The tools do not request Remote Desktop or Input Capture access through the desktop
portal, so their normal operation does not display an authorization dialog. GNOME
has one read-only exception: wdotool reads the active input-source setting through
the portal's Settings interface. That call has no consent dialog and grants no input
capability. The VM tests monitor portal traffic, polkit requests, new windows and both
screens while commands run, and the static tests restrict the exception to that one
named interface.

Users who want less standing input access can run wdotool through `sudo` instead of
installing the udev rule. GNOME window management still requires the bridge because
root access does not provide a route into Mutter's window model. The full boundaries
and invariants are documented in the [README threat model](../README.md#threat-model)
and [Technical.md](Technical.md#12-the-threat-model-in-full).

## A compatibility layer, while we wait

w11 is not the standard automation protocol that Wayland still lacks. It is a
compatibility layer built from the interfaces available today. Its shape follows the
desktops whose differences it has to hide, which explains the KWin scripts, the
GNOME Shell bridge, the IPC clients and the specific errors for operations that a
backend cannot yet perform.

The result is deliberately ordinary from the outside. The four-line automation
script works again on GNOME, Plasma, sway and X11 without knowing which session is
underneath it. That was the requirement.

The code is at [github.com/fixing-wayland/w11](https://github.com/fixing-wayland/w11).
Start with [Technical.md](Technical.md) if you want to understand or change the
implementation.
