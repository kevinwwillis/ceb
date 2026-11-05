# How CEB finds screens and audio devices, and what it does when one is missing

Status: core library, CLI and window, 2026-11-05.

## Screens are found by what they are, not by port

A screen's identity is the vendor, product and serial from its EDID, as
`org.gnome.Mutter.DisplayConfig.GetCurrentState` reports them, with surrounding whitespace removed
(the ASUS ROG PG248Q reports `'L8LMQS075392 '`). The port name is never used to find a screen: on a
machine with two GPUs, the driver numbers the ports in the order the cards finish loading, so the
same cables appear as HDMI-1/2/3 on one boot and HDMI-2/3/4 on the next.

## Identical twins

Two monitors of the same model can report the same serial (for example two Lenovo LI2364d that both
report `0x01010101`, with byte-for-byte identical EDIDs). Nothing about the monitors tells them
apart, so CEB uses port order:

- Among the connected screens that share an identity, the one on the lowest port is twin 0, the
  next twin 1, and so on. A profile stores the twin number, not the port.
- Port order is: GPU first (the card's PCI address, read from `/sys/class/drm/card*-<port>`), then
  the connector type (DP, DVI, HDMI, ...), then the port's numbers compared as numbers (HDMI-9
  before HDMI-10; MST ports like DP-1-2 compare part by part). Mutter's `HDMI-1` and the kernel's
  `HDMI-A-1` are the same type.
- Why this order survives renumbering: a boot race shifts all ports of a card by the same amount, so
  the order within a card stays. The type comes before the number because comparing numbers across
  types is not stable (HDMI-1 vs DP-2 can become HDMI-2 vs DP-2). The GPU comes first so twins on two
  different cards keep their order when the cards swap numbers.
- If the GPU of any twin's port cannot be told for sure (sysfs unreadable, the port not reported
  as connected during a hotplug, or two cards both have a port of that name), the GPU is left out
  for every twin of that model, and they are ordered by type and number only. Leaving it out for
  just one twin would let a failed lookup reorder them. When this happens with some GPUs known,
  `ceb apply` prints a note.

Limits (no data can fix these):

- If the two twins' cables are swapped between ports, the twins swap places in every profile.
- The order has no memory: it is worked out from the ports the twins are on now. Moving only one
  twin's cable can swap the twins, for example from one GPU to the other, or from HDMI to DP (DP
  sorts before HDMI), whenever the moved twin lands on the other side of its twin in port order.
  Re-save the affected profiles after moving a twin's cable, or swap their places in the profile.
- On a two-GPU machine where the twins are on different cards, a GPU lookup that fails falls back
  to port-name order, which can differ from the card order after the cards swap numbers at boot.
- If only one twin is connected, it is always twin 0. A profile that needs both reports twin 2 as
  missing and says CEB cannot tell which physical twin is unplugged. A profile saved while only
  one twin was connected uses whichever twin is on the lowest port later.

## Remembered screens and audio devices

Every time CEB reads the screens or audio devices, it records each one in `known.json`. It reads
them on `ceb list`, `save-current` and `apply`; in the window at start and on Mutter's
`MonitorsChanged` signal or an audio server device event (a sink, source or card appearing or going
away, or the defaults changing; pulsectl event subscription in a background thread), 0.3 s later,
in a background thread, and not while a screen is being dragged or a popover is open; and in `ceb
watch`, the background watcher, on the same events 1 s after the last one. `ceb watch` runs only
when started, or at every login when the window's Main menu > Remember Screens in the Background is
on (an autostart entry, `~/.config/autostart/io.github.kevinwwillis.Ceb.Watch.desktop`, running the
absolute path of `ceb`, with `--config-dir` when the profiles are not in the default folder). One
watcher runs per config folder (`$XDG_RUNTIME_DIR/ceb-watch-<hash>.lock` holds its process id); it
ends on SIGTERM, SIGINT or SIGHUP and when the session's D-Bus connection closes. The record holds: identity and twin number,
display name, all modes, the last port and GPU, first and last seen. Entries are never removed
automatically, so the GUI can show and place screens that are not connected. They are removed only
when asked (`Ceb.forget_screen`, `Ceb.forget_audio`; in the window, Remembered Devices > Forget,
offered only for ones not connected now); profiles that use a forgotten screen or device keep it. Twins get one entry
each. Audio devices are remembered by name with their description.

- "Last seen" is kept to the hour: seeing the same screens and devices again rewrites `known.json`
  only when something else changed or the stored time is more than an hour old.
