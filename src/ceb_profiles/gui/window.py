"""CEB's main window, built from the picked mockup (option A, "Settings-style sidebar").

Header bar: + (new profile), the title, and a menu (rename, delete, remembered devices, remember
screens in the background, about).
Left: the profiles, each with its keyboard shortcut. Right, for the selected profile: the
arrangement canvas, "Other screens" (screens not in this profile, connected or remembered, each with
Add), "Selected screen" (Make primary, Turn off in profile), "Audio for this profile" (Output, Input,
Keyboard shortcut), and the action bar docked along the bottom of that pane (mockup round 2,
option A): the status ("Unsaved changes" / "Saved"), Undo, Redo, Discard, Save, the Test length in
seconds, Test, and "Switch to profile".

Editing never touches the live screens or audio, and nothing is written until Save: the edits are
kept in a Draft (draft.py). Only three things change the live screens and audio: "Switch to
profile" (the saved version), the profile's keyboard shortcut (`ceb apply`), and Test, which
applies the profile as shown and goes back to what was on before after N seconds unless Keep.
"""
from __future__ import annotations

import copy
import math
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from .. import desktop  # noqa: E402
from ..audio import AudioError  # noqa: E402
from ..core import ApplyResult, Ceb, CebError, ProfileChanged  # noqa: E402
from ..model import AudioRef, Profile, ScreenId  # noqa: E402
from ..mutter import BUS_NAME, INTERFACE, OBJECT_PATH, DisplayError  # noqa: E402
from ..store import (TEST_SECONDS_MAX, TEST_SECONDS_MIN, Known, Settings, StoreError,  # noqa: E402
                     clamp_test_seconds)
from . import APP_ID, editor  # noqa: E402  (APP_ID: --demo runs as APP_ID.Demo, which has no icon)
from .canvas import ArrangementCanvas, Item  # noqa: E402
from .draft import Draft  # noqa: E402
from .geometry import is_valid  # noqa: E402
from .shortcuts import NOT_FOUND, Shortcuts, ShortcutError, delete_profile  # noqa: E402

CSS = """
.ceb-sidebar-key { font-family: monospace; font-size: 0.85em; }
.ceb-chip { padding: 8px 12px; border-radius: 10px; }
.ceb-dot { min-width: 9px; min-height: 9px; border-radius: 99px; }
.ceb-dot.connected { background-color: #2ec27e; }  /* the mockup's green (GNOME palette Green 4) */
.ceb-dot.remembered { background-color: #777777; }
.ceb-canvas-frame { border-radius: 12px; }
.ceb-testing { background-color: alpha(@accent_bg_color, 0.30); }  /* the mockup's blue Test bar */
"""


def accel_label(accel: str | None) -> str:
    if not accel:
        return ""
    ok, key, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_get_label(key, mods) if ok else accel


def same_accel(a: str, b: str) -> bool:
    pa, pb = Gtk.accelerator_parse(a), Gtk.accelerator_parse(b)
    return pa[0] and pb[0] and Gdk.keyval_to_lower(pa[1]) == Gdk.keyval_to_lower(pb[1]) and pa[2] == pb[2]


class CebWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, ceb: Ceb, shortcuts: Shortcuts, *, dry_run: bool = False,
                 demo: bool = False, config_dir: str | None = None, autostart_base: str | None = None):
        """config_dir: the non-default config folder (store.explicit_config_dir), passed on to the
        background watcher's command. autostart_base: where the watcher's autostart entry goes
        instead of $XDG_CONFIG_HOME (the demo and tests)."""
        super().__init__(application=app, title="CEB", default_width=1100, default_height=760)
        self.ceb, self.shortcuts, self.dry_run, self.demo = ceb, shortcuts, dry_run, demo
        self.config_dir = config_dir
        self.autostart_base = autostart_base
        self._audio_events = None
        self.profiles: list[Profile] = []
        self.known = Known()
        self.display_state = None
        self.audio_state = None
        self.keys: dict[str, str] = {}
        self.current: str | None = None      # name of the selected profile
        self.selected: ScreenId | None = None
        self.draft: Draft | None = None      # the selected profile as edited (saved or not)
        self._updating = False
        self._applying = False               # a switch, a test or going back is running in a thread
        self._busy_kind: str | None = None   # "switch", "test" or "back"
        self._testing: dict | None = None    # a test that is on screen now: name, before, deadline
        self._test_timer = None
        self._close_after_apply = False
        self._force_quit = False             # SIGTERM/SIGINT/SIGHUP: go back from a test, then close, no questions
        self._generation = 0                 # bumped by every edit, save, discard and reload
        self._orphan_asked: str | None = None
        self.settings, settings_problem = self._load_settings()
        self._settings_problem = settings_problem
        self._closed = False
        self._signal_id = None
        self._monitors_pending = None   # GLib source id of a deferred MonitorsChanged reload
        self._monitors_reading = False  # a worker thread is reading the screens and audio

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._build()
        self._install_actions()
        self.reload(select=None)
        self._watch_monitors()
        self.connect("close-request", self._on_close)

    # ================================================================ building

    def _build(self) -> None:
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        self.split = Adw.OverlaySplitView(min_sidebar_width=220, max_sidebar_width=280,
                                          sidebar_width_fraction=0.24)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="CEB", subtitle="")
        header.set_title_widget(self.title)

        self.sidebar_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Show Profiles",
                                               visible=False)
        self.sidebar_button.bind_property("active", self.split, "show-sidebar",
                                          GObject.BindingFlags.BIDIRECTIONAL)
        header.pack_start(self.sidebar_button)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New Profile From Current Screens")
        add.set_action_name("win.new-profile")
        header.pack_start(add)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("_Rename Profile…", "win.rename-profile")
        section.append("_Delete Profile…", "win.delete-profile")
        menu.append_section(None, section)
        history = Gio.Menu()
        history.append("_Remembered Devices…", "win.remembered")
        history.append("Remember Screens in the _Background", "win.remember-background")
        menu.append_section(None, history)
        about = Gio.Menu()
        about.append("_About CEB", "win.about")
        menu.append_section(None, about)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Main Menu"))
        view.add_top_bar(header)

        self.banner = Adw.Banner(revealed=False)
        view.add_top_bar(self.banner)

        # ---- sidebar
        # The selected profile changes only when a row is activated (click, Enter, Space), never
        # because keyboard focus moved onto another row (Tab, arrow keys).
        self.sidebar_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar_list.add_css_class("navigation-sidebar")
        self.sidebar_list.connect("row-selected", self._on_row_selected)
        self.sidebar_list.connect("row-activated", self._on_row_activated)
        side_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        side_scroll.set_child(self.sidebar_list)
        self.split.set_sidebar(side_scroll)

        # ---- content
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=14, margin_bottom=14,
                       margin_start=14, margin_end=14)
        # The profile's name; shown only in the narrow layout, where the header has no room for it.
        self.narrow_title = Gtk.Label(xalign=0, ellipsize=3, visible=False)
        self.narrow_title.add_css_class("title-4")
        main.append(self.narrow_title)
        self.canvas = ArrangementCanvas(self._on_canvas_select, self._on_canvas_moved)
        self.canvas.set_vexpand(True)
        main.append(self.canvas)

        self.other_group = Adw.PreferencesGroup(title="Other screens")
        self.tray = Adw.WrapBox(child_spacing=10, line_spacing=10)
        tray_box = Gtk.Box(margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        tray_box.append(self.tray)
        tray_card = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        tray_card.add_css_class("boxed-list")
        row = Gtk.ListBoxRow(activatable=False)
        row.set_child(tray_box)
        tray_card.append(row)
        self.other_group.add(tray_card)
        main.append(self.other_group)

        self.selected_group = Adw.PreferencesGroup(title="Selected screen")
        # A row like AdwActionRow, whose buttons move under the name when the window is narrow.
        self.selected_layout = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER, spacing=2)
        self.selected_title = Gtk.Label(xalign=0, ellipsize=3)
        self.selected_detail = Gtk.Label(xalign=0, wrap=True)
        self.selected_detail.add_css_class("dim-label")
        self.selected_detail.add_css_class("caption")
        labels.append(self.selected_title)
        labels.append(self.selected_detail)
        buttons = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.primary_button = Gtk.Button()
        self.primary_button.connect("clicked", self._on_make_primary)
        off = Gtk.Button(label="Turn off in profile")
        off.connect("clicked", self._on_turn_off)
        buttons.append(self.primary_button)
        buttons.append(off)
        self.selected_layout.append(labels)
        self.selected_layout.append(buttons)
        selected_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        selected_list.add_css_class("boxed-list")
        row = Gtk.ListBoxRow(activatable=False, child=self.selected_layout)
        row.set_size_request(-1, 50)
        selected_list.append(row)
        self.selected_list = selected_list
        self.mirror_rows: list[Gtk.Widget] = []  # the other screens of a mirror group, below the row
        self.selected_group.add(selected_list)
        main.append(self.selected_group)

        self.audio_group = Adw.PreferencesGroup(title="Audio for this profile")
        self.output_row = Adw.ComboRow(title="Output", factory=_wide_label_factory())
        self.input_row = Adw.ComboRow(title="Input", factory=_wide_label_factory())
        for row in (self.output_row, self.input_row):
            row.set_list_factory(_checked_list_factory(row))
        self.output_row.connect("notify::selected", self._on_audio_changed, "output")
        self.input_row.connect("notify::selected", self._on_audio_changed, "input")
        self.audio_group.add(self.output_row)
        self.audio_group.add(self.input_row)
        self.key_row = Adw.ActionRow(title="Keyboard shortcut", subtitle="Switches to this profile from anywhere")
        self.key_label = Adw.ShortcutLabel(valign=Gtk.Align.CENTER)
        self.key_none = Gtk.Label(label="Not set", valign=Gtk.Align.CENTER)
        self.key_none.add_css_class("dim-label")
        change = Gtk.Button(label="Change", valign=Gtk.Align.CENTER)
        change.connect("clicked", self._on_change_shortcut)
        self.change_button = change
        self.key_row.add_suffix(self.key_label)
        self.key_row.add_suffix(self.key_none)
        self.key_row.add_suffix(change)
        self.audio_group.add(self.key_row)
        main.append(self.audio_group)

        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(main)
        # The action bar is docked along the bottom of the profile pane; the sidebar keeps its
        # full height (mockup round 2, option A).
        profile_page = Adw.ToolbarView(bottom_bar_style=Adw.ToolbarStyle.RAISED_BORDER)
        profile_page.set_content(scroll)
        profile_page.add_bottom_bar(self._build_bar())
        self.content_stack = Gtk.Stack()
        self.content_stack.add_named(profile_page, "profile")
        self.empty_page = Adw.StatusPage(icon_name="video-display-symbolic", title="No Profiles",
                                         description="Arrange your screens in GNOME Settings, then press + to "
                                                     "save them as a profile.")
        new = Gtk.Button(label="New Profile From Current Screens", halign=Gtk.Align.CENTER)
        new.add_css_class("pill")
        new.add_css_class("suggested-action")
        new.set_action_name("win.new-profile")
        self.empty_page.set_child(new)
        self.content_stack.add_named(self.empty_page, "empty")
        self.split.set_content(self.content_stack)

        view.set_content(self.split)
        self.toasts.set_child(view)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        bp.add_setter(self.split, "collapsed", True)
        bp.add_setter(self.sidebar_button, "visible", True)
        bp.add_setter(self.selected_layout, "orientation", Gtk.Orientation.VERTICAL)
        # With minimize/maximize/close in the title bar the header needs 388 px; without the title
        # it fits 360 px (GNOME's smallest window width).
        bp.add_setter(self.title, "visible", False)
        bp.add_setter(self.narrow_title, "visible", True)
        bp.add_setter(self.bar_normal, "orientation", Gtk.Orientation.VERTICAL)
        bp.add_setter(self.bar_testing, "orientation", Gtk.Orientation.VERTICAL)
        self.add_breakpoint(bp)

    def _build_bar(self) -> Gtk.Widget:
        """Status on the left; Undo, Redo; then Discard, Save, the test length, Test and "Switch to
        profile" on the right. While a test is on screen: "Testing … going back in N s", Go back
        now, Keep."""
        self.bar_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hhomogeneous=True,
                                   vhomogeneous=False)

        self.bar_normal = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        left = Gtk.Box(spacing=6, hexpand=True, valign=Gtk.Align.CENTER)
        self.status_label = Gtk.Label(xalign=0, margin_end=6)
        undo = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Undo (Ctrl+Z)", action_name="win.undo")
        redo = Gtk.Button(icon_name="edit-redo-symbolic", tooltip_text="Redo (Ctrl+Shift+Z)", action_name="win.redo")
        for b in (undo, redo):
            b.add_css_class("flat")
        left.append(self.status_label)
        left.append(undo)
        left.append(redo)
        self.bar_normal.append(left)

        right = Adw.WrapBox(child_spacing=6, line_spacing=6, align=1.0, valign=Gtk.Align.CENTER)
        discard = Gtk.Button(label="Discard", action_name="win.discard",
                             tooltip_text="Throw away the unsaved changes to this profile")
        save = Gtk.Button(label="Save", action_name="win.save",
                          tooltip_text="Save this profile (Ctrl+S); your screens and audio do not change")
        test_box = Gtk.Box(spacing=4)
        # A small text box for the test length (Kevin: "a small test timer text box"): digits only,
        # taken when Enter is pressed, the box is left, or Test is pressed.
        self.test_seconds = Gtk.Entry(text=str(self.settings.test_seconds), width_chars=3, max_width_chars=3,
                                      max_length=3, xalign=1, input_purpose=Gtk.InputPurpose.DIGITS,
                                      valign=Gtk.Align.CENTER,
                                      tooltip_text=f"How many seconds Test keeps the profile before it goes "
                                                   f"back ({TEST_SECONDS_MIN} to {TEST_SECONDS_MAX}; for every "
                                                   f"profile)")
        self.test_seconds.update_property([Gtk.AccessibleProperty.LABEL], ["Test length in seconds"])
        self.test_seconds.connect("changed", _digits_only)
        self.test_seconds.connect("activate", lambda *_: self.commit_test_seconds())
        leave = Gtk.EventControllerFocus()
        leave.connect("leave", lambda *_: self.commit_test_seconds())
        self.test_seconds.add_controller(leave)
        self.test_button = Gtk.Button(label="Test", action_name="win.test",
                                      tooltip_text="Switch to this profile as shown now, then go back to your "
                                                   "screens and audio after the seconds set here, unless you "
                                                   "press Keep")
        seconds_label = Gtk.Label(label="s", margin_end=2)
        seconds_label.add_css_class("dim-label")
        test_box.append(self.test_seconds)
        test_box.append(seconds_label)
        test_box.append(self.test_button)
        self.switch_button = Gtk.Button(action_name="win.switch",
                                        tooltip_text="Switch the screens and audio to the saved profile (Ctrl+Return)")
        switch_box = Gtk.Box(spacing=6)
        self.switch_spinner = Adw.Spinner(visible=False)
        self.switch_label = Gtk.Label(label="Switch to profile")
        switch_box.append(self.switch_spinner)
        switch_box.append(self.switch_label)
        self.switch_button.set_child(switch_box)
        self.switch_button.add_css_class("suggested-action")
        for w in (discard, save, test_box, self.switch_button):
            right.append(w)
        self.bar_normal.append(right)
        self.bar_stack.add_named(self.bar_normal, "normal")

        self.bar_testing = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        self.testing_label = Gtk.Label(xalign=0, hexpand=True, wrap=True, valign=Gtk.Align.CENTER)
        self.testing_label.update_property([Gtk.AccessibleProperty.LABEL], ["Test countdown"])
        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        self.back_button = Gtk.Button(label="Go back now", action_name="win.test-back")
        self.keep_button = Gtk.Button(label="Keep", action_name="win.test-keep",
                                      tooltip_text="Stay on this profile")
        self.keep_button.add_css_class("suggested-action")
        buttons.append(self.back_button)
        buttons.append(self.keep_button)
        self.bar_testing.append(self.testing_label)
        self.bar_testing.append(buttons)
        self.bar_stack.add_named(self.bar_testing, "testing")
        return self.bar_stack

    def _install_actions(self) -> None:
        for name, cb in (("new-profile", self._on_new_profile), ("switch", self._on_switch),
                         ("save", self._on_save), ("discard", self._on_discard),
                         ("undo", self._on_undo), ("redo", self._on_redo),
                         ("test", self._on_test), ("test-back", self._on_test_back),
                         ("test-keep", self._on_test_keep),
                         ("rename-profile", self._on_rename), ("delete-profile", self._on_delete),
                         ("remembered", self._on_remembered), ("about", self._on_about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(action)
        background = Gio.SimpleAction.new_stateful(
            "remember-background", None, GLib.Variant.new_boolean(self._background_on()))
        background.connect("change-state", self._on_remember_background)
        self.add_action(background)
        app = self.get_application()
        app.set_accels_for_action("win.new-profile", ["<Control>n"])
        app.set_accels_for_action("win.switch", ["<Control>Return"])
        app.set_accels_for_action("win.save", ["<Control>s"])
        app.set_accels_for_action("win.undo", ["<Control>z"])
        app.set_accels_for_action("win.redo", ["<Control><Shift>z", "<Control>y"])

    # ================================================================ data

    def profile(self) -> Profile | None:
        """The selected profile as shown: with its unsaved edits."""
        if self.draft is not None and self.draft.name == self.current:
            return self.draft.profile
        return self.stored_profile()

    def stored_profile(self) -> Profile | None:
        """The selected profile as profiles.json held it when last read."""
        return next((p for p in self.profiles if p.name == self.current), None)

    def dirty(self) -> bool:
        return self.draft is not None and self.draft.dirty

    def _sync_draft(self) -> None:
        """Make the draft follow the selected profile. Unsaved edits are kept across reloads (a
        screen plugged in, a shortcut set); Save then finds out if profiles.json changed meanwhile."""
        stored = self.stored_profile()
        if stored is None:
            if self.draft is not None and self.draft.dirty and self.draft.name == self.current:
                return  # deleted outside while edited: kept until the person decides (_ask_orphan)
            self.draft = None
            return
        accel = self.keys.get(stored.name)
        d = self.draft
        if d is not None and d.name == stored.name:
            if d.dirty:
                return
            if d.saved.to_json() == stored.to_json() and (d.saved_accel or None) == (accel or None):
                return  # nothing changed: keep its undo and redo
        self.draft = Draft(stored, accel)

    def reload(self, select: str | None = None) -> None:
        """Read profiles, history, connected screens and audio devices, and redraw everything."""
        self._generation += 1
        self._show_state(self._read_state(), select)

    def _read_state(self) -> dict:
        """Everything reload() reads, without touching any widget (so it can run in a thread).
        "generation" says which edits and saves the read may already miss (_monitors_read).
        "profiles" is None when profiles.json cannot be read."""
        generation = self._generation
        problems = []
        try:
            display_state = self.ceb.display.get_state()
        except DisplayError as e:
            display_state = None
            problems.append(f"Could not read the screens: {e}")
        try:
            audio_state = self.ceb.audio.snapshot()
        except AudioError as e:
            audio_state = None
            problems.append(f"Could not read the audio devices: {e}")
        # Grows the history of every screen and audio device seen (docs/behavior.md). A history
        # that cannot be read or written is reported, and the window keeps working without it.
        history_error = self.ceb.observe_best_effort(display_state, audio_state)
        if history_error:
            problems.append(history_error)
        try:
            known = self.ceb.known()
        except StoreError as e:
            known = Known()
            if not history_error:
                problems.append(str(e))
        try:
            profiles = self.ceb.profiles()
        except StoreError as e:
            problems.append(str(e))
            profiles = None
        return {"display": display_state, "audio": audio_state, "known": known, "profiles": profiles,
                "problems": problems, "generation": generation}

    def _show_state(self, state: dict, select: str | None) -> None:
        self.display_state, self.audio_state = state["display"], state["audio"]
        self.known = state["known"]
        if state["profiles"] is not None:
            self.profiles = state["profiles"]
        # else: profiles.json cannot be read now (reported in the banner). Keep showing the
        # profiles as last read, and any unsaved edits; Save reports the problem until it is fixed.
        problems = list(state["problems"])
        try:
            self.keys = self.shortcuts.all()
        except (ShortcutError, GLib.Error) as e:
            self.keys = {}
            problems.append(f"Could not read the keyboard shortcuts: {e}")
        if not self.shortcuts.available:
            problems.append("GNOME's shortcut settings are not installed; keyboard shortcuts are off")
        elif not self.shortcuts.command:
            problems.append(NOT_FOUND)
        # A hand-edited profiles.json can hold two profiles with one name; CEB uses the first.
        seen, unique, twice = set(), [], []
        for p in self.profiles:
            if p.name in seen:
                if p.name not in twice:
                    twice.append(p.name)
                continue
            seen.add(p.name)
            unique.append(p)
        if twice:
            self.profiles = unique
            problems.append(f"{self.ceb.store.profiles_path} has more than one profile named "
                            + ", ".join(f"“{n}”" for n in twice) + "; CEB shows and uses the first of each")
        self._show_problems(problems)
        names = [p.name for p in self.profiles]
        d = self.draft
        orphan = d if d is not None and d.dirty and d.name not in names and d.name == self.current else None
        if orphan is not None and select in (None, orphan.name):
            self.current = orphan.name   # deleted outside while edited: keep showing the edits
        elif select in names:
            self.current = select
        elif self.current not in names:
            self.current = names[0] if names else None
        self._sync_draft()
        self._fill_sidebar()
        self._select_first_screen()
        self.refresh()
        if orphan is not None and self.draft is orphan and self._orphan_asked != orphan.name:
            self._orphan_asked = orphan.name
            orphan.saved_accel = None  # its shortcut went with it; Save sets the one shown again
            self._ask_orphan(orphan)

    def _ask_orphan(self, d: Draft) -> None:
        """The profile shown was deleted outside the window while it had unsaved edits."""
        dialog = Adw.AlertDialog(heading=f"“{d.name}” Was Deleted Outside CEB",
                                 body="The profile was deleted (for example with `ceb delete`) while it had "
                                      "changes that are not saved. Save them as a new profile with this name, "
                                      "or discard them.")
        dialog.add_response("cancel", "_Keep Editing")
        dialog.add_response("discard", "_Discard")
        dialog.add_response("save", "_Save as New Profile")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def done(_d, response):
            if self.draft is not d:
                return
            if response == "save":
                self.save()
            elif response == "discard":
                self.discard()
        dialog.connect("response", done)
        dialog.present(self)

    def _show_problems(self, problems: list[str]) -> None:
        if self._settings_problem:
            problems = problems + [self._settings_problem]
        if self.demo:
            problems = ["Demo mode: example screens and audio devices; Switch and Test change nothing"] + problems
        elif self.dry_run:
            problems = ["Dry run (--dry-run): Switch and Test only ask GNOME to check the layout; "
                        "nothing changes"] + problems
        self.banner.set_title(" · ".join(problems))
        self.banner.set_revealed(bool(problems))

    def _select_first_screen(self) -> None:
        p = self.profile()
        ids = editor.screen_ids(p) if p else []
        if self.selected not in ids:
            self.selected = (editor.primary_screen(p) if p else None) or (ids[0] if ids else None)

    def edit(self, change, *, from_widget_signal: bool = False) -> bool:
        """Change the selected profile with `change(profile)`. The change stays an unsaved edit
        (Undo, Redo, Discard, Save); nothing is written and the live screens and audio are never
        touched. Returns True when there is a profile to edit (the caller redraws).
        `from_widget_signal`: called from a widget's own signal (a combo row's notify::selected);
        only the bar is redrawn, which leaves that widget alone."""
        d = self.draft
        if d is None or d.name != self.current:
            return False
        d.edit(change)
        self._generation += 1
        self._refresh_bar()
        return True

    @staticmethod
    def _after(defer: bool, fn) -> None:
        if defer:
            GLib.idle_add(lambda: (fn(), False)[1])
        else:
            fn()

    def _shown_is_stored(self, name: str) -> bool:
        """True when profiles.json holds the profile as this window shows it."""
        shown = next((p for p in self.profiles if p.name == name), None)
        try:
            stored = self.ceb.profile(name)
        except (CebError, StoreError):
            return False
        return shown is not None and shown.to_json() == stored.to_json()

    # ================================================================ drawing

    def _fill_sidebar(self) -> None:
        self._updating = True
        try:
            while (row := self.sidebar_list.get_row_at_index(0)) is not None:
                self.sidebar_list.remove(row)
            for p in self.profiles:
                row = Gtk.ListBoxRow()
                row.profile_name = p.name
                box = Gtk.Box(spacing=10, margin_top=4, margin_bottom=4)
                name = Gtk.Label(label=p.name, xalign=0, hexpand=True, ellipsize=3)
                key = Gtk.Label(label=accel_label(self.keys.get(p.name)))
                key.add_css_class("dim-label")
                key.add_css_class("ceb-sidebar-key")
                box.append(name)
                box.append(key)
                row.set_child(box)
                self.sidebar_list.append(row)
                if p.name == self.current:
                    self.sidebar_list.select_row(row)
        finally:
            self._updating = False

    def refresh(self) -> None:
        """Redraw the right side for the selected profile."""
        p = self.profile()
        self.content_stack.set_visible_child_name("profile" if p else "empty")
        self._refresh_bar()
        if p is None:
            self.title.set_subtitle("")
            self.narrow_title.set_label("")
            return
        self.title.set_subtitle(p.name)
        self.narrow_title.set_label(p.name)
        self._updating = True
        try:
            self._refresh_canvas(p)
            self._refresh_tray(p)
            self._refresh_selected(p)
            self._refresh_audio(p)
            self._refresh_shortcut(p)
        finally:
            self._updating = False

    def _refresh_bar(self) -> None:
        """The action bar and the actions it holds."""
        p = self.profile()
        d = self.draft if self.draft is not None and self.draft.name == self.current else None
        dirty = d is not None and d.dirty
        testing = self._testing is not None or self._busy_kind == "back"
        free = p is not None and not self._applying and not testing
        enabled = {
            # "+" saves the screens as they are now: not while a test (or a switch) has them changed.
            "new-profile": not testing and not self._applying,
            "rename-profile": p is not None, "delete-profile": p is not None,
            "save": dirty, "discard": dirty,
            "undo": d is not None and d.can_undo, "redo": d is not None and d.can_redo,
            "switch": free, "test": free,
            "test-back": self._testing is not None and not self._applying,
            "test-keep": self._testing is not None and not self._applying,
        }
        for name, on in enabled.items():
            self.lookup_action(name).set_enabled(on)
        self.status_label.set_label("● Unsaved changes" if dirty else "✓ Saved")
        for cls, on in (("warning", dirty), ("dim-label", not dirty)):
            (self.status_label.add_css_class if on else self.status_label.remove_css_class)(cls)
        switching = self._busy_kind == "switch"
        self.switch_spinner.set_visible(switching)
        self.switch_label.set_label("Switching…" if switching else "Switch to profile")
        self.test_button.set_label("Testing…" if self._busy_kind == "test" else "Test")
        self.bar_stack.set_visible_child_name("testing" if testing else "normal")
        failed = self._testing is not None and self._testing.get("failed", False)
        self.back_button.set_label("Retry" if failed else "Go back now")
        self.back_button.set_tooltip_text("Try again to go back to your screens and audio from before the test"
                                          if failed else "")
        self.keep_button.set_tooltip_text("Stay as it is now" if failed else "Stay on this profile")
        if testing:
            self.bar_stack.add_css_class("ceb-testing")
            self._show_countdown()
        else:
            self.bar_stack.remove_css_class("ceb-testing")

    def _refresh_canvas(self, p: Profile) -> None:
        items = []
        primary = editor.primary_screen(p)
        if p.displays:
            for lm in p.displays.logical_monitors:
                sid = lm.screens[0].screen
                lab = editor.label(sid, self.known, self.display_state)
                mode = lm.screens[0].mode
                if lab.connected:
                    sub = f"{mode.width}×{mode.height} · {mode.refresh:.0f} Hz"
                    if lm.scale != 1:
                        sub += f" · {lm.scale * 100:.0f} %"
                else:
                    sub = "not connected"
                title = lab.title
                if len(lm.screens) > 1:
                    sub += f" · mirrored on {len(lm.screens)} screens"
                    title += f" + {len(lm.screens) - 1}"
                items.append(Item(sid, editor.lm_rect(lm, p.displays.layout_mode), title, sub,
                                  sid == primary, lab.connected, tuple(ps.screen for ps in lm.screens)))
        valid = is_valid([i.rect for i in items])
        self.canvas.warning = "" if valid else "These screens do not all touch; GNOME will refuse this layout"
        self.canvas.set_items(items, self.selected)

    def _refresh_tray(self, p: Profile) -> None:
        while (child := self.tray.get_first_child()) is not None:
            self.tray.remove(child)
        in_profile = set(editor.screen_ids(p))
        connected = set(self.display_state.by_screen()) if self.display_state else set()
        others = [ks.screen for ks in self.known.screens if ks.screen not in in_profile]
        others += [sid for sid in connected if sid not in in_profile and sid not in others]
        others.sort(key=lambda sid: sid not in connected)  # connected first, history order kept
        if not others:
            lab = Gtk.Label(label="Every known screen is in this profile")
            lab.add_css_class("dim-label")
            self.tray.append(lab)
        for sid in others:
            lab = editor.label(sid, self.known, self.display_state)
            chip = Gtk.Box(spacing=10)
            chip.add_css_class("card")
            chip.add_css_class("ceb-chip")
            dot = Gtk.Box(valign=Gtk.Align.CENTER)
            dot.add_css_class("ceb-dot")
            dot.add_css_class("connected" if lab.connected else "remembered")
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            text.append(Gtk.Label(label=lab.title, xalign=0))
            words = "connected" if lab.connected else "remembered · not connected"
            if "twin" in lab.detail:  # two chips with the same name: say which twin each is
                words += " · " + next(part for part in lab.detail.split(" · ") if "twin" in part)
            state = Gtk.Label(label=words, xalign=0)
            state.add_css_class("dim-label")
            state.add_css_class("caption")
            text.append(state)
            add = Gtk.Button(label="Add", valign=Gtk.Align.CENTER,
                             tooltip_text=f"Turn {lab.title} on in this profile")
            add.connect("clicked", self._on_add_screen, sid)
            chip.append(dot)
            chip.append(text)
            chip.append(add)
            chip.set_tooltip_text(lab.detail)
            self.tray.append(chip)

    def _refresh_selected(self, p: Profile) -> None:
        sid = self.selected
        found = editor.find(p, sid) if sid else None
        self.selected_group.set_visible(found is not None)
        for row in self.mirror_rows:
            self.selected_list.remove(row)
        self.mirror_rows = []
        if found is None:
            return
        # A mirror group (one logical monitor, several screens): one row per other screen, so each
        # can be selected and turned off on its own.
        for ps in found[0].screens:
            if ps.screen == sid:
                continue
            other = editor.label(ps.screen, self.known, self.display_state)
            row = Adw.ActionRow(title=GLib.markup_escape_text(other.title),
                                subtitle="Mirrors the selected screen"
                                         + ("" if other.connected else " · not connected"))
            pick = Gtk.Button(label="Select", valign=Gtk.Align.CENTER,
                              tooltip_text=f"Select {other.title} to turn it off or make it primary")
            pick.connect("clicked", self._on_select_member, ps.screen)
            row.add_suffix(pick)
            self.selected_list.append(row)
            self.mirror_rows.append(row)
        lab = editor.label(sid, self.known, self.display_state)
        detail = lab.detail if lab.connected else f"{lab.detail} · not connected"
        self.selected_title.set_label(lab.title)
        self.selected_detail.set_label(detail)
        is_primary = found[0].primary
        self.primary_button.set_label("★ Primary" if is_primary else "Make primary")
        self.primary_button.set_sensitive(not is_primary)

    def _audio_items(self, kind: str, p: Profile) -> list[AudioRef | None]:
        known = self.known.outputs if kind == "output" else self.known.inputs
        refs: list[AudioRef | None] = [None]
        refs += [AudioRef(k.name, k.description) for k in known]
        current = p.audio_output if kind == "output" else p.audio_input
        if current is not None and all(r is None or r.name != current.name for r in refs):
            refs.append(current)
        return refs

    def _present(self, kind: str) -> set[str]:
        if self.audio_state is None:
            return set()
        devs = self.audio_state.outputs if kind == "output" else self.audio_state.sources()
        return {d.name for d in devs}

    def _refresh_audio(self, p: Profile) -> None:
        for kind, row in (("output", self.output_row), ("input", self.input_row)):
            refs = self._audio_items(kind, p)
            present = self._present(kind)
            names = ["Leave unchanged"]
            for r in refs[1:]:
                text = r.description or r.name
                if r.name not in present:
                    text += " (not connected)"
                names.append(text)
            row.set_model(Gtk.StringList.new(names))
            row.refs = refs
            current = p.audio_output if kind == "output" else p.audio_input
            index = 0 if current is None else next(i for i, r in enumerate(refs) if r and r.name == current.name)
            row.set_selected(index)

    def _refresh_shortcut(self, p: Profile) -> None:
        accel = self.draft.accel if self.draft is not None and self.draft.name == p.name else self.keys.get(p.name)
        self.key_label.set_accelerator(accel or "")
        self.key_label.set_visible(bool(accel))
        self.key_none.set_visible(not accel)
        self.change_button.set_sensitive(self.shortcuts.available)

    # ================================================================ handlers: profile list

    def _row_of(self, name: str | None):
        i = 0
        while (row := self.sidebar_list.get_row_at_index(i)) is not None:
            if row.profile_name == name:
                return row
            i += 1
        return None

    def _on_row_selected(self, listbox, row) -> None:
        """Keyboard focus moving onto a row selects it in GtkListBox; put the selection back on the
        profile that is shown. Only activation (_on_row_activated) changes the profile."""
        if self._updating:
            return
        shown = self._row_of(self.current)
        if row is not shown and shown is not None:
            self._updating = True
            try:
                listbox.select_row(shown)
            finally:
                self._updating = False

    def _on_row_activated(self, listbox, row) -> None:
        """Open the profile of the row. With unsaved edits in the one shown, ask first (Save,
        Discard, Cancel); the list keeps showing the current profile until that is answered."""
        name = row.profile_name
        self._select_row(self.current)
        if name == self.current:
            if self.split.get_collapsed():
                self.split.set_show_sidebar(False)
            return
        self.resolve_unsaved(lambda: self.open_profile(name))

    def open_profile(self, name: str) -> None:
        if name not in [p.name for p in self.profiles]:
            return
        self.current = name
        self.selected = None
        self._sync_draft()
        self._select_first_screen()
        self.refresh()
        self._select_row(name)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _select_row(self, name: str | None) -> None:
        row = self._row_of(name)
        if row is None:
            return
        self._updating = True
        try:
            self.sidebar_list.select_row(row)
        finally:
            self._updating = False

    # ================================================================ unsaved edits

    def resolve_unsaved(self, then) -> None:
        """Run `then()` now if the profile shown has no unsaved edits. Otherwise ask: Save (then
        `then()` if saving worked), Discard (then `then()`), or Cancel (nothing)."""
        d = self.draft
        if d is None or not d.dirty:
            then()
            return
        dialog = Adw.AlertDialog(heading=f"Save Changes to “{d.name}”?",
                                 body="This profile has changes that are not saved. Your screens and audio "
                                      "do not change either way.")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("discard", "_Discard")
        dialog.add_response("save", "_Save")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def done(_d, response):
            if response == "save":
                if self.save():
                    then()
            elif response == "discard":
                self.discard()
                then()
        dialog.connect("response", done)
        dialog.present(self)

    def save(self, *, force: bool = False) -> bool:
        """Save the profile shown (profiles.json and its shortcut). Returns True when everything
        was saved. The live screens and audio are not touched."""
        d = self.draft
        if d is None:
            return False
        # Deleted outside while edited: saved as a new profile (never over one made meanwhile).
        create = d.name not in [p.name for p in self.profiles]
        try:
            problem = d.save(self.ceb, self._write_shortcut, force=force, create=create)
        except ProfileChanged:
            self._ask_overwrite(d)
            return False
        except (StoreError, CebError) as e:
            self.error("Could Not Save the Profile", str(e))
            return False
        self._orphan_asked = None
        self.reload(select=self.current)
        if problem:
            self.error("Shortcut Not Saved", f"“{d.name}” was saved, but not its keyboard shortcut: {problem}")
            return False
        self.toast(f"Saved “{d.name}”; your screens and audio did not change")
        return True

    def _ask_overwrite(self, d: Draft) -> None:
        dialog = Adw.AlertDialog(heading=f"“{d.name}” Was Changed Outside CEB",
                                 body="The saved profile changed after you started editing it (for example "
                                      "with `ceb save-current --force`). Replace it with yours, or discard your "
                                      "changes and show the newer one.")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("discard", "_Discard Mine")
        dialog.add_response("replace", "_Replace It")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def done(_d, response):
            if response == "replace" and self.draft is d:
                self.save(force=True)
            elif response == "discard" and self.draft is d:
                self.draft = None
                self.reload(select=self.current)
                self.toast(f"Showing the newer “{d.name}”")
        dialog.connect("response", done)
        dialog.present(self)

    def discard(self) -> None:
        """Drop the unsaved edits and show the profile as profiles.json holds it now (which may be
        newer than the version the edits started from)."""
        if self.draft is not None and self.draft.dirty:
            # A new draft is made from what reload() reads; if profiles.json cannot be read, from
            # the version last read.
            self.draft = None
            self.reload(select=self.current)
            self.toast("Changes discarded")

    def _write_shortcut(self, name: str, accel: str | None) -> str | None:
        if accel:
            clash = self.shortcuts.clash(accel, name, same_accel)
            if clash:
                return f"{accel_label(accel)} is already used by {clash}"
        try:
            self.shortcuts.set(name, accel)
        except (ShortcutError, GLib.Error) as e:
            return str(e)
        return None

    def _on_save(self) -> None:
        self.save()

    def _on_discard(self) -> None:
        self.discard()

    def _on_undo(self) -> None:
        if self.draft is not None and self.draft.undo():
            self._after_history_step()

    def _on_redo(self) -> None:
        if self.draft is not None and self.draft.redo():
            self._after_history_step()

    def _after_history_step(self) -> None:
        self._generation += 1
        self._select_first_screen()
        self.refresh()

    # ================================================================ handlers: canvas and screens

    def _on_canvas_select(self, sid) -> None:
        self.selected = sid
        p = self.profile()
        if p:
            self._updating = True
            try:
                self._refresh_selected(p)
            finally:
                self._updating = False

    def _on_select_member(self, _button, sid) -> None:
        self.selected = sid
        self.canvas.selected = sid
        self.canvas.queue_draw()
        self._on_canvas_select(sid)

    def _on_canvas_moved(self, sid, x: int, y: int, as_dropped: bool) -> None:
        if self.edit(lambda p: editor.move(p, sid, x, y)):
            self.refresh()
            if not as_dropped:
                self.toast("Moved so the screen touches its neighbour, as GNOME requires")

    def _on_add_screen(self, _button, sid: ScreenId) -> None:
        p = self.profile()
        if p is None:
            return
        found = editor.mode_for(sid, self.display_state, self.known)
        if found is None:
            self.error("Cannot Add the Screen", "CEB does not know any mode of this screen.")
            return
        mode, scale = found
        layout_mode = (p.displays.layout_mode if p.displays else None) or (
            self.display_state.layout_mode if self.display_state else None)
        if self.edit(lambda p: editor.add(p, sid, mode, scale, layout_mode)):
            self.selected = sid
            self.refresh()

    def _on_make_primary(self, _button) -> None:
        sid = self.selected
        if sid and self.edit(lambda p: editor.set_primary(p, sid)):
            self.refresh()

    def _on_turn_off(self, _button) -> None:
        sid = self.selected
        if sid and self.edit(lambda p: editor.remove(p, sid)):
            self.selected = sid  # the mockup keeps it selected; it is now in "Other screens"
            self.refresh()

    # ================================================================ handlers: audio and shortcut

    def _on_audio_changed(self, row, _pspec, kind: str) -> None:
        if self._updating:
            return
        p = self.profile()
        refs = getattr(row, "refs", None)
        if p is None or not refs:
            return
        ref = refs[row.get_selected()] if row.get_selected() < len(refs) else None

        def change(p):
            if kind == "output":
                p.audio_output = ref
            else:
                p.audio_input = ref
        self.edit(change, from_widget_signal=True)

    def _on_change_shortcut(self, _button) -> None:
        p = self.profile()
        if p is not None:
            ShortcutDialog(self, p.name).present(self)

    def set_shortcut(self, name: str, accel: str | None) -> str | None:
        """Called by the shortcut dialog. Returns an error message, or None when done. The new
        shortcut is an unsaved edit like the others; Save writes it to GNOME."""
        if accel:
            clash = self.shortcuts.clash(accel, name, same_accel)
            if clash:
                return f"{accel_label(accel)} is already used by {clash}"
        d = self.draft
        if d is None or d.name != name:
            return f"“{name}” is not the profile shown"
        d.set_accel(accel)
        p = self.profile()
        if p is not None:
            self._refresh_shortcut(p)
        self._refresh_bar()
        return None

    # ================================================================ handlers: header and menu

    def _on_new_profile(self) -> None:
        self.resolve_unsaved(self._new_profile)  # the new profile is shown next

    def _new_profile(self) -> None:
        dialog = Adw.AlertDialog(heading="New Profile",
                                 body="Saves the screens as they are now, and the current audio output and "
                                      "input, as a new profile. You can change everything afterwards.")
        entry = Gtk.Entry(placeholder_text="Name", activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("create", "_Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")
        dialog.set_response_enabled("create", False)
        entry.connect("changed", lambda e: dialog.set_response_enabled("create", self._name_ok(e.get_text())))

        def done(d, response):
            if response != "create":
                return
            name = entry.get_text().strip()
            try:
                self.ceb.save_current(name)
                note = ""
            except CebError as e:
                if "--no-audio" not in str(e):  # only an audio read failure: save the screens alone
                    self.error("Could Not Create the Profile", str(e))
                    return
                try:
                    self.ceb.save_current(name, include_audio=False)
                    note = " (screens only: the audio devices could not be read)"
                except (CebError, StoreError, DisplayError) as e2:
                    self.error("Could Not Create the Profile", str(e2))
                    return
            except (StoreError, DisplayError) as e:
                self.error("Could Not Create the Profile", str(e))
                return
            self.reload(select=name)
            self.toast(f"Profile “{name}” created{note}")

        dialog.connect("response", done)
        dialog.present(self)

    def _name_ok(self, text: str, allow: str | None = None) -> bool:
        name = text.strip()
        return bool(name) and (name == allow or all(p.name != name for p in self.profiles))

    def _on_rename(self) -> None:
        self.resolve_unsaved(self._rename)  # renaming writes profiles.json: save or drop the edits first

    def _rename(self) -> None:
        p = self.profile()
        if p is None:
            return
        old = p.name
        dialog = Adw.AlertDialog(heading="Rename Profile")
        entry = Gtk.Entry(text=old, activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("rename", "_Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")
        entry.connect("changed", lambda e: dialog.set_response_enabled(
            "rename", self._name_ok(e.get_text()) and e.get_text().strip() != old))
        dialog.set_response_enabled("rename", False)

        def done(d, response):
            if response != "rename":
                return
            new = entry.get_text().strip()
            try:
                self.ceb.rename_profile(old, new)  # keeps its place in the list; one write
            except (CebError, StoreError) as e:
                self.error("Could Not Rename the Profile", str(e))
                return
            try:
                self.shortcuts.rename(old, new)
            except (ShortcutError, GLib.Error) as e:
                self.error("Shortcut Not Updated", f"The profile was renamed, but its shortcut still runs “{old}”: {e}")
            self.reload(select=new)

        dialog.connect("response", done)
        dialog.present(self)

    def _on_delete(self) -> None:
        p = self.profile()
        if p is None:
            return
        dialog = Adw.AlertDialog(heading=f"Delete “{p.name}”?",
                                 body="The profile and its keyboard shortcut are removed. Your screens and "
                                      "audio stay as they are.")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("delete", "_Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def done(d, response):
            if response != "delete":
                return
            try:
                problem = delete_profile(self.ceb, self.shortcuts, p.name)  # the same code as `ceb delete`
            except (CebError, StoreError) as e:
                self.error("Could Not Delete the Profile", str(e))
                return
            if problem:
                self.error("Shortcut Not Removed", f"The profile was deleted, but {problem}")
            self.current = None
            self.draft = None  # its unsaved edits go with it
            self.reload()
            self.toast(f"Profile “{p.name}” deleted")

        dialog.connect("response", done)
        dialog.present(self)

    def _on_about(self) -> None:
        about = Adw.AboutDialog(application_name="CEB", application_icon=APP_ID,
                                developer_name="Kevin Willis", version=_version(),
                                license_type=Gtk.License.MIT_X11,
                                comments="Combined Environment Bundles: display and audio profiles for GNOME. A profile sets which screens are on, where they are, the primary screen, and the default audio output and input; switch with one key.")
        about.present(self)

    # ================================================================ switch to profile

    def _on_switch(self, name: str | None = None, allow_missing: bool = False) -> None:
        """Switch the screens and audio to a saved profile: the one named (from "Apply the Rest",
        which names the profile its dialog was about), else the selected one. Unsaved edits are not
        used: this is what the profile's keyboard shortcut does too. Runs in a thread; the window
        stays responsive."""
        if self._applying or self._testing is not None:
            return
        unsaved = False
        if name is None:
            p = self.profile()
            if p is None:
                return
            name = p.name
            unsaved = self.dirty()
            if not self._shown_is_stored(name):
                # profiles.json changed outside this window: show what would be applied first.
                self.reload(select=self.current)
                self.toast(f"“{name}” was changed outside CEB's window; check it and switch again")
                return
        self._set_applying("switch")
        dry = self.dry_run

        def work():
            try:
                result = self.ceb.apply(name, dry_run=dry, allow_missing=allow_missing)
            except Exception as e:  # report anything; never leave the button disabled
                result = e
            GLib.idle_add(self._switch_done, name, allow_missing, unsaved, result)

        self._run(work)

    def _run(self, work) -> None:
        # Not a daemon thread: if the program ends while a switch is running, Python waits for it
        # instead of killing it half-way (screens changed, audio not yet).
        self._apply_thread = threading.Thread(target=work, name="ceb-apply")
        self._apply_thread.start()

    def _set_applying(self, kind: str | None) -> None:
        """kind: "switch", "test" or "back" while that runs in its thread; None when done."""
        self._applying = kind is not None
        self._busy_kind = kind
        self._refresh_bar()

    def _close_if_asked(self, ok: bool) -> bool:
        """After a switch, test or going back: close the window if the person closed it meanwhile and
        it went well. Returns True when the window closes."""
        if not self._close_after_apply:
            return False
        self._close_after_apply = False
        if ok or self._force_quit:
            self.close()  # done as asked; the screens show the outcome (told to quit: no choice left)
            return True
        return False  # something went wrong: stay open and show it; the person closes again

    def quit_now(self) -> None:
        """SIGTERM, SIGINT or SIGHUP: go back from a running test (and let a running switch end),
        then close without asking about unsaved edits."""
        self._force_quit = True
        self.close()

    def _switch_done(self, name: str, allow_missing: bool, unsaved: bool, result) -> bool:
        self._set_applying(None)
        if self._close_if_asked(not isinstance(result, Exception) and result.ok):
            return False
        if isinstance(result, Exception):
            self.error(f"“{name}” Not Applied", f"{type(result).__name__}: {result}")
            return False
        r: ApplyResult = result
        if r.ok:
            if r.dry_run:
                self.toast(f"GNOME accepts “{name}”; dry run, nothing changed")
            elif unsaved:
                self.toast(f"Switched to the saved “{name}”; your unsaved changes are not used")
            else:
                self.toast(f"Switched to “{name}”")
        else:
            self._report(name, r, allow_missing, lambda: self._on_switch(name, allow_missing=True), "Apply the _Rest")
        for note in r.notes:
            if note.startswith("history not updated"):
                self.toast(note)
        self.reload(select=self.current)
        return False

    def _report(self, name: str, r: ApplyResult, allow_missing: bool, rest, rest_label: str) -> None:
        """What went wrong in a switch or a test, as Apply reported it before (docs/behavior.md)."""
        changed = r.applied_display or r.applied_output or r.applied_input
        blocked = any(pr.blocking_always for pr in r.problems)
        if changed:
            # Something was switched: never offer "Apply the rest" now. This includes an audio
            # device (HDMI audio) that did not appear after the screens changed.
            skipped = allow_missing and not r.errors
            head = f"“{name}” Applied With Parts Skipped" if skipped else f"“{name}” Only Partly Applied"
            self.error(head, _bullets(r.summary_lines()))
        elif r.dry_run and allow_missing and r.goes_ahead and not r.errors:
            self.error(f"GNOME Accepts the Rest of “{name}”",
                       "Dry run: nothing was changed. Would be skipped:\n\n" + _bullets(r.summary_lines()))
        elif r.problems and not r.errors and not blocked and not allow_missing:
            self._ask_apply_rest(name, r, rest, rest_label)
        else:
            self.error(f"“{name}” Not Applied", "Nothing was changed.\n\n" + _bullets(r.summary_lines()))

    def _ask_apply_rest(self, name: str, r: ApplyResult, rest, rest_label: str = "Apply the _Rest") -> None:
        dialog = Adw.AlertDialog(heading=f"“{name}” Not Applied",
                                 body="Nothing was changed, because part of the profile is missing:\n\n"
                                      + _bullets(r.summary_lines()))
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("rest", rest_label)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda d, resp: resp == "rest" and rest())
        dialog.present(self)

    # ================================================================ test

    def _load_settings(self) -> tuple[Settings, str | None]:
        try:
            return self.ceb.store.load_settings(), None
        except StoreError as e:
            return Settings(), f"Test length not remembered: {e}"

    def commit_test_seconds(self) -> int:
        """Take the number in the test length box: kept in range, shown as taken, and remembered
        for every profile (settings.json). Anything but ASCII digits, or an empty box, goes back to
        the number in use."""
        text = self.test_seconds.get_text().strip()
        ok = bool(text) and all(c in _ASCII_DIGITS for c in text)
        seconds = clamp_test_seconds(int(text)) if ok else self.settings.test_seconds
        if self.test_seconds.get_text() != str(seconds):
            self.test_seconds.set_text(str(seconds))
        if seconds == self.settings.test_seconds:
            return seconds
        self.settings.test_seconds = seconds
        self._save_settings()
        return seconds

    def _save_settings(self) -> None:
        if self._settings_problem:
            return  # settings.json could not be read; never overwrite it
        try:
            self.ceb.store.save_settings(self.settings)
        except StoreError as e:
            self.toast(f"Test length not remembered: {e}")

    def _on_test(self, profile: Profile | None = None, allow_missing: bool = False) -> None:
        """Switch to the profile as shown now (unsaved edits included), temporarily: what was on
        before is read first, and CEB goes back to it after the test length unless Keep is pressed.
        If the audio devices cannot be read first, the test leaves audio alone (it could not set it
        back) and says so. `profile`: the one to test again from "Test the Rest"."""
        if self._applying or self._testing is not None:
            return
        tested = copy.deepcopy(profile or self.profile())
        if tested is None:
            return
        seconds = self.commit_test_seconds()  # a number typed but not yet confirmed counts
        self._set_applying("test")
        dry = self.dry_run

        def work():
            before, audio_note = None, None
            try:
                try:
                    before = self.ceb.live_profile()   # what to go back to; nothing is applied without it
                except AudioError as e:
                    before = self.ceb.live_profile(include_audio=False)
                    if tested.audio_output is not None or tested.audio_input is not None:
                        tested.audio_output = tested.audio_input = None
                        audio_note = f"Audio not tested: CEB could not read the audio devices ({e})"
                result = self.ceb.apply_profile(tested, dry_run=dry, allow_missing=allow_missing)
            except Exception as e:  # report anything; never leave the bar disabled
                result = e
            GLib.idle_add(self._test_done, tested, before, seconds, allow_missing, result, audio_note)

        self._run(work)

    def _test_done(self, tested: Profile, before: Profile | None, seconds: int, allow_missing: bool,
                   result, audio_note: str | None = None) -> bool:
        self._set_applying(None)
        name = tested.name
        if audio_note:
            self.toast(audio_note)
        if isinstance(result, Exception):
            if before is None:
                self._close_if_asked(False)
                self.error(f"“{name}” Not Tested", "Nothing was changed: CEB could not read the screens that are "
                           f"on now, so it could not go back to them afterwards.\n\n{result}")
                return False
            # The switch itself failed half-way or not at all: go back at once, to be sure.
            self.error(f"“{name}” Not Tested", f"{type(result).__name__}: {result}")
            self._testing = {"name": name, "before": before, "deadline": time.monotonic()}
            self._go_back()
            return False
        r: ApplyResult = result
        changed = r.applied_display or r.applied_output or r.applied_input
        if changed:
            self._testing = {"name": name, "before": before, "deadline": time.monotonic() + seconds}
            self._test_timer = GLib.timeout_add(200, self._test_tick)
            self._refresh_bar()
            if self._close_after_apply:  # closed (or told to quit) while the test was starting
                self._go_back()
            elif not r.ok:
                self._report(name, r, allow_missing, None, "")
        else:
            if self._close_if_asked(r.ok):
                return False
            if r.ok and r.dry_run:
                self.toast(f"GNOME accepts “{name}”; dry run, nothing changed")
            elif r.ok:
                self.toast(f"“{name}” is what is on already")
            else:
                self._report(name, r, allow_missing, lambda: self._on_test(tested, allow_missing=True),
                             "Test the _Rest")
        return False

    def test_remaining(self) -> int:
        if self._testing is None or self._testing.get("deadline") is None:
            return 0
        return max(0, math.ceil(self._testing["deadline"] - time.monotonic()))

    def _show_countdown(self) -> None:
        t = self._testing
        if t is not None and t.get("failed"):
            self.testing_label.set_label(f"Could not go back from testing “{t['name']}”. Retry, or keep the "
                                         "screens and audio as they are now.")
        elif t is not None:
            self.testing_label.set_label(f"Testing “{t['name']}” — going back in {self.test_remaining()} s")
        else:
            self.testing_label.set_label("Going back to your screens and audio…")

    def _test_tick(self) -> bool:
        """Runs in the main loop whatever the window shows or where it is: going back needs no
        click, so it works even when the tested layout left this window on a screen that is off."""
        if self._testing is None or self._testing.get("failed"):
            self._test_timer = None
            return False
        if self.test_remaining() <= 0:
            self._test_timer = None
            self._go_back()
            return False
        self._show_countdown()
        return True

    def _stop_test_timer(self) -> None:
        if self._test_timer is not None:
            GLib.source_remove(self._test_timer)
            self._test_timer = None

    def _on_test_keep(self) -> None:
        if self._testing is None or self._applying:
            return
        name = self._testing["name"]
        self._stop_test_timer()
        self._testing = None
        self._refresh_bar()
        self.toast(f"Kept “{name}”")

    def _on_test_back(self) -> None:
        """Go back now, or Retry after going back failed."""
        if self._testing is not None and not self._applying:
            self._go_back()

    def _go_back(self) -> None:
        """Apply what was on before the test. Screens or devices that went away meanwhile are
        skipped (allow_missing), so as much as possible comes back."""
        test = self._testing
        if test is None or self._applying:
            return
        self._stop_test_timer()
        self._testing = None
        self._set_applying("back")
        dry = self.dry_run

        def work():
            try:
                result = self.ceb.apply_profile(test["before"], dry_run=dry, allow_missing=True)
            except Exception as e:
                result = e
            GLib.idle_add(self._back_done, test, result)

        self._run(work)

    def _back_done(self, test: dict, result) -> bool:
        self._set_applying(None)
        name = test["name"]
        ok = not isinstance(result, Exception) and result.ok
        if self._close_if_asked(ok):
            return False
        if not ok:
            # Keep what to go back to: the bar offers Retry (and Keep, to stay as it is now).
            self._testing = {"name": name, "before": test["before"], "deadline": None, "failed": True}
            self._refresh_bar()
        if isinstance(result, Exception):
            self.error("Could Not Go Back", f"{type(result).__name__}: {result}")
        elif ok:
            self.toast("Back to your previous screens and audio")
        else:
            self.error("Could Not Go Back Completely",
                       f"After testing “{name}”, this was not restored:\n\n" + _bullets(result.summary_lines()))
        self.reload(select=self.current)
        return False

    # ================================================================ messages

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))

    def error(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", "_Close")
        dialog.present(self)

    # ================================================================ GNOME's MonitorsChanged

    def _watch_monitors(self) -> None:
        """Reload when GNOME's screens change (MonitorsChanged) or an audio device appears or goes
        away (the audio server's events, in a background thread); both take the same deferred path,
        so a reload never interrupts a drag or closes an open popover."""
        if self.demo:
            return
        watch = getattr(self.ceb.audio, "watch", None)
        if watch is not None:
            self._audio_events = watch(lambda: GLib.idle_add(self._monitors_changed))
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return
        self._signal_id = self._bus.signal_subscribe(
            BUS_NAME, INTERFACE, "MonitorsChanged", OBJECT_PATH, None, Gio.DBusSignalFlags.NONE,
            lambda *args: GLib.idle_add(self._monitors_changed))

    MONITORS_SETTLE_MS = 300   # GNOME often sends several MonitorsChanged in a row
    MONITORS_RETRY_MS = 500    # while a drag or a popover is open

    def _monitors_changed(self) -> bool:
        """GNOME's screens changed. Reload a moment later, when the person is not dragging a screen
        or using a popover (a reload would cancel the drag and close the popover), and read the
        screens and audio devices in a thread so the window never stalls."""
        if self._monitors_pending is not None:
            GLib.source_remove(self._monitors_pending)
        self._monitors_pending = GLib.timeout_add(self.MONITORS_SETTLE_MS, self._monitors_reload)
        return False

    def _busy(self) -> bool:
        """The person is in the middle of something a redraw would break."""
        if self.canvas.dragging:
            return True
        focus = self.get_focus()
        return focus is not None and focus.get_ancestor(Gtk.Popover) is not None

    def _monitors_reload(self) -> bool:
        if self._busy() or self._monitors_reading:
            self._monitors_pending = GLib.timeout_add(self.MONITORS_RETRY_MS, self._monitors_reload)
            return False
        self._monitors_pending = None
        self._monitors_reading = True

        def work():
            try:
                state = self._read_state()  # reads the screens and records them (history grows)
            except Exception as e:  # never lose the window over a failed background read
                state = e
            GLib.idle_add(self._monitors_read, state)

        threading.Thread(target=work, name="ceb-monitors", daemon=True).start()
        return False

    def _monitors_read(self, state) -> bool:
        self._monitors_reading = False
        if self._closed:
            return False
        if isinstance(state, Exception):
            self.toast(f"Could not read the screens: {state}")
        elif self._busy():
            # Started a drag or opened a popover while reading: show it when they are done.
            self._monitors_pending = GLib.timeout_add(self.MONITORS_RETRY_MS, self._monitors_reload)
        elif state.get("generation") != self._generation:
            # An edit, Save, Discard or reload happened while reading: this read may hold profiles
            # older than what was just saved. Read again instead of showing it.
            self._monitors_changed()
        else:
            self._show_state(state, self.current)
        return False

    def _on_close(self, *_args) -> bool:
        if not self._closed:
            self.commit_test_seconds()  # a test length typed but not confirmed is kept too
        if self._applying:
            # Closing now would end the program in the middle of a switch (screens changed, audio
            # not yet). Close when it is done; if it fails, stay open and show why. A test that is
            # starting goes back first (_test_done).
            self._close_after_apply = True
            self.toast("CEB closes when the switch is done")
            return True
        if self._testing is not None:
            # Never leave a test on screen with nobody to take it back: go back, then close.
            self._close_after_apply = True
            self._go_back()
            self.toast("CEB closes once your previous screens and audio are back")
            return True
        if self.dirty() and not self._force_quit:
            self.resolve_unsaved(self.close)  # Save or Discard, then close; Cancel stays open
            return True
        self._closed = True
        self._stop_test_timer()
        if self._audio_events is not None:
            self._audio_events.stop()
            self._audio_events = None
        if self._signal_id is not None:
            self._bus.signal_unsubscribe(self._signal_id)
            self._signal_id = None
        if self._monitors_pending is not None:
            GLib.source_remove(self._monitors_pending)
            self._monitors_pending = None
        return False

    # ================================================================ remembered devices

    def _on_remembered(self) -> None:
        RememberedDialog(self).present(self)

    # ================================================================ background watcher

    def _background_on(self) -> bool:
        return desktop.autostart_enabled(self.config_dir, self._autostart_base())

    def _autostart_base(self):
        from pathlib import Path
        return Path(self.autostart_base) if self.autostart_base else None

    def _on_remember_background(self, action, value) -> None:
        """Main menu > Remember Screens in the Background: an autostart entry that runs `ceb watch`
        at login, and the watcher itself, started or stopped at once (not in the demo)."""
        on = value.get_boolean()
        try:
            if on:
                desktop.enable_autostart(self.config_dir, self._autostart_base())
                if not self.demo:
                    desktop.start_watcher(self.config_dir, self.ceb.store.dir)
            else:
                desktop.disable_autostart(self._autostart_base())
                if not self.demo:
                    desktop.stop_watcher(self.ceb.store.dir)
        except (OSError, RuntimeError) as e:
            self.error("Background Memory Not Changed", str(e))
            action.set_state(GLib.Variant.new_boolean(self._background_on()))
            return
        action.set_state(value)
        self.toast("CEB now remembers screens and audio devices in the background, also after you log in"
                   if on else "CEB remembers screens and audio devices only while it runs")

    def forget_screen(self, sid: ScreenId) -> None:
        try:
            self.ceb.forget_screen(sid)
        except (StoreError, CebError) as e:
            self.error("Could Not Forget the Screen", str(e))
        self.reload(select=self.current)

    def forget_audio(self, kind: str, name: str) -> None:
        try:
            self.ceb.forget_audio(kind, name)
        except (StoreError, CebError) as e:
            self.error("Could Not Forget the Device", str(e))
        self.reload(select=self.current)


class ShortcutDialog(Adw.Dialog):
    """Press the new keys (as in GNOME Settings): Esc cancels, Backspace removes the shortcut."""

    def __init__(self, window: CebWindow, profile: str):
        super().__init__(title="Set Shortcut", content_width=440)
        self.window, self.profile = window, profile
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=24,
                      margin_start=24, margin_end=24)
        title = Gtk.Label(label=f"Press the keys that switch to “{profile}”", wrap=True)
        title.add_css_class("title-4")
        hint = Gtk.Label(label="Esc to cancel, Backspace to remove the shortcut", wrap=True)
        hint.add_css_class("dim-label")
        self.shown = Adw.ShortcutLabel(accelerator=window.keys.get(profile) or "", halign=Gtk.Align.CENTER)
        self.message = Gtk.Label(wrap=True, visible=False)
        self.message.add_css_class("error")
        for w in (title, self.shown, hint, self.message):
            box.append(w)
        view.set_content(box)
        self.set_child(view)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        # While the dialog waits for keys, GNOME's own shortcuts (Super, Alt+Tab, ...) go to this
        # window instead of the desktop, as in GNOME Settings; released when the dialog closes.
        self._inhibited = None
        self.connect("map", lambda *_: self._inhibit(True))
        self.connect("closed", lambda *_: self._inhibit(False))

    def _toplevel(self):
        root = self.get_root()
        surface = root.get_surface() if root is not None else None
        return surface if isinstance(surface, Gdk.Toplevel) else None

    def _inhibit(self, on: bool) -> None:
        if on and self._inhibited is None:
            top = self._toplevel()
            if top is not None:
                top.inhibit_system_shortcuts(None)
                self._inhibited = top
        elif not on and self._inhibited is not None:
            self._inhibited.restore_system_shortcuts()
            self._inhibited = None

    def _key(self, _controller, keyval, keycode, state) -> bool:
        mods = state & Gtk.accelerator_get_default_mod_mask()
        keyval = Gdk.keyval_to_lower(keyval)
        if keyval in _MODIFIER_KEYS:
            return True
        if not mods and keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if not mods and keyval == Gdk.KEY_BackSpace:
            self._done(None)
            return True
        # Shift alone is not enough: Shift+A is just a capital A everywhere else.
        if not mods & _STRONG_MODS and not (Gdk.KEY_F1 <= keyval <= Gdk.KEY_F35):
            self._fail("Use at least one of Super, Ctrl or Alt (or a function key on its own)")
            return True
        accel = Gtk.accelerator_name(keyval, mods)
        self.shown.set_accelerator(accel)
        self._done(accel)
        return True

    def _done(self, accel) -> None:
        error = self.window.set_shortcut(self.profile, accel)
        if error:
            self._fail(error + ". Press other keys.")
        else:
            self.close()

    def _fail(self, text: str) -> None:
        self.message.set_label(text)
        self.message.set_visible(True)


_MODIFIER_KEYS = {Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Alt_L,
                  Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
                  Gdk.KEY_ISO_Level3_Shift, Gdk.KEY_Caps_Lock, Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R}


_STRONG_MODS = (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
                | Gdk.ModifierType.META_MASK | Gdk.ModifierType.HYPER_MASK)


class RememberedDialog(Adw.Dialog):
    """Screens and audio devices CEB remembers that are not connected now, each with Forget."""

    def __init__(self, window: CebWindow):
        super().__init__(title="Remembered Devices", content_width=560, content_height=560)
        self.window = window
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage(description="CEB remembers every screen and audio device it has seen, so "
                                                    "profiles can use them while they are unplugged. Forget the ones "
                                                    "you no longer have. Connected ones are not listed: CEB would "
                                                    "remember them again at once.")
        view.set_content(self.page)
        self.set_child(view)
        self.groups = []
        self.fill()

    def fill(self) -> None:
        for g in self.groups:
            self.page.remove(g)
        self.groups = []
        w = self.window
        connected = set(w.display_state.by_screen()) if w.display_state else set()
        screens = Adw.PreferencesGroup(title="Screens")
        n = 0
        for ks in w.known.screens:
            if ks.screen in connected:
                continue
            lab = editor.label(ks.screen, w.known, w.display_state)
            used = [p.name for p in w.profiles if ks.screen in editor.screen_ids(p)]
            sub = " · ".join(x for x in (lab.detail, f"last seen {ks.last_seen[:10]}" if ks.last_seen else "",
                                         "in " + ", ".join(used) if used else "") if x)
            screens.add(self._row(lab.title, sub, lambda sid=ks.screen: w.forget_screen(sid)))
            n += 1
        self._add(screens, n, "No remembered screens that are not connected")
        for kind, title in (("output", "Audio outputs"), ("input", "Audio inputs")):
            group = Adw.PreferencesGroup(title=title)
            present = w._present(kind)
            n = 0
            for dev in (w.known.outputs if kind == "output" else w.known.inputs):
                if dev.name in present:
                    continue
                used = [p.name for p in w.profiles
                        if (p.audio_output if kind == "output" else p.audio_input) is not None
                        and (p.audio_output if kind == "output" else p.audio_input).name == dev.name]
                sub = " · ".join(x for x in (f"last seen {dev.last_seen[:10]}" if dev.last_seen else "",
                                             "in " + ", ".join(used) if used else "") if x)
                group.add(self._row(dev.description or dev.name, sub,
                                    lambda k=kind, name=dev.name: w.forget_audio(k, name)))
                n += 1
            self._add(group, n, "No remembered devices that are not connected")

    def _add(self, group, rows: int, empty: str) -> None:
        if not rows:
            row = Adw.ActionRow(title=empty)
            row.add_css_class("dim-label")
            group.add(row)
        self.page.add(group)
        self.groups.append(group)

    def _row(self, title: str, subtitle: str, forget) -> Adw.ActionRow:
        row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle))
        button = Gtk.Button(label="Forget", valign=Gtk.Align.CENTER, tooltip_text=f"Forget {title}")
        button.connect("clicked", lambda *_: (forget(), self.fill()))
        row.add_suffix(button)
        return row


