# PAM

Display and audio profiles for GNOME (Wayland and X11). A profile says which screens are on, where
they are, which one is primary, and the default audio output and input. PAM remembers every screen
and audio device it has seen, so profiles can include ones that are not plugged in right now.

Screens are recognised by their make, model and serial, not by the port they are plugged into, so
profiles keep working when a multi-GPU machine renumbers its ports between boots. Two identical
monitors are told apart by port order. Details: [docs/behavior.md](docs/behavior.md).

Status: core library and command-line tool. The GTK window comes later.

## Install (development)

Needs GNOME (Mutter), PipeWire or PulseAudio, and PyGObject from your distribution
(`sudo apt install python3-gi`, `sudo dnf install python3-gobject`). PyGObject is a system
requirement, not a pip dependency, because pip would try to build it from source; the `gi` extra
(`pip install '.[gi]'`) does that for those who want it.

```bash
python3 -m venv --system-site-packages .venv   # system site packages for PyGObject
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
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

## Library

```python
from pam_profiles.core import Pam

pam = Pam()
state, audio, errors = pam.current()      # connected screens and audio devices, recorded in history
known = pam.known()                       # every screen and audio device ever seen
pam.save_current("desk")
result = pam.apply("desk", dry_run=True)  # ApplyResult: problems, errors, display_plan, audio_plan
```

## License

MIT
