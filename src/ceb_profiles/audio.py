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

import threading
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

    CLIENT_NAME = "ceb"

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
            # Every output also has a "Monitor of ..." source; those are not microphones. They are
            # kept apart (AudioState.monitors), since one can be the default source.
            sources = pulse.source_list()
            inputs = [AudioDevice(s.name, s.description) for s in sources if s.monitor_of_sink == PA_INVALID_INDEX]
            monitors = [AudioDevice(s.name, s.description) for s in sources if s.monitor_of_sink != PA_INVALID_INDEX]
            return AudioState(outputs, inputs, info.default_sink_name or None,
                              info.default_source_name or None, monitors)
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

    def watch(self, callback) -> "AudioEvents":
        """Call `callback()` (from a background thread) whenever an audio device appears or goes
        away, or the default device changes. Returns the running watcher; stop it with .stop()."""
        events = AudioEvents(callback, self.CLIENT_NAME + "-events")
        events.start()
        return events

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


class AudioEvents:
    """The audio server's device events, in a background thread (pulsectl event subscription).

    `callback()` is called from that thread, never from libpulse's own callback, after a sink or
    source (output or input), or a card, appears or goes away, or the server's defaults change.
    Volume changes and streams are not subscribed to. The thread waits in poll() and wakes at
    least every LISTEN_S to check whether it was stopped, so it costs no CPU while nothing
    happens. If the audio server is not running or restarts, it reconnects (after 1 s, doubling to
    at most 30 s) and calls `callback()` once it is connected again, so nothing is missed."""

    LISTEN_S = 2.0
    RETRY_MAX_S = 30.0

    def __init__(self, callback, client_name: str = "ceb-events", pulse_factory=None):
        self.callback = callback
        self.client_name = client_name
        self._pulse_factory = pulse_factory
        self._stop = threading.Event()
        self._pulse = None
        self._fired = False
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="ceb-audio-events", daemon=True)
        self.thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        pulse = self._pulse
        if pulse is not None:
            try:
                pulse.event_listen_stop()
            except Exception:  # already closed: the loop ends within LISTEN_S anyway
                pass
        if self.thread is not None and timeout is not None:
            self.thread.join(timeout)

    def _connect(self):
        if self._pulse_factory is not None:
            return self._pulse_factory()
        import pulsectl
        return pulsectl.Pulse(self.client_name, connect=True)

    def _on_event(self, ev) -> None:
        if (ev.facility in ("sink", "source", "card") and ev.t in ("new", "remove")) or (
                ev.facility == "server" and ev.t == "change"):
            self._fired = True
            import pulsectl
            raise pulsectl.PulseLoopStop  # leave event_listen; the callback runs outside libpulse

    def _run(self) -> None:
        delay, connected_before = 1.0, False
        while not self._stop.is_set():
            try:
                pulse = self._connect()
            except Exception:  # no server (yet), or libpulse/pulsectl missing
                self._stop.wait(delay)
                delay = min(delay * 2, self.RETRY_MAX_S)
                continue
            delay = 1.0
            self._pulse = pulse
            try:
                pulse.event_mask_set("sink", "source", "card", "server")
                pulse.event_callback_set(self._on_event)
                if connected_before:
                    self.callback()  # the server restarted: devices may have changed meanwhile
                connected_before = True
                while not self._stop.is_set():
                    self._fired = False
                    pulse.event_listen(timeout=self.LISTEN_S)
                    if self._fired and not self._stop.is_set():
                        self.callback()
            except Exception:  # disconnected (pulsectl.PulseDisconnected) or a failed call
                pass
            finally:
                self._pulse = None
                try:
                    pulse.close()
                except Exception:
                    pass
            self._stop.wait(delay)
