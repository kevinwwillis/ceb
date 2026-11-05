"""Editing a profile's screen layout, and the words shown for each screen. Pure Python (no GTK).

The GUI edits a `Profile` in place with these functions and then stores it with Pam.put_profile.
A profile's layout is a list of logical monitors; each usually holds one screen (more than one
means the screens mirror each other). The canvas draws one box per logical monitor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..model import (DisplayLayout, DisplayState, Identity, ModeSpec, Profile, ProfileLogicalMonitor,
                     ProfileScreen, ScreenId)
from ..store import Known, KnownScreen
from .geometry import Rect, close_gap, origin_shift

LAYOUT_LOGICAL = 1  # Mutter layout mode: positions in logical (scaled) pixels
ROTATED = {1, 3, 5, 7}  # Mutter transforms that turn the screen by 90 or 270 degrees


def lm_rect(lm: ProfileLogicalMonitor, layout_mode: int | None) -> Rect:
    """The area a logical monitor covers in the layout. In logical layout mode (1) a screen at scale
    2 covers half its pixels; in physical mode (2) it covers all of them."""
    mode = lm.screens[0].mode
    w, h = mode.width, mode.height
    if lm.transform in ROTATED:
        w, h = h, w
    if layout_mode == LAYOUT_LOGICAL and lm.scale > 0:
        w, h = round(w / lm.scale), round(h / lm.scale)
    return Rect(lm.x, lm.y, w, h)


def rects(profile: Profile) -> list[Rect]:
    if not profile.displays:
        return []
    return [lm_rect(lm, profile.displays.layout_mode) for lm in profile.displays.logical_monitors]


def find(profile: Profile, sid: ScreenId) -> tuple[ProfileLogicalMonitor, ProfileScreen] | None:
    if not profile.displays:
        return None
    for lm in profile.displays.logical_monitors:
        for ps in lm.screens:
            if ps.screen == sid:
                return lm, ps
    return None


def screen_ids(profile: Profile) -> list[ScreenId]:
    return [ps.screen for ps in profile.displays.screens()] if profile.displays else []


def primary_screen(profile: Profile) -> ScreenId | None:
    if not profile.displays:
        return None
    lm = next((lm for lm in profile.displays.logical_monitors if lm.primary), None)
    return lm.screens[0].screen if lm else None


def move(profile: Profile, sid: ScreenId, x: int, y: int) -> None:
    """Put the logical monitor holding `sid` at (x, y), then shift the layout to start at (0, 0)."""
    found = find(profile, sid)
    if found is None:
        return
    found[0].x, found[0].y = x, y
    normalize(profile)


def normalize(profile: Profile) -> None:
    dx, dy = origin_shift(rects(profile))
    if dx or dy:
        for lm in profile.displays.logical_monitors:
            lm.x += dx
            lm.y += dy


def set_primary(profile: Profile, sid: ScreenId) -> None:
    found = find(profile, sid)
    if found is None:
        return
    for lm in profile.displays.logical_monitors:
        lm.primary = lm is found[0]


def remove(profile: Profile, sid: ScreenId) -> None:
    """Turn a screen off in this profile. If it was the primary, the first remaining screen becomes
    primary. If taking it out leaves a gap, the screens beyond it slide back to close the gap."""
    found = find(profile, sid)
    if found is None:
        return
    lm, ps = found
    layout = profile.displays
    removed = lm_rect(lm, layout.layout_mode)
    lm.screens.remove(ps)
    if lm.screens:
        return  # it was mirrored; the logical monitor stays with the other screens
    was_primary = lm.primary
    layout.logical_monitors.remove(lm)
    if was_primary and layout.logical_monitors:
        layout.logical_monitors[0].primary = True
    new = close_gap(rects(profile), removed)
    for m, r in zip(layout.logical_monitors, new):
        m.x, m.y = r.x, r.y
    normalize(profile)


def add(profile: Profile, sid: ScreenId, mode: ModeSpec, scale: float = 1.0,
        layout_mode: int | None = None) -> None:
    """Turn a screen on in this profile, to the right of the rightmost screen, top-aligned with it
    (the mockup's rule). The first screen added becomes primary."""
    if find(profile, sid) is not None:
        return
    if profile.displays is None:
        profile.displays = DisplayLayout([], layout_mode)
    layout = profile.displays
    current = rects(profile)
    if current:
        right = max(current, key=lambda r: r.x2)
        x, y = right.x2, right.y
    else:
        x, y = 0, 0
    layout.logical_monitors.append(ProfileLogicalMonitor(x, y, scale, 0, not current,
                                                         [ProfileScreen(sid, mode)]))


def mode_for(sid: ScreenId, state: DisplayState | None, known: Known) -> tuple[ModeSpec, float] | None:
    """The mode and scale to use when a screen is added to a profile: the screen's preferred mode
    and preferred scale, from the connected monitor if it is connected, else from the history."""
    if state is not None:
        cm = state.by_screen().get(sid)
        if cm is not None:
            mode = cm.preferred_mode() or cm.current_mode() or (cm.modes[0] if cm.modes else None)
            if mode is not None:
                return mode.spec(), _scale(mode.preferred_scale, mode.supported_scales)
    ks = known.screen(sid)
    if ks is not None:
        km = ks.preferred_mode()
        if km is not None:
            return km.spec(), _scale(km.preferred_scale, km.supported_scales)
    return None


def _scale(preferred: float, supported) -> float:
    return preferred if preferred and any(abs(s - preferred) < 1e-3 for s in supported) else 1.0


# ---------------------------------------------------------------- words shown for a screen


@dataclass
class ScreenLabel:
    title: str      # e.g. "Lenovo LI2364d"
    detail: str     # e.g. '23" · left twin (lower port)' or '24" · serial L8LMQS075392'
    connected: bool


_SIZE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:"|”|″|inch(?:es)?)\s*$')


def screen_title(display_name: str, identity: Identity) -> str:
    """A short name: the maker's first word from GNOME's display name plus the model, e.g.
    'Lenovo Group Limited 23"' + 'LEN LI2364d' -> 'Lenovo LI2364d'."""
    product = identity.product
    first = identity.vendor
    if product.split(" ", 1)[0] == identity.vendor and " " in product:
        product = product.split(" ", 1)[1]  # 'LEN LI2364d' -> 'LI2364d'
    words = _SIZE.sub("", display_name).split()
    maker = words[0] if words else first
    if not product:
        return display_name or identity.vendor
    if product.lower().startswith(maker.lower()):
        return product
    return f"{maker} {product}"


def screen_size(display_name: str) -> str:
    m = _SIZE.search(display_name)
    return f'{m.group(1)}"' if m else ""


def twin_words(twin: int, total: int) -> str:
    if total <= 1:
        return ""
    # Not "left"/"right" as in the mockup: which twin is on the left depends on the profile.
    if total == 2:
        return "twin 1 of 2 (lower port)" if twin == 0 else "twin 2 of 2 (higher port)"
    return f"twin {twin + 1} of {total} by port order"


def label(sid: ScreenId, known: Known, state: DisplayState | None) -> ScreenLabel:
    cm = state.by_screen().get(sid) if state is not None else None
    ks: KnownScreen | None = known.screen(sid)
    name = cm.display_name if cm else (ks.display_name if ks else "")
    title = screen_title(name, sid.identity)
    total = max(len(known.twins_of(sid.identity)), cm.twin_count if cm else 0, sid.twin + 1)
    parts = [p for p in (screen_size(name), twin_words(sid.twin, total)) if p]
    if total <= 1:
        parts.append(f"serial {sid.identity.serial}" if sid.identity.serial else "no serial")
    return ScreenLabel(title, " · ".join(parts), cm is not None)
