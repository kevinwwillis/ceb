"""The arrangement canvas: one box per screen of the profile, drawn to scale.

Click a box to select it; drag it to move it. A press that moves less than GTK's drag threshold is
a click and never moves the screen. While dragging, the box snaps to its neighbours' edges; when it
is dropped somewhere GNOME would refuse (overlapping, or not touching another screen along an edge)
it moves to the nearest place GNOME accepts. The selected box has a highlight ring, the primary
screen is marked "★ primary", and a screen that is not connected is drawn hatched with a dashed
border and "not connected".

Keyboard: the arrow keys select the next or previous screen; Ctrl+arrow keys move the selected
screen one step that way, snapping like a drag, to the next place GNOME accepts. When the canvas
has keyboard focus it draws a focus ring.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
gi.require_foreign("cairo")  # cairo.Context for the draw function (python3-gi-cairo)
from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo  # noqa: E402

from .geometry import Rect, nudge, settle, snap  # noqa: E402

PAD = 24          # px around the layout inside the canvas (the mockup's 24)
MAX_SCALE = 0.12  # canvas px per layout px at most (the mockup's 0.12)


@dataclass
class Item:
    key: object          # the ScreenId of the logical monitor's first screen
    rect: Rect           # layout pixels
    title: str
    subtitle: str
    primary: bool
    connected: bool
    members: tuple = ()  # every ScreenId in this logical monitor (more than one: mirrored)

    def holds(self, sid) -> bool:
        return sid == self.key or sid in self.members


def _rgba(hex_: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    h = hex_.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, alpha)


# Colours from the picked mockup (dark); the light set keeps the same roles.
DARK = {"bg": _rgba("#1a1a1c"), "fill": _rgba("#3a4a63"), "border": _rgba("#6b85b3"),
        "text": (1, 1, 1, 0.87), "dim": (1, 1, 1, 0.55), "sel": (1, 1, 1, 1),
        "gone1": _rgba("#333333"), "gone2": _rgba("#2b2b2b"), "gone_border": _rgba("#888888"),
        "star": _rgba("#f6d32d")}
LIGHT = {"bg": _rgba("#e6e6ea"), "fill": _rgba("#c9d6ea"), "border": _rgba("#6b85b3"),
         "text": (0, 0, 0, 0.8), "dim": (0, 0, 0, 0.55), "sel": (0, 0, 0, 0.9),
         "gone1": _rgba("#d6d6d6"), "gone2": _rgba("#cbcbcb"), "gone_border": _rgba("#777777"),
         "star": _rgba("#c88800")}


class ArrangementCanvas(Gtk.DrawingArea):
    def __init__(self, on_select: Callable[[object], None], on_moved: Callable[[object, int, int, bool], None]):
        super().__init__()
        self.items: list[Item] = []
        self.selected = None
        self.empty_text = "No screens in this profile — add one below"
        self.warning = ""
        self._on_select, self._on_moved = on_select, on_moved
        self._view = None        # (scale, ox, oy), frozen while dragging
        self._drag = None        # (index, start rect)
        self._moved = False      # the pointer passed the drag threshold during this press
        self.set_size_request(-1, 260)
        self.set_hexpand(True)
        self.set_focusable(True)
        self.set_draw_func(self._draw)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Screen arrangement"])

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self.queue_draw())
        focus.connect("leave", lambda *_: self.queue_draw())
        self.add_controller(focus)
        self._root_handler = None
        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                             ["Arrow keys select a screen; Ctrl+arrow keys move it"])
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.queue_draw())

    def _on_realize(self, _w):
        root = self.get_root()
        if root is not None:
            self._root_handler = (root, root.connect("notify::focus-visible", lambda *_: self.queue_draw()))

    def _on_unrealize(self, _w):
        if self._root_handler:
            root, hid = self._root_handler
            root.disconnect(hid)
            self._root_handler = None

    @property
    def dragging(self) -> bool:
        return self._drag is not None

    # ------------------------------------------------------------ data

    def set_items(self, items: list[Item], selected) -> None:
        self.items = items
        self.selected = selected
        self._drag = None
        self.queue_draw()

    # ------------------------------------------------------------ geometry

    def _compute_view(self, width: int, height: int):
        rects = [i.rect for i in self.items]
        min_x = min([0] + [r.x for r in rects])
        min_y = min([0] + [r.y for r in rects])
        max_x = max([1920] + [r.x2 for r in rects])
        max_y = max([1080] + [r.y2 for r in rects])
        w, h = max(width - 2 * PAD, 1), max(height - 2 * PAD, 1)
        s = min(w / (max_x - min_x), h / (max_y - min_y), MAX_SCALE)
        ox = PAD + (w - (max_x - min_x) * s) / 2 - min_x * s
        oy = PAD + (h - (max_y - min_y) * s) / 2 - min_y * s
        return s, ox, oy

    def _view_now(self):
        if self._drag is not None and self._view is not None:
            return self._view
        self._view = self._compute_view(self.get_width(), self.get_height())
        return self._view

    def _hit(self, x: float, y: float) -> int | None:
        s, ox, oy = self._view_now()
        for i in reversed(range(len(self.items))):
            r = self.items[i].rect
            if ox + r.x * s <= x <= ox + r.x2 * s and oy + r.y * s <= y <= oy + r.y2 * s:
                return i
        return None

    # ------------------------------------------------------------ input

    def _drag_begin(self, gesture, x, y):
        self.grab_focus()
        index = self._hit(x, y)
        if index is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._view = self._compute_view(self.get_width(), self.get_height())
        self._drag = (index, self.items[index].rect)
        self._moved = False
        if not self.items[index].holds(self.selected):
            self.selected = self.items[index].key
        self._on_select(self.selected)
        self.queue_draw()

    def _drag_update(self, gesture, dx, dy):
        if self._drag is None:
            return
        if not self._moved:
            if max(abs(dx), abs(dy)) < _drag_threshold(self):
                return  # still a click
            self._moved = True
            self.set_cursor(Gdk.Cursor.new_from_name("grabbing"))
        index, start = self._drag
        s = self._view[0]
        others = [it.rect for i, it in enumerate(self.items) if i != index]
        x, y = snap(start.x + dx / s, start.y + dy / s, start.w, start.h, others)
        self.items[index] = replace(self.items[index], rect=start.moved(x, y))
        self.queue_draw()

    def _drag_end(self, gesture, dx, dy):
        if self._drag is None:
            return
        index, start = self._drag
        self._drag = None
        self.set_cursor(None)
        rect = self.items[index].rect
        if not self._moved or rect == start:
            self.items[index] = replace(self.items[index], rect=start)
            self.queue_draw()
            return
        rects, as_dropped = settle([it.rect for it in self.items], index, start)
        new = rects[index]
        self.items[index] = replace(self.items[index], rect=new)
        self._on_moved(self.items[index].key, new.x, new.y, as_dropped)

    _DIRECTIONS = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0), Gdk.KEY_Up: (0, -1), Gdk.KEY_Down: (0, 1),
                   Gdk.KEY_KP_Left: (-1, 0), Gdk.KEY_KP_Right: (1, 0), Gdk.KEY_KP_Up: (0, -1),
                   Gdk.KEY_KP_Down: (0, 1)}

    def _key(self, controller, keyval, keycode, state):
        """Arrow keys select the next or previous screen; Ctrl+arrow keys move the selected one."""
        if not self.items or keyval not in self._DIRECTIONS:
            return False
        mods = state & Gtk.accelerator_get_default_mod_mask()
        if mods == Gdk.ModifierType.CONTROL_MASK:
            return self.move_selected(*self._DIRECTIONS[keyval])
        if mods:
            return False
        keys = [it.key for it in self.items]
        i = next((n for n, it in enumerate(self.items) if it.holds(self.selected)), -1)
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_KP_Right, Gdk.KEY_KP_Down):
            i = (i + 1) % len(keys)
        else:
            i = (i - 1) % len(keys)
        self.selected = keys[i]
        self._on_select(self.selected)
        self.queue_draw()
        return True

    def move_selected(self, dx: int, dy: int) -> bool:
        """Move the selected screen one step (see geometry.nudge). Returns True if the key was used."""
        index = next((n for n, it in enumerate(self.items) if it.holds(self.selected)), None)
        if index is None or self._drag is not None:
            return False
        rects = nudge([it.rect for it in self.items], index, dx, dy)
        if rects is None:
            self.error_bell()
            return True
        new = rects[index]
        self.items[index] = replace(self.items[index], rect=new)
        self.queue_draw()
        self._on_moved(self.items[index].key, new.x, new.y, True)
        return True

    # ------------------------------------------------------------ drawing

    def _draw(self, area, cr, width, height):
        c = DARK if Adw.StyleManager.get_default().get_dark() else LIGHT
        _rounded(cr, 0, 0, width, height, 12)
        cr.set_source_rgba(*c["bg"])
        cr.fill()
        if not self.items:
            self._text(cr, self.empty_text, width / 2, height / 2, width - 2 * PAD, c["dim"], 10, center=True)
            self._focus_ring(cr, width, height)
            return
        s, ox, oy = self._view_now() if self._drag else self._compute_view(width, height)
        if not self._drag:
            self._view = (s, ox, oy)
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        for it in self.items:
            r = it.rect
            x, y, w, h = ox + r.x * s, oy + r.y * s, r.w * s, r.h * s
            selected = it.holds(self.selected)
            if selected:  # the mockup's 3 px accent ring outside the border
                _rounded(cr, x - 3, y - 3, w + 6, h + 6, 8)
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
                cr.fill()
            _rounded(cr, x, y, w, h, 6)
            if it.connected:
                cr.set_source_rgba(*c["fill"])
                cr.fill_preserve()
            else:
                cr.save()
                cr.clip_preserve()
                cr.set_source_rgba(*c["gone1"])
                cr.fill_preserve()
                cr.set_source_rgba(*c["gone2"])
                cr.set_line_width(8)  # perpendicular stripe width
                step = 16 * math.sqrt(2)
                k = -h
                while k < w + h:  # 45° stripes, 8 px on / 8 px off
                    cr.move_to(x + k, y + h)
                    cr.line_to(x + k + h, y)
                    k += step
                cr.stroke()
                cr.restore()
            cr.set_line_width(2)
            if selected:
                cr.set_source_rgba(*c["sel"])
            elif it.connected:
                cr.set_source_rgba(*c["border"])
            else:
                cr.set_source_rgba(*c["gone_border"])
            cr.set_dash([5, 4] if not it.connected else [])
            _rounded(cr, x + 1, y + 1, w - 2, h - 2, 5)
            cr.stroke()
            cr.set_dash([])
            self._labels(cr, it, x, y, w, h, c)
        if self.warning:
            self._text(cr, self.warning, width / 2, height - 14, width - 2 * PAD, _rgba("#ff7b63"), 9, center=True)
        self._focus_ring(cr, width, height)

    def _focus_ring(self, cr, width, height):
        """libadwaita's focus ring: 2 px of the accent colour at half opacity, drawn only for keyboard
        focus (as GTK does for its own widgets)."""
        root = self.get_root()
        if not self.has_focus() or root is None or not root.get_focus_visible():
            return
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.5)
        cr.set_line_width(2)
        _rounded(cr, 1, 1, width - 2, height - 2, 11)
        cr.stroke()

    def _labels(self, cr, it: Item, x, y, w, h, c):
        lines = [(it.title, c["text"], 9, True), (it.subtitle, c["dim"], 8, False)]
        if it.primary:
            lines.append(("★ primary", c["star"], 8, False))
        layouts = []
        for text, color, size, bold in lines:
            lay = self._layout(text, size, bold, w - 8)
            layouts.append((lay, color))
        total = sum(lay.get_pixel_size()[1] for lay, _ in layouts)
        ty = y + (h - total) / 2
        cr.save()
        _rounded(cr, x, y, w, h, 6)
        cr.clip()
        for lay, color in layouts:
            lh = lay.get_pixel_size()[1]
            cr.move_to(x + 4, ty)  # the layout is w - 8 wide and centres its own text
            cr.set_source_rgba(*color)
            PangoCairo.show_layout(cr, lay)
            ty += lh
        cr.restore()

    def _layout(self, text, size_pt, bold, max_w):
        lay = self.create_pango_layout(text)
        font = self.get_pango_context().get_font_description().copy()
        font.set_size(int(size_pt * Pango.SCALE))
        if bold:
            font.set_weight(Pango.Weight.BOLD)
        lay.set_font_description(font)
        lay.set_width(int(max(max_w, 10) * Pango.SCALE))
        lay.set_ellipsize(Pango.EllipsizeMode.END)
        lay.set_alignment(Pango.Alignment.CENTER)
        return lay

    def _text(self, cr, text, cx, cy, max_w, color, size, center=False):
        lay = self._layout(text, size, False, max_w)
        lh = lay.get_pixel_size()[1]
        cr.move_to(cx - max_w / 2, cy - lh / 2)
        cr.set_source_rgba(*color)
        PangoCairo.show_layout(cr, lay)


def _drag_threshold(widget) -> int:
    settings = widget.get_settings()
    try:
        return int(settings.get_property("gtk-dnd-drag-threshold"))
    except (TypeError, ValueError):
        return 8


def _rounded(cr, x, y, w, h, r):
    r = max(0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
