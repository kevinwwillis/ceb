# CEB — Combined Environment Bundles

Display and audio profiles for GNOME (Wayland and X11). A profile says which screens are on, where
they are, which one is primary, and the default audio output and input. CEB remembers every screen
and audio device it has seen, so profiles can include ones that are not plugged in right now.

> **Requires GNOME 49 or newer.** The window needs libadwaita 1.8 or newer (GNOME 49: Ubuntu 25.10,
> Fedora 43 and later); the command-line tool needs GNOME's Mutter and PyGObject. Older GNOME
> versions are not supported.

Screens are recognised by their make, model and serial, not by the port they are plugged into, so
profiles keep working when a multi-GPU machine renumbers its ports between boots. Two identical
monitors are told apart by port order. Details: [docs/behavior.md](docs/behavior.md).

Status: first release (0.1): core library, command-line tool and a GTK 4 window.

## Install

1. System packages (PyGObject, GTK 4, libadwaita 1.8+, cairo support for PyGObject):

   ```bash
   # Ubuntu / Debian
   sudo apt install pipx python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1
   # Fedora
   sudo dnf install pipx python3-gobject gtk4 libadwaita
   ```

   Audio goes through the PulseAudio client library, so you need PipeWire with its PulseAudio
   server (`pipewire-pulse`, the default on current Ubuntu and Fedora) or PulseAudio itself.

2. CEB itself, with pipx. `--system-site-packages` lets it use the PyGObject installed above
   (PyGObject from PyPI would have to be compiled):

   ```bash
   pipx install --system-site-packages git+https://github.com/kevinwwillis/ceb
   ```

   This puts `ceb` and `ceb-gui` in `~/.local/bin`. `pip install --user .` from a checkout works
   too, on distributions that allow it (Ubuntu 23.04+ and Debian 12+ refuse it; use pipx there).

3. Add CEB to the applications menu (a `.desktop` file and the icon in `~/.local/share`):

   ```bash
   ceb install-desktop              # ceb install-desktop --uninstall removes them
   ```

   The menu entry runs the absolute path of the installed `ceb-gui`, so it works even though
   GNOME's PATH may not include `~/.local/bin`.

To remove CEB: `ceb install-desktop --uninstall`, turn off "Remember Screens in the Background" in
the window (if you turned it on), then `pipx uninstall ceb-profiles`. Profiles and history stay in
`~/.config/ceb/`.

## What applying a profile does

Applying switches the screens at once, without GNOME's "Keep changes / Revert" prompt, and sets the
default audio output and input. The layout is temporary: it stays until GNOME reconfigures the
screens (a screen is plugged in or unplugged, you log out, or the computer restarts); then GNOME goes
back to the layout set in Settings > Displays. Press the profile's keyboard
shortcut (or `ceb apply <profile>`) to apply it again.

If something the profile needs is missing (a screen not connected, an audio device not present),
nothing is changed and you are told what is missing; you can then apply the rest (the window's
"Apply the Rest" or "Test the Rest", or `ceb apply --allow-missing`).

## When screens and audio devices are remembered

CEB records every screen and audio device that is connected whenever it runs: each `ceb` command
that reads them (`list`, `save-current`, `apply`), and the window while it is open (also when a
screen or audio device is plugged in or out meanwhile). A screen that is connected only while
CEB is not running is not remembered, unless you turn on **Main menu > Remember Screens in the
Background**: that runs `ceb watch` now and at every login (an entry in `~/.config/autostart`),
which records every change as it happens. Turning it off removes the entry and stops the watcher.

## Window

```bash
ceb-gui                 # or: ceb gui, or CEB in the applications menu
ceb-gui --dry-run       # Switch and Test only ask GNOME to check the layout; nothing changes
ceb-gui --demo          # example screens and audio, throwaway store, in-memory shortcuts
```

Profiles are listed on the left with their keyboard shortcut. For the selected profile: drag the
screens (they snap to each other's edges; a screen dropped where GNOME would refuse it moves to the
nearest place GNOME accepts), add screens that are connected or remembered, make one primary or
turn it off, pick the audio output and input ("Leave unchanged" keeps whatever is set), and set a
keyboard shortcut. A profile may hold screens that are not connected now.

Editing never changes your screens or audio. The bar along the bottom of the profile says whether
there are unsaved changes, and holds:

- **Undo** and **Redo** for the changes made since the last Save or Discard;
- **Discard** and **Save**: nothing is written until you press Save (this includes the keyboard
  shortcut). Opening another profile, making a new one, renaming, or closing the window with
  unsaved changes asks first: Save, Discard or Cancel. If the profile was changed outside the window
  meanwhile (for example `ceb save-current --force`), Save asks whether to replace it or discard your
  changes and show the newer one;