def _checked_list_factory(row: Adw.ComboRow) -> Gtk.SignalListItemFactory:
    """The popup list of an audio picker: the full device name, and a check mark on the item that is
    selected (as AdwComboRow's own list does; a custom factory has to draw it itself)."""
    factory = Gtk.SignalListItemFactory()

    def setup(_f, item):
        box = Gtk.Box(spacing=6)
        label = Gtk.Label(xalign=0, ellipsize=3, max_width_chars=60, hexpand=True, valign=Gtk.Align.CENTER)
        check = Gtk.Image(icon_name="object-select-symbolic", accessible_role=Gtk.AccessibleRole.PRESENTATION)
        box.append(label)
        box.append(check)
        item.set_child(box)

    def update(item):
        box = item.get_child()
        box.get_last_child().set_opacity(1 if item.get_position() == row.get_selected() else 0)

    def bind(_f, item):
        item.get_child().get_first_child().set_label(item.get_item().get_string())
        update(item)
        item.ceb_handler = row.connect("notify::selected", lambda *_: update(item))

    def unbind(_f, item):
        handler = getattr(item, "ceb_handler", None)
        if handler is not None:
            row.disconnect(handler)
            item.ceb_handler = None

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    factory.connect("unbind", unbind)
    return factory


def _wide_label_factory() -> Gtk.SignalListItemFactory:
    """Like AdwComboRow's default item, but wide enough for full device names (the default stops at
    20 characters, which cuts 'Audio Adapter (Unitek Y-247A) Analog Stereo' short)."""
    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda _f, item: item.set_child(
        Gtk.Label(xalign=0, ellipsize=3, max_width_chars=60, valign=Gtk.Align.CENTER)))
    factory.connect("bind", lambda _f, item: item.get_child().set_label(item.get_item().get_string()))
    return factory


_ASCII_DIGITS = "0123456789"  # str.isdigit() also takes "²" or "٣", which int() refuses or misreads


def _digits_only(entry: Gtk.Entry) -> None:
    """The test length box keeps the digits 0-9 only (typed or pasted)."""
    text = entry.get_text()
    digits = "".join(c for c in text if c in _ASCII_DIGITS)
    if digits != text:
        entry.set_text(digits)  # emits "changed" again, with nothing left to remove
        entry.set_position(-1)


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"• {line}" for line in lines)


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("ceb-profiles")
    except Exception:
        return ""
