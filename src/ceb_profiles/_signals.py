"""Run a function in the GLib main loop when the process gets a Unix signal."""
from __future__ import annotations


def on_signals(signums, fn) -> list[int]:
    """Call fn(signum) from the main loop on each of these signals (GLibUnix, else the older
    GLib.unix_signal_add). Returns the GLib source ids (GLib.source_remove them to stop)."""
    from gi.repository import GLib
    try:
        from gi.repository import GLibUnix
        add = GLibUnix.signal_add
    except ImportError:
        add = GLib.unix_signal_add
    return [add(GLib.PRIORITY_DEFAULT, signum, lambda *_a, s=signum: (fn(s), GLib.SOURCE_CONTINUE)[1])
            for signum in signums]
