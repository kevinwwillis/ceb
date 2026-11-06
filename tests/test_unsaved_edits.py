"""Unsaved edits (Draft), undo and redo, Save and Discard, the remembered Test length, and the core
calls Test uses. No display needed."""
import json

import pytest
from conftest import UNITEK_OUT, WEBCAM_IN, desk

from ceb_profiles.core import ProfileChanged
from ceb_profiles.gui import editor
from ceb_profiles.gui.draft import UNDO_LIMIT, Draft
from ceb_profiles.store import (TEST_SECONDS_DEFAULT, TEST_SECONDS_MAX, TEST_SECONDS_MIN, Settings, Store,
                                StoreError)


@pytest.fixture
def ceb(make_ceb):
    c = make_ceb(desk(1))
    c.save_current("desk")
    c.display.calls.clear()
    c.audio.set_calls.clear()
    return c


def shortcut_log():
    calls = []

    def set_shortcut(name, accel):
        calls.append((name, accel))
        return None
    return calls, set_shortcut


def move_first(p, y=200):
    sid = p.displays.logical_monitors[0].screens[0].screen
    editor.move(p, sid, 0, y)


# ------------------------------------------------------------ Draft


def test_edits_are_pending_and_never_written_or_applied(ceb):
    d = Draft(ceb.profile("desk"), "<Super>F2")
    stored = ceb.profile("desk").to_json()
    mtime = ceb.store.profiles_path.stat().st_mtime_ns
    d.edit(move_first)
    d.edit(lambda p: setattr(p, "audio_output", None))
    d.set_accel("<Super>F9")
    assert d.dirty and d.profile.audio_output is None
    assert ceb.profile("desk").to_json() == stored
    assert ceb.store.profiles_path.stat().st_mtime_ns == mtime
    assert ceb.display.calls == [] and ceb.audio.set_calls == []   # nothing applied, not even verified


def test_a_change_that_changes_nothing_is_not_an_undo_step(ceb):
    d = Draft(ceb.profile("desk"), None)
    assert d.edit(lambda p: None) is False
    assert not d.can_undo and not d.dirty
    assert d.set_accel(None) is False and d.set_accel("") is False   # "" and None are both "no shortcut"


def test_undo_and_redo_step_through_the_edits(ceb):
    d = Draft(ceb.profile("desk"), "<Super>F2")
    original = d.profile.to_json()
    d.edit(move_first)
    moved = d.profile.to_json()
    d.set_accel("<Super>F9")
    assert d.undo() and d.accel == "<Super>F2" and d.profile.to_json() == moved
    assert d.undo() and d.profile.to_json() == original and not d.dirty
    assert not d.undo() and d.can_redo
    assert d.redo() and d.profile.to_json() == moved
    assert d.redo() and d.accel == "<Super>F9" and not d.can_redo
    d.undo()
    d.edit(lambda p: setattr(p, "audio_input", None))   # a new edit drops what could be redone
    assert not d.can_redo


def test_undo_history_is_bounded(ceb):
    d = Draft(ceb.profile("desk"), None)
    for i in range(UNDO_LIMIT + 20):
        d.edit(lambda p, i=i: move_first(p, i + 1))
    n = 0
    while d.undo():
        n += 1
    assert n == UNDO_LIMIT


def test_discard_goes_back_to_what_is_saved(ceb):
    d = Draft(ceb.profile("desk"), "<Super>F2")
    d.edit(move_first)
    d.set_accel(None)
    d.discard()
    assert not d.dirty and d.accel == "<Super>F2" and not d.can_undo and not d.can_redo
    assert d.profile.to_json() == ceb.profile("desk").to_json()


def test_save_writes_the_profile_and_the_changed_shortcut_only(ceb):
    calls, set_shortcut = shortcut_log()
    d = Draft(ceb.profile("desk"), "<Super>F2")
    d.edit(move_first)
    assert d.save(ceb, set_shortcut) is None
    assert ceb.profile("desk").to_json() == d.profile.to_json()
    assert calls == [] and not d.dirty and not d.can_undo         # shortcut unchanged: not touched
    d.set_accel("<Super>F9")
    assert d.save(ceb, set_shortcut) is None
    assert calls == [("desk", "<Super>F9")] and not d.dirty
    assert ceb.display.calls == [] and ceb.audio.set_calls == []


def test_save_refuses_a_profile_changed_outside_and_force_replaces_it(ceb):
    d = Draft(ceb.profile("desk"), None)
    d.edit(move_first)
    ceb.save_current("desk", overwrite=True, include_audio=False)    # `ceb save-current --force`
    newer = ceb.profile("desk").to_json()
    with pytest.raises(ProfileChanged):
        d.save(ceb, shortcut_log()[1])
    assert ceb.profile("desk").to_json() == newer and d.dirty
    d.save(ceb, shortcut_log()[1], force=True)
    assert ceb.profile("desk").to_json() == d.profile.to_json() and not d.dirty


