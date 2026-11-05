"""The API the GUI and the CLI use."""
from __future__ import annotations

import fcntl
import os
import time
from dataclasses import dataclass, field

from .audio import AudioBackend, AudioError, PulseAudioBackend
from .model import (AudioRef, AudioState, DisplayLayout, DisplayState, Profile, ProfileLogicalMonitor,
                    ProfileScreen)
from .mutter import (METHOD_TEMPORARY, METHOD_VERIFY, DisplayBackend, DisplayError,
                     MutterDisplayConfig, StaleSerialError)
from .planner import AudioPlan, DisplayPlan, Problem, plan_audio, plan_display
from .store import Known, Store, StoreError

APPLY_LOCK_WAIT_S = 40


class PamError(Exception):
    """A request that cannot be done (unknown profile, name taken, nothing to save...)."""


@dataclass
class ApplyResult:
    profile: str
    dry_run: bool
    allow_missing: bool = False
    applied_display: bool = False
    applied_output: bool = False
    applied_input: bool = False
    problems: list[Problem] = field(default_factory=list)   # missing screens/devices etc.
    errors: list[str] = field(default_factory=list)          # failures while talking to GNOME/audio
    notes: list[str] = field(default_factory=list)
    display_plan: DisplayPlan | None = None
    audio_plan: AudioPlan | None = None

    @property
    def refused(self) -> bool:
        """Nothing was changed because of problems found before applying."""
        return bool(self.problems) and not (self.applied_display or self.applied_output or self.applied_input)

    @property
    def goes_ahead(self) -> bool:
        """True when the plan is (or, in a dry run, would be) sent: no problems, or only skippable
        problems with allow_missing."""
        if any(p.blocking_always for p in self.problems):
            return False
        return not self.problems or self.allow_missing

    @property
    def ok(self) -> bool:
        return not self.problems and not self.errors

    def summary_lines(self) -> list[str]:
        return [p.message for p in self.problems] + self.errors


