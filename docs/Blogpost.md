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

I started with a narrower and more useful question: had the capability disappeared,
or had its address changed?

What I found was that its address had changed. GNOME, KWin, sway and the other
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

### Why GNOME needs two JavaScript extensions

GNOME was where this stopped looking like a normal compatibility project. Display
control worked first: Mutter already had a D-Bus interface for reading and applying
monitor layouts. Window control did not. Mutter plainly knew the title, process ID,
position and workspace of every window because GNOME Shell displayed and managed all
of them, but I could not find an interface that gave an ordinary client the same
view.

I tried several doors that looked almost right. The generic toplevel protocols
were either absent or lacked the required actions. `org.gnome.Shell.Introspect` was
read-only and restricted its callers. `org.gnome.Shell.Eval` could run code inside
the shell, but only after enabling GNOME's unsafe mode. None was an interface that an
old `xdotool` or `wmctrl` script could depend on.

The remaining place to stand was inside GNOME Shell itself. A Shell extension can use
Mutter's public JavaScript APIs, so `w11-bridge` does exactly that and exports the
needed operations over a small D-Bus interface. It accepts defined requests rather
than code to evaluate, and it never injects input. From the command line, it is simply
the missing bridge to state that GNOME already owns. `wdotool`, `wwmctl` and `wxprop`
use it. Display tools do not, because Mutter's existing DisplayConfig interface is
already sufficient for ordinary monitor layouts.

Then came overlapping monitors.

On X11, two outputs may cover some or all of the same desktop rectangle. That is how
output-level mirroring has traditionally been expressed, and scripts also use partial
overlap for unusual display walls. Mutter's public configuration method rejects such
a layout before applying it. Saving the same coordinates in
`~/.config/monitors.xml` does not help because the file passes through the same
validator, and one rejected entry causes Mutter to discard the whole file.

At first that looked like a limit of the compositor. It was actually a limit of the
configuration path. Mutter's internal monitor objects can represent overlapping
rectangles, and GNOME on Xorg can place them there. The renderer knows what to do;
the public Wayland configuration route simply refuses to ask it.

Bypassing that refusal requires a much less comfortable extension. `w11-overlap`
loads a description of Mutter's private monitor structures, finds the configuration
used by the running shell, changes the 32-bit `x` and `y` fields for the monitors that
must move, and asks Mutter to apply that object. In plain terms, it patches the memory
of the running `gnome-shell`. A wrong offset can corrupt the heap and end the desktop
session.

That code is deliberately kept out of `w11-bridge`. The bridge uses public APIs and
is meant to remain enabled; the overlap extension is installed disabled and is needed
only by someone who chooses this specific layout. Before every write it runs six
compatibility checks, verifies addresses against the shell's mapped memory, bounds
every read and list walk, and reads the result back. These checks make a measured
private layout less reckless to use. They do not make it a public API.