def test_a_failed_shortcut_leaves_the_profile_saved_and_the_shortcut_unsaved(ceb):
    d = Draft(ceb.profile("desk"), None)
    d.edit(move_first)
    d.set_accel("<Super>F9")
    problem = d.save(ceb, lambda name, accel: "dconf is read-only")
    assert problem == "dconf is read-only"
    assert ceb.profile("desk").to_json() == d.profile.to_json()
    assert d.dirty and not d.profile_dirty and d.accel == "<Super>F9"


def test_a_profile_with_a_screen_that_is_not_connected_saves(ceb, make_ceb):
    from conftest import PROJECTOR, mon, reply
    projector = make_ceb(reply([mon("DP-1", PROJECTOR)])).display.get_state().monitors[0]  # seen elsewhere
    sid = projector.screen_id
    d = Draft(ceb.profile("desk"), None)
    d.edit(lambda p: editor.add(p, sid, projector.modes[0].spec()))
    d.save(ceb, shortcut_log()[1])
    assert sid in editor.screen_ids(ceb.profile("desk"))
    r = ceb.apply("desk", dry_run=True)                    # switching to it reports the missing screen
    assert r.problems and any(p.kind.startswith("missing") for p in r.problems)


# ------------------------------------------------------------ the Test length setting


def test_test_length_defaults_to_15_and_is_remembered(tmp_path):
    store = Store(tmp_path / "ceb")
    assert store.load_settings().test_seconds == TEST_SECONDS_DEFAULT == 15
    store.save_settings(Settings(5))
    assert Store(tmp_path / "ceb").load_settings().test_seconds == 5
    data = json.loads((tmp_path / "ceb" / "settings.json").read_text())
    assert data == {"version": 1, "test_seconds": 5}
    assert not (tmp_path / "ceb" / "profiles.json").exists()   # its own file, next to profiles.json


def test_test_length_is_kept_in_range(tmp_path):
    store = Store(tmp_path / "ceb")
    store.dir.mkdir(parents=True)
    store.settings_path.write_text('{"version": 1, "test_seconds": 100000}')
    assert store.load_settings().test_seconds == TEST_SECONDS_MAX
    store.settings_path.write_text('{"version": 1, "test_seconds": 0}')
    assert store.load_settings().test_seconds == TEST_SECONDS_MIN


def test_a_settings_file_ceb_cannot_read_is_never_overwritten(tmp_path):
    store = Store(tmp_path / "ceb")
    store.dir.mkdir(parents=True)
    for text in ('{"version": 1, "test_seconds": "ten"}', "{not json", '{"version": 99}'):
        store.settings_path.write_text(text)
        with pytest.raises(StoreError):
            store.load_settings()
        with pytest.raises(StoreError):
            store.save_settings(Settings(5))
        assert store.settings_path.read_text() == text


# ------------------------------------------------------------ core: what Test applies


def test_live_profile_is_what_is_on_now_and_is_not_stored(ceb):
    before = ceb.live_profile()
    assert [p.name for p in ceb.profiles()] == ["desk"]
    assert before.audio_output.name == UNITEK_OUT.name
    assert [ps.screen for ps in before.displays.screens()] == [ps.screen for ps in ceb.profile("desk").displays.screens()]
    assert ceb.display.calls == []


def test_live_profile_raises_when_audio_cannot_be_read_and_can_leave_it_out(ceb):
    from ceb_profiles.audio import AudioError
    ceb.audio.down = True
    with pytest.raises(AudioError):
        ceb.live_profile()                       # never a silent "leave audio unchanged"
    before = ceb.live_profile(include_audio=False)
    assert before.audio_output is None and before.audio_input is None and before.displays


MONITOR = UNITEK_OUT.name + ".monitor"           # PipeWire's default source when there is no microphone


def test_live_profile_records_a_monitor_default_source_and_going_back_sets_it(ceb):
    ceb.audio.state.default_input = MONITOR
    before = ceb.live_profile()
    assert before.audio_input.name == MONITOR
    ceb.audio.state.default_input = WEBCAM_IN.name   # what a test changed
    r = ceb.apply_profile(before, allow_missing=True)
    assert r.ok, r.summary_lines()
    assert ceb.audio.state.default_input == MONITOR


def fake_clock(ceb):
    waits, t = [], [0.0]
    ceb._clock = lambda: t[0]

    def tick(s):
        waits.append(s)
        t[0] += s
    ceb._sleep = tick
    return waits


