# vm/demo — recording the README GIFs

Three GIFs in `media/` are recorded here, all of them on **`resolute-gnome-iso`**,
the image built by the real Ubuntu 26.04 desktop installer, because a README example
should be a default desktop unless the example is about some other configuration:

| file | what it shows | head | length |
|---|---|---|---|
| `media/install-demo.gif` | `sudo apt install ./w11_0.4.0_all.deb`, the package's own post-install note, then the six tools answering `--version` | 0 | ~28 s |
| `media/warandr-demo.gif` | warandr on a two head layout: the second monitor dragged under the first, Apply, a window sent down onto the monitor that is now below, then `warandr --save` and the script it wrote | 0 | ~32 s |
| `media/wdotool-demo.gif` | wdotool and wwmctl on the same desktop: a window placed and sized, text typed into it, `wwmctl -l -G`, fullscreen on and off, pointer moved and clicked | 0 | ~34 s |

## How it works

Nothing is captured inside the guest. `qmprec.py` holds one QMP connection open and
issues `screendump` on a fixed clock, so what lands on disk is what the virtual GPU
scans out and the recorder does not care which compositor is running. `ppm` is used
because a 1280x720 dump costs about 3 ms that way against about 240 ms as png, which
is the difference between 12 fps and 4.

The guest side is `typer.py`: it runs a real interactive `bash` in a pty inside the
terminal window and types into it, character by character, at a readable cadence. So
every command in the recordings really ran, in the terminal you are looking at. A
take is a small script in `takes/`:

```
# comment
~ SECONDS          sleep
> COMMAND          type COMMAND at the cadence, then Return
% TEXT             write TEXT with no delay (\r for Return): the beats a viewer
                   is not meant to read, such as the mouse driving in take 2
@ REGEX SECONDS    wait until REGEX shows up in the output
? REGEX<TAB>SECONDS<TAB>TEXT   type TEXT only if REGEX shows up (the sudo prompt,
                   which is there or not depending on the sudo timestamp)
```

`takes/stage-<take>.sh`, if it exists, runs in the guest just before the recorder
starts: it puts the desktop into the state the take assumes (layout side by side, no
leftover windows, no restored editor draft, pointer parked).

## Recording them again

Three things about the guest are recording settings, not part of what is being
demonstrated. Set them on the instance before the first take:

```sh
vm/vmctl start demo --flavor resolute-gnome-iso --heads 2 --head-size 1280x720 --mem 6G

# the pointer is on a hardware cursor plane, which screendump does not see
vm/vmctl ssh demo -- 'printf "MUTTER_DEBUG_DISABLE_HW_CURSORS=1\n" >> /etc/environment; reboot'

# a legible terminal, and no screen blanking in the middle of a take
vm/vmctl user demo -- sh -c '
  gsettings set org.gnome.Ptyxis use-system-font false
  gsettings set org.gnome.Ptyxis font-name "Ubuntu Mono 17"
  gsettings set org.gnome.Ptyxis interface-style dark
  gsettings set org.gnome.Ptyxis audible-bell false
  gsettings set org.gnome.Ptyxis prompt-on-close false
  gsettings set org.gnome.desktop.session idle-delay 0
  gsettings set org.gnome.desktop.screensaver lock-enabled false'
```

Then build the package, put it in the guest's home, and take them in this order,
because take 1 must run on a machine where the tools are *not* installed yet and
takes 2 and 3 need them installed and the GNOME session restarted once so the
bridge extension is loaded:

```sh
sh scripts/build-deb.sh
vm/vmctl scp demo release/w11_0.4.0_all.deb demo:/home/test/
vm/vmctl ssh demo -- 'chown test:test /home/test/w11_0.4.0_all.deb'

# take 1, on a machine without the tools
vm/vmctl ssh demo -- 'apt-get -qq purge -y w11'
vm/demo/record.sh install 0 83 420
vm/demo/encode.sh ~/vm-data/frames/install 0 12 960 media/install-demo.gif 3 340

# the relogin the package asks for, then takes 2 and 3
vm/vmctl ssh demo -- reboot
vm/demo/record.sh warandr 0 83 460
vm/demo/encode.sh ~/vm-data/frames/warandr 0 12 960 media/warandr-demo.gif 6 392
CPS=34 vm/demo/record.sh wdotool 0 83 480
vm/demo/encode.sh ~/vm-data/frames/wdotool 0 12 960 media/wdotool-demo.gif 4 408
```

The last two arguments of `encode.sh` trim frames off the ends, which is how the
shell's own `exit` at the end of a take is kept out of the picture. Adjust them if a
take drifts by a beat.

## Things that will bite

- **The pointer.** Mutter puts the cursor on a hardware plane and `screendump` dumps
  the scanout, so without `MUTTER_DEBUG_DISABLE_HW_CURSORS` every recording has an
  invisible mouse. Take 1 has no pointer motion in it, so park the pointer with
  `wdotool mousemove` while the package is still installed, and purge afterwards.
- **The drag in take 2 is pixel exact.** warandr snaps on drop within `SNAP_PX = 5`
  canvas pixels, and Mutter refuses any layout whose monitors are not exactly
  adjacent, with the refusal in a dialog. The drop point in `takes/wa-drag.sh` is
  computed from the window being at 313,98 sized 720x557, which is why the script
  moves it there first.
- **A window has to have settled before it can be moved.** `search --sync` returns
  when the window is mapped, and Mutter may still place it after that, undoing a
  `windowmove` issued too early. Take 3 sleeps 1.2 s inside the chain, and again
  between the resize and the move, because a resize can trigger a re-place of its
  own.
- **Frames are big.** 400 ppm frames of 1280x720 is about 1.1 GB. `record.sh` clears
  the frame directory each run, but keep an eye on the disk.