- **Test** with its length in seconds: switches to the profile as shown (unsaved changes included)
  and goes back to the screens and audio you had before after that many seconds, unless you press
  **Keep**; **Go back now** goes back at once, and **Retry** appears if going back failed. The
  length (15 s at first) is one setting for every profile, kept in `settings.json` next to the
  profiles. Closing the window, or stopping CEB (logout, `kill`, Ctrl+C), during a test goes back
  first. If the audio devices cannot be read when a test starts, it tests the screens only;
- **Switch to profile**: switches the screens and audio to the saved profile, as its keyboard shortcut
  does. Unsaved changes are not used; save them first to switch to them.

Switching and testing say what is missing when the profile needs a screen or audio device that is
not connected, and offer to apply or test the rest.

Keyboard: in the profile list, Tab and the arrow keys move the focus and Enter or Space opens the
profile. In the arrangement, the arrow keys select the next or previous screen and Ctrl+arrow keys
move the selected screen one step, snapping like a drag, to the next place GNOME accepts. Ctrl+S
saves, Ctrl+Z undoes, Ctrl+Shift+Z (or Ctrl+Y) redoes, Ctrl+Return switches to the saved profile,
Ctrl+N saves the current screens as a new profile. These do nothing while a dialog is open or a
text field (such as the test length) has the focus; there Ctrl+Z undoes typing.

Screens that mirror each other are one box with "+ N" in its name; each screen of the group is listed
under "Selected screen", so it can be turned off on its own. Main menu > Remembered Devices lists the
screens and audio devices CEB remembers that are not connected now, each with Forget.

## Keyboard shortcuts

Shortcuts are GNOME custom shortcuts (Settings > Keyboard > Custom Shortcuts) that run
`/absolute/path/to/ceb apply -- <profile>` (with `--config-dir DIR` before `apply` when the profiles
are not in the default folder). CEB uses the absolute path of the installed `ceb` because GNOME
starts shortcuts without your shell's PATH; if it cannot find the installed `ceb`, it says so and
sets no shortcut. CEB changes only the custom shortcuts whose command is exactly that, and deleting a
profile (in the window or with `ceb delete`) removes its shortcut.

## Command line

```bash
ceb list                         # profiles, every screen ever seen, audio devices
ceb save-current desk            # save the current screens + default output and input
ceb save-current desk --force    # replace it
ceb save-current tv --no-audio   # screens only; applying it leaves audio alone
ceb show desk
ceb apply desk --dry-run         # plan it and let GNOME verify it; changes nothing
ceb apply desk                   # switch (no "Keep changes" prompt)
ceb apply desk --allow-missing   # apply what is present, report what is skipped
ceb delete desk                  # the profile and its keyboard shortcut
ceb watch                        # keep running and remember every screen and audio device
ceb install-desktop              # add CEB to the applications menu
ceb --config-dir DIR <command>   # profiles and history in DIR instead of ~/.config/ceb
```

A profile name may start with "-"; on the command line it then goes after `--`, as with other
Unix tools: `ceb apply -- -work`.

`ceb apply` is meant for keyboard shortcuts: every failure shows as a desktop notification, and so
does a wrong command line when there is no terminal. Exit codes: 0 done, 1 failed or refused
(nothing changed unless the message says "partly"), 2 wrong command line, 3 applied with parts
skipped (`--allow-missing`).

## Development

```bash
python3 -m venv --system-site-packages .venv   # system site packages for PyGObject
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

PyGObject is a system requirement, not a pip dependency, because pip would try to build it from
source; the `gi` extra (`pip install '.[gi]'`) does that for those who want it.

The window tests need a display; they run only under GTK's Broadway backend, so they never open a
window on your desktop:

```bash
gtk4-broadwayd :7 &
GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 .venv/bin/python -m pytest tests/test_gui_window.py
```

## Library

```python
from ceb_profiles.core import Ceb

ceb = Ceb()
state, audio, errors = ceb.current()      # connected screens and audio devices, recorded in history
known = ceb.known()                       # every screen and audio device ever seen
ceb.save_current("desk")
result = ceb.apply("desk", dry_run=True)  # ApplyResult: problems, errors, display_plan, audio_plan
before = ceb.live_profile()               # what is on now, not stored (AudioError if audio can't be read)
ceb.apply_profile(before)                 # apply a profile object (the window's Test goes back this way)
ceb.rename_profile("desk", "office")      # keeps its place in the list
ceb.forget_screen(known.screens[0].screen)  # drop a screen from the history (forget_audio too)
```

## License

MIT
