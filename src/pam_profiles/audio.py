"""Default audio output (sink) and input (source), through the PulseAudio client API.

Why pulsectl (libpulse via ctypes):
- PipeWire desktops run pipewire-pulse, so the PulseAudio API is the one interface that works on
  both PipeWire and plain PulseAudio systems.
- It needs no command-line tool: pactl is often not installed on PipeWire systems, and wpctl prints
  text meant for people, which would have to be parsed.
- It works inside a Flatpak: libpulse is part of the freedesktop/GNOME runtimes, and access is the
  standard `--socket=pulseaudio` permission. A Flatpak cannot run the host's wpctl.
- It is pure Python on top of libpulse.so.0 (no compiled extension), MIT-licensed, maintained.

Devices are stored by their node name (e.g. alsa_output.usb-C-Media_..._USB_Audio_Device-00.analog-stereo),
which PipeWire derives from the bus path and the device's own ids, so it survives reboots. The numeric
index changes on every restart of the audio server and is never stored.

Setting the default through pipewire-pulse stores it as WirePlumber's configured default, and
WirePlumber moves streams that follow the default.
"""
from __future__ import annotations

from typing import Protocol

from .model import AudioDevice, AudioState

PA_INVALID_INDEX = 0xFFFFFFFF


class AudioError(Exception):
    """Talking to the audio server failed."""


class AudioBackend(Protocol):
    def snapshot(self) -> AudioState: ...

    def set_default_output(self, name: str) -> None: ...

    def set_default_input(self, name: str) -> None: ...


class PulseAudioBackend:
    """The real backend (pulsectl). Connects for each call; profile switches are rare."""

    CLIENT_NAME = "pam"

    def _connect(self):
        try:
            import pulsectl
        except ImportError as e:
            raise AudioError(f"the Python package pulsectl is not installed: {e}") from e
        try:
            return pulsectl.Pulse(self.CLIENT_NAME, connect=True)
        except Exception as e:  # pulsectl.PulseError and ctypes/OSError when libpulse is missing
            raise AudioError(f"could not connect to the audio server: {e}") from e

    def snapshot(self) -> AudioState:
        pulse = self._connect()
        try:
            info = pulse.server_info()
            outputs = [AudioDevice(s.name, s.description) for s in pulse.sink_list()]
            # Every output also has a "Monitor of ..." source; those are not microphones.
            inputs = [AudioDevice(s.name, s.description) for s in pulse.source_list()
                      if s.monitor_of_sink == PA_INVALID_INDEX]
            return AudioState(outputs, inputs, info.default_sink_name or None,
                              info.default_source_name or None)
        except AudioError:
            raise
        except Exception as e:
            raise AudioError(f"could not read the audio devices: {e}") from e
        finally:
            pulse.close()

    def set_default_output(self, name: str) -> None:
        self._set_default(name, "sink")

    def set_default_input(self, name: str) -> None:
        self._set_default(name, "source")

    def _set_default(self, name: str, kind: str) -> None:
        pulse = self._connect()
        try:
            devices = pulse.sink_list() if kind == "sink" else pulse.source_list()
            device = next((d for d in devices if d.name == name), None)
            if device is None:
                raise AudioError(f"audio {'output' if kind == 'sink' else 'input'} {name} is not present")
            pulse.default_set(device)
        except AudioError:
            raise
        except Exception as e:
            raise AudioError(f"could not set the default {kind} to {name}: {e}") from e
        finally:
            pulse.close()