- Updating the history is best-effort. If `known.json` cannot be read or written (a corrupt file,
  a read-only or full disk), applying and saving still work and the problem is reported (`ceb
  apply` prints it as a note, `ceb list` prints it and exits 1 after listing what it can).

## Applying a profile

- Screens are applied with `ApplyMonitorsConfig` method 1 (temporary): no "Keep changes / Revert"
  prompt. CEB first asks Mutter to verify the layout (method 0) and changes nothing if Mutter
  refuses. Method 2 (persistent) is never used; the backend refuses it. A temporary layout lasts
  until GNOME reconfigures the monitors (re-plugging a cable, a reboot); then apply again.
- Every connected screen the profile does not list is turned off.
- A profile stores each screen's mode as size and refresh rate; the mode must be offered by that
  screen now (refresh within 0.01 Hz) and the profile's scale must be supported in that mode.
- Audio output and input are stored by their PipeWire/PulseAudio node name, which survives reboots
  (the numeric index does not). A profile can leave screens, output or input unchanged (`null`).

When something the profile needs is missing (a screen not connected, a mode not offered, an audio
device not present):

- By default nothing at all is changed, every missing item is listed, and the command exits 1. From
  a keyboard shortcut the same text appears as a desktop notification. Reason: a partial layout is a
  different layout from the one asked for, and silently doing something else is what CEB must not do.
- With `--allow-missing` (`allow_missing=True` in the API), CEB applies the rest, reports each
  skipped item, and exits 3. Problems that make the layout itself wrong are never skipped: a
  profile saved in a layout mode GNOME cannot switch to, or one with no screen left to turn on. If the primary screen was skipped, the first remaining screen becomes
  primary; the layout is moved so it starts at (0, 0). Gaps are not closed: if the skipped screen
  was between two others, Mutter refuses the layout ("Logical monitors not adjacent") and that is
  reported.
- A profile with no screen left to turn on is never applied.
- Screens are applied first, then audio. If the screens fail to apply, audio is not changed.
- An HDMI or DisplayPort audio device (its node name or description says "hdmi" or "displayport")
  can exist only while its screen is on. When such a device is missing and the profile turns on a
  screen that is off now, CEB does not refuse: it applies the screens, then waits up to 5 s for the
  device. If it appears, it is set; if not, it is reported as missing (the screens have then
  changed, so `ceb apply` says "only partly applied" and exits 1, or 3 with `--allow-missing`).
  Another device is never used in its place. Any other missing device refuses as above.

## Desktop notifications

`ceb apply` reports every failure through `org.freedesktop.Notifications` (D-Bus, through Gio),
falling back to `notify-send`, and always prints it to stderr. `--dry-run` and `--no-notify` print
only. Unexpected errors in `apply` are notified too, with the error type. A wrong command line
(`ceb apply` without a name, a misspelt command) is notified when stderr is not a terminal, which is
the case for a keyboard shortcut, and exits 2. Failures use critical urgency; a partial success
with `--allow-missing` (exit 3) uses normal urgency.

## Audio library: pulsectl

CEB uses [pulsectl](https://github.com/mk-fg/python-pulse-control) (MIT, ctypes bindings to
libpulse) to read and set the default sink and source through pipewire-pulse:

- one API that works on PipeWire (through pipewire-pulse) and on plain PulseAudio;
- no command-line tool to install or parse (`pactl` is often missing; `wpctl` output is for people);
- works in a Flatpak with the standard `--socket=pulseaudio` permission, and libpulse is in the
  GNOME runtime; a Flatpak cannot run the host's `wpctl`;
- pure Python, no compiled extension.

## Files

`$XDG_CONFIG_HOME/ceb/` (default `~/.config/ceb/`): `profiles.json` and `known.json`, both
`{"version": 1, ...}`. Writes are atomic and locked. A file CEB cannot parse, one with values of the
wrong type, or one written by a newer CEB, is reported and never overwritten. If either file is a
symlink, CEB writes to the file it points to and the link stays. If `profiles.json` holds two
profiles with the same name (only possible by editing it by hand), CEB uses the first one and the
window says so in its banner.

Lock files: `.lock` in that folder for the files, and `$XDG_RUNTIME_DIR/ceb-apply.lock` (or
`.apply.lock` in the config folder when there is no runtime folder) so only one profile switch runs
at a time. CEB never follows a symlink there, never truncates them, and refuses one owned by
another user.
