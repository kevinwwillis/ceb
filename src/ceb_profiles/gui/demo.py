"""Demo backends for `ceb-gui --demo`: example screens and audio devices, nothing real is touched.

The demo shows the GUI without a GNOME session (screenshots, development): the screens are a
three-monitor desk (two identical Lenovo twins and an ASUS ROG) plus a remembered projector that is
not connected; the audio devices are made up from the same desk. Applying a profile only records
what would have been sent. Shortcuts use an in-memory gsettings backend (see app.py).
"""
from __future__ import annotations

from ..model import (AudioDevice, AudioState, DisplayLayout, ModeSpec, Profile, ProfileLogicalMonitor,
                     ProfileScreen)
from ..mutter import parse_current_state

SCALES = [1.0, 1.25, 1.5, 2.0]


def _modes():
    out = []
    for mid, w, h, r in (("1920x1080@60.000", 1920, 1080, 60.0), ("1920x1080@59.940", 1920, 1080, 59.94),
                         ("1280x720@60.000", 1280, 720, 60.0)):
        props = {"is-preferred": True, "is-current": True} if mid == "1920x1080@60.000" else {}
        out.append((mid, w, h, r, 1.0, SCALES, props))
    return out


LENOVO = ("LEN", "LEN LI2364d", "0x01010101", 'Lenovo Group Limited 23"')
ROG = ("AUS", "ROG PG248Q", "L8LMQS075392", 'ASUSTek COMPUTER INC 24"')
PROJECTOR = ("STK", "S2-TEK TV", "SN-000000001", "SANTAK S2-TEK TV")


def _mon(connector, kind):
    vendor, product, serial, name = kind
    return ((connector, vendor, product, serial), _modes(), {"display-name": name, "is-builtin": False})


def _reply(monitors, serial):
    specs = {m[0][0]: m[0] for m in monitors}
    lms = [(i * 1920, 0, 1.0, 0, i == 0, [specs[m[0][0]]], {}) for i, m in enumerate(monitors)]
    return (serial, monitors, lms, {"layout-mode": 1, "supports-changing-layout-mode": False})


DESK = [_mon("HDMI-1", LENOVO), _mon("HDMI-2", LENOVO), _mon("HDMI-3", ROG)]
WITH_PROJECTOR = DESK + [_mon("DP-1", PROJECTOR)]


class DemoDisplay:
    def __init__(self, with_projector: bool = False):
        self.serial = 1
        self.monitors = WITH_PROJECTOR if with_projector else DESK
        self.sent = []  # (method, logical monitors) for every apply

    def get_state(self):
        return parse_current_state(_reply(self.monitors, self.serial))

    def apply(self, serial, method, logical_monitors, properties):
        assert method in (0, 1), "CEB never uses method 2"
        self.sent.append((method, logical_monitors))


OUTPUTS = [AudioDevice("alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.analog-stereo",
                       "Audio Adapter (Unitek Y-247A) Analog Stereo"),
           AudioDevice("alsa_output.pci-0000_2d_00.1.hdmi-stereo-extra1", "GA102 HDMI 2 (NVIDIA)"),
           AudioDevice("alsa_output.pci-0000_2f_00.4.iec958-stereo", "Starship/Matisse Digital (S/PDIF)")]
INPUTS = [AudioDevice("alsa_input.usb-NexiGo_N60_FHD_Webcam-02.mono-fallback", "NexiGo N60 FHD Webcam Mono"),
          AudioDevice("alsa_input.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.mono-fallback",
                      "Audio Adapter (Unitek Y-247A) Mono"),
          AudioDevice("alsa_input.pci-0000_2f_00.4.analog-stereo", "Starship/Matisse Analog Stereo")]


class DemoAudio:
    def __init__(self):
        self.state = AudioState(list(OUTPUTS), list(INPUTS), OUTPUTS[0].name, INPUTS[0].name)
        self.sent = []

    def snapshot(self):
        return AudioState(list(self.state.outputs), list(self.state.inputs),
                          self.state.default_output, self.state.default_input)

    def set_default_output(self, name):
        self.sent.append(("output", name))
        self.state.default_output = name

    def set_default_input(self, name):
        self.sent.append(("input", name))
        self.state.default_input = name


# Free on stock GNOME (Super+F1 opens Help there). seed() still checks for clashes like the window.
DEMO_KEYS = {"Desk — 3 monitors": "<Super>F2", "Center only": "<Super>F3", "Projector": "<Super>F4"}


def seed(ceb, shortcuts, same=None) -> None:
    """Fill an empty store with the example history and three profiles, and give the example
    profiles their shortcuts (the in-memory shortcut settings start empty on every run). A shortcut
    that clashes with one GNOME or another custom shortcut uses is left out, as the window would
    refuse it (`same` compares two accelerators; the window's same_accel)."""
    if not ceb.profiles():
        ceb.observe(DemoDisplay(with_projector=True).get_state(), ceb.audio.snapshot())
        for p in _profiles(ceb.display.get_state()):
            ceb.put_profile(p)
    if shortcuts.available:
        have = shortcuts.all()
        for p in ceb.profiles():
            if p.name in DEMO_KEYS and p.name not in have and not shortcuts.clash(DEMO_KEYS[p.name], p.name, same):
                shortcuts.set(p.name, DEMO_KEYS[p.name])


def _profiles(state):
    by_conn = {m.connector: m.screen_id for m in state.monitors}
    proj = parse_current_state(_reply(WITH_PROJECTOR, 1)).by_connector("DP-1").screen_id
    m = ModeSpec(1920, 1080, 60.0)

    def lm(x, sid, primary):
        return ProfileLogicalMonitor(x, 0, 1.0, 0, primary, [ProfileScreen(sid, m)])

    out, inp = OUTPUTS[0], INPUTS[0]
    return [
        Profile("Desk — 3 monitors", DisplayLayout([lm(0, by_conn["HDMI-1"], False), lm(1920, by_conn["HDMI-3"], True),
                                                    lm(3840, by_conn["HDMI-2"], False)], 1), out.ref(), inp.ref()),
        Profile("Center only", DisplayLayout([lm(0, by_conn["HDMI-3"], True)], 1), out.ref(), inp.ref()),
        Profile("Projector", DisplayLayout([lm(0, proj, True)], 1), OUTPUTS[1].ref(), inp.ref()),
    ]
