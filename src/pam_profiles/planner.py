"""Turn a stored profile into what to send to Mutter and the audio server, or say why it cannot.

Rules (explained in docs/behavior.md):
- Screens are found by identity + twin rank, never by port name.
- Every connected screen the profile does not list is turned off.
- A problem (a listed screen not connected, its mode not offered, an audio device not present) means
  NOTHING is changed, and every problem is reported. With allow_missing=True, the parts that can be
  applied are applied and each skipped part is reported.
- A profile with no screen left to turn on is never applied, allow_missing or not.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import (SCALE_TOLERANCE, AudioRef, AudioState, ConnectedMonitor, DisplayLayout,
                    DisplayState, Profile, ProfileScreen, ScreenId)
from .mutter import ApplyLogicalMonitor, ApplyMonitor


@dataclass
class Problem:
    kind: str  # missing_screen | missing_mode | bad_scale | layout_mode | missing_output | missing_input | no_screens
    message: str
    blocking_always: bool = False  # True: allow_missing does not help


@dataclass
class DisplayPlan:
    logical_monitors: list[ApplyLogicalMonitor]
    properties: dict
    problems: list[Problem]
    notes: list[str] = field(default_factory=list)
    turned_off: list[ConnectedMonitor] = field(default_factory=list)


@dataclass
class AudioPlan:
    output: AudioRef | None  # None: leave unchanged
    input: AudioRef | None
    problems: list[Problem]


def describe_screen(sid: ScreenId, twin_total: int | None = None, display_name: str = "") -> str:
    label = f"{display_name} ({sid.identity.describe()})" if display_name else sid.identity.describe()
    if twin_total and twin_total > 1 or sid.twin > 0:
        total = max(twin_total or 0, sid.twin + 1)
        position = "lowest port" if sid.twin == 0 else ("highest port" if sid.twin == total - 1
                                                         else f"port position {sid.twin + 1}")
        label += f", identical twin {sid.twin + 1} of {total} (the one on the {position})"
    return label


def plan_display(layout: DisplayLayout, state: DisplayState) -> DisplayPlan:
    """Map the profile's screens to the connected monitors. Always returns a plan for whatever can be
    applied plus the list of problems; the caller decides whether problems block."""
    connected = state.by_screen()
    problems: list[Problem] = []
    notes: list[str] = []
    twin_total = {}
    for s in layout.screens():
        twin_total[s.screen.identity] = max(twin_total.get(s.screen.identity, 0), s.screen.twin + 1)

    out: list[ApplyLogicalMonitor] = []
    primary_dropped = False
    used: set[str] = set()
    for lm in layout.logical_monitors:
        monitors: list[ApplyMonitor] = []
        for ps in lm.screens:
            cm = connected.get(ps.screen)
            if cm is None:
                problems.append(Problem("missing_screen", _missing_screen_message(ps, state, twin_total)))
                continue
            mode = cm.find_mode(ps.mode)
            if mode is None:
                problems.append(Problem("missing_mode",
                                        f"{describe_screen(ps.screen, cm.twin_count, cm.display_name)} "
                                        f"on {cm.connector} does not offer the mode {ps.mode.describe()}"))
                continue
            if not any(abs(s - lm.scale) < SCALE_TOLERANCE for s in mode.supported_scales):
                problems.append(Problem("bad_scale",
                                        f"{describe_screen(ps.screen, cm.twin_count, cm.display_name)} "
                                        f"does not support scale {lm.scale:g} at {ps.mode.describe()}"))
                continue
            monitors.append(ApplyMonitor(cm.connector, mode.id))
            used.add(cm.connector)
        if monitors:
            out.append(ApplyLogicalMonitor(lm.x, lm.y, lm.scale, lm.transform, lm.primary, monitors))
        elif lm.primary:
            primary_dropped = True

    if not out:
        problems.append(Problem("no_screens", "none of the profile's screens can be turned on",
                                blocking_always=True))
    else:
        if primary_dropped or not any(lm.primary for lm in out):
            for lm in out:
                lm.primary = False
            out[0].primary = True
            notes.append(f"the primary screen was skipped; {out[0].monitors[0].connector} is primary instead")
        # Mutter requires the layout to start at (0, 0); skipping a screen can move the origin.
        min_x, min_y = min(lm.x for lm in out), min(lm.y for lm in out)
        if min_x or min_y:
            for lm in out:
                lm.x -= min_x
                lm.y -= min_y

    properties = {}
    if layout.layout_mode is not None and state.layout_mode is not None and layout.layout_mode != state.layout_mode:
        if state.supports_changing_layout_mode:
            properties["layout-mode"] = layout.layout_mode
        else:
            problems.append(Problem("layout_mode",
                                    f"the profile was saved in layout mode {layout.layout_mode}, GNOME is in "
                                    f"mode {state.layout_mode} and cannot change it; positions would be wrong"))

    turned_off = [m for m in state.monitors if m.connector not in used]
    return DisplayPlan(out, properties, problems, notes, turned_off)


def _missing_screen_message(ps: ProfileScreen, state: DisplayState, twin_total: dict) -> str:
    sid = ps.screen
    same = [m for m in state.monitors if m.identity == sid.identity]
    msg = f"{describe_screen(sid, twin_total.get(sid.identity))} is not connected"
    if same:
        msg += (f"; only {len(same)} screen(s) of this model are connected, so PAM cannot tell which "
                f"of the identical twins is unplugged")
    return msg


def plan_audio(profile: Profile, state: AudioState | None) -> AudioPlan:
    problems: list[Problem] = []
    output, input_ = profile.audio_output, profile.audio_input
    if state is not None:
        if output and not any(d.name == output.name for d in state.outputs):
            problems.append(Problem("missing_output", f"audio output {output.describe()} is not present"))
            output = None
        if input_ and not any(d.name == input_.name for d in state.inputs):
            problems.append(Problem("missing_input", f"audio input {input_.describe()} is not present"))
            input_ = None
    return AudioPlan(output, input_, problems)
