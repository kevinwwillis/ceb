"""Unsaved edits (Draft), undo and redo, Save and Discard, the remembered Test length, and the core
calls Test uses. No display needed."""
import json

import pytest
from conftest import UNITEK_OUT, desk

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


def test_live_profile_leaves_audio_alone_when_it_cannot_be_read(ceb):
    ceb.audio.down = True
    before = ceb.live_profile()
    assert before.audio_output is None and before.audio_input is None and before.displays


def test_apply_profile_applies_an_unsaved_profile_temporarily(ceb):
    d = Draft(ceb.profile("desk"), None)
    d.edit(lambda p: setattr(p, "audio_output", None))
    d.edit(lambda p: editor.set_primary(p, p.displays.logical_monitors[0].screens[0].screen))
    r = ceb.apply_profile(d.profile)
    assert r.ok and r.applied_display
    assert [c[0] for c in ceb.display.calls] == [0, 1]                 # verify, then temporary; never 2
    assert ceb.display.applied()[0][4] is True                         # the new primary
    assert ceb.profile("desk").to_json() != d.profile.to_json()        # nothing was saved
