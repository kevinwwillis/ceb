"""`ceb watch`, the audio server's device events and the background watcher's autostart entry.
No real audio server, D-Bus session or ~/.config is used."""
import os
import threading
import time

import pytest

from conftest import HDMI_OUT, PROJECTOR, desk, mon
from ceb_profiles import desktop
from ceb_profiles.audio import AudioEvents

gi = pytest.importorskip("gi")
from gi.repository import GLib  # noqa: E402


# ---------------------------------------------------------------- audio events


class Ev:
    def __init__(self, facility, t):
        self.facility, self.t = facility, t


class FakePulse:
    """Stands in for pulsectl.Pulse: event_listen delivers queued events, then waits."""

    def __init__(self, events, disconnect_after=None):
        self.events = list(events)
        self.callback = None
        self.mask = None
        self.closed = False
        self.listens = 0
        self.disconnect_after = disconnect_after
        self.wake = threading.Event()

    def event_mask_set(self, *masks):
        self.mask = masks

    def event_callback_set(self, fn):
        self.callback = fn

    def event_listen(self, timeout=None):
        import pulsectl
        self.listens += 1
        if self.disconnect_after is not None and self.listens > self.disconnect_after:
            raise pulsectl.PulseDisconnected()
        while self.events:
            try:
                self.callback(self.events.pop(0))
            except pulsectl.PulseLoopStop:
                return
        self.wake.wait(min(timeout or 0.05, 0.05))

    def event_listen_stop(self):
        self.wake.set()

    def close(self):
        self.closed = True


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_device_appearing_or_going_calls_back_volume_changes_do_not():
    pytest.importorskip("pulsectl")
    pulse = FakePulse([Ev("sink", "change"), Ev("sink_input", "new"), Ev("sink", "new"),
                       Ev("source", "remove"), Ev("server", "change")])
    calls = []
    events = AudioEvents(lambda: calls.append(1), pulse_factory=lambda: pulse)
    events.start()
    assert wait_for(lambda: len(calls) == 3)
    events.stop(timeout=2)
    assert not events.thread.is_alive() and pulse.closed
    assert set(pulse.mask) == {"sink", "source", "card", "server"}
    assert len(calls) == 3


def test_reconnects_after_the_audio_server_restarts(monkeypatch):
    pytest.importorskip("pulsectl")
    first, second = FakePulse([], disconnect_after=1), FakePulse([])
    made = iter([first, second])
    calls = []
    events = AudioEvents(lambda: calls.append(1), pulse_factory=lambda: next(made, second))
    events._stop.wait = lambda delay: events._stop.is_set()  # no real 1 s back-off in the test
    events.start()
    assert wait_for(lambda: calls)  # called once connected again: devices may have changed
    events.stop(timeout=2)
    assert first.closed


def test_no_audio_server_keeps_retrying_and_stops_cleanly():
    tries = []

    def refuse():
        tries.append(1)
        raise ConnectionRefusedError("no server")
    events = AudioEvents(lambda: None, pulse_factory=refuse)
    events.start()
    assert wait_for(lambda: tries)
    events.stop(timeout=3)
    assert not events.thread.is_alive()


# ---------------------------------------------------------------- the watcher


class FakeBus:
    def __init__(self):
        self.subscribed = []
        self.unsubscribed = []
        self.closed_handlers = []

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        self.subscribed.append((sender, iface, member, path))
        self.callback = callback
        return 7

    def signal_unsubscribe(self, sid):
        self.unsubscribed.append(sid)

    def connect(self, name, handler):
        self.closed_handlers.append(handler)


def run_loop_until(watcher, cond, timeout=3.0):
    ctx = watcher.loop.get_context()
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        ctx.iteration(False)
        time.sleep(0.005)
    return cond()


def test_watch_records_screens_at_start_and_on_monitors_changed(make_ceb):
    from ceb_profiles.watch import Watcher
    ceb = make_ceb(desk(1))
    bus = FakeBus()
    w = Watcher(ceb, settle_ms=10, log=lambda t: None)
    w.start(bus=bus, watch_audio=False)
    assert bus.subscribed == [("org.gnome.Mutter.DisplayConfig", "org.gnome.Mutter.DisplayConfig",
                               "MonitorsChanged", "/org/gnome/Mutter/DisplayConfig")]
    assert len(ceb.known().screens) == 3 and w.observations == 1

    ceb.display.set(desk(1, extra=[mon("DP-1", PROJECTOR)]))
    for _ in range(5):  # a burst of signals is read once
        bus.callback()
    assert run_loop_until(w, lambda: w.observations == 2)
    assert len(ceb.known().screens) == 4
    w.stop()
    assert bus.unsubscribed == [7]


