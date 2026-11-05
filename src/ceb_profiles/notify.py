"""Desktop notifications. A keyboard shortcut has no terminal, so this is how failures reach the user.

Order of attempts: the org.freedesktop.Notifications D-Bus service (via Gio, no extra tool), then
notify-send. The message always goes to stderr as well. Never raises.
"""
from __future__ import annotations

import subprocess
import sys

APP_NAME = "CEB"
URGENCY_NORMAL = 1
URGENCY_CRITICAL = 2


def _dbus_notify(title: str, body: str, urgency: int) -> bool:
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                      "org.freedesktop.Notifications", "Notify",
                      GLib.Variant("(susssasa{sv}i)", (APP_NAME, 0, "video-display", title, body, [],
                                                       {"urgency": GLib.Variant("y", urgency)}, -1)),
                      None, Gio.DBusCallFlags.NONE, 5000, None)
        return True
    except Exception:
        return False


def _notify_send(title: str, body: str, urgency: int) -> bool:
    level = "critical" if urgency >= URGENCY_CRITICAL else "normal"
    try:
        return subprocess.run(["notify-send", "-a", APP_NAME, "-u", level, title, body],
                              check=False, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def notify(title: str, body: str, *, critical: bool = True, senders=None) -> bool:
    """Show a desktop notification; returns True if one of the senders accepted it."""
    urgency = URGENCY_CRITICAL if critical else URGENCY_NORMAL
    print(f"ceb: {title}: {body}", file=sys.stderr)
    for send in senders if senders is not None else (_dbus_notify, _notify_send):
        try:
            if send(title, body, urgency):
                return True
        except Exception:
            continue
    return False
