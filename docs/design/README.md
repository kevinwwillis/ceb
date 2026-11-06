# Design

The app was called PAM when these designs were made; it was renamed CEB on 2026-11-05. The mockup
files in `mockup-round1/` (`index.html`, `pam.js`, `picked-A.jpg`) are kept as they were shown, so they
still say PAM; only the tag that loaded a feedback script from outside this repository was removed
from `index.html`.

The owner picked **option A, the Settings-style sidebar**, on 2026-11-05 15:50 ET, from
mockup round 1 (`mockup-round1/index.html`, the first window on the page; screenshot `picked-A.jpg`).

Option A: profile list on the left (name + shortcut); on the right the arrangement canvas (drag,
snap, remembered screens drawn dashed as "not connected"), then "Other screens" (connected and
remembered screens not in this profile, with Add), the selected screen (make primary, turn off in
profile), audio output and input pickers, and the keyboard shortcut. Header: add profile (+) and
Apply (moved to the bottom bar in round 2, below). Options B and C on the same page were not picked
and are not a reference.

## Round 2: save without applying, and the bottom bar (TASK-1.12, 2026-11-05 18:31 ET)

Mockup: `mockup-round2/index.html` with `data.js`; screenshots `bar-A.jpg`, `save-explicit.jpg`,
`test.jpg`. The owner's picks:

- **Editing never changes the live screens or audio.** Only "Switch to profile" (formerly Apply),
  the keyboard shortcuts and Test do. The header keeps only +, the title and the menu.
- **Bar option A:** a bar docked along the bottom of the profile pane (the sidebar keeps its full
  height): the status ("● Unsaved changes" / "✓ Saved") on the left; Discard, Save, Test and
  "Switch to profile" on the right. Options B (floating pill) and C (full-width bar) were not picked.
- **Explicit saving** with Save and Discard ("Saved automatically" was not picked); opening another
  profile with unsaved edits asks first. Owner's note: add Undo and Redo.
- **Test:** switches now and goes back after N seconds unless Keep ("No Test" was not picked).
  Owner's note: a small box for the seconds, 15 by default, one setting for all profiles.

Where the window differs from the round-2 mockup, and why:

- **Undo and Redo** are icon buttons after the status (the owner asked for them; the mockup had none).
- **The test length box** ("15 s") sits just before Test: a three-digit text box, as the owner asked
  for, rather than a number field with − and + buttons, which was wide and split the number from "s".
- **Test uses the profile as shown, unsaved edits included; Switch uses the saved profile** (as
  the mockup's Switch does). Trying a layout before saving it is what Test is for.
- **The keyboard shortcut is an unsaved edit too**, written on Save, so "Saved" is never wrong.
- **"Test the Rest"** is offered when a tested profile needs a screen or device that is missing, as
  "Apply the Rest" is for Switch.
- **Retry** replaces "Go back now" when going back failed (the mockup had no failure state).
- **Colors** come from the system accent (the mockup's blue is the GNOME default accent).
- **Narrow windows** stack the status above the buttons, which wrap.

## Where the window differs from mockup A, and why (review fixes, 2026-11-05 16:57 ET)

- **Connected dot** is the mockup's green `#2ec27e` set directly; `var(--success-color)` rendered red
  on GTK 4.22 / libadwaita 1.9.
- **"Switch to profile"** (Apply until round 2) shows a spinner and "Switching…" while a switch runs
  (the mockup had no running state).
- **Profile name above the canvas in the narrow layout** (below 640 sp), because the header has no
  room for the title there.
- **Canvas focus ring** (libadwaita's: accent colour at half opacity) when the canvas has keyboard
  focus, and **Ctrl+arrow keys move the selected screen** (the mockup could only be dragged).
- **Snapping** also aligns right edges and bottom edges, and on each axis the nearest edge wins (the
  mockup applied every pair in turn). A press that moves less than GTK's drag threshold is a click
  and never moves a screen.
- **Mirror groups**: the box's name gets "+ N", and "Selected screen" lists the group's other screens,
  each with Select (the mockup had no mirrored screens).
- **Audio pickers' popup** marks the selected device with a check mark, as AdwComboRow does.
- **Main menu > Remembered Devices…** (a dialog, not in the mockup) to forget remembered screens and
  audio devices that are not connected. Kept out of the main page so it stays as picked.
- **Demo shortcuts** are Super+F2..F4 (the mockup showed Super+F1..F3; Super+F1 is GNOME's Help).
- **Main menu > Remember Screens in the Background** (a check item, not in the mockup): runs `ceb watch`
  now and at every login, so screens are remembered also while the window is closed.