def test_watch_records_audio_devices_from_audio_events(make_ceb):
    from ceb_profiles.watch import Watcher
    ceb = make_ceb(desk(1))
    fired = {}

    class Events:
        stopped = False

        def stop(self, timeout=None):
            Events.stopped = True

    def watch(callback):
        fired["cb"] = callback
        return Events()
    ceb.audio.watch = watch
    w = Watcher(ceb, settle_ms=10, log=lambda t: None)
    w.start(bus=FakeBus())
    new = type(HDMI_OUT)("alsa_output.usb-new-device.analog-stereo", "New USB Speaker")
    ceb.audio.state.outputs.append(new)
    threading.Thread(target=fired["cb"]).start()   # the audio thread calls back
    assert run_loop_until(w, lambda: any(a.name == new.name for a in ceb.known().outputs))
    w.stop()
    assert Events.stopped


def test_watch_survives_mutter_and_audio_being_unreadable(make_ceb):
    from ceb_profiles.mutter import DisplayError
    from ceb_profiles.watch import Watcher
    ceb = make_ceb(DisplayError("no GNOME"))
    ceb.audio.down = True
    logged = []
    w = Watcher(ceb, settle_ms=10, log=logged.append)
    w.start(bus=FakeBus(), watch_audio=False)
    w.observe()
    assert w.observations == 2
    assert len([t for t in logged if "no GNOME" in t]) == 1  # said once, not on every event


def test_watch_quits_on_sigterm_and_on_session_end(make_ceb):
    import signal
    from ceb_profiles.watch import Watcher
    ceb = make_ceb(desk(1))
    bus = FakeBus()
    w = Watcher(ceb, settle_ms=10, log=lambda t: None)
    w.start(bus=bus, watch_audio=False)
    GLib.timeout_add(50, lambda: (os.kill(os.getpid(), signal.SIGTERM), False)[1])
    done = threading.Event()
    GLib.timeout_add(3000, lambda: (w.loop.quit(), False)[1])  # safety net
    start = time.monotonic()
    w.run()
    done.set()
    assert time.monotonic() - start < 2.5
    w2 = Watcher(ceb, settle_ms=10, log=lambda t: None)
    bus2 = FakeBus()
    w2.start(bus=bus2, watch_audio=False)
    GLib.timeout_add(20, lambda: (bus2.closed_handlers[0](), False)[1])
    GLib.timeout_add(3000, lambda: (w2.loop.quit(), False)[1])
    start = time.monotonic()
    w2.run()
    assert time.monotonic() - start < 2.5


def test_only_one_watcher_per_config_folder(tmp_path):
    from ceb_profiles.watch import _single_instance
    first = _single_instance(tmp_path / "ceb")
    assert first is not None
    assert desktop.running_watcher(tmp_path / "ceb") == os.getpid()
    assert _single_instance(tmp_path / "ceb") is None
    assert _single_instance(tmp_path / "other") is not None
    os.close(first)
    assert desktop.running_watcher(tmp_path / "ceb") is None


# ---------------------------------------------------------------- autostart entry


def test_autostart_entry_runs_absolute_ceb_watch(tmp_path):
    path = desktop.enable_autostart(None, ceb="/opt/ceb/bin/ceb")
    assert path == tmp_path / "config" / "autostart" / "io.github.kevinwwillis.Ceb.Watch.desktop"
    text = path.read_text()
    assert "Exec=/opt/ceb/bin/ceb watch\n" in text and "X-GNOME-Autostart-enabled=true" in text
    assert desktop.autostart_enabled(None)
    assert not desktop.autostart_enabled("/home/k/other")
    assert desktop.disable_autostart() and not path.exists()
    assert not desktop.autostart_enabled(None)


def test_autostart_entry_carries_the_config_dir(tmp_path):
    path = desktop.enable_autostart("/home/k/my $profiles", ceb="/opt/ceb/bin/ceb")
    assert 'Exec=/opt/ceb/bin/ceb --config-dir "/home/k/my \\\\$profiles" watch' in path.read_text()
    assert desktop.autostart_enabled("/home/k/my $profiles")
    assert not desktop.autostart_enabled("/home/k/my $profiles2")
    assert not desktop.autostart_enabled(None)


def test_autostart_without_installed_ceb_says_so(monkeypatch):
    monkeypatch.setattr(desktop, "find_executable", lambda name: None)
    with pytest.raises(RuntimeError, match="could not find the installed `ceb`"):
        desktop.enable_autostart(None)


def test_exec_line_quoting():
    assert desktop.exec_line(["/usr/bin/ceb", "watch"]) == "/usr/bin/ceb watch"
    assert desktop.exec_line(["/a b/ceb"]) == '"/a b/ceb"'
    assert desktop.exec_line(["50%"]) == "50%%"
    assert desktop.exec_line(['a"b']) == '"a\\\\"b"'
