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

from ceb_profiles.core import Ceb  # noqa: E402
from ceb_profiles.gui import editor  # noqa: E402
from ceb_profiles.gui.demo import DemoAudio, DemoDisplay, seed  # noqa: E402
from ceb_profiles.gui.shortcuts import Shortcuts  # noqa: E402
from ceb_profiles.gui.window import CebWindow, ShortcutDialog, same_accel  # noqa: E402
from ceb_profiles.store import Store  # noqa: E402


@pytest.fixture(scope="module")
def app():
    application = Adw.Application(application_id="io.github.kevinwwillis.Ceb.Test",
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
    application.register(None)
    return application


@pytest.fixture
def win(tmp_path, app):
    ceb = Ceb(display=DemoDisplay(), audio=DemoAudio(), store=Store(tmp_path / "ceb"))
    shortcuts = Shortcuts(backend=Gio.memory_settings_backend_new(), command="ceb")
    seed(ceb, shortcuts, same_accel)
    w = CebWindow(app, ceb, shortcuts, demo=True)
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
    assert win.keys["Desk — 3 monitors"] == "<Super>F2"


def test_add_turn_off_and_primary_are_unsaved_edits_until_save(win):
    saved = win.ceb.profile("Desk — 3 monitors").to_json()
    projector = next(k.screen for k in win.known.screens if k.screen.identity.vendor == "STK")
    win._on_add_screen(None, projector)
    assert projector in editor.screen_ids(win.profile())
    assert len(win.canvas.items) == 4 and not win.canvas.items[-1].connected

    win.selected = projector
    win._on_make_primary(None)
    assert editor.primary_screen(win.profile()) == projector
    assert win.ceb.profile("Desk — 3 monitors").to_json() == saved      # nothing written yet
    assert win.status_label.get_label() == "● Unsaved changes"

    assert win.save()                                                   # a not-connected screen saves
    stored = win.ceb.profile("Desk — 3 monitors")
    assert editor.primary_screen(stored) == projector
    assert win.status_label.get_label() == "✓ Saved"

    win._on_turn_off(None)
    assert projector in editor.screen_ids(win.ceb.profile("Desk — 3 monitors"))
    assert win.save()
    stored = win.ceb.profile("Desk — 3 monitors")
    assert projector not in editor.screen_ids(stored)
    assert sum(lm.primary for lm in stored.displays.logical_monitors) == 1
    assert win.ceb.display.sent == [] and win.ceb.audio.sent == []      # never applied


def test_canvas_move_is_kept_from_the_origin(win):
    left = win.profile().displays.logical_monitors[0].screens[0].screen
    win._on_canvas_moved(left, 0, 400, True)
    xs = [(lm.x, lm.y) for lm in win.profile().displays.logical_monitors]
    assert min(x for x, _ in xs) == 0 and min(y for _, y in xs) == 0


def test_audio_choice_is_an_edit_and_leave_unchanged_is_none(win):
    win.output_row.set_selected(0)  # "Leave unchanged"
    assert win.profile().audio_output is None
    win.output_row.set_selected(2)
    assert win.profile().audio_output.description == "GA102 HDMI 2 (NVIDIA)"
    assert win.ceb.profile("Desk — 3 monitors").audio_output.description != "GA102 HDMI 2 (NVIDIA)"
    assert win.ceb.audio.sent == []


def test_switch_sends_method_1_only_in_demo_and_dry_run_sends_only_0(win):
    win._on_switch()
    assert spin(lambda: not win._applying)
    assert [m for m, _ in win.ceb.display.sent] == [0, 1]
    win.dry_run = True
    win.ceb.display.sent.clear()
    win._on_switch()
    assert spin(lambda: not win._applying)
    assert [m for m, _ in win.ceb.display.sent] == [0]
    assert win.ceb.audio.sent == [("output", win.ceb.profile("Desk — 3 monitors").audio_output.name),
                                  ("input", win.ceb.profile("Desk — 3 monitors").audio_input.name)]


def test_set_shortcut_refuses_a_clash_and_saves_a_free_one(win):
    win.open_profile("Center only")
    assert "Desk" in win.set_shortcut("Center only", "<Super>F2")
    assert "GNOME" in win.set_shortcut("Center only", "<Super>F1")  # GNOME help
    assert win.set_shortcut("Center only", "<Control><Alt>p") is None
    assert win.keys["Center only"] == "<Super>F3" and win.dirty()       # not written before Save
    assert win.save()
    assert win.keys["Center only"] == "<Control><Alt>p"
    assert win.set_shortcut("Center only", None) is None
    assert win.save()
    assert "Center only" not in win.keys


class OnlyRogActive(DemoDisplay):
    """The desk with only the ROG on, so applying "Desk" turns screens on (HDMI audio may follow)."""

    def get_state(self):
        from ceb_profiles.gui.demo import DESK
        from ceb_profiles.mutter import parse_current_state
        specs = {m[0][0]: m[0] for m in DESK}
        return parse_current_state((self.serial, DESK, [(0, 0, 1.0, 0, True, [specs["HDMI-3"]], {})],
                                    {"layout-mode": 1, "supports-changing-layout-mode": False}))


def test_late_hdmi_audio_waits_off_the_main_loop_and_never_offers_apply_the_rest(win):
    from ceb_profiles.model import AudioRef
    shown = []
    win.error = lambda head, body: shown.append(("error", head, body))
    win._ask_apply_rest = lambda name, r, *rest: shown.append(("ask", name))
    win.ceb.display = OnlyRogActive()
    p = win.ceb.profile("Desk — 3 monitors")
    p.audio_output = AudioRef("alsa_output.pci-0000_2d_00.1.hdmi-stereo-extra9", "GA102 HDMI 9")
    win.ceb.put_profile(p)
    win.reload(select=win.current)  # the window shows what was stored
    win.ceb.audio_appear_wait_s = 1.0

    ticks = []
    GLib.timeout_add(20, lambda: ticks.append(1) or win._applying)
    win._on_switch()
    assert spin(lambda: not win._applying, timeout=10)
    assert len(ticks) >= 10  # the main loop kept running during the 1 s wait
    assert [m for m, _ in win.ceb.display.sent] == [0, 1]
    assert [s[0] for s in shown] == ["error"]
    assert "Partly Applied" in shown[0][1] and "HDMI 9" in shown[0][2]


def test_history_problem_is_shown_in_the_banner(win, tmp_path):
    win.ceb.store.known_path.write_text("{not json")
    win.reload(select=win.current)
    assert win.banner.get_revealed()
    assert "history not updated" in win.banner.get_title()
    assert win.content_stack.get_visible_child_name() == "profile"  # still usable


# ------------------------------------------------------------ review fixes (2026-11-05)


def by_name(listbox, name):
    i = 0
    while (row := listbox.get_row_at_index(i)) is not None:
        if row.profile_name == name:
            return row
        i += 1


def shown(win, size=(1100, 760)):
    win.set_default_size(*size)
    win.present()
    assert spin(lambda: win.get_mapped() and win.canvas.get_width() > 0)
    spin(lambda: False, 0.2)


@pytest.fixture
def dialogs(monkeypatch):
    """Adw.AlertDialogs the window shows, captured instead of presented."""
    got = []
    monkeypatch.setattr(Adw.AlertDialog, "present", lambda self, parent=None: got.append(self))
    return got


def slow(ceb, seconds):
    real = ceb.apply
    calls = []

    def apply(name, **kw):
        calls.append((name, kw))
        time.sleep(seconds)
        return real(name, **kw)
    ceb.apply = apply
    return calls


def test_apply_the_rest_applies_the_profile_the_dialog_named(win, dialogs):
    projector = next(k.screen for k in win.known.screens if k.screen.identity.vendor == "STK")
    win._on_add_screen(None, projector)          # "Desk" now needs a screen that is not connected
    assert win.save()                            # Switch uses the saved profile
    calls = slow(win.ceb, 0)
    win._on_switch()
    assert spin(lambda: not win._applying) and dialogs
    assert "Desk" in dialogs[-1].get_heading()
    win._on_row_activated(win.sidebar_list, by_name(win.sidebar_list, "Center only"))
    assert win.current == "Center only"
    dialogs[-1].emit("response", "rest")
    assert spin(lambda: len(calls) == 2 and not win._applying)
    assert calls[1] == ("Desk — 3 monitors", {"dry_run": False, "allow_missing": True})


def test_closing_during_apply_waits_for_it_then_closes(win):
    shown(win)
    calls = slow(win.ceb, 0.6)
    win._on_switch()
    assert win._applying and win.switch_spinner.get_visible() and win.switch_label.get_label() == "Switching…"
    win.close()
    spin(lambda: False, 0.1)
    assert win.get_visible() and win._applying      # still open, switch still running
    assert spin(lambda: not win.get_visible(), 5)   # closed once it was done
    assert [m for m, _ in win.ceb.display.sent] == [0, 1] and len(calls) == 1


def test_closing_during_a_failed_apply_stays_open_and_says_why(win, dialogs):
    shown(win)
    win.ceb.display.apply = lambda *a: (_ for _ in ()).throw(__import__("ceb_profiles.mutter").mutter.DisplayError("refused"))
    slow(win.ceb, 0.3)
    win._on_switch()
    win.close()
    assert spin(lambda: not win._applying)
    spin(lambda: False, 0.1)
    assert win.get_visible() and dialogs and "Not Applied" in dialogs[-1].get_heading()


def test_shortcut_dialog_needs_super_ctrl_or_alt_and_inhibits_system_shortcuts(win):
    shown(win)
    win.open_profile("Center only")
    d = ShortcutDialog(win, "Center only")

    class Top:
        calls = []

        def inhibit_system_shortcuts(self, event):
            self.calls.append("inhibit")

        def restore_system_shortcuts(self):
            self.calls.append("restore")
    top = Top()
    d._toplevel = lambda: top
    d.present(win)
    spin(lambda: False, 0.2)
    assert top.calls == ["inhibit"]
    d._key(None, Gdk.KEY_a, 0, Gdk.ModifierType.SHIFT_MASK)
    assert d.message.get_visible() and win.keys["Center only"] == "<Super>F3"  # unchanged
    d._key(None, Gdk.KEY_p, 0, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)
    assert win.draft.accel == "<Control><Alt>p" and win.key_label.get_accelerator() == "<Control><Alt>p"
    assert win.keys["Center only"] == "<Super>F3"      # GNOME gets it on Save
    spin(lambda: False, 0.3)
    assert top.calls == ["inhibit", "restore"]


def test_moving_focus_in_the_sidebar_does_not_change_the_profile(win):
    shown(win)
    first = win.current
    by_name(win.sidebar_list, first).grab_focus()
    win.sidebar_list.emit("move-cursor", Gtk.MovementStep.DISPLAY_LINES, 1, False, False)
    spin(lambda: False, 0.1)
    assert win.current == first
    assert win.sidebar_list.get_selected_row().profile_name == first
    win.sidebar_list.get_focus_child().emit("activate")   # Enter / Space
    assert win.current == "Center only"
    assert win.sidebar_list.get_selected_row().profile_name == "Center only"


def test_ctrl_arrow_moves_the_selected_screen_and_arrows_select(win):
    shown(win)
    right = win.profile().displays.logical_monitors[2].screens[0].screen
    win.canvas.grab_focus()
    while win.selected != right:
        win.canvas._key(None, Gdk.KEY_Right, 0, 0)
    win.canvas._key(None, Gdk.KEY_Down, 0, Gdk.ModifierType.CONTROL_MASK)
    lms = win.profile().displays.logical_monitors
    assert (lms[2].x, lms[2].y) == (3840, 200) and (lms[0].y, lms[1].y) == (0, 0)
    assert win.selected == right


def test_a_click_never_moves_a_screen(win):
    shown(win)
    right = win.profile().displays.logical_monitors[2].screens[0].screen
    win._on_canvas_moved(right, 3840, 60, True)     # valid, but 60 px off its neighbour's top
    before = win.profile().to_json()
    c = win.canvas
    s, ox, oy = c._view_now()
    item = next(it for it in c.items if it.key == right)
    px, py = ox + (item.rect.x + item.rect.w / 2) * s, oy + (item.rect.y + item.rect.h / 2) * s
    c._drag_begin(None, px, py)
    c._drag_update(None, 2, -2)                     # hand jitter during a click
    c._drag_end(None, 2, -2)
    assert win.profile().to_json() == before
    assert win.selected == right


def test_narrow_window_shows_the_profile_name(win):
    shown(win, (360, 700))
    assert spin(lambda: win.narrow_title.get_mapped())
    assert win.narrow_title.get_label() == win.current
    assert not win.title.get_visible()


def test_mirrored_screens_can_each_be_selected_and_turned_off(win):
    desk = win.profile()
    lms = desk.displays.logical_monitors
    left, right = lms[0].screens[0].screen, lms[2].screens[0].screen

    def mirror(p):
        a, b = p.displays.logical_monitors[0], p.displays.logical_monitors.pop(2)
        a.screens.append(b.screens[0])
    assert win.edit(mirror)
    win.reload(select=win.current)
    item = next(it for it in win.canvas.items if it.key == left)
    assert item.members == (left, right) and "+ 1" in item.title
    win._on_canvas_select(left)
    assert len(win.mirror_rows) == 1
    win._on_select_member(None, right)
    assert win.selected == right and win.selected_title.get_label() == "Lenovo LI2364d"
    win._on_turn_off(None)
    shown_now = win.profile()
    assert right not in editor.screen_ids(shown_now) and left in editor.screen_ids(shown_now)


def test_save_never_overwrites_a_newer_profile_from_outside_unless_asked(win, dialogs):
    win.ceb.display = OnlyRogActive()
    win.ceb.save_current("Desk — 3 monitors", overwrite=True)   # `ceb save-current ... --force`
    newer = win.ceb.profile("Desk — 3 monitors").to_json()
    win.output_row.set_selected(0)                                # an edit on the old version
    assert not win.save()
    assert win.ceb.profile("Desk — 3 monitors").to_json() == newer
    assert "Changed Outside" in dialogs[-1].get_heading() and win.dirty()   # edits kept
    dialogs[-1].emit("response", "discard")                       # show the newer one
    assert len(win.canvas.items) == 1 and not win.dirty()
    win.output_row.set_selected(0)                                # now on the version shown
    assert win.save()
    assert win.ceb.profile("Desk — 3 monitors").audio_output is None


def test_save_can_replace_a_newer_profile_from_outside(win, dialogs):
    win.ceb.display = OnlyRogActive()
    win.ceb.save_current("Desk — 3 monitors", overwrite=True)
    win.output_row.set_selected(0)
    assert not win.save()
    dialogs[-1].emit("response", "replace")
    stored = win.ceb.profile("Desk — 3 monitors")
    assert stored.audio_output is None and len(stored.displays.logical_monitors) == 3
    assert not win.dirty()


def test_apply_after_an_outside_change_shows_it_first(win):
    win.ceb.display = OnlyRogActive()
    win.ceb.save_current("Desk — 3 monitors", overwrite=True)
    win._on_switch()
    assert not win._applying and win.ceb.display.sent == []
    assert len(win.canvas.items) == 1


def test_audio_popup_marks_the_selected_device(win):
    shown(win)
    win.output_row.set_selected(2)
    win.output_row.activate()
    spin(lambda: False, 0.3)

    def walk(w):
        yield w
        c = w.get_first_child()
        while c is not None:
            yield from walk(c)
            c = c.get_next_sibling()
    popovers = [w for w in walk(win.output_row) if isinstance(w, Gtk.Popover)]
    assert popovers
    marks = {}
    for w in walk(popovers[0]):
        if isinstance(w, Gtk.Label) and w.get_parent() and isinstance(w.get_parent().get_last_child(), Gtk.Image):
            marks[w.get_label()] = w.get_parent().get_last_child().get_opacity()
    assert marks["GA102 HDMI 2 (NVIDIA)"] == 1
    assert sum(1 for v in marks.values() if v == 1) == 1
    popovers[0].popdown()


def test_demo_shortcuts_do_not_clash_with_gnome(win):
    for name, key in win.keys.items():
        assert win.shortcuts.clash(key, name, same_accel) is None


def test_monitors_changed_waits_for_a_drag_and_reads_off_the_main_loop(win, monkeypatch):
    shown(win)
    win.MONITORS_SETTLE_MS = win.MONITORS_RETRY_MS = 20
    real = win.ceb.display.get_state
    reads = []

    def slow_state():
        reads.append(1)
        time.sleep(0.5)
        return real()
    win.ceb.display.get_state = slow_state
    win.canvas._drag = (0, win.canvas.items[0].rect)              # a drag in progress
    win._monitors_changed()
    spin(lambda: False, 0.3)
    assert reads == [] and win.canvas.dragging                    # waited, drag untouched
    win.canvas._drag = None
    ticks = []
    GLib.timeout_add(20, lambda: ticks.append(1) or True)
    assert spin(lambda: reads and not win._monitors_reading and win._monitors_pending is None, 5)
    assert len(ticks) >= 15                                       # main loop ran during the 0.5 s read


def test_forget_a_remembered_screen_and_audio_device(win):
    projector = next(k.screen for k in win.known.screens if k.screen.identity.vendor == "STK")
    win.forget_screen(projector)
    assert projector not in [k.screen for k in win.ceb.known().screens]
    assert projector not in [k.screen for k in win.known.screens]
    win.forget_audio("output", "nonexistent")       # not remembered: nothing happens
    from ceb_profiles.store import KnownAudio
    k = win.ceb.known()
    k.inputs.append(KnownAudio("alsa_input.gone", "Old Microphone"))
    win.ceb.store.save_known(k)
    win.forget_audio("input", "alsa_input.gone")
    assert "alsa_input.gone" not in [a.name for a in win.ceb.known().inputs]
    assert "alsa_input.gone" not in [a.name for a in win.known.inputs]


def test_remembered_dialog_lists_only_devices_that_are_not_connected(win):
    from ceb_profiles.gui.window import RememberedDialog
    from ceb_profiles.store import KnownAudio
    k = win.ceb.known()
    k.outputs.append(KnownAudio("alsa_output.gone", "Old Speakers"))
    win.ceb.store.save_known(k)
    win.reload(select=win.current)
    d = RememberedDialog(win)
    titles = []
    for g in d.groups:
        i = 0
        while (row := g.get_row(i) if hasattr(g, "get_row") else None) is not None:
            titles.append(row.get_title())
            i += 1
    assert "Old Speakers" in titles and any("TV" in t for t in titles)
    assert not any("ROG" in t for t in titles)          # connected: not offered


def test_a_screen_read_that_ends_after_the_window_closed_is_dropped(win):
    shown(win)
    win.MONITORS_SETTLE_MS = 1
    real = win.ceb.display.get_state
    win.ceb.display.get_state = lambda: (time.sleep(0.3), real())[1]
    win._monitors_changed()
    assert spin(lambda: win._monitors_reading)
    win.close()
    assert spin(lambda: not win._monitors_reading)   # the late result did not touch the closed window


# ------------------------------------------------------------ final review fixes (2026-11-05)


class WatchedAudio(DemoAudio):
    """DemoAudio with the real backend's watch(): the test calls the callback as the audio
    thread would."""

    def __init__(self):
        super().__init__()
        self.callbacks, self.stopped = [], False

    def watch(self, callback):
        self.callbacks.append(callback)
        audio = self

        class Events:
            def stop(self, timeout=None):
                audio.stopped = True
        return Events()


def test_audio_device_plugged_in_while_open_shows_in_the_pickers(tmp_path, app):
    import threading
    from ceb_profiles.model import AudioDevice
    audio = WatchedAudio()
    ceb = Ceb(display=DemoDisplay(), audio=audio, store=Store(tmp_path / "ceb"))
    shortcuts = Shortcuts(backend=Gio.memory_settings_backend_new(), command="ceb")
    seed(ceb, shortcuts, same_accel)
    w = CebWindow(app, ceb, shortcuts, dry_run=True)  # not the demo: it watches devices
    try:
        w.MONITORS_SETTLE_MS = w.MONITORS_RETRY_MS = 20
        assert len(audio.callbacks) == 1
        new = AudioDevice("alsa_output.usb-Plugged_In_Later-00.analog-stereo", "Plugged-In Speaker")
        audio.state.outputs.append(new)
        threading.Thread(target=audio.callbacks[0]).start()      # from the audio thread
        names = lambda: [w.output_row.get_model().get_string(i)
                         for i in range(w.output_row.get_model().get_n_items())]
        assert spin(lambda: "Plugged-In Speaker" in names())
        assert any(k.name == new.name for k in ceb.known().outputs)  # remembered too
        shown(w)
        w.close()
        assert audio.stopped
    finally:
        w.destroy()


def test_audio_event_waits_while_a_popover_is_open(tmp_path, app, monkeypatch):
    audio = WatchedAudio()
    ceb = Ceb(display=DemoDisplay(), audio=audio, store=Store(tmp_path / "ceb"))
    shortcuts = Shortcuts(backend=Gio.memory_settings_backend_new(), command="ceb")
    seed(ceb, shortcuts, same_accel)
    w = CebWindow(app, ceb, shortcuts, dry_run=True)
    try:
        w.MONITORS_SETTLE_MS = w.MONITORS_RETRY_MS = 20
        reads = []
        real = w._read_state
        monkeypatch.setattr(w, "_read_state", lambda: reads.append(1) or real())
        busy = [True]
        monkeypatch.setattr(w, "_busy", lambda: busy[0])
        audio.callbacks[0]()
        spin(lambda: False, 0.3)
        assert reads == []                     # nothing redrawn while the popover is open
        busy[0] = False
        assert spin(lambda: reads)
    finally:
        w.destroy()


def test_duplicate_profile_names_show_once_and_are_reported(win):
    import json
    path = win.ceb.store.profiles_path
    data = json.loads(path.read_text())
    copy_ = dict(data["profiles"][1])
    data["profiles"].append(copy_)
    path.write_text(json.dumps(data))
    win.reload(select=win.current)
    assert rows(win.sidebar_list) == ["Desk — 3 monitors", "Center only", "Projector"]
    assert "more than one profile named “Center only”" in win.banner.get_title()


def test_mirror_row_title_is_escaped(win):
    desk = win.profile()
    # The screen on HDMI-2 (the right one in "Desk") gets a name with markup characters.
    win.ceb.display.monitors = [((c, *rest), modes, dict(props, **{"display-name": "<b>Dell&Co 27\""}))
                                if c == "HDMI-2" else ((c, *rest), modes, props)
                                for (c, *rest), modes, props in win.ceb.display.monitors]

    def mirror(p):
        a, b = p.displays.logical_monitors[0], p.displays.logical_monitors.pop(2)
        a.screens.append(b.screens[0])
    assert win.edit(mirror)
    win.reload(select=win.current)
    win._on_canvas_select(desk.displays.logical_monitors[0].screens[0].screen)
    assert len(win.mirror_rows) == 1
    assert win.mirror_rows[0].get_title() == "&lt;b&gt;Dell&amp;Co LI2364d"


def test_delete_removes_the_profile_and_only_its_shortcut(win, monkeypatch, dialogs):
    s = win.shortcuts
    assert "Center only" in s.all()
    win.current = "Center only"
    win._on_delete()
    dialogs[-1].emit("response", "delete")
    assert "Center only" not in [p.name for p in win.ceb.profiles()]
    assert "Center only" not in s.all() and "Desk — 3 monitors" in s.all()


def test_remember_in_background_switch_writes_and_removes_autostart(tmp_path, app, monkeypatch):
    from ceb_profiles import desktop
    monkeypatch.setattr(desktop, "find_executable", lambda name: "/opt/ceb/bin/ceb")
    ceb = Ceb(display=DemoDisplay(), audio=DemoAudio(), store=Store(tmp_path / "ceb"))
    shortcuts = Shortcuts(backend=Gio.memory_settings_backend_new(), command="ceb")
    seed(ceb, shortcuts, same_accel)
    base = tmp_path / "autostart-base"
    w = CebWindow(app, ceb, shortcuts, demo=True, config_dir="/home/k/profiles", autostart_base=str(base))
    try:
        action = w.lookup_action("remember-background")
        assert action.get_state().get_boolean() is False
        action.change_state(GLib.Variant.new_boolean(True))
        path = base / "autostart" / "io.github.kevinwwillis.Ceb.Watch.desktop"
        assert "Exec=/opt/ceb/bin/ceb --config-dir /home/k/profiles watch" in path.read_text()
        assert action.get_state().get_boolean() is True
        action.change_state(GLib.Variant.new_boolean(False))
        assert not path.exists() and action.get_state().get_boolean() is False
        # The real ~/.config/autostart (here the test's XDG_CONFIG_HOME) is never touched.
        assert not (tmp_path / "config" / "autostart").exists()
    finally:
        w.destroy()


def test_remember_in_background_is_in_the_main_menu(win):
    menu = None
    child = win.get_content()
    stack = [child]
    while stack:
        widget = stack.pop()
        if isinstance(widget, Gtk.MenuButton):
            menu = widget.get_menu_model()
        c = widget.get_first_child()
        while c is not None:
            stack.append(c)
            c = c.get_next_sibling()
    labels = []
    for i in range(menu.get_n_items()):
        section = menu.get_item_link(i, "section")
        for j in range(section.get_n_items()):
            labels.append((section.get_item_attribute_value(j, "label", None).get_string(),
                           section.get_item_attribute_value(j, "action", None).get_string()))
    assert ("Remember Screens in the _Background", "win.remember-background") in labels


def test_no_installed_ceb_is_said_in_the_banner(tmp_path, app):
    ceb = Ceb(display=DemoDisplay(), audio=DemoAudio(), store=Store(tmp_path / "ceb"))
    shortcuts = Shortcuts(backend=Gio.memory_settings_backend_new(), command="ceb")
    shortcuts.command = None
    w = CebWindow(app, ceb, shortcuts, dry_run=True)
    try:
        assert "could not find its installed `ceb`" in w.banner.get_title()
    finally:
        w.destroy()


# ------------------------------------------------------------ TASK-1.12: save without applying, the action bar, Test


def walk(widget):
    yield widget
    c = widget.get_first_child()
    while c is not None:
        yield from walk(c)
        c = c.get_next_sibling()


def button_labels(widget):
    return [w.get_label() for w in walk(widget) if isinstance(w, Gtk.Button) and w.get_label()]


def to_hdmi(win):
    """An unsaved edit: the profile's output becomes the HDMI device."""
    win.output_row.set_selected(2)
    assert win.profile().audio_output.description == "GA102 HDMI 2 (NVIDIA)"


def layout(sent_lms):
    return [(m.connector, lm.x, lm.y, lm.primary) for lm in sent_lms for m in lm.monitors]


DEMO_LIVE = [("HDMI-1", 0, 0, True), ("HDMI-2", 1920, 0, False), ("HDMI-3", 3840, 0, False)]  # demo's "now"


def test_the_bar_holds_the_actions_and_the_header_does_not(win):
    shown(win)
    header = next(w for w in walk(win.get_content()) if isinstance(w, Adw.HeaderBar))
    assert not any("Apply" in label or "Switch" in label for label in button_labels(header))
    assert button_labels(win.bar_normal) == ["Discard", "Save", "Test"]
    assert win.switch_label.get_label() == "Switch to profile" and win.switch_label.is_ancestor(win.bar_normal)
    assert win.switch_button.has_css_class("suggested-action")
    assert win.status_label.get_label() == "✓ Saved"
    for name in ("save", "discard", "undo", "redo"):
        assert not win.lookup_action(name).get_enabled()
    for name in ("test", "switch"):
        assert win.lookup_action(name).get_enabled()
    assert win.test_seconds.get_text() == "15"
    # The bar is in the profile pane, not under the sidebar.
    assert win.bar_stack.get_ancestor(Adw.OverlaySplitView) is not None
    side = win.sidebar_list.get_parent().compute_bounds(win)[1]
    bar = win.bar_stack.compute_bounds(win)[1]
    assert bar.get_x() >= side.get_x() + side.get_width() - 1
    assert side.get_y() + side.get_height() >= bar.get_y() + bar.get_height() - 1
    app = win.get_application()
    assert app.get_accels_for_action("win.undo") == ["<Control>z"]
    assert any(same_accel(a, "<Control><Shift>z") for a in app.get_accels_for_action("win.redo"))
    assert app.get_accels_for_action("win.save") == ["<Control>s"]
    assert app.get_accels_for_action("win.switch") == ["<Control>Return"]


def test_editing_never_touches_the_live_screens_or_audio(win):
    projector = next(k.screen for k in win.known.screens if k.screen.identity.vendor == "STK")
    win._on_add_screen(None, projector)
    win.selected = projector
    win._on_make_primary(None)
    left = win.profile().displays.logical_monitors[0].screens[0].screen
    win._on_canvas_moved(left, 0, 300, True)
    to_hdmi(win)
    win.input_row.set_selected(0)
    assert win.set_shortcut("Desk — 3 monitors", "<Control><Alt>p") is None
    win._on_turn_off(None)
    win.activate_action("win.undo", None)
    win.activate_action("win.save", None)
    assert win.ceb.display.sent == [] and win.ceb.audio.sent == []


def test_unsaved_status_undo_redo_and_discard_in_the_bar(win):
    saved = win.ceb.profile("Desk — 3 monitors").to_json()
    to_hdmi(win)
    assert win.status_label.get_label() == "● Unsaved changes" and win.status_label.has_css_class("warning")
    assert win.lookup_action("save").get_enabled() and win.lookup_action("discard").get_enabled()
    assert win.lookup_action("undo").get_enabled() and not win.lookup_action("redo").get_enabled()
    win.activate_action("win.undo", None)
    assert win.output_row.get_selected() == 1 and win.status_label.get_label() == "✓ Saved"
    assert win.lookup_action("redo").get_enabled()
    win.activate_action("win.redo", None)
    assert win.profile().audio_output.description == "GA102 HDMI 2 (NVIDIA)"
    win.activate_action("win.discard", None)
    assert win.profile().to_json() == saved and win.status_label.get_label() == "✓ Saved"
    assert not win.lookup_action("undo").get_enabled()
    to_hdmi(win)
    win.activate_action("win.save", None)
    assert win.ceb.profile("Desk — 3 monitors").audio_output.description == "GA102 HDMI 2 (NVIDIA)"
    assert win.status_label.get_label() == "✓ Saved" and not win.lookup_action("undo").get_enabled()


def test_unsaved_edits_survive_a_screen_change_reload(win):
    to_hdmi(win)
    win.reload(select=win.current)                       # what MonitorsChanged does
    assert win.dirty() and win.profile().audio_output.description == "GA102 HDMI 2 (NVIDIA)"
    assert win.output_row.get_selected() == 2


def test_opening_another_profile_with_unsaved_edits_asks_first(win, dialogs):
    desk_saved = win.ceb.profile("Desk — 3 monitors").to_json()
    to_hdmi(win)
    center = by_name(win.sidebar_list, "Center only")
    win._on_row_activated(win.sidebar_list, center)
    assert "Save Changes" in dialogs[-1].get_heading() and win.current == "Desk — 3 monitors"
    assert win.sidebar_list.get_selected_row().profile_name == "Desk — 3 monitors"
    dialogs[-1].emit("response", "cancel")
    assert win.current == "Desk — 3 monitors" and win.dirty()

    win._on_row_activated(win.sidebar_list, center)
    dialogs[-1].emit("response", "discard")
    assert win.current == "Center only" and not win.dirty()
    assert win.ceb.profile("Desk — 3 monitors").to_json() == desk_saved
    assert win.sidebar_list.get_selected_row().profile_name == "Center only"

    win._on_row_activated(win.sidebar_list, by_name(win.sidebar_list, "Desk — 3 monitors"))
    assert win.current == "Desk — 3 monitors"            # nothing unsaved: no question
    n = len(dialogs)
    to_hdmi(win)
    win._on_row_activated(win.sidebar_list, center)
    assert len(dialogs) == n + 1
    dialogs[-1].emit("response", "save")
    assert win.current == "Center only"
    assert win.ceb.profile("Desk — 3 monitors").audio_output.description == "GA102 HDMI 2 (NVIDIA)"


def test_new_profile_rename_and_close_ask_about_unsaved_edits_first(win, dialogs):
    shown(win)
    to_hdmi(win)
    win.activate_action("win.new-profile", None)
    assert "Save Changes" in dialogs[-1].get_heading()
    dialogs[-1].emit("response", "cancel")
    win.activate_action("win.rename-profile", None)
    assert "Save Changes" in dialogs[-1].get_heading()
    dialogs[-1].emit("response", "cancel")
    win.close()
    spin(lambda: False, 0.1)
    assert win.get_visible() and "Save Changes" in dialogs[-1].get_heading()
    dialogs[-1].emit("response", "cancel")
    spin(lambda: False, 0.1)
    assert win.get_visible() and win.dirty()
    win.close()
    dialogs[-1].emit("response", "discard")
    assert spin(lambda: not win.get_visible())
    assert win.ceb.display.sent == []


def test_switch_with_unsaved_edits_uses_the_saved_profile(win):
    toasts = []
    win.toast = toasts.append
    saved_output = win.ceb.profile("Desk — 3 monitors").audio_output.name
    to_hdmi(win)
    win.activate_action("win.switch", None)
    assert spin(lambda: not win._applying)
    assert ("output", saved_output) in win.ceb.audio.sent
    assert not any("hdmi" in name for _, name in win.ceb.audio.sent)
    assert win.dirty()                                   # the edits are still there, unsaved
    assert any("unsaved changes are not used" in t for t in toasts)


def type_seconds(win, text):
    win.test_seconds.set_text("")
    for ch in text:                                       # typed key by key
        win.test_seconds.insert_text(ch, -1)
    win.test_seconds.emit("activate")                   # Enter


def start_test(win, seconds=5):
    type_seconds(win, str(seconds))
    win.activate_action("win.test", None)
    assert spin(lambda: win._testing is not None and not win._applying)


def test_test_applies_the_profile_as_shown_then_goes_back(win):
    shown(win)
    to_hdmi(win)                                         # Test uses unsaved edits too
    start_test(win, 5)
    sent = win.ceb.display.sent
    assert [m for m, _ in sent] == [0, 1]
    assert layout(sent[1][1]) == [("HDMI-1", 0, 0, False), ("HDMI-3", 1920, 0, True), ("HDMI-2", 3840, 0, False)]
    assert win.ceb.audio.sent[0] == ("output", "alsa_output.pci-0000_2d_00.1.hdmi-stereo-extra1")
    assert win.bar_stack.get_visible_child_name() == "testing" and win.bar_stack.has_css_class("ceb-testing")
    assert win.testing_label.get_label() == "Testing “Desk — 3 monitors” — going back in 5 s"
    assert button_labels(win.bar_testing) == ["Go back now", "Keep"]
    assert not win.lookup_action("switch").get_enabled() and not win.lookup_action("test").get_enabled()
    spin(lambda: False, 1.3)
    assert win.testing_label.get_label().endswith("going back in 4 s")      # it counts down

    win._testing["deadline"] = time.monotonic()           # skip the rest of the wait
    assert spin(lambda: win._testing is None and not win._applying)
    assert [m for m, _ in sent] == [0, 1, 0, 1]
    assert layout(sent[3][1]) == DEMO_LIVE                # back to what was on before the test
    assert win.ceb.audio.sent[-2:] == [
        ("output", "alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.analog-stereo"),
        ("input", "alsa_input.usb-NexiGo_N60_FHD_Webcam-02.mono-fallback")]
    assert win.bar_stack.get_visible_child_name() == "normal" and not win.bar_stack.has_css_class("ceb-testing")
    assert win.dirty()                                   # testing saves nothing


def test_going_back_needs_no_click_and_no_visible_window(win):
    shown(win)
    start_test(win, 3)
    win.set_visible(False)                                # e.g. left on a screen the test turned off
    assert spin(lambda: win._testing is None and not win._applying, timeout=8)
    assert [m for m, _ in win.ceb.display.sent] == [0, 1, 0, 1]
    assert layout(win.ceb.display.sent[3][1]) == DEMO_LIVE


def test_keep_stays_on_the_tested_profile(win):
    start_test(win, 3)
    win.activate_action("win.test-keep", None)
    assert win._testing is None and win.bar_stack.get_visible_child_name() == "normal"
    spin(lambda: False, 3.5)
    assert [m for m, _ in win.ceb.display.sent] == [0, 1]   # never went back


def test_go_back_now(win):
    start_test(win, 60)
    win.activate_action("win.test-back", None)
    assert spin(lambda: win._testing is None and not win._applying)
    assert [m for m, _ in win.ceb.display.sent] == [0, 1, 0, 1]


def test_closing_during_a_test_goes_back_then_closes(win):
    shown(win)
    start_test(win, 60)
    win.close()
    assert spin(lambda: not win.get_visible())
    assert [m for m, _ in win.ceb.display.sent] == [0, 1, 0, 1]
    assert layout(win.ceb.display.sent[3][1]) == DEMO_LIVE


def test_test_reports_a_missing_screen_and_can_test_the_rest(win, dialogs):
    projector = next(k.screen for k in win.known.screens if k.screen.identity.vendor == "STK")
    win._on_add_screen(None, projector)
    win.activate_action("win.test", None)
    assert spin(lambda: not win._applying)
    assert win.ceb.display.sent == [] and win._testing is None
    assert "Not Applied" in dialogs[-1].get_heading() and "S2-TEK" in dialogs[-1].get_body()
    dialogs[-1].emit("response", "rest")
    assert spin(lambda: win._testing is not None and not win._applying)
    assert [m for m, _ in win.ceb.display.sent] == [0, 1]
    win.activate_action("win.test-back", None)
    assert spin(lambda: win._testing is None and not win._applying)


def test_the_test_length_is_one_setting_remembered_across_profiles_and_restarts(win, app):
    assert not win.ceb.store.settings_path.exists()
    type_seconds(win, "5")
    assert win.ceb.store.load_settings().test_seconds == 5
    assert '"test_seconds": 5' in win.ceb.store.settings_path.read_text()
    win.open_profile("Center only")
    assert win.test_seconds.get_text() == "5"
    again = CebWindow(app, win.ceb, win.shortcuts, demo=True)   # CEB started again
    try:
        assert again.test_seconds.get_text() == "5"
    finally:
        again.destroy()


def test_the_test_length_box_takes_digits_in_range(win):
    type_seconds(win, "1a2")                              # letters never get in
    assert win.test_seconds.get_text() == "12" and win.settings.test_seconds == 12
    type_seconds(win, "1")
    assert win.test_seconds.get_text() == "3"             # the shortest test is 3 s
    type_seconds(win, "999")
    assert win.test_seconds.get_text() == "600"
    win.test_seconds.set_text("")
    win.commit_test_seconds()
    assert win.test_seconds.get_text() == "600"           # empty: back to the one in use
    win.test_seconds.set_text("7")                        # typed, Test pressed without Enter
    win.activate_action("win.test", None)
    assert spin(lambda: win._testing is not None and not win._applying)
    assert win.test_remaining() == 7 and win.ceb.store.load_settings().test_seconds == 7
    win.activate_action("win.test-back", None)
    assert spin(lambda: win._testing is None and not win._applying)


def test_an_unreadable_settings_file_is_reported_and_kept(tmp_path, app):
    ceb = Ceb(display=DemoDisplay(), audio=DemoAudio(), store=Store(tmp_path / "ceb"))
    shortcuts = Shortcuts(backend=Gio.memory_settings_backend_new(), command="ceb")
    seed(ceb, shortcuts, same_accel)
    ceb.store.settings_path.write_text("{oops")
    w = CebWindow(app, ceb, shortcuts, demo=True)
    try:
        assert "Test length not remembered" in w.banner.get_title()
        assert w.test_seconds.get_text() == "15"
        type_seconds(w, "7")
        assert w.settings.test_seconds == 7                       # used while CEB runs
        assert ceb.store.settings_path.read_text() == "{oops"
    finally:
        w.destroy()


# ------------------------------------------------------------ review of 24ad3c1 (2026-11-05)


class StatefulDisplay:
    """conftest's FakeDisplay whose state follows what is applied with method 1, so going back can
    be checked against what is "on" (method 2 is refused, as in every fake)."""

    def __init__(self, reply_):
        from conftest import FakeDisplay
        self.fake = FakeDisplay(reply_)
        self.cur = reply_
        self.down = False

    def __getattr__(self, name):
        return getattr(self.fake, name)

    def get_state(self):
        from ceb_profiles.mutter import DisplayError, parse_current_state
        if self.down:
            raise DisplayError("mutter gone")
        return parse_current_state(self.cur)

    def apply(self, serial, method, lms, props):
        self.fake.apply(serial, method, lms, props)
        if method == 1:
            serial0, monitors, _old, p = self.cur
            specs = {m[0][0]: m[0] for m in monitors}
            self.cur = (serial0 + 1, monitors,
                        [(lm.x, lm.y, lm.scale, lm.transform, lm.primary, [specs[m.connector] for m in lm.monitors], {})
                         for lm in lms], p)

    def live(self):
        from ceb_profiles.mutter import parse_current_state
        return sorted((c, lm.x, lm.y, lm.primary) for lm in parse_current_state(self.cur).logical_monitors
                      for c in lm.connectors)


@pytest.fixture
def real(tmp_path, app):
    """A window over the real-desk fakes (conftest): a stateful display and FakeAudio, with
    monitor sources. Not --demo for its data, but demo=True so nothing is watched."""
    from conftest import FakeAudio, desk
    display = StatefulDisplay(desk(1))
    ceb = Ceb(display=display, audio=FakeAudio(), store=Store(tmp_path / "ceb"))
    ceb._sleep = lambda s: None
    ceb.save_current("desk")
    w = CebWindow(app, ceb, Shortcuts(backend=Gio.memory_settings_backend_new(), command="ceb"), demo=True)
    display.calls.clear()
    yield w
    w.destroy()


def only_rog(w):
    for lm in list(w.profile().displays.logical_monitors):
        sid = lm.screens[0].screen
        if sid.identity.vendor != "AUS":
            w.selected = sid
            w._on_turn_off(None)


def run_out(w):
    w._testing["deadline"] = time.monotonic()
    assert spin(lambda: w._testing is None and not w._applying)


USB_MONITOR = "alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.analog-stereo.monitor"


def test_go_back_restores_screens_and_audio_as_they_were(real):
    before = real.ceb.display.live()
    only_rog(real)
    real.output_row.set_selected(2)                       # the HDMI output
    start_test(real, 3)
    assert real.ceb.display.live() != before and "hdmi" in real.ceb.audio.state.default_output
    run_out(real)
    assert real.ceb.display.live() == before
    assert real.ceb.audio.state.default_output.startswith("alsa_output.usb-C-Media")


@pytest.mark.parametrize("profile_input", [0, 1])           # 0: leave the input unchanged; 1: the webcam
def test_a_monitor_default_source_comes_back_exactly_with_no_error(real, dialogs, profile_input):
    real.ceb.audio.state.default_input = USB_MONITOR       # PipeWire's default when there is no microphone
    only_rog(real)
    real.input_row.set_selected(profile_input)
    start_test(real, 3)
    run_out(real)
    assert real.ceb.audio.state.default_input == USB_MONITOR
    assert not dialogs, [d.get_heading() for d in dialogs]


def test_a_test_that_cannot_read_audio_leaves_audio_alone_and_says_so(real, monkeypatch):
    toasts = []
    monkeypatch.setattr(real, "toast", toasts.append)
    real.output_row.set_selected(2)                       # the profile would change the output
    real.ceb.audio.down = True                            # but the audio server cannot be read
    only_rog(real)
    start_test(real, 3)
    assert real.ceb.audio.set_calls == []
    assert any("Audio not tested" in t for t in toasts)
    real.ceb.audio.down = False
    run_out(real)
    assert real.ceb.audio.set_calls == []                 # going back does not touch audio either


def test_the_test_length_box_ignores_non_ascii_digits(real):
    real.test_seconds.set_text("²٣5")                     # superscript two, Arabic-Indic three
    assert real.test_seconds.get_text() == "5"
    real.test_seconds.set_text("")
    real.test_seconds.get_delegate().set_text("²")        # whatever got past the filter
    assert real.commit_test_seconds() == 15
    only_rog(real)
    start_test(real, 4)
    assert real.test_remaining() == 4
    run_out(real)


def test_a_typed_test_length_is_kept_when_the_window_closes(real):
    shown(real)
    real.test_seconds.grab_focus()
    real.test_seconds.set_text("42")
    real.close()
    assert spin(lambda: not real.get_visible())
    assert real.ceb.store.load_settings().test_seconds == 42


def test_unsaved_edits_survive_an_unreadable_profiles_file(real):
    only_rog(real)
    path = real.ceb.store.profiles_path
    good = path.read_text()
    path.write_text("{oops")
    real.reload(select=real.current)
    assert real.dirty() and "profiles.json" in real.banner.get_title()
    assert rows(real.sidebar_list) == ["desk"]                # still shown as last read
    assert not real.save()                                   # reported, not lost
    assert path.read_text() == "{oops"
    path.write_text(good)
    real.reload(select="desk")
    assert real.dirty() and len(real.profile().displays.logical_monitors) == 1
    assert real.save()


def test_a_profile_deleted_outside_while_edited_asks_and_can_be_saved_again(real, dialogs):
    real.ceb.save_current("second")
    real.reload(select="desk")
    only_rog(real)
    real.ceb.delete_profile("desk")
    real.reload(select=real.current)
    assert real.current == "desk" and real.dirty()            # the edits are still shown
    assert "Deleted Outside" in dialogs[-1].get_heading()
    n = len(dialogs)
    real.reload(select=real.current)
    assert len(dialogs) == n                                  # asked once
    dialogs[-1].emit("response", "save")
    stored = real.ceb.profile("desk")
    assert len(stored.displays.logical_monitors) == 1 and not real.dirty()
    assert [p.name for p in real.ceb.profiles()] == ["second", "desk"]


def test_a_profile_deleted_outside_while_edited_can_be_discarded(real, dialogs):
    real.ceb.save_current("second")
    real.reload(select="desk")
    only_rog(real)
    real.ceb.delete_profile("desk")
    real.reload(select=real.current)
    dialogs[-1].emit("response", "discard")
    assert real.current == "second" and not real.dirty()
    assert [p.name for p in real.ceb.profiles()] == ["second"]


def test_a_failed_go_back_keeps_what_to_go_back_to_and_offers_retry(real, dialogs):
    from ceb_profiles.mutter import DisplayError
    before = real.ceb.display.live()
    only_rog(real)
    start_test(real, 30)
    real.ceb.display.fail_with[0] = DisplayError("refused")   # GNOME refuses the go-back once
    real.activate_action("win.test-back", None)
    assert spin(lambda: not real._applying and real._testing is not None)
    assert real._testing["failed"] and "Go Back" in dialogs[-1].get_heading()
    assert real.bar_stack.get_visible_child_name() == "testing"
    assert real.back_button.get_label() == "Retry" and real.lookup_action("test-back").get_enabled()
    assert "Could not go back" in real.testing_label.get_label()
    spin(lambda: False, 0.5)
    assert real._testing is not None                          # no countdown runs out on a failure
    real.activate_action("win.test-back", None)               # Retry
    assert spin(lambda: real._testing is None and not real._applying)
    assert real.ceb.display.live() == before


def test_new_profile_is_blocked_during_a_test(real):
    only_rog(real)
    start_test(real, 30)
    assert not real.lookup_action("new-profile").get_enabled()
    real.activate_action("win.test-back", None)
    assert spin(lambda: real._testing is None and not real._applying)
    assert real.lookup_action("new-profile").get_enabled()


def test_a_signal_during_a_test_goes_back_then_closes_without_asking(real, dialogs):
    from ceb_profiles.gui import quit_on_signal
    shown(real)
    before = real.ceb.display.live()
    only_rog(real)                                            # unsaved edits too: no question asked
    start_test(real, 30)
    quit_on_signal(real.get_application())                    # what SIGTERM/SIGINT/SIGHUP run
    assert spin(lambda: not real.get_visible())
    assert real.ceb.display.live() == before and not dialogs


def test_a_screen_read_that_started_before_a_save_is_not_shown(real):
    stale = real._read_state()                                # a MonitorsChanged read, in its thread
    only_rog(real)
    assert real.save()
    real.MONITORS_SETTLE_MS = real.MONITORS_RETRY_MS = 20
    real._monitors_read(stale)                                # its result comes back late
    assert len(real.profile().displays.logical_monitors) == 1 and not real.dirty()
    assert spin(lambda: real._monitors_pending is None and not real._monitors_reading)   # read again
    assert len(real.profile().displays.logical_monitors) == 1


def test_discard_after_an_outside_change_shows_what_is_stored_now(real):
    only_rog(real)
    newer = real.ceb.profile("desk")
    newer.audio_output = None                                 # changed outside, e.g. save-current --force
    real.ceb.put_profile(newer)
    real.reload(select="desk")                                # the edits are kept over a reload
    real.discard()
    assert real.profile().audio_output is None and not real.dirty()
    assert len(real.profile().displays.logical_monitors) == 3
