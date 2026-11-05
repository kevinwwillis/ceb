"""Fakes for Mutter's DisplayConfig and the audio server, and builders for a real three-monitor desk.

Monitor and audio data copied from GetCurrentState and the audio server of a real two-GPU desktop
(two identical Lenovo LI2364d that report the same serial, an ASUS ROG PG248Q, a TV on DP),
2026-11-05 (modes shortened)."""
from __future__ import annotations

import pytest

from pam_profiles.audio import AudioError
from pam_profiles.core import Pam
from pam_profiles.model import AudioDevice, AudioState
from pam_profiles.mutter import parse_current_state
from pam_profiles.store import Store

SCALES = [1.0, 1.25, 1.3333333730697632, 1.5, 1.6666666269302368, 2.0]


def modes(current="1920x1080@60.000"):
    out = []
    for mid, w, h, r in (("1920x1080@74.986", 1920, 1080, 74.9858627319336),
                         ("1920x1080@60.000", 1920, 1080, 60.0),
                         ("1920x1080@59.940", 1920, 1080, 59.94005584716797),
                         ("1280x720@60.000", 1280, 720, 60.0)):
        props = {}
        if mid == current:
            props["is-current"] = True
        if mid == "1920x1080@60.000":
            props["is-preferred"] = True
        out.append((mid, w, h, r, 1.0, SCALES, props))
    return out


LENOVO = ("LEN", "LEN LI2364d", "0x01010101", 'Lenovo Group Limited 23"')
ROG = ("AUS", "ROG PG248Q", "L8LMQS075392 ", 'ASUSTek COMPUTER INC 24"')  # real trailing space
PROJECTOR = ("STK", "S2-TEK TV", "SN-000000001", "S2-TEK TV")


def mon(connector, kind, current="1920x1080@60.000"):
    vendor, product, serial, name = kind
    return ((connector, vendor, product, serial), modes(current), {"display-name": name, "is-builtin": False})


def reply(monitors, logical=None, serial=1, layout_mode=1):
    """A GetCurrentState reply. logical: [(x, y, primary, [connector...])]; default: all off-layout
    monitors placed left to right in the given order, first one primary."""
    specs = {m[0][0]: m[0] for m in monitors}
    if logical is None:
        logical = [(i * 1920, 0, i == 0, [m[0][0]]) for i, m in enumerate(monitors)]
    lms = [(x, y, 1.0, 0, primary, [specs[c] for c in conns], {}) for x, y, primary, conns in logical]
    return (serial, monitors, lms, {"layout-mode": layout_mode, "supports-changing-layout-mode": True})


def desk(base, shuffle=False, extra=(), gpu="0000:2d:00.0"):
    """The real desk with the ports numbered from `base` (1 -> HDMI-1/2/3, 2 -> HDMI-2/3/4).
    Lenovo on the lowest port is on the left, ROG in the middle, the other Lenovo on the right."""
    left, right, rog = (mon(f"HDMI-{base}", LENOVO), mon(f"HDMI-{base + 1}", LENOVO),
                        mon(f"HDMI-{base + 2}", ROG))
    monitors = [left, right, rog] + list(extra)
    logical = [(0, 0, False, [f"HDMI-{base}"]), (1920, 0, True, [f"HDMI-{base + 2}"]),
               (3840, 0, False, [f"HDMI-{base + 1}"])]
    if shuffle:
        monitors = list(reversed(monitors))
    return reply(monitors, logical)


class FakeDisplay:
    def __init__(self, *replies, gpus=None):
        self.replies = list(replies)
        self.gpus = gpus or {}
        self.calls = []          # (method, [(connector, mode_id, x, y, primary)], properties)
        self.fail_with = {}      # method -> exception to raise once
        self.reads = 0

    def set(self, r):
        self.replies = [r]

    def get_state(self):
        self.reads += 1
        r = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return parse_current_state(r, lambda c: self.gpus.get(c))

    def apply(self, serial, method, logical_monitors, properties):
        # Literal values on purpose: 0 = verify, 1 = temporary. 2 (persistent) shows GNOME's prompt.
        assert method in (0, 1), f"method {method} must never be used"
        self.calls.append((method, [(m.connector, m.mode_id, lm.x, lm.y, lm.primary)
                                    for lm in logical_monitors for m in lm.monitors], dict(properties)))
        exc = self.fail_with.pop(method, None)
        if exc:
            raise exc

    def applied(self):
        """The layout sent with method 1 (temporary), or None."""
        temp = [c for c in self.calls if c[0] == 1]
        return temp[-1][1] if temp else None


UNITEK_OUT = AudioDevice("alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.analog-stereo",
                         "Audio Adapter (Unitek Y-247A) Analog Stereo")
HDMI_OUT = AudioDevice("alsa_output.pci-0000_2d_00.1.hdmi-stereo-extra1",
                       "GA102 High Definition Audio Controller Digital Stereo (HDMI 2)")
WEBCAM_IN = AudioDevice("alsa_input.usb-webcamvendor_NexiGo_N60_FHD_Webcam_Audio_Sep_8_2023-18_48_27-N60-02.mono-fallback",
                        "NexiGo N60 FHD Webcam Audio Mono")
UNITEK_IN = AudioDevice("alsa_input.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.mono-fallback",
                        "Audio Adapter (Unitek Y-247A) Mono")


class FakeAudio:
    def __init__(self, outputs=(UNITEK_OUT, HDMI_OUT), inputs=(WEBCAM_IN, UNITEK_IN),
                 default_output=UNITEK_OUT.name, default_input=WEBCAM_IN.name):
        self.state = AudioState(list(outputs), list(inputs), default_output, default_input)
        self.set_calls = []
        self.down = False
        self.snapshots = 0
        self.on_snapshot = None   # called with the snapshot number before each snapshot

    def snapshot(self):
        self.snapshots += 1
        if self.on_snapshot:
            self.on_snapshot(self.snapshots)
        if self.down:
            raise AudioError("could not connect to the audio server: connection refused")
        return AudioState(list(self.state.outputs), list(self.state.inputs),
                          self.state.default_output, self.state.default_input)

    def set_default_output(self, name):
        if not any(d.name == name for d in self.state.outputs):
            raise AudioError(f"audio output {name} is not present")
        self.set_calls.append(("output", name))
        self.state.default_output = name

    def set_default_input(self, name):
        if not any(d.name == name for d in self.state.inputs):
            raise AudioError(f"audio input {name} is not present")
        self.set_calls.append(("input", name))
        self.state.default_input = name


@pytest.fixture(autouse=True)
def private_dirs(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.config/pam and $XDG_RUNTIME_DIR."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return runtime


@pytest.fixture
def make_pam(tmp_path):
    def make(*replies, audio=None, gpus=None):
        display = FakeDisplay(*replies, gpus=gpus)
        pam = Pam(display=display, audio=audio or FakeAudio(), store=Store(tmp_path / "pam"))
        pam._sleep = lambda s: None   # no real waiting for audio devices
        return pam
    return make