class Pam:
    def __init__(self, display: DisplayBackend | None = None, audio: AudioBackend | None = None,
                 store: Store | None = None):
        self.display = display or MutterDisplayConfig()
        self.audio = audio or PulseAudioBackend()
        self.store = store or Store()

    # ------------------------------------------------------------ reading

    def profiles(self) -> list[Profile]:
        return self.store.load_profiles()

    def profile(self, name: str) -> Profile:
        p = next((p for p in self.profiles() if p.name == name), None)
        if p is None:
            raise PamError(f"no profile named {name!r}")
        return p

    def known(self) -> Known:
        return self.store.load_known()

    def observe(self, display_state: DisplayState | None = None,
                audio_state: AudioState | None = None) -> Known:
        """Record what is connected now in the history (screens and audio devices). The GUI should
        call this whenever Mutter emits MonitorsChanged. Returns the updated history."""
        with self.store.locked():
            known = self.store.load_known()
            changed = False
            if display_state is not None:
                changed |= known.observe_display(display_state)
            if audio_state is not None:
                changed |= known.observe_audio(audio_state)
            if changed:
                self.store.save_known(known)
            return known

    def current(self, *, need_audio: bool = True) -> tuple[DisplayState, AudioState | None, list[str]]:
        """Read displays and audio, record both in the history. Audio failures are returned as
        errors rather than raised, so screens can still be listed."""
        errors = []
        display_state = self.display.get_state()
        audio_state = None
        if need_audio:
            try:
                audio_state = self.audio.snapshot()
            except AudioError as e:
                errors.append(str(e))
        self.observe(display_state, audio_state)
        return display_state, audio_state, errors

    # ------------------------------------------------------------ saving

    def capture(self, name: str, display_state: DisplayState, audio_state: AudioState | None,
                include_audio: bool = True) -> Profile:
        """Build a profile from the given state (not stored)."""
        lms = []
        for lm in display_state.logical_monitors:
            screens = []
            for connector in lm.connectors:
                cm = display_state.by_connector(connector)
                if cm is None:
                    raise PamError(f"GNOME lists {connector} in the layout but not as a connected monitor")
                mode = cm.current_mode()
                if mode is None:
                    raise PamError(f"{connector} has no current mode")
                screens.append(ProfileScreen(cm.screen_id, mode.spec(), connector))
            lms.append(ProfileLogicalMonitor(lm.x, lm.y, lm.scale, lm.transform, lm.primary, screens))
        if not lms:
            raise PamError("no screen is on; nothing to save")
        profile = Profile(name, DisplayLayout(lms, display_state.layout_mode))
        if include_audio and audio_state is not None:
            profile.audio_output = _ref(audio_state.outputs, audio_state.default_output)
            profile.audio_input = _ref(audio_state.inputs, audio_state.default_input)
        return profile

    def save_current(self, name: str, *, overwrite: bool = False, include_audio: bool = True) -> Profile:
        name = name.strip()
        if not name:
            raise PamError("a profile needs a name")
        display_state, audio_state, errors = self.current(need_audio=include_audio)
        if include_audio and errors:
            raise PamError("; ".join(errors) + " (use --no-audio to save the screens only)")
        profile = self.capture(name, display_state, audio_state, include_audio)
        self.put_profile(profile, overwrite=overwrite)
        return profile

    def put_profile(self, profile: Profile, *, overwrite: bool = True) -> None:
        """Add or replace a profile (the GUI uses this after editing positions)."""
        with self.store.locked():
            profiles = self.store.load_profiles()
            index = next((i for i, p in enumerate(profiles) if p.name == profile.name), None)
            if index is not None and not overwrite:
                raise PamError(f"a profile named {profile.name!r} already exists")
            if index is None:
                profiles.append(profile)
            else:
                profiles[index] = profile
            self.store.save_profiles(profiles)

    def delete_profile(self, name: str) -> None:
        with self.store.locked():
            profiles = self.store.load_profiles()
            kept = [p for p in profiles if p.name != name]
            if len(kept) == len(profiles):
                raise PamError(f"no profile named {name!r}")
            self.store.save_profiles(kept)

    # ------------------------------------------------------------ applying

    def apply(self, name: str, *, dry_run: bool = False, allow_missing: bool = False) -> ApplyResult:
        """Apply a profile. Without allow_missing, any missing screen, mode or audio device means
        nothing is changed. A dry run plans and asks Mutter to verify the layout (method 0) without
        changing anything."""
        profile = self.profile(name)
        result = ApplyResult(profile=name, dry_run=dry_run, allow_missing=allow_missing)
        lock = None if dry_run else _apply_lock()
        if lock is False:
            result.errors.append(f"another profile switch is still running after {APPLY_LOCK_WAIT_S} s")
            return result
        try:
            self._apply(profile, result, dry_run, allow_missing, retry_stale=True)
        finally:
            if lock:
                lock.close()
        return result

    def _apply(self, profile: Profile, result: ApplyResult, dry_run: bool, allow_missing: bool,
               retry_stale: bool) -> None:
        needs_audio = profile.audio_output is not None or profile.audio_input is not None
        try:
            display_state = self.display.get_state() if profile.displays else None
        except DisplayError as e:
            result.errors.append(f"could not read the screens: {e}")
            return
        audio_state = None
        if needs_audio:
            try:
                audio_state = self.audio.snapshot()
            except AudioError as e:
                result.errors.append(str(e))
                if not allow_missing:
                    return
        try:
            self.observe(display_state, audio_state)
        except StoreError as e:
            result.notes.append(f"history not updated: {e}")

        dplan = plan_display(profile.displays, display_state) if profile.displays else None
        aplan = plan_audio(profile, audio_state) if audio_state is not None else AudioPlan(None, None, [])
        result.display_plan, result.audio_plan = dplan, aplan
        result.problems = (dplan.problems if dplan else []) + aplan.problems
        if dplan:
            result.notes.extend(dplan.notes)
        if any(p.blocking_always for p in result.problems):
            return
        if result.problems and not allow_missing:
            return

        if dplan:
            try:
                self.display.apply(display_state.serial, METHOD_VERIFY, dplan.logical_monitors, dplan.properties)
                if not dry_run:
                    self.display.apply(display_state.serial, METHOD_TEMPORARY, dplan.logical_monitors,
                                       dplan.properties)
                    result.applied_display = True
            except StaleSerialError:
                if retry_stale:  # the monitors changed under us; read again and re-plan once
                    result.problems, result.notes = [], []
                    self._apply(profile, result, dry_run, allow_missing, retry_stale=False)
                    return
                result.errors.append("the screens kept changing while applying; try again")
                return
            except DisplayError as e:
                result.errors.append(f"GNOME refused the layout: {e}")
                return  # do not change audio when the screens failed

        if dry_run:
            return
        for kind, ref, setter in (("output", aplan.output, self.audio.set_default_output),
                                  ("input", aplan.input, self.audio.set_default_input)):
            if ref is None:
                continue
            try:
                setter(ref.name)
                setattr(result, f"applied_{kind}", True)
            except AudioError as e:
                result.errors.append(str(e))


def _ref(devices, name):
    if not name:
        return None
    dev = next((d for d in devices if d.name == name), None)
    return AudioRef(name, dev.description if dev else "")


def _apply_lock():
    """One switch at a time (a fast double key press). Returns the held file, or False on timeout."""
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", f"pam-apply-{os.getuid()}.lock")
    handle = open(path, "w")
    deadline = time.monotonic() + APPLY_LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                return False
            time.sleep(0.1)
