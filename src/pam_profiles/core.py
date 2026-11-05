"""The API the GUI and the CLI use."""
from __future__ import annotations

import fcntl
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .audio import AudioBackend, AudioError, PulseAudioBackend
from .model import (AudioRef, AudioState, DisplayLayout, DisplayState, Profile, ProfileLogicalMonitor,
                    ProfileScreen, ScreenId)
from .mutter import (METHOD_TEMPORARY, METHOD_VERIFY, DisplayBackend, DisplayError,
                     MutterDisplayConfig, StaleSerialError)
from .planner import AudioPlan, DisplayPlan, Problem, plan_audio, plan_display
from .store import Known, Store, StoreError, open_lock_file

APPLY_LOCK_WAIT_S = 40
# An HDMI/DisplayPort audio output can appear only once its screen is on. When a profile turns a
# screen on and needs such a device that is not present yet, PAM applies the screens first and
# then waits up to this long for the device (it is never replaced by another one).
AUDIO_APPEAR_WAIT_S = 5.0
AUDIO_POLL_S = 0.25


class PamError(Exception):
    """A request that cannot be done (unknown profile, name taken, nothing to save...)."""


class ProfileChanged(PamError):
    """The stored profile is not the one an edit was made on (put_profile's `expected`)."""


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
        self.audio_appear_wait_s = AUDIO_APPEAR_WAIT_S
        # Set by current() and apply(): why the history could not be updated, or None. Updating the
        # history is best-effort; it never stops listing, saving or applying.
        self.last_history_error: str | None = None
        self._sleep = time.sleep
        self._clock = time.monotonic

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

    def observe_best_effort(self, display_state: DisplayState | None = None,
                            audio_state: AudioState | None = None) -> str | None:
        """observe(), but a history that cannot be read or written (corrupt file, read-only or full
        disk) is returned as a message instead of raised. Also stored in last_history_error."""
        try:
            self.observe(display_state, audio_state)
            self.last_history_error = None
        except (StoreError, OSError) as e:
            self.last_history_error = f"history not updated: {e}"
        return self.last_history_error

    def current(self, *, need_audio: bool = True) -> tuple[DisplayState, AudioState | None, list[str]]:
        """Read displays and audio, record both in the history. Audio failures are returned as
        errors rather than raised, so screens can still be listed. A history that cannot be updated
        is not an error here; see last_history_error. Raises DisplayError if Mutter cannot be read."""
        errors = []
        display_state = self.display.get_state()
        audio_state = None
        if need_audio:
            try:
                audio_state = self.audio.snapshot()
            except AudioError as e:
                errors.append(str(e))
        self.observe_best_effort(display_state, audio_state)
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

    def put_profile(self, profile: Profile, *, overwrite: bool = True, expected: Profile | None = None) -> None:
        """Add or replace a profile (the GUI uses this after editing positions).

        `expected`: the stored profile the edit was made on. If the stored profile is no longer
        that (someone ran `pam save-current --force` or edited the file meanwhile), nothing is
        written and ProfileChanged is raised, so a newer version is never silently overwritten."""
        with self.store.locked():
            profiles = self.store.load_profiles()
            index = next((i for i, p in enumerate(profiles) if p.name == profile.name), None)
            if index is not None and not overwrite:
                raise PamError(f"a profile named {profile.name!r} already exists")
            if expected is not None:
                stored = profiles[index] if index is not None else None
                if stored is None or stored.to_json() != expected.to_json():
                    raise ProfileChanged(f"profile {profile.name!r} was changed or removed outside this window")
            if index is None:
                profiles.append(profile)
            else:
                profiles[index] = profile
            self.store.save_profiles(profiles)

    def rename_profile(self, old: str, new: str) -> None:
        """Rename a profile in place (it keeps its position in the list), in one write."""
        new = new.strip()
        if not new:
            raise PamError("a profile needs a name")
        with self.store.locked():
            profiles = self.store.load_profiles()
            index = next((i for i, p in enumerate(profiles) if p.name == old), None)
            if index is None:
                raise PamError(f"no profile named {old!r}")
            if new != old and any(p.name == new for p in profiles):
                raise PamError(f"a profile named {new!r} already exists")
            p = profiles[index]
            profiles[index] = Profile(new, p.displays, p.audio_output, p.audio_input)
            self.store.save_profiles(profiles)

    def forget_screen(self, sid: ScreenId) -> bool:
        """Remove a screen from the history (known.json). Profiles that use it keep it. A connected
        screen is recorded again the next time PAM reads the screens. Returns False if it was not
        remembered."""
        with self.store.locked():
            known = self.store.load_known()
            kept = [s for s in known.screens if s.screen != sid]
            if len(kept) == len(known.screens):
                return False
            known.screens = kept
            self.store.save_known(known)
            return True

    def forget_audio(self, kind: str, name: str) -> bool:
        """Remove an audio output (kind 'output') or input ('input') from the history, by node name.
        Returns False if it was not remembered."""
        if kind not in ("output", "input"):
            raise ValueError(f"kind must be 'output' or 'input', not {kind!r}")
        with self.store.locked():
            known = self.store.load_known()
            devices = known.outputs if kind == "output" else known.inputs
            kept = [d for d in devices if d.name != name]
            if len(kept) == len(devices):
                return False
            if kind == "output":
                known.outputs = kept
            else:
                known.inputs = kept
            self.store.save_known(known)
            return True

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
        changing anything.

        Screens are applied before audio. A display-audio device (HDMI/DisplayPort) that is missing
        while the profile turns a screen on is waited for after the screens change (up to
        audio_appear_wait_s); if it does not appear it is reported, never replaced."""
        profile = self.profile(name)
        result = ApplyResult(profile=name, dry_run=dry_run, allow_missing=allow_missing)
        lock = None
        if not dry_run:
            try:
                lock = _apply_lock(self.store.dir)
            except StoreError as e:
                result.errors.append(f"cannot take the apply lock: {e}")
                return result
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
        history_error = self.observe_best_effort(display_state, audio_state)
        if history_error:
            result.notes.append(history_error)

        dplan = plan_display(profile.displays, display_state) if profile.displays else None
        aplan = plan_audio(profile, audio_state) if audio_state is not None else AudioPlan(None, None, [])
        awaited = _awaited_audio(profile, aplan, dplan, display_state)
        if awaited:
            aplan.problems = [p for p in aplan.problems if p.kind not in {f"missing_{k}" for k, _ in awaited}]
            for kind, ref in awaited:
                result.notes.append(f"audio {kind} {ref.describe()} is not present yet; it may appear when "
                                    f"the screens change, PAM waits up to {self.audio_appear_wait_s:g} s for it")
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
                    result.problems, result.notes, result.errors = [], [], []
                    result.display_plan = result.audio_plan = None
                    self._apply(profile, result, dry_run, allow_missing, retry_stale=False)
                    return
                result.errors.append("the screens kept changing while applying; try again")
                return
            except DisplayError as e:
                result.errors.append(f"GNOME refused the layout: {e}")
                return  # do not change audio when the screens failed

        if dry_run:
            return
        if awaited:
            missing = self._wait_for_audio(awaited)
            for kind, ref in awaited:
                if ref in missing:
                    problem = Problem(f"missing_{kind}", f"audio {kind} {ref.describe()} is not present "
                                      f"(waited {self.audio_appear_wait_s:g} s after the screens changed)")
                    aplan.problems.append(problem)
                    result.problems.append(problem)
                else:
                    setattr(aplan, kind, ref)
        for kind, ref, setter in (("output", aplan.output, self.audio.set_default_output),
                                  ("input", aplan.input, self.audio.set_default_input)):
            if ref is None:
                continue
            try:
                setter(ref.name)
                setattr(result, f"applied_{kind}", True)
            except AudioError as e:
                result.errors.append(str(e))

    def _wait_for_audio(self, awaited: list[tuple[str, AudioRef]]) -> set[AudioRef]:
        """Poll the audio server until every awaited device is present or the wait is over. Returns
        the devices still missing."""
        deadline = self._clock() + self.audio_appear_wait_s
        missing = {ref for _, ref in awaited}
        while True:
            try:
                state = self.audio.snapshot()
            except AudioError:
                state = None
            if state is not None:
                present = {d.name for d in state.outputs} | {d.name for d in state.inputs}
                if any(ref.name in present for ref in missing):
                    self.observe_best_effort(None, state)
                missing = {ref for ref in missing if ref.name not in present}
            if not missing or self._clock() >= deadline:
                return missing
            self._sleep(AUDIO_POLL_S)


def _ref(devices, name):
    if not name:
        return None
    dev = next((d for d in devices if d.name == name), None)
    return AudioRef(name, dev.description if dev else "")


_DISPLAY_AUDIO_HINTS = ("hdmi", "displayport")


def _awaited_audio(profile: Profile, aplan: AudioPlan, dplan: DisplayPlan | None,
                   display_state: DisplayState | None) -> list[tuple[str, AudioRef]]:
    """Missing audio devices that may appear once the screens change: HDMI/DisplayPort audio (by
    node name or description) when the plan turns on a screen that is off now."""
    if dplan is None or display_state is None:
        return []
    active = {c for lm in display_state.logical_monitors for c in lm.connectors}
    if all(m.connector in active for lm in dplan.logical_monitors for m in lm.monitors):
        return []
    out = []
    kinds = {p.kind for p in aplan.problems}
    for kind, ref in (("output", profile.audio_output), ("input", profile.audio_input)):
        if ref is not None and f"missing_{kind}" in kinds:
            text = f"{ref.name} {ref.description}".lower()
            if any(h in text for h in _DISPLAY_AUDIO_HINTS):
                out.append((kind, ref))
    return out


def _apply_lock(fallback_dir: str | os.PathLike):
    """One switch at a time (a fast double key press). Returns the held file, or False on timeout.

    The lock lives in $XDG_RUNTIME_DIR (private to the user), or else in PAM's own config folder;
    never in a shared folder such as /tmp. It is opened without following symlinks or truncating
    (store.open_lock_file); a problem with it raises StoreError."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        path = Path(runtime) / "pam-apply.lock"
    else:
        try:
            Path(fallback_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StoreError(f"could not create {fallback_dir}: {e.strerror or e}") from e
        path = Path(fallback_dir) / ".apply.lock"
    handle = open_lock_file(path)
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
