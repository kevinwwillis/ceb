# How PAM finds screens and audio devices, and what it does when one is missing

Status: core library and CLI, 2026-11-05 16:00 ET (TASK-1.3, TASK-1.4, TASK-1.6 CLI part).

## Screens are found by what they are, not by port

A screen's identity is the vendor, product and serial from its EDID, as
`org.gnome.Mutter.DisplayConfig.GetCurrentState` reports them, with surrounding whitespace removed
(the ASUS ROG PG248Q reports `'L8LMQS075392 '`). The port name is never used to find a screen: on a
machine with two GPUs, the driver numbers the ports in the order the cards finish loading, so the
same cables appear as HDMI-1/2/3 on one boot and HDMI-2/3/4 on the next.

## Identical twins

Two monitors of the same model can report the same serial (Kevin's two Lenovo LI2364d both report
`0x01010101`, and their EDIDs are byte-for-byte identical, checked 2026-11-05 15:50 ET). Nothing
about the monitors tells them apart, so PAM uses port order:

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
- If the GPU of a port cannot be told for sure (no sysfs, or two cards both have a port of that
  name), it is left out of the key for that port and ordering falls back to type and number.

Limits (no data can fix these):

- If the two twins' cables are swapped between ports, the twins swap places in every profile.
- If only one twin is connected, it is always twin 0. A profile that needs both reports twin 2 as
  missing and says PAM cannot tell which physical twin is unplugged. A profile saved while only
  one twin was connected uses whichever twin is on the lowest port later.

## Remembered screens and audio devices

Every time PAM reads the screens or audio devices (list, save, apply; the GUI should also do it on
Mutter's `MonitorsChanged` signal), it records each one in `known.json`: identity and twin number,
display name, all modes, the last port and GPU, first and last seen. Entries are never removed
automatically, so the GUI can show and place screens that are not connected. Twins get one entry
each. Audio devices are remembered by name with their description.

## Applying a profile

- Screens are applied with `ApplyMonitorsConfig` method 1 (temporary): no "Keep changes / Revert"
  prompt. PAM first asks Mutter to verify the layout (method 0) and changes nothing if Mutter
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
  different layout from the one asked for, and silently doing something else is what PAM must not do.
- With `--allow-missing` (`allow_missing=True` in the API), PAM applies the rest, reports each
  skipped item, and exits 3. If the primary screen was skipped, the first remaining screen becomes
  primary; the layout is moved so it starts at (0, 0). Gaps are not closed: if the skipped screen
  was between two others, Mutter refuses the layout ("Logical monitors not adjacent") and that is
  reported.
- A profile with no screen left to turn on is never applied.
- If the screens fail to apply, audio is not changed.

## Desktop notifications

`pam apply` reports every failure through `org.freedesktop.Notifications` (D-Bus, through Gio),
falling back to `notify-send`, and always prints it to stderr. `--dry-run` and `--no-notify` print
only. Unexpected errors in `apply` are notified too, with the error type.

## Audio library: pulsectl

PAM uses [pulsectl](https://github.com/mk-fg/python-pulse-control) (MIT, ctypes bindings to
libpulse) to read and set the default sink and source through pipewire-pulse:

- one API that works on PipeWire (through pipewire-pulse) and on plain PulseAudio;
- no command-line tool to install or parse (`pactl` is often missing; `wpctl` output is for people);
- works in a Flatpak with the standard `--socket=pulseaudio` permission, and libpulse is in the
  GNOME runtime; a Flatpak cannot run the host's `wpctl`;
- pure Python, no compiled extension.

## Files

`$XDG_CONFIG_HOME/pam/` (default `~/.config/pam/`): `profiles.json` and `known.json`, both
`{"version": 1, ...}`. Writes are atomic and locked. A file PAM cannot parse, or one written by a
newer PAM, is reported and never overwritten.