def test_a_monitor_source_is_waited_for_only_when_its_output_is_display_audio(ceb):
    from conftest import LENOVO, ROG, mon, reply
    from ceb_profiles.model import AudioRef
    # Only the ROG is on now, so applying "desk" turns two screens on (HDMI audio may follow).
    ceb.display.set(reply([mon("HDMI-1", LENOVO), mon("HDMI-2", LENOVO), mon("HDMI-3", ROG)],
                          [(0, 0, True, ["HDMI-3"])]))
    waits = fake_clock(ceb)
    p = ceb.profile("desk")
    p.audio_output = None
    p.audio_input = AudioRef("alsa_output.usb-Some_Speaker-00.analog-stereo.monitor", "Monitor of Speaker")
    r = ceb.apply_profile(p)
    assert waits == [] and r.refused                      # not display audio: missing at once
    assert any(pr.kind == "missing_input" for pr in r.problems)
    # The monitor of an HDMI output appears with that output: waited for. The description is not
    # looked at for a monitor ("Monitor of HDMI" says nothing about a USB output's monitor).
    p.audio_input = AudioRef("alsa_output.pci-0000_2d_00.1.hdmi-stereo.monitor", "Monitor of GA102 HDMI")
    ceb.apply_profile(p)
    assert waits


def test_going_back_restores_the_monitor_of_an_hdmi_output_the_test_turned_off(make_ceb):
    """The default source is the monitor of the HDMI output on HDMI-1's screen. A test with only the
    ROG on removes that output and its monitor; going back turns the screen on again, waits for the
    monitor and sets it."""
    from conftest import FakeAudio, HDMI_OUT, reply
    hdmi_monitor = HDMI_OUT.name + ".monitor"
    audio = FakeAudio(default_input=hdmi_monitor)
    c = make_ceb(desk(1), audio=audio)
    monitors = desk(1)[1]
    real_apply = c.display.apply
    appear_after = []                              # polls left before the HDMI output shows up

    def poll(_n):
        if appear_after and appear_after[0] > 0:
            appear_after[0] -= 1
            if appear_after[0] == 0:
                audio.state.outputs.append(HDMI_OUT)
    audio.on_snapshot = poll

    def apply(serial, method, lms, props):          # GNOME + PipeWire: HDMI audio follows the screen
        real_apply(serial, method, lms, props)
        if method != 1:
            return
        c.display.set(reply(monitors, [(lm.x, lm.y, lm.primary, [m.connector for m in lm.monitors]) for lm in lms]))
        if any(m.connector == "HDMI-1" for lm in lms for m in lm.monitors):
            if HDMI_OUT not in audio.state.outputs:
                appear_after[:] = [3]               # a moment after the screen comes on
        else:
            audio.state.outputs = [o for o in audio.state.outputs if o != HDMI_OUT]
            if audio.state.default_input == hdmi_monitor:
                audio.state.default_input = WEBCAM_IN.name    # PipeWire picks another source
    c.display.apply = apply
    waits = fake_clock(c)
    before = c.live_profile()
    assert before.audio_input.name == hdmi_monitor
    c.save_current("rog only")
    rog = c.profile("rog only")
    rog.displays.logical_monitors = [lm for lm in rog.displays.logical_monitors
                                     if lm.screens[0].screen.identity.vendor == "AUS"]
    rog.displays.logical_monitors[0].x = 0
    rog.displays.logical_monitors[0].primary = True
    rog.audio_output = rog.audio_input = None
    assert c.apply_profile(rog).ok
    assert audio.state.default_input == WEBCAM_IN.name and HDMI_OUT not in audio.state.outputs
    r = c.apply_profile(before, allow_missing=True)            # going back
    assert r.ok, r.summary_lines()
    assert waits                                                # it waited for the monitor
    assert audio.state.default_input == hdmi_monitor
    assert audio.state.default_output == before.audio_output.name


def test_save_current_leaves_the_input_unchanged_when_the_default_is_a_monitor(ceb):
    ceb.audio.state.default_input = MONITOR
    p = ceb.save_current("no mic")
    assert p.audio_input is None and p.audio_output.name == UNITEK_OUT.name
    assert ceb.apply("no mic", dry_run=True).ok   # nothing reported missing


def test_apply_profile_applies_an_unsaved_profile_temporarily(ceb):
    d = Draft(ceb.profile("desk"), None)
    d.edit(lambda p: setattr(p, "audio_output", None))
    d.edit(lambda p: editor.set_primary(p, p.displays.logical_monitors[0].screens[0].screen))
    r = ceb.apply_profile(d.profile)
    assert r.ok and r.applied_display
    assert [c[0] for c in ceb.display.calls] == [0, 1]                 # verify, then temporary; never 2
    assert ceb.display.applied()[0][4] is True                         # the new primary
    assert ceb.profile("desk").to_json() != d.profile.to_json()        # nothing was saved
