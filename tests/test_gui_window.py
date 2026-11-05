"""The window with the demo backends. Needs a display: run under GTK's Broadway backend, e.g.
    gtk4-broadwayd :7 &  GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 pytest tests/test_gui_window.py
Skipped unless GDK_BACKEND=broadway (so they never open anything on a real desktop), or when GTK
cannot open the display."""
import os
import time

import pytest

os.environ["GSETTINGS_BACKEND"] = "memory"  # never the user's real shortcuts

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

if os.environ.get("GDK_BACKEND") != "broadway":  # never open windows on a real desktop session
    pytest.skip("window tests run only under GDK_BACKEND=broadway", allow_module_level=True)
if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("no display for GTK", allow_module_level=True)

from pam_profiles.core import Pam  # noqa: E402
from pam_profiles.gui import editor  # noqa: E402
from pam_profiles.gui.demo import DemoAudio, DemoDisplay, seed  # noqa: E402
from pam_profiles.gui.shortcuts import Shortcuts  # noqa: E402
from pam_profiles.gui.window import PamWindow  # noqa: E402
from pam_profiles.store import Store  # noqa: E402


@pytest.fixture(scope="module")
def app():
    application = Adw.Application(application_id="io.github.kevinwwillis.Pam.Test",
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
    application.register(None)
    return application


@pytest.fixture
def win(tmp_path, app):
    pam = Pam(display=DemoDisplay(), audio=DemoAudio(), store=Store(tmp_path / "pam"))
    shortcuts = Shortcuts(backend=Gio.memory_settings_backend_new(), command="pam")
    seed(pam, shortcuts)
    w = PamWindow(app, pam, shortcuts, demo=True)
    yield w
    w.destroy()


def spin(until, timeout=5.0):
    end = time.monotonic() + timeout
    ctx = GLib.MainContext.default()
    while not until() and time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)
    return until()


def rows(listbox):
    out, i = [], 0
    while (row := listbox.get_row_at_index(i)) is not None:
        out.append(row.profile_name)
        i += 1
    return out


def test_sidebar_lists_profiles_and_selects_the_primary(win):
    assert rows(win.sidebar_list) == ["Desk — 3 monitors", "Center only", "Projector"]
    assert win.current == "Desk — 3 monitors"
    assert win.selected == editor.primary_screen(win.profile())
    assert win.keys["Desk — 3 monitors"] == "<Super>F1"


def test_add_turn_off_and_primary_are_saved(win):
    projector = next(k.screen for k in win.known.screens if k.screen.identity.vendor == "STK")
    win._on_add_screen(None, projector)
    stored = win.pam.profile("Desk — 3 monitors")
    assert projector in editor.screen_ids(stored)
    assert len(win.canvas.items) == 4 and not win.canvas.items[-1].connected

    win.selected = projector
    win._on_make_primary(None)
    assert editor.primary_screen(win.pam.profile("Desk — 3 monitors")) == projector

    win._on_turn_off(None)
    stored = win.pam.profile("Desk — 3 monitors")
    assert projector not in editor.screen_ids(stored)
    assert sum(lm.primary for lm in stored.displays.logical_monitors) == 1


def test_canvas_move_is_saved_from_the_origin(win):
    left = win.profile().displays.logical_monitors[0].screens[0].screen
    win._on_canvas_moved(left, 0, 400, True)
    xs = [(lm.x, lm.y) for lm in win.pam.profile("Desk — 3 monitors").displays.logical_monitors]
    assert min(x for x, _ in xs) == 0 and min(y for _, y in xs) == 0


def test_audio_choice_is_saved_and_leave_unchanged_is_none(win):
    win.output_row.set_selected(0)  # "Leave unchanged"
    assert win.pam.profile("Desk — 3 monitors").audio_output is None
    win.output_row.set_selected(2)
    assert win.pam.profile("Desk — 3 monitors").audio_output.description == "GA102 HDMI 2 (NVIDIA)"


def test_apply_sends_method_1_only_in_demo_and_dry_run_sends_only_0(win):
    win._on_apply()
    assert spin(lambda: not win._applying)
    assert [m for m, _ in win.pam.display.sent] == [0, 1]
    win.dry_run = True
    win.pam.display.sent.clear()
    win._on_apply()
    assert spin(lambda: not win._applying)
    assert [m for m, _ in win.pam.display.sent] == [0]
    assert win.pam.audio.sent == [("output", win.pam.profile("Desk — 3 monitors").audio_output.name),
                                  ("input", win.pam.profile("Desk — 3 monitors").audio_input.name)]


def test_set_shortcut_refuses_a_clash_and_sets_a_free_one(win):
    assert "Desk" in win.set_shortcut("Center only", "<Super>F1")
    assert win.set_shortcut("Center only", "<Control><Alt>p") is None
    assert win.keys["Center only"] == "<Control><Alt>p"
    assert win.set_shortcut("Center only", None) is None
    assert "Center only" not in win.keys


class OnlyRogActive(DemoDisplay):
    """The desk with only the ROG on, so applying "Desk" turns screens on (HDMI audio may follow)."""

    def get_state(self):
        from pam_profiles.gui.demo import DESK
        from pam_profiles.mutter import parse_current_state
        specs = {m[0][0]: m[0] for m in DESK}
        return parse_current_state((self.serial, DESK, [(0, 0, 1.0, 0, True, [specs["HDMI-3"]], {})],
                                    {"layout-mode": 1, "supports-changing-layout-mode": False}))


def test_late_hdmi_audio_waits_off_the_main_loop_and_never_offers_apply_the_rest(win):
    from pam_profiles.model import AudioRef
    shown = []
    win.error = lambda head, body: shown.append(("error", head, body))
    win._ask_apply_rest = lambda name, r: shown.append(("ask", name))
    win.pam.display = OnlyRogActive()
    p = win.pam.profile("Desk — 3 monitors")
    p.audio_output = AudioRef("alsa_output.pci-0000_2d_00.1.hdmi-stereo-extra9", "GA102 HDMI 9")
    win.pam.put_profile(p)
    win.pam.audio_appear_wait_s = 1.0

    ticks = []
    GLib.timeout_add(20, lambda: ticks.append(1) or win._applying)
    win._on_apply()
    assert spin(lambda: not win._applying, timeout=10)
    assert len(ticks) >= 10  # the main loop kept running during the 1 s wait
    assert [m for m, _ in win.pam.display.sent] == [0, 1]
    assert [s[0] for s in shown] == ["error"]
    assert "Partly Applied" in shown[0][1] and "HDMI 9" in shown[0][2]


def test_history_problem_is_shown_in_the_banner(win, tmp_path):
    win.pam.store.known_path.write_text("{not json")
    win.reload(select=win.current)
    assert win.banner.get_revealed()
    assert "history not updated" in win.banner.get_title()
    assert win.content_stack.get_visible_child_name() == "profile"  # still usable
