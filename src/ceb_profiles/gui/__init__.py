"""CEB's GTK 4 + libadwaita window. Run with `ceb-gui` or `ceb gui`.

  ceb-gui                 the real thing: GNOME's screens, the audio server, GNOME's shortcuts
  ceb-gui --dry-run       Apply only asks GNOME to check the layout (ApplyMonitorsConfig method 0);
                          nothing on screen or in audio changes
  ceb-gui --demo          example screens and audio devices, a throwaway profile store and in-memory
                          shortcuts; touches nothing real (for screenshots and development)
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

APP_ID = "io.github.kevinwwillis.Ceb"

# The window uses Adw.ShortcutLabel, new in libadwaita 1.8 (GNOME 49).
MIN_ADW = (1, 8)


# What to install, per distribution family, for everything CEB needs (CLI and window).
INSTALL_HINT = ("Ubuntu/Debian: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1; "
                "Fedora: sudo dnf install python3-gobject gtk4 libadwaita")


def version_problem(adw: tuple[int, int, int] | None, have_gi: bool = True) -> str | None:
    """Why the window cannot run (None if it can). adw: the libadwaita version, or None when GTK 4 or
    libadwaita cannot be loaded. have_gi: PyGObject itself can be imported."""
    need = ".".join(map(str, MIN_ADW))
    if not have_gi:
        return (f"CEB needs PyGObject (python3-gi), and its window also GTK 4 and libadwaita {need} or newer. "
                f"Both `ceb` and `ceb-gui` need PyGObject. Install them with your system's packages ({INSTALL_HINT}). "
                "In a virtual environment or pipx, create it with --system-site-packages so it sees them.")
    if adw is None:
        return (f"ceb-gui needs GTK 4 and libadwaita {need} or newer with their GObject introspection data "
                "(Debian/Ubuntu: gir1.2-gtk-4.0 gir1.2-adw-1; Fedora: gtk4 libadwaita). The command line "
                "tool `ceb` works without them.")
    if tuple(adw[:2]) < MIN_ADW:
        return (f"ceb-gui needs libadwaita {need} or newer (GNOME 49); this system has "
                f"{'.'.join(map(str, adw))}. The command line tool `ceb` works with it.")
    return None


def _have_gi() -> bool:
    try:
        import gi  # noqa: F401
    except ImportError:
        return False
    return True


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
    ap = argparse.ArgumentParser(prog="ceb-gui", description="CEB (Combined Environment Bundles): display and audio profiles for GNOME")
    ap.add_argument("--dry-run", action="store_true",
                    help="Apply only asks GNOME to verify the layout; nothing is changed")
    ap.add_argument("--demo", action="store_true",
                    help="example screens and audio, throwaway store, in-memory shortcuts; touches nothing real")
    ap.add_argument("--size", help=argparse.SUPPRESS)  # WIDTHxHEIGHT, for screenshots
    ap.add_argument("--config-dir", help="where profiles.json and known.json live (default $XDG_CONFIG_HOME/ceb)")
    return ap


def main(argv=None) -> int:
    opts = build_parser().parse_args(argv)
    if opts.demo:
        # Before Gio is loaded anywhere: keep every gsettings write in memory.
        os.environ["GSETTINGS_BACKEND"] = "memory"

    problem = version_problem(_adw_version(), _have_gi())
    if problem:
        print(f"ceb-gui: {problem}", file=sys.stderr)
        return 1
    from gi.repository import Adw, Gio, Gtk

    from ..core import Ceb
    from ..store import Store, explicit_config_dir
    from .shortcuts import Shortcuts, ceb_command

    demo_dir = None
    if opts.demo:
        from .demo import DemoAudio, DemoDisplay, seed
        from .window import same_accel
        demo_dir = tempfile.mkdtemp(prefix="ceb-demo-")  # removed when the program ends
        config_dir = opts.config_dir or os.path.join(demo_dir, "ceb")
        ceb = Ceb(display=DemoDisplay(), audio=DemoAudio(), store=Store(config_dir))
        # In-memory shortcuts that never run; "ceb" only if the installed one cannot be found.
        shortcuts = Shortcuts(backend=Gio.memory_settings_backend_new(), command=ceb_command() or "ceb")
        seed(ceb, shortcuts, same_accel)
        explicit = explicit_config_dir(opts.config_dir)
        # The background-watcher switch writes its autostart entry here, never to ~/.config.
        autostart_base = os.path.join(demo_dir, "config")
    else:
        explicit = explicit_config_dir(opts.config_dir)
        ceb = Ceb(store=Store(opts.config_dir))
        shortcuts = Shortcuts(config_dir=explicit)
        autostart_base = None

    from .window import CebWindow

    app = Adw.Application(application_id=APP_ID if not opts.demo else APP_ID + ".Demo",
                          flags=Gio.ApplicationFlags.NON_UNIQUE if opts.demo else Gio.ApplicationFlags.DEFAULT_FLAGS)

    def activate(application):
        win = application.get_active_window()
        if win is None:
            _add_icon_path(Gtk)
            win = CebWindow(application, ceb, shortcuts, dry_run=opts.dry_run, demo=opts.demo,
                            config_dir=explicit, autostart_base=autostart_base)
            if opts.size:
                w, h = (int(v) for v in opts.size.lower().split("x"))
                win.set_default_size(w, h)
        win.present()

    app.connect("activate", activate)
    if demo_dir:  # end cleanly (and remove the demo folder) when stopped from outside too
        import signal

        from .._signals import on_signals
        on_signals((signal.SIGTERM, signal.SIGINT, signal.SIGHUP), lambda _s: app.quit())
    try:
        return app.run([sys.argv[0]])
    finally:
        if demo_dir:
            shutil.rmtree(demo_dir, ignore_errors=True)


def _add_icon_path(Gtk) -> None:
    """Find the app icon when running from the source tree (an installed copy uses the icon theme)."""
    from gi.repository import Gdk
    here = os.path.join(os.path.dirname(__file__), "data", "icons")
    display = Gdk.Display.get_default()
    if display is not None and os.path.isdir(here):
        Gtk.IconTheme.get_for_display(display).add_search_path(here)
        Gtk.Window.set_default_icon_name(APP_ID)