`wxrandr` still tries Mutter's public DisplayConfig route first. It contacts the
overlap extension only when Mutter rejects the requested positions and the user has
supplied the explicit unsafe option. The exact checks, warning and recovery path are
documented in [WXRANDR.md](WXRANDR.md#--unsafe-gnome-overlap-the-one-route-through).

Once the tools could reach each compositor, the next surprises came from the answers
they received.

## When correct APIs produce incompatible answers

The first replies looked reasonable. GNOME returned a workspace count, KWin returned
a handle for each window, and the display APIs accepted new layouts. Only when those
answers were fed back into old commands did their different meanings become visible.

X11 tools assume that a desktop count is fixed, a window ID is numeric and a display
change can be temporary. The compositor APIs describe newer models in which none of
those assumptions necessarily holds. The APIs were answering correctly, but a direct
translation would still lie to the script. Each case below forced a choice between an
exact mapping, a stable substitute and an explicit refusal.

### The workspace that exists only to remain empty

GNOME uses dynamic workspaces by default. Mutter always keeps an empty workspace at
the end of the list so that the user has somewhere to move a window. Its
`get_n_workspaces()` method includes that workspace.

On a fresh session, `wmctrl -d` therefore reports two workspaces even when the
overview appears to show only one in use. Mutter also refuses a request such as
`wdotool set_num_desktops 4`, because a fixed count is incompatible with dynamic
workspaces.

w11 had not misread the reply. The count was Mutter's real count, and a fixed setter
had no honest implementation while dynamic workspaces were enabled. The only useful
answer was to report Mutter's count and explain why the setter was refused.

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

That gives native Wayland windows an ID that old scripts can carry between commands.
XWayland windows are a separate problem because those scripts need the real X ID, not
a substitute. The first solution appeared to work until a deliberately ambiguous
pair of windows exposed it. That failure is the fourth story in the next section.

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

## What only a running desktop revealed

The mismatches above could be found by comparing the X11 model with each compositor's
API. The next problems were less polite. In each case, the protocol connection
worked, the request was valid and at least one component believed it had succeeded.
The result on the desktop was still wrong.

That distinction changed how the tools had to be tested. A reply from the compositor
was no longer enough. The tests also had to watch the window after it acknowledged a
resize, the X server chosen by a root process, and the pixels displayed after a
capture began. The four stories below are the failures that made those observations
part of the contract.

### 1. Mirroring turned both displays black

The purpose of `wmirror` sounds simple: capture a region of one output and display it
in a fullscreen `wl-mirror` window on another. This is useful when the source is only
part of a display or when the two displays have shapes that output-level mirroring
cannot represent. It is also safe while the source and target occupy separate
rectangles.

Now place both outputs at the same coordinates, as is commonly done to mirror a
wlroots layout. The fullscreen target window occupies pixels that also belong to the
source. The next captured frame therefore contains the window displaying the previous
captured frame.

I expected a video-feedback tunnel. On a live sway session, both screens instead
became completely black and stayed that way until I killed `wl-mirror` from another
tty.
For this reason, `wmirror` does not merely warn when source and target overlap. It
refuses to start, identifies the conflicting geometry and prints the `wxrandr`
command for output-level mirroring when that is the operation the user intended.

### 2. The valid X cookie belonged to the wrong display

The next failure started with a common administrative task: run an existing desktop
script through `sudo`, a root SSH session or cron. On an X11 desktop, w11 hands the
command to the original tool. Before it can do that, it must recover the active
user's `DISPLAY` and `XAUTHORITY` so the tool can open the correct X server.

The obvious search checks the caller's environment, the runtime directory and
`~/.Xauthority`. On a Plasma session started by SDDM 0.20, I found a real cookie and
the X server still rejected it:

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

Maximizing a window looks like one action to a person, but wmctrl represents the two
axes separately. On X11, the following command becomes one client message containing
both state atoms:

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

The window-ID problem returned when native and XWayland windows had to appear in the
same `wwmctl` list. Plasma 6 removed the scriptable properties that once exposed an
XWayland window's real X ID. KWin still described the window, and XWayland still
listed the ID in `_NET_CLIENT_LIST`, but neither side supplied a value that directly
joined the two records.

The first matcher filtered on process ID and `WM_CLASS`, then scored the remaining
pairs by title and geometry. It looked exact with ordinary applications. To test the
assumption, I wrote one X client that created two top-level windows with the same
process ID, class, title and rectangle. `WM_WINDOW_ROLE` recorded which was which for
the test, but the matcher did not use it. With no meaningful difference between the
candidates, their order decided the answer. The matcher swapped the two IDs in six of
eight runs.

A more complicated score could only hide the ambiguity. The fix was to state when an
answer was justified: a pair must agree on process ID or class, and candidates that
cannot be distinguished keep ID 0. The useful promise is not that every window gets
an ID. It is that an ID, when present, belongs to the right window.

This became a rule for the rest of w11. A specific refusal is better than a successful
command aimed at the wrong object.

## Why typing text becomes a keyboard-layout problem

Window management varies visibly between compositors. Typing the letter `a` looks as
if it should be the easy part. The command receives a character, the virtual keyboard
presses a key, and the focused application receives that character.

The missing step is the one a physical keyboard normally hides. Input interfaces do
not send a character; they send a keycode. A keymap turns that keycode, together with
modifiers and the selected layout, into `a`, `A`, `ä` or something else. wdotool must
therefore answer three questions for every piece of text: how to inject the event,
which keymap will interpret it, and which configured layout is active now.

The portable answer to the first question is `/dev/uinput`. wdotool creates a virtual
keyboard, a relative mouse and an absolute tablet. Compositors process those devices
like physical hardware, so the path works on GNOME, KDE and desktops without a
virtual-input protocol. It requires root or a udev rule. Device creation also causes
roughly 600 milliseconds of hotplug settling, so a small daemon owns the devices and
later commands connect to it.

Many wlroots compositors provide a second answer that needs no special access.
`zwp_virtual_keyboard_v1` accepts an uploaded keymap and key events, while
`zwlr_virtual_pointer_v1` accepts motion, buttons and scrolling. sway advertises both
interfaces to every client on its Wayland socket. wdotool uses them when the kernel
device is unavailable.

That solves the transport question, but each path answers the keymap question
differently. With the Wayland protocol, the compositor interprets keycodes through a
keymap uploaded by wdotool. That keymap is currently US, so this path cannot produce
characters absent from that keymap, such as `ü`. With `/dev/uinput`, the compositor
uses the desktop's active XKB keymap. A fixed US table then types `z` when a German
user asks for `y`.

wdotool solves this by reading the compositor's keymap and constructing the mapping
in reverse: character to keycode plus modifier mask. AltGr is discovered from the
keymap rather than assumed to be Right Alt, and dead-key characters become two key
presses that the application composes.

That leaves the third question: which configured layout is active? XKB calls the
selected layout a group. Wayland sends the current group to the focused client, but
an input injector is never that client. wdotool therefore asks each desktop through
the best interface it provides: KWin's keyboard-layout method, GNOME's input-source
setting, Hyprland's device state, Wayfire's keyboard state and Cinnamon's input-source
setting. sway and COSMIC provide the state on the Wayland connection itself. Where no
source exists, wdotool states which layout it has assumed.

A final trap appeared in `--clearmodifiers`. Linux discards a key-up event when the
emitting device does not hold that key. A virtual keyboard therefore cannot release
Shift or Control held on a physical keyboard. Trying to fake the release can leave
the modifier stuck because Mutter and KWin combine key state across the seat's
devices. wdotool clears only modifiers held by its own device and reports the
physical modifier when it can identify one.

## What the test environment had to model

By this point, a valid reply from a protocol no longer looked like proof that a tool
worked. The test environment had to reproduce the layers around the protocol: the
client responding to a configure event, the display manager choosing an authority
file, and the pixels produced by a capture loop. The repository currently contains
4302 tests, but the useful part is the range of disagreements they can expose.

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

The VM tests still had a blind spot: they behaved like test machines. They booted,
ran a workload and disappeared. A real desktop logs out, sits idle for an afternoon,
changes keyboard layouts and survives upgrades. When I began using w11 that way,
several failures appeared that the short-lived tests had never created.

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

The overlap extension raised a different long-term question: what happens when an
upgrade moves the private fields it writes? To exercise that failure before a real
upgrade did, the tests installed twelve deliberately incorrect structure
descriptions. Every one had to be rejected by the six compatibility checks before a
write occurred, with the shell session still alive. This is why the extension checks
the running build on every use rather than trusting what was true when the package was
installed. It reduces the risk described earlier; it does not make the private
interface stable.

## The security boundary is part of the interface

One question remained after the tools worked: who had received the power to use them?
These commands exist to give scripts access that ordinary Wayland clients do not
have, so the answer had to be part of the interface rather than an installation
footnote.

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
