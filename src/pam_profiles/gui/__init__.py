"""PAM's GTK 4 + libadwaita window. Run with `pam-gui` or `pam gui`.

  pam-gui                 the real thing: GNOME's screens, the audio server, GNOME's shortcuts
  pam-gui --dry-run       Apply only asks GNOME to check the layout (ApplyMonitorsConfig method 0);
                          nothing on screen or in audio changes
  pam-gui --demo          example screens and audio devices, a throwaway profile store and in-memory
                          shortcuts; touches nothing real (for screenshots and development)
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

APP_ID = "io.github.kevinwwillis.Pam"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pam-gui", description="PAM: display and audio profiles for GNOME")
    ap.add_argument("--dry-run", action="store_true",
                    help="Apply only asks GNOME to verify the layout; nothing is changed")
    ap.add_argument("--demo", action="store_true",
                    help="example screens and audio, throwaway store, in-memory shortcuts; touches nothing real")
    ap.add_argument("--config-dir", help="where profiles.json and known.json live (default $XDG_CONFIG_HOME/pam)")
    return ap


def main(argv=None) -> int:
    opts = build_parser().parse_args(argv)
    if opts.demo:
        # Before Gio is loaded anywhere: keep every gsettings write in memory.
        os.environ["GSETTINGS_BACKEND"] = "memory"

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, Gtk

    from ..core import Pam
    from ..store import Store
    from .shortcuts import Shortcuts

    if opts.demo:
        from .demo import DemoAudio, DemoDisplay, seed
        config_dir = opts.config_dir or tempfile.mkdtemp(prefix="pam-demo-")
        pam = Pam(display=DemoDisplay(), audio=DemoAudio(), store=Store(config_dir))
        shortcuts = Shortcuts(backend=Gio.memory_settings_backend_new())
        seed(pam, shortcuts)
    else:
        pam = Pam(store=Store(opts.config_dir))
        shortcuts = Shortcuts()

    from .window import PamWindow

    app = Adw.Application(application_id=APP_ID if not opts.demo else APP_ID + ".Demo",
                          flags=Gio.ApplicationFlags.NON_UNIQUE if opts.demo else Gio.ApplicationFlags.DEFAULT_FLAGS)

    def activate(application):
        win = application.get_active_window()
        if win is None:
            _add_icon_path(Gtk)
            win = PamWindow(application, pam, shortcuts, dry_run=opts.dry_run, demo=opts.demo)
        win.present()

    app.connect("activate", activate)
    return app.run([sys.argv[0]])


def _add_icon_path(Gtk) -> None:
    """Find the app icon when running from the source tree (an installed copy uses the icon theme)."""
    from gi.repository import Gdk
    here = os.path.join(os.path.dirname(__file__), "data", "icons")
    display = Gdk.Display.get_default()
    if display is not None and os.path.isdir(here):
        Gtk.IconTheme.get_for_display(display).add_search_path(here)
        Gtk.Window.set_default_icon_name(APP_ID)
