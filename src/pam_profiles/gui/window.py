"""PAM's main window, built from the picked mockup (option A, "Settings-style sidebar").

Header bar: + (new profile), the title, a menu (rename, delete, about) and Apply.
Left: the profiles, each with its keyboard shortcut. Right, for the selected profile: the
arrangement canvas, "Other screens" (screens not in this profile, connected or remembered, each with
Add), "Selected screen" (Make primary, Turn off in profile), "Audio for this profile" (Output, Input,
Keyboard shortcut).

Every edit is saved at once (profiles.json); Apply switches the screens and audio to the profile.
"""
from __future__ import annotations

import copy
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from ..audio import AudioError  # noqa: E402
from ..core import ApplyResult, Pam, PamError, ProfileChanged  # noqa: E402
from ..model import AudioRef, Profile, ScreenId  # noqa: E402
from ..mutter import BUS_NAME, INTERFACE, OBJECT_PATH, DisplayError  # noqa: E402
from ..store import Known, StoreError  # noqa: E402
from . import editor  # noqa: E402
from .canvas import ArrangementCanvas, Item  # noqa: E402
from .geometry import is_valid  # noqa: E402
from .shortcuts import Shortcuts, ShortcutError  # noqa: E402

CSS = """
.pam-sidebar-key { font-family: monospace; font-size: 0.85em; }
.pam-chip { padding: 8px 12px; border-radius: 10px; }
.pam-dot { min-width: 9px; min-height: 9px; border-radius: 99px; }
.pam-dot.connected { background-color: #2ec27e; }  /* the mockup's green (GNOME palette Green 4) */
.pam-dot.remembered { background-color: #777777; }
.pam-canvas-frame { border-radius: 12px; }
"""


def accel_label(accel: str | None) -> str:
    if not accel:
        return ""
    ok, key, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_get_label(key, mods) if ok else accel


def same_accel(a: str, b: str) -> bool:
    pa, pb = Gtk.accelerator_parse(a), Gtk.accelerator_parse(b)
    return pa[0] and pb[0] and Gdk.keyval_to_lower(pa[1]) == Gdk.keyval_to_lower(pb[1]) and pa[2] == pb[2]


class PamWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, pam: Pam, shortcuts: Shortcuts, *, dry_run: bool = False,
                 demo: bool = False):
        super().__init__(application=app, title="PAM", default_width=1100, default_height=760)
        self.pam, self.shortcuts, self.dry_run, self.demo = pam, shortcuts, dry_run, demo
        self.profiles: list[Profile] = []
        self.known = Known()
        self.display_state = None
        self.audio_state = None
        self.keys: dict[str, str] = {}
        self.current: str | None = None      # name of the selected profile
        self.selected: ScreenId | None = None
        self._updating = False
        self._applying = False
        self._close_after_apply = False
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
        self.title = Adw.WindowTitle(title="PAM", subtitle="")
        header.set_title_widget(self.title)

        self.sidebar_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Show Profiles",
                                               visible=False)
        self.sidebar_button.bind_property("active", self.split, "show-sidebar",
                                          GObject.BindingFlags.BIDIRECTIONAL)
        header.pack_start(self.sidebar_button)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New Profile From Current Screens")
        add.set_action_name("win.new-profile")
        header.pack_start(add)

        self.apply_button = Gtk.Button(tooltip_text="Switch the screens and audio to this profile")
        apply_box = Gtk.Box(spacing=6)
        self.apply_spinner = Adw.Spinner(visible=False)
        self.apply_label = Gtk.Label(label="Apply")
        apply_box.append(self.apply_spinner)
        apply_box.append(self.apply_label)
        self.apply_button.set_child(apply_box)
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.set_action_name("win.apply")
        header.pack_end(self.apply_button)
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("_Rename Profile…", "win.rename-profile")
        section.append("_Delete Profile…", "win.delete-profile")
        menu.append_section(None, section)
        history = Gio.Menu()
        history.append("_Remembered Devices…", "win.remembered")
        menu.append_section(None, history)
        about = Gio.Menu()
        about.append("_About PAM", "win.about")
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
        self.content_stack = Gtk.Stack()
        self.content_stack.add_named(scroll, "profile")
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
        self.add_breakpoint(bp)

    def _install_actions(self) -> None:
        for name, cb in (("new-profile", self._on_new_profile), ("apply", self._on_apply),
                         ("rename-profile", self._on_rename), ("delete-profile", self._on_delete),
                         ("remembered", self._on_remembered), ("about", self._on_about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(action)
        app = self.get_application()
        app.set_accels_for_action("win.new-profile", ["<Control>n"])
        app.set_accels_for_action("win.apply", ["<Control>Return"])

    # ================================================================ data

    def profile(self) -> Profile | None:
        return next((p for p in self.profiles if p.name == self.current), None)

    def reload(self, select: str | None = None) -> None:
        """Read profiles, history, connected screens and audio devices, and redraw everything."""
        self._show_state(self._read_state(), select)

    def _read_state(self) -> dict:
        """Everything reload() reads, without touching any widget (so it can run in a thread)."""
        problems = []
        try:
            display_state = self.pam.display.get_state()
        except DisplayError as e:
            display_state = None
            problems.append(f"Could not read the screens: {e}")
        try:
            audio_state = self.pam.audio.snapshot()
        except AudioError as e:
            audio_state = None
            problems.append(f"Could not read the audio devices: {e}")
        # Grows the history of every screen and audio device seen (docs/behavior.md). A history
        # that cannot be read or written is reported, and the window keeps working without it.
        history_error = self.pam.observe_best_effort(display_state, audio_state)
        if history_error:
            problems.append(history_error)
        try:
            known = self.pam.known()
        except StoreError as e:
            known = Known()
            if not history_error:
                problems.append(str(e))
        try:
            profiles = self.pam.profiles()
        except StoreError as e:
            problems.append(str(e))
            profiles = []
        return {"display": display_state, "audio": audio_state, "known": known, "profiles": profiles,
                "problems": problems}

    def _show_state(self, state: dict, select: str | None) -> None:
        self.display_state, self.audio_state = state["display"], state["audio"]
        self.known, self.profiles = state["known"], state["profiles"]
        problems = list(state["problems"])
        try:
            self.keys = self.shortcuts.all()
        except (ShortcutError, GLib.Error) as e:
            self.keys = {}
            problems.append(f"Could not read the keyboard shortcuts: {e}")
        if not self.shortcuts.available:
            problems.append("GNOME's shortcut settings are not installed; keyboard shortcuts are off")
        self._show_problems(problems)
        names = [p.name for p in self.profiles]
        if select in names:
            self.current = select
        elif self.current not in names:
            self.current = names[0] if names else None
        self._fill_sidebar()
        self._select_first_screen()
        self.refresh()

    def _show_problems(self, problems: list[str]) -> None:
        if self.demo:
            problems = ["Demo mode: example screens and audio devices; Apply changes nothing"] + problems
        elif self.dry_run:
            problems = ["Test mode (--dry-run): Apply only asks GNOME to check the layout; nothing changes"] + problems
        self.banner.set_title(" · ".join(problems))
        self.banner.set_revealed(bool(problems))

    def _select_first_screen(self) -> None:
        p = self.profile()
        ids = editor.screen_ids(p) if p else []
        if self.selected not in ids:
            self.selected = (editor.primary_screen(p) if p else None) or (ids[0] if ids else None)

    def edit(self, change, *, from_widget_signal: bool = False) -> bool:
        """Change the selected profile with `change(profile)` and store it at once. The edit is
        stored only if profiles.json still holds the profile as shown; if it was changed outside
        this window (for example `pam save-current --force`), nothing is written and the window
        shows the newer version. `from_widget_signal`: called from a widget's own signal (a combo
        row's notify::selected), so the redraw that replaces that widget's model waits until idle."""
        p = self.profile()
        if p is None:
            return False
        before = copy.deepcopy(p)
        change(p)
        try:
            self.pam.put_profile(p, expected=before)
            return True
        except ProfileChanged:
            message = (f"“{p.name}” was changed outside PAM's window; showing the newer version. "
                       "Make your change again.")
            self._after(from_widget_signal, lambda: (self.reload(select=self.current), self.toast(message)))
            return False
        except (StoreError, PamError) as e:
            self._after(from_widget_signal, lambda: (self.error("Could Not Save the Profile", str(e)),
                                                      self.reload(select=self.current)))
            return False

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
            stored = self.pam.profile(name)
        except (PamError, StoreError):
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
                key.add_css_class("pam-sidebar-key")
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
        for name in ("apply", "rename-profile", "delete-profile"):
            self.lookup_action(name).set_enabled(p is not None and not (name == "apply" and self._applying))
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
            chip.add_css_class("pam-chip")
            dot = Gtk.Box(valign=Gtk.Align.CENTER)
            dot.add_css_class("pam-dot")
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
            row = Adw.ActionRow(title=other.title,
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
        devs = self.audio_state.outputs if kind == "output" else self.audio_state.inputs
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
        accel = self.keys.get(p.name)
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
        if row.profile_name != self.current:
            self.current = row.profile_name
            self.selected = None
            self._select_first_screen()
            self.refresh()
        self._updating = True
        try:
            listbox.select_row(row)
        finally:
            self._updating = False
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

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
            self.error("Cannot Add the Screen", "PAM does not know any mode of this screen.")
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
        if self.edit(change, from_widget_signal=True):
            label = "Output" if kind == "output" else "Input"
            self.toast(f"{label} set for “{p.name}”")

    def _on_change_shortcut(self, _button) -> None:
        p = self.profile()
        if p is not None:
            ShortcutDialog(self, p.name).present(self)

    def set_shortcut(self, name: str, accel: str | None) -> str | None:
        """Called by the shortcut dialog. Returns an error message, or None when done."""
        if accel:
            clash = self.shortcuts.clash(accel, name, same_accel)
            if clash:
                return f"{accel_label(accel)} is already used by {clash}"
        try:
            self.shortcuts.set(name, accel)
        except (ShortcutError, GLib.Error) as e:
            return str(e)
        self.reload(select=self.current)
        self.toast(f"Shortcut for “{name}”: {accel_label(accel)}" if accel else f"Shortcut for “{name}” removed")
        return None

    # ================================================================ handlers: header and menu

    def _on_new_profile(self) -> None:
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
                self.pam.save_current(name)
                note = ""
            except PamError as e:
                if "--no-audio" not in str(e):  # only an audio read failure: save the screens alone
                    self.error("Could Not Create the Profile", str(e))
                    return
                try:
                    self.pam.save_current(name, include_audio=False)
                    note = " (screens only: the audio devices could not be read)"
                except (PamError, StoreError, DisplayError) as e2:
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
                self.pam.rename_profile(old, new)  # keeps its place in the list; one write
            except (PamError, StoreError) as e:
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
                self.pam.delete_profile(p.name)
            except (PamError, StoreError) as e:
                self.error("Could Not Delete the Profile", str(e))
                return
            try:
                self.shortcuts.set(p.name, None)
            except (ShortcutError, GLib.Error) as e:
                self.error("Shortcut Not Removed", str(e))
            self.current = None
            self.reload()
            self.toast(f"Profile “{p.name}” deleted")

        dialog.connect("response", done)
        dialog.present(self)

    def _on_about(self) -> None:
        about = Adw.AboutDialog(application_name="PAM", application_icon=self.get_application().get_application_id(),
                                developer_name="Kevin Willis", version=_version(),
                                license_type=Gtk.License.MIT_X11,
                                comments="Display and audio profiles for GNOME")
        about.present(self)

    # ================================================================ apply

    def _on_apply(self, name: str | None = None, allow_missing: bool = False) -> None:
        """Apply a profile: the one named (from "Apply the Rest", which names the profile its
        dialog was about), else the selected one. Runs in a thread; the window stays responsive."""
        if self._applying:
            return
        if name is None:
            p = self.profile()
            if p is None:
                return
            name = p.name
            if not self._shown_is_stored(name):
                # profiles.json changed outside this window: show what would be applied first.
                self.reload(select=self.current)
                self.toast(f"“{name}” was changed outside PAM's window; check it and press Apply again")
                return
        self._set_applying(True)
        dry = self.dry_run

        def work():
            try:
                result = self.pam.apply(name, dry_run=dry, allow_missing=allow_missing)
            except Exception as e:  # report anything; never leave the button disabled
                result = e
            GLib.idle_add(self._apply_done, name, allow_missing, result)

        # Not a daemon thread: if the program ends while a switch is running, Python waits for it
        # instead of killing it half-way (screens changed, audio not yet).
        self._apply_thread = threading.Thread(target=work, name="pam-apply")
        self._apply_thread.start()

    def _set_applying(self, on: bool) -> None:
        self._applying = on
        self.lookup_action("apply").set_enabled(not on and self.profile() is not None)
        self.apply_spinner.set_visible(on)
        self.apply_label.set_label("Applying…" if on else "Apply")

    def _apply_done(self, name: str, allow_missing: bool, result) -> bool:
        self._set_applying(False)
        if self._close_after_apply:
            self._close_after_apply = False
            if not isinstance(result, Exception) and result.ok:
                self.close()  # done as asked; the screens show the outcome
                return False
            # Something went wrong: stay open and show it (below); the person closes again.
        if isinstance(result, Exception):
            self.error(f"“{name}” Not Applied", f"{type(result).__name__}: {result}")
            return False
        r: ApplyResult = result
        changed = r.applied_display or r.applied_output or r.applied_input
        blocked = any(pr.blocking_always for pr in r.problems)
        if r.ok:
            self.toast(f"GNOME accepts “{name}”; test mode, nothing changed" if r.dry_run else f"Applied “{name}”")
        elif changed:
            # Something was switched: never offer "Apply the rest" now. This includes an audio
            # device (HDMI audio) that did not appear after the screens changed.
            skipped = allow_missing and not r.errors
            head = f"“{name}” Applied With Parts Skipped" if skipped else f"“{name}” Only Partly Applied"
            self.error(head, _bullets(r.summary_lines()))
        elif r.dry_run and allow_missing and r.goes_ahead and not r.errors:
            self.error(f"GNOME Accepts the Rest of “{name}”",
                       "Test mode: nothing was changed. Would be skipped:\n\n" + _bullets(r.summary_lines()))
        elif r.problems and not r.errors and not blocked and not allow_missing:
            self._ask_apply_rest(name, r)
        else:
            self.error(f"“{name}” Not Applied", "Nothing was changed.\n\n" + _bullets(r.summary_lines()))
        for note in r.notes:
            if note.startswith("history not updated"):
                self.toast(note)
        self.reload(select=self.current)
        return False

    def _ask_apply_rest(self, name: str, r: ApplyResult) -> None:
        dialog = Adw.AlertDialog(heading=f"“{name}” Not Applied",
                                 body="Nothing was changed, because part of the profile is missing:\n\n"
                                      + _bullets(r.summary_lines()))
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("rest", "Apply the _Rest")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda d, resp: resp == "rest" and self._on_apply(name, allow_missing=True))
        dialog.present(self)

    # ================================================================ messages

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))

    def error(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", "_Close")
        dialog.present(self)

    # ================================================================ GNOME's MonitorsChanged

    def _watch_monitors(self) -> None:
        if self.demo:
            return
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

        threading.Thread(target=work, name="pam-monitors", daemon=True).start()
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
        else:
            self._show_state(state, self.current)
        return False

    def _on_close(self, *_args) -> bool:
        if self._applying:
            # Closing now would end the program in the middle of a switch (screens changed, audio
            # not yet). Close when it is done; if it fails, stay open and show why.
            self._close_after_apply = True
            self.toast("PAM closes when the switch is done")
            return True
        self._closed = True
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

    def forget_screen(self, sid: ScreenId) -> None:
        try:
            self.pam.forget_screen(sid)
        except (StoreError, PamError) as e:
            self.error("Could Not Forget the Screen", str(e))
        self.reload(select=self.current)

    def forget_audio(self, kind: str, name: str) -> None:
        try:
            self.pam.forget_audio(kind, name)
        except (StoreError, PamError) as e:
            self.error("Could Not Forget the Device", str(e))
        self.reload(select=self.current)


class ShortcutDialog(Adw.Dialog):
    """Press the new keys (as in GNOME Settings): Esc cancels, Backspace removes the shortcut."""

    def __init__(self, window: PamWindow, profile: str):
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
    """Screens and audio devices PAM remembers that are not connected now, each with Forget."""

    def __init__(self, window: PamWindow):
        super().__init__(title="Remembered Devices", content_width=560, content_height=560)
        self.window = window
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage(description="PAM remembers every screen and audio device it has seen, so "
                                                    "profiles can use them while they are unplugged. Forget the ones "
                                                    "you no longer have. Connected ones are not listed: PAM would "
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
        item.pam_handler = row.connect("notify::selected", lambda *_: update(item))

    def unbind(_f, item):
        handler = getattr(item, "pam_handler", None)
        if handler is not None:
            row.disconnect(handler)
            item.pam_handler = None

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


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"• {line}" for line in lines)


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("pam-profiles")
    except Exception:
        return ""
