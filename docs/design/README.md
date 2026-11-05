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
Apply. Options B and C on the same page were not picked and are not a reference.

## Where the window differs from mockup A, and why (review fixes, 2026-11-05 16:57 ET)

- **Connected dot** is the mockup's green `#2ec27e` set directly; `var(--success-color)` rendered red
  on GTK 4.22 / libadwaita 1.9.
- **Apply** shows a spinner and "Applying…" while a switch runs (the mockup had no running state).
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
