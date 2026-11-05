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

# The window uses Adw.ShortcutLabel, new in libadwaita 1.8 (GNOME 49).
MIN_ADW = (1, 8)


def version_problem(adw: tuple[int, int, int] | None) -> str | None:
    """Why the window cannot run with this libadwaita version (None if it can)."""
    need = ".".join(map(str, MIN_ADW))
    if adw is None:
        return (f"pam-gui needs GTK 4 and libadwaita {need} or newer with their GObject introspection data "
                "(Debian/Ubuntu: gir1.2-gtk-4.0 gir1.2-adw-1; Fedora: gtk4 libadwaita). The command line "
                "tool `pam` works without them.")
    if tuple(adw[:2]) < MIN_ADW:
        return (f"pam-gui needs libadwaita {need} or newer (GNOME 49); this system has "
                f"{'.'.join(map(str, adw))}. The command line tool `pam` works with it.")
    return None


def _adw_version():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
    except (ValueError, ImportError):
        return None
    return Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pam-gui", description="PAM: display and audio profiles for GNOME")
    ap.add_argument("--dry-run", action="store_true",
                    help="Apply only asks GNOME to verify the layout; nothing is changed")
    ap.add_argument("--demo", action="store_true",
                    help="example screens and audio, throwaway store, in-memory shortcuts; touches nothing real")
    ap.add_argument("--size", help=argparse.SUPPRESS)  # WIDTHxHEIGHT, for screenshots
    ap.add_argument("--config-dir", help="where profiles.json and known.json live (default $XDG_CONFIG_HOME/pam)")
    return ap


def main(argv=None) -> int:
    opts = build_parser().parse_args(argv)
    if opts.demo:
        # Before Gio is loaded anywhere: keep every gsettings write in memory.
        os.environ["GSETTINGS_BACKEND"] = "memory"

    problem = version_problem(_adw_version())
    if problem:
        print(f"pam-gui: {problem}", file=sys.stderr)
        return 1
    from gi.repository import Adw, Gio, Gtk

    from ..core import Pam
    from ..store import Store
    from .shortcuts import Shortcuts

    if opts.demo:
        from .demo import DemoAudio, DemoDisplay, seed
        from .window import same_accel
        config_dir = opts.config_dir or tempfile.mkdtemp(prefix="pam-demo-")
        pam = Pam(display=DemoDisplay(), audio=DemoAudio(), store=Store(config_dir))
        shortcuts = Shortcuts(backend=Gio.memory_settings_backend_new())
        seed(pam, shortcuts, same_accel)
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
            if opts.size:
                w, h = (int(v) for v in opts.size.lower().split("x"))
                win.set_default_size(w, h)
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
