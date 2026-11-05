# PAM

Display and audio profiles for GNOME (Wayland and X11). A profile says which screens are on, where
they are, which one is primary, and the default audio output and input. PAM remembers every screen
and audio device it has seen, so profiles can include ones that are not plugged in right now.

Screens are recognised by their make, model and serial, not by the port they are plugged into, so
profiles keep working when a multi-GPU machine renumbers its ports between boots. Two identical
monitors are told apart by port order. Details: [docs/behavior.md](docs/behavior.md).

Status: core library, command-line tool and a first GTK 4 window.

## Install (development)

Needs GNOME (Mutter), PipeWire or PulseAudio, and PyGObject from your distribution
(`sudo apt install python3-gi`, `sudo dnf install python3-gobject`). PyGObject is a system
requirement, not a pip dependency, because pip would try to build it from source; the `gi` extra
(`pip install '.[gi]'`) does that for those who want it.

```bash
python3 -m venv --system-site-packages .venv   # system site packages for PyGObject
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

## Command line

```bash
pam list                         # profiles, every screen ever seen, audio devices
pam save-current desk            # save the current screens + default output and input
pam save-current desk --force    # replace it
pam save-current tv --no-audio   # screens only; applying it leaves audio alone
pam show desk
pam apply desk --dry-run         # plan it and let GNOME verify it; changes nothing
pam apply desk                   # switch (no "Keep changes" prompt)
pam apply desk --allow-missing   # apply what is present, report what is skipped
pam delete desk
```

`pam apply` is meant for keyboard shortcuts: every failure shows as a desktop notification, and so
does a wrong command line when there is no terminal. Exit codes: 0 done, 1 failed or refused
(nothing changed unless the message says "partly"), 2 wrong command line, 3 applied with parts
skipped (`--allow-missing`).

## Window

Needs GTK 4 and libadwaita 1.8 or newer (GNOME 49 and later); `pam-gui` checks this when it starts and
says what is missing. The command-line tool works without them.

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1   # GTK 4, libadwaita 1.8+
pam-gui                 # or: pam gui
pam-gui --dry-run       # Apply only asks GNOME to check the layout; nothing changes
pam-gui --demo          # example screens and audio, throwaway store, in-memory shortcuts
```

Profiles are listed on the left with their keyboard shortcut. For the selected profile: drag the
screens (they snap to each other's edges; a screen dropped where GNOME would refuse it moves to the
nearest place GNOME accepts), add screens that are connected or remembered, make one primary or
turn it off, pick the audio output and input ("Leave unchanged" keeps whatever is set), and set a
keyboard shortcut. Every change is saved at once; Apply switches to the profile. If the profile was
changed outside the window meanwhile (for example `pam save-current --force`), the window shows the
newer version instead of overwriting it.

Keyboard: in the profile list, Tab and the arrow keys move the focus and Enter or Space opens the
profile. In the arrangement, the arrow keys select the next or previous screen and Ctrl+arrow keys
move the selected screen one step, snapping like a drag, to the next place GNOME accepts. Ctrl+Return
applies, Ctrl+N saves the current screens as a new profile.

Screens that mirror each other are one box with "+ N" in its name; each screen of the group is listed
under "Selected screen", so it can be turned off on its own. Main menu > Remembered Devices lists the
screens and audio devices PAM remembers that are not connected now, each with Forget.

Shortcuts are GNOME custom shortcuts (Settings > Keyboard > Custom Shortcuts) that run
`pam apply <profile>`; PAM changes only the custom shortcuts whose command is exactly that.

The window tests need a display; they run only under GTK's Broadway backend, so they never open a
window on your desktop:

```bash
gtk4-broadwayd :7 &
GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 .venv/bin/python -m pytest tests/test_gui_window.py
```

## Library

```python
from pam_profiles.core import Pam

pam = Pam()
state, audio, errors = pam.current()      # connected screens and audio devices, recorded in history
known = pam.known()                       # every screen and audio device ever seen
pam.save_current("desk")
result = pam.apply("desk", dry_run=True)  # ApplyResult: problems, errors, display_plan, audio_plan
pam.rename_profile("desk", "office")      # keeps its place in the list
pam.forget_screen(known.screens[0].screen)  # drop a screen from the history (forget_audio too)
```

## License

MIT
