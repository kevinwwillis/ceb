"""`ceb watch`: keep running and remember every screen and audio device that is connected.

Without it, CEB records screens and audio devices only while it runs (a `ceb` command, or the
window). The watcher listens for GNOME's MonitorsChanged signal (org.gnome.Mutter.DisplayConfig) and
for the audio server's device events (audio.AudioEvents), waits SETTLE_MS for a burst of events
to end, then reads the screens and audio devices once and records them (Ceb.observe_best_effort).
It sleeps in the GLib main loop between events, so it uses no CPU while nothing changes. It ends
on SIGTERM, SIGINT or SIGHUP, and when the session's D-Bus connection closes (logout). Only one
watcher runs per config folder; a second one exits at once (code 0).

The window's main menu > "Remember Screens in the Background" writes an autostart entry that runs
this at login (desktop.enable_autostart) and starts it at once.
"""
from __future__ import annotations

import fcntl
import os
import signal
import sys

from ._signals import on_signals
from .audio import AudioError
from .core import Ceb
from .desktop import watch_lock_path
from .mutter import BUS_NAME, INTERFACE, OBJECT_PATH, DisplayError
from .store import Store, StoreError

SETTLE_MS = 1000


class Watcher:
    def __init__(self, ceb: Ceb, *, settle_ms: int = SETTLE_MS, log=None):
        from gi.repository import GLib
        self.GLib = GLib
        self.ceb = ceb
        self.settle_ms = settle_ms
        self.log = log or (lambda text: print(f"ceb watch: {text}", file=sys.stderr, flush=True))
        self.loop = GLib.MainLoop()
        self.observations = 0
        self._pending = None
        self._bus = None
        self._signal_id = None
        self._audio_events = None
        self._last_problems: tuple[str, ...] = ()

    # -------------------------------------------------------------- start and stop

    def start(self, bus=None, watch_audio: bool = True) -> None:
        """Subscribe to the events and record what is connected now. `bus`: the session bus (a
        Gio.DBusConnection; tests pass their own), or None to connect to it."""
        from gi.repository import Gio
        if bus is None:
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except self.GLib.Error as e:
                self.log(f"no D-Bus session bus, screens are not watched: {e.message}")
        self._bus = bus
        if bus is not None:
            self._signal_id = bus.signal_subscribe(
                BUS_NAME, INTERFACE, "MonitorsChanged", OBJECT_PATH, None, Gio.DBusSignalFlags.NONE,
                lambda *args: self.changed())
            bus.connect("closed", lambda *args: self.quit("the D-Bus session ended"))
        if watch_audio:
            watch = getattr(self.ceb.audio, "watch", None)
            if watch is not None:
                self._audio_events = watch(lambda: self.GLib.idle_add(self._idle_changed))
        self.observe()

    def stop(self) -> None:
        if self._pending is not None:
            self.GLib.source_remove(self._pending)
            self._pending = None
        if self._signal_id is not None and self._bus is not None:
            self._bus.signal_unsubscribe(self._signal_id)
            self._signal_id = None
        if self._audio_events is not None:
            self._audio_events.stop(timeout=3)
            self._audio_events = None

    def quit(self, why: str = "") -> None:
        if why:
            self.log(f"stopping: {why}")
        self.loop.quit()

    def run(self) -> None:
        sources = on_signals((signal.SIGTERM, signal.SIGINT, signal.SIGHUP),
                             lambda s: self.quit(signal.Signals(s).name))
        try:
            self.loop.run()
        finally:
            for source in sources:
                self.GLib.source_remove(source)
            self.stop()

    # -------------------------------------------------------------- events

    def _idle_changed(self) -> bool:
        self.changed()
        return False

    def changed(self) -> None:
        """Something changed: record it once the burst of events is over (main loop thread)."""
        if self._pending is not None:
            self.GLib.source_remove(self._pending)
        self._pending = self.GLib.timeout_add(self.settle_ms, self._settled)

    def _settled(self) -> bool:
        self._pending = None
        self.observe()
        return False

    def observe(self) -> None:
        problems = []
        try:
            display_state = self.ceb.display.get_state()
        except DisplayError as e:
            display_state = None
            problems.append(f"could not read the screens: {e}")
        try:
            audio_state = self.ceb.audio.snapshot()
        except AudioError as e:
            audio_state = None
            problems.append(str(e))
        history_error = self.ceb.observe_best_effort(display_state, audio_state)
        if history_error:
            problems.append(history_error)
        self.observations += 1
        # Say each problem once, not on every event; and say when it is gone.
        if tuple(problems) != self._last_problems:
            for p in problems:
                self.log(p)
            if not problems and self._last_problems:
                self.log("reading and recording work again")
            self._last_problems = tuple(problems)


def _single_instance(store_dir) -> object | None:
    """Hold the watcher lock for this folder; None if another watcher holds it."""
    path = watch_lock_path(store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    return fd


def run(config_dir: str | None = None, *, ceb: Ceb | None = None) -> int:
    """The `ceb watch` command. Returns the exit code."""
    try:
        import importlib

        import gi
        gi.require_version("Gio", "2.0")
        importlib.import_module("gi.repository.GLib")  # only checks that GLib loads
    except (ImportError, ValueError) as e:
        print(f"ceb watch: needs PyGObject (python3-gi): {e}", file=sys.stderr)
        return 1
    ceb = ceb or Ceb(store=Store(config_dir))
    try:
        lock = _single_instance(ceb.store.dir)
    except OSError as e:
        print(f"ceb watch: cannot take the watcher lock: {e}", file=sys.stderr)
        return 1
    if lock is None:
        print(f"ceb watch: already running for {ceb.store.dir}", file=sys.stderr)
        return 0
    try:
        watcher = Watcher(ceb)
        watcher.start()
        watcher.run()
    except StoreError as e:
        print(f"ceb watch: {e}", file=sys.stderr)
        return 1
    finally:
        os.close(lock)
    return 0
