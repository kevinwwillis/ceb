import pytest

from conftest import (HDMI_OUT, LENOVO, PROJECTOR, ROG, UNITEK_IN, UNITEK_OUT, WEBCAM_IN, FakeAudio,
                      desk, mon, reply)
from ceb_profiles.audio import AudioError
from ceb_profiles.core import CebError, ProfileChanged
from ceb_profiles.mutter import METHOD_TEMPORARY, METHOD_VERIFY, DisplayError, StaleSerialError

M60 = "1920x1080@60.000"


def layout(ceb):
    return ceb.display.applied()


# ------------------------------------------------------------ port numberings and twins


@pytest.mark.parametrize("saved_base,applied_base", [(1, 2), (2, 1), (1, 1), (2, 2)])
@pytest.mark.parametrize("shuffle", [False, True])
def test_same_screens_under_both_port_numberings(make_ceb, saved_base, applied_base, shuffle):
    ceb = make_ceb(desk(saved_base))
    ceb.save_current("desk")
    ceb.display.set(desk(applied_base, shuffle=shuffle))
    result = ceb.apply("desk")
    assert result.ok, result.summary_lines()
    b = applied_base
    assert sorted(layout(ceb)) == sorted([
        (f"HDMI-{b}", M60, 0, 0, False),          # left Lenovo = lower port
        (f"HDMI-{b + 2}", M60, 1920, 0, True),    # ROG, primary
        (f"HDMI-{b + 1}", M60, 3840, 0, False),   # right Lenovo = higher port
    ])


def test_twins_keep_their_sides_when_the_profile_swaps_them(make_ceb):
    # Saved with the lower-port Lenovo on the RIGHT; must stay on the right after renumbering.
    r = reply([mon("HDMI-1", LENOVO), mon("HDMI-2", LENOVO), mon("HDMI-3", ROG)],
              [(0, 0, False, ["HDMI-2"]), (1920, 0, True, ["HDMI-3"]), (3840, 0, False, ["HDMI-1"])])
    ceb = make_ceb(r)
    ceb.save_current("swapped")
    ceb.display.set(desk(2))
    assert ceb.apply("swapped").ok
    assert ("HDMI-2", M60, 3840, 0, False) in layout(ceb)
    assert ("HDMI-3", M60, 0, 0, False) in layout(ceb)


def test_twins_on_different_connector_types(make_ceb):
    # One twin on DP, one on HDMI; only the HDMI numbers shift (HDMI-1 -> HDMI-3). Comparing numbers
    # across types would swap them (1 < 2, then 2 < 3); type first (DP before HDMI) does not.
    r = reply([mon("HDMI-1", LENOVO), mon("DP-2", LENOVO)],
              [(0, 0, True, ["DP-2"]), (1920, 0, False, ["HDMI-1"])])
    ceb = make_ceb(r)
    ceb.save_current("mixed")
    ceb.display.set(reply([mon("HDMI-3", LENOVO), mon("DP-2", LENOVO)]))
    assert ceb.apply("mixed").ok
    assert sorted(layout(ceb)) == [("DP-2", M60, 0, 0, True), ("HDMI-3", M60, 1920, 0, False)]


def test_twins_on_two_gpus_follow_the_gpu_when_cards_swap_numbers(make_ceb):
    r = reply([mon("HDMI-1", LENOVO), mon("HDMI-4", LENOVO)],
              [(0, 0, True, ["HDMI-1"]), (1920, 0, False, ["HDMI-4"])])
    ceb = make_ceb(r, gpus={"HDMI-1": "0000:2d:00.0", "HDMI-4": "0000:2e:00.0"})
    ceb.save_current("two-gpus")
    # Next boot the second card loads first: its port is now HDMI-1, the first card's is HDMI-2.
    ceb.display.set(reply([mon("HDMI-1", LENOVO), mon("HDMI-2", LENOVO)]))
    ceb.display.gpus = {"HDMI-1": "0000:2e:00.0", "HDMI-2": "0000:2d:00.0"}
    assert ceb.apply("two-gpus").ok
    assert sorted(layout(ceb)) == [("HDMI-1", M60, 1920, 0, False), ("HDMI-2", M60, 0, 0, True)]


def test_screens_not_in_the_profile_are_turned_off(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.display.set(desk(2, extra=[mon("DP-2", PROJECTOR)]))
    result = ceb.apply("desk")
    assert result.ok
    assert "DP-2" not in [c[0] for c in layout(ceb)]
    assert [m.connector for m in result.display_plan.turned_off] == ["DP-2"]


def test_only_verify_and_temporary_methods_are_used(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.apply("desk")
    assert [c[0] for c in ceb.display.calls] == [0, 1]  # verify, then temporary; never 2


# ------------------------------------------------------------ unplugged screens


def projector_profile(make_ceb):
    ceb = make_ceb(reply([mon("DP-2", PROJECTOR), mon("HDMI-1", LENOVO), mon("HDMI-2", LENOVO),
                          mon("HDMI-3", ROG)], [(0, 0, True, ["DP-2"])]))
    ceb.save_current("projector")
    return ceb


def test_unplugged_screen_refuses_and_changes_nothing(make_ceb):
    ceb = projector_profile(make_ceb)
    ceb.audio.set_calls.clear()
    ceb.display.set(desk(1))
    result = ceb.apply("projector")
    assert not result.ok and result.refused
    assert ceb.display.applied() is None and ceb.audio.set_calls == []
    assert any("S2-TEK TV" in line and "not connected" in line for line in result.summary_lines())


def test_unplugged_screen_stays_in_the_history(make_ceb):
    ceb = projector_profile(make_ceb)
    ceb.display.set(desk(1))
    ceb.current()
    known = ceb.known()
    assert {s.screen.identity.vendor for s in known.screens} == {"STK", "LEN", "AUS"}
    stk = next(s for s in known.screens if s.screen.identity.vendor == "STK")
    assert stk.last_connector == "DP-2" and stk.display_name == "S2-TEK TV"
    assert [(m.width, m.height) for m in stk.modes][:2] == [(1920, 1080), (1920, 1080)]


def test_twins_get_one_history_entry_each(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.current()
    lenovos = ceb.known().twins_of(ceb.display.get_state().by_connector("HDMI-1").identity)
    assert sorted((s.screen.twin, s.last_connector) for s in lenovos) == [(0, "HDMI-1"), (1, "HDMI-2")]


def test_one_twin_missing_says_which_one_cannot_be_told(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.display.set(reply([mon("HDMI-2", LENOVO), mon("HDMI-3", ROG)]))
    result = ceb.apply("desk")
    assert result.refused
    msg = " ".join(result.summary_lines())
    assert "twin 2 of 2" in msg and "cannot tell which" in msg


def test_allow_missing_applies_the_rest_and_reports(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.display.set(reply([mon("HDMI-1", LENOVO), mon("HDMI-3", ROG)]))   # right Lenovo unplugged
    result = ceb.apply("desk", allow_missing=True)
    assert result.applied_display and not result.ok and not result.refused
    assert sorted(layout(ceb)) == [("HDMI-1", M60, 0, 0, False), ("HDMI-3", M60, 1920, 0, True)]
    assert [p.kind for p in result.problems] == ["missing_screen"]


def test_allow_missing_moves_primary_and_origin(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    # Left Lenovo and ROG gone: the right Lenovo alone, made primary and moved to (0, 0).
    ceb.display.set(reply([mon("HDMI-5", LENOVO)]))
    result = ceb.apply("desk", allow_missing=True)
    assert layout(ceb) == [("HDMI-5", M60, 0, 0, True)]
    assert any("primary" in n for n in result.notes)


def test_nothing_left_to_show_never_applies(make_ceb):
    ceb = projector_profile(make_ceb)
    ceb.display.set(desk(1))
    result = ceb.apply("projector", allow_missing=True)
    assert ceb.display.applied() is None and ceb.display.calls == []
    assert any(p.kind == "no_screens" for p in result.problems)


def test_layout_mode_mismatch_blocks_even_with_allow_missing(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    r = desk(2)
    ceb.display.set((r[0], r[1], r[2], {"layout-mode": 2, "supports-changing-layout-mode": False}))
    for dry_run in (True, False):
        result = ceb.apply("desk", allow_missing=True, dry_run=dry_run)
        assert not result.goes_ahead and result.refused
        assert ceb.display.calls == []
        assert [p.kind for p in result.problems] == ["layout_mode"]


def test_missing_mode_refuses(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    no60 = reply([mon("HDMI-1", LENOVO, current="1920x1080@74.986"),
                  mon("HDMI-2", LENOVO), mon("HDMI-3", ROG)])
    # drop the 60 Hz mode from the first Lenovo
    no60[1][0] = (no60[1][0][0], [m for m in no60[1][0][1] if m[0] != M60], no60[1][0][2])
    ceb.display.set(no60)
    result = ceb.apply("desk")
    assert result.refused and result.problems[0].kind == "missing_mode"


# ------------------------------------------------------------ audio


def test_audio_is_saved_by_name_and_applied(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    p = ceb.profile("desk")
    assert (p.audio_output.name, p.audio_input.name) == (UNITEK_OUT.name, WEBCAM_IN.name)
    ceb.audio.state.default_output, ceb.audio.state.default_input = HDMI_OUT.name, UNITEK_IN.name
    assert ceb.apply("desk").ok
    assert ceb.audio.set_calls == [("output", UNITEK_OUT.name), ("input", WEBCAM_IN.name)]


def test_missing_audio_output_refuses_everything(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.audio.state.outputs = [HDMI_OUT]  # the USB adapter is unplugged
    result = ceb.apply("desk")
    assert result.refused
    assert ceb.display.applied() is None and ceb.audio.set_calls == []
    assert "Unitek" in result.summary_lines()[0] and "not present" in result.summary_lines()[0]


def test_missing_audio_input_with_allow_missing(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.audio.state.inputs = [UNITEK_IN]  # webcam unplugged
    result = ceb.apply("desk", allow_missing=True)
    assert result.applied_display and result.applied_output and not result.applied_input
    assert ceb.audio.set_calls == [("output", UNITEK_OUT.name)]
    assert [p.kind for p in result.problems] == ["missing_input"]


def test_audio_devices_are_remembered_when_gone(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.current()
    ceb.audio.state.inputs = [UNITEK_IN]
    ceb.current()
    assert WEBCAM_IN.name in [a.name for a in ceb.known().inputs]


def test_audio_server_down_refuses(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.audio.down = True
    result = ceb.apply("desk")
    assert not result.ok and ceb.display.applied() is None
    assert "audio server" in result.errors[0]


def test_profile_without_audio_never_touches_audio(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("screens", include_audio=False)
    ceb.audio.down = True
    snapshots = ceb.audio.snapshots
    assert ceb.apply("screens").ok
    assert ceb.audio.snapshots == snapshots and ceb.audio.set_calls == []


def test_audio_set_failure_is_reported(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")

    def boom(name):
        raise AudioError("could not set the default sink: lost connection")
    ceb.audio.set_default_output = boom
    result = ceb.apply("desk")
    assert result.applied_display and not result.ok
    assert "lost connection" in result.errors[0]


# ------------------------------------------------------------ dry run and GNOME errors


def test_dry_run_only_verifies(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    result = ceb.apply("desk", dry_run=True)
    assert result.ok and not result.applied_display
    assert [c[0] for c in ceb.display.calls] == [METHOD_VERIFY]
    assert ceb.audio.set_calls == []


def test_gnome_refusal_stops_before_audio(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.display.fail_with[METHOD_VERIFY] = DisplayError("Logical monitors not adjacent")
    result = ceb.apply("desk")
    assert not result.applied_display and ceb.audio.set_calls == []
    assert "not adjacent" in result.errors[0]


def test_stale_serial_rereads_once(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.display.fail_with[METHOD_TEMPORARY] = StaleSerialError("stale")
    reads = ceb.display.reads
    ceb.display.set(desk(1))
    result = ceb.apply("desk")
    assert result.ok and result.applied_display
    assert ceb.display.reads == reads + 2


def test_read_failure_is_reported(make_ceb):
    ceb = make_ceb(DisplayError("org.gnome.Mutter.DisplayConfig is not running"))
    ceb.put_profile(__import__("ceb_profiles.model", fromlist=["Profile"]).Profile(
        "x", __import__("ceb_profiles.model", fromlist=["DisplayLayout"]).DisplayLayout([])))
    result = ceb.apply("x")
    assert "could not read the screens" in result.errors[0]


# ------------------------------------------------------------ profiles


def test_save_refuses_to_overwrite_without_force(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    with pytest.raises(CebError):
        ceb.save_current("desk")
    ceb.save_current("desk", overwrite=True)
    assert [p.name for p in ceb.profiles()] == ["desk"]


def test_unknown_profile(make_ceb):
    with pytest.raises(CebError):
        make_ceb(desk(1)).apply("nope")


def test_delete(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.delete_profile("desk")
    assert ceb.profiles() == []
    with pytest.raises(CebError):
        ceb.delete_profile("desk")


def test_audio_down_still_lets_screens_be_listed(make_ceb):
    ceb = make_ceb(desk(1), audio=FakeAudio())
    ceb.audio.down = True
    state, audio, errors = ceb.current()
    assert len(state.monitors) == 3 and audio is None and errors


# ------------------------------------------------------------ history is best-effort


def test_history_write_failure_does_not_block_apply(make_ceb, monkeypatch):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")

    def full(known):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(ceb.store, "save_known", full)
    ceb.display.set(desk(2, extra=[mon("DP-2", PROJECTOR)]))   # a new screen: history must change
    result = ceb.apply("desk")
    assert result.ok and result.applied_display
    assert any("history not updated" in n and "No space" in n for n in result.notes)


def test_read_only_config_folder_does_not_block_apply(make_ceb, monkeypatch):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    ceb.apply("desk")                       # creates the fallback lock file while writable
    ceb.display.set(desk(2, extra=[mon("DP-2", PROJECTOR)]))
    ceb.store.dir.chmod(0o500)
    try:
        result = ceb.apply("desk")
    finally:
        ceb.store.dir.chmod(0o700)
    assert result.applied_display and not result.errors
    assert any("history not updated" in n for n in result.notes)


def test_corrupt_history_does_not_block_listing(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.store.dir.mkdir(parents=True)
    ceb.store.known_path.write_text("{broken")
    state, audio, errors = ceb.current()
    assert len(state.monitors) == 3 and errors == []
    assert "not valid JSON" in ceb.last_history_error
    assert ceb.store.known_path.read_text() == "{broken"


# ------------------------------------------------------------ the apply lock


def test_apply_lock_without_runtime_dir_stays_in_the_config_folder(make_ceb, monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    assert ceb.apply("desk").ok
    assert (ceb.store.dir / ".apply.lock").is_file()


@pytest.mark.parametrize("runtime", [True, False])
def test_apply_lock_symlink_is_refused_and_not_followed(make_ceb, monkeypatch, tmp_path, private_dirs, runtime):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    if runtime:
        lock = private_dirs / "ceb-apply.lock"
    else:
        monkeypatch.delenv("XDG_RUNTIME_DIR")
        lock = ceb.store.dir / ".apply.lock"
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    lock.symlink_to(victim)
    result = ceb.apply("desk")
    assert not result.ok and "symlink" in result.errors[0]
    assert ceb.display.calls == [] and victim.read_text() == "keep me"


def test_apply_lock_owned_by_someone_else_is_refused(make_ceb, monkeypatch, private_dirs):
    import os
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    (private_dirs / "ceb-apply.lock").write_text("theirs")
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    result = ceb.apply("desk")
    assert "not a regular file owned by you" in result.errors[0] and ceb.display.calls == []
    assert (private_dirs / "ceb-apply.lock").read_text() == "theirs"


def test_apply_lock_is_not_truncated(make_ceb, private_dirs):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    (private_dirs / "ceb-apply.lock").write_text("x")
    assert ceb.apply("desk").ok
    assert (private_dirs / "ceb-apply.lock").read_text() == "x"


# ------------------------------------------------------------ stale serial retry


def test_stale_retry_does_not_repeat_errors(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.audio.down = True                      # an error on each pass, allowed by allow_missing
    ceb.display.fail_with[METHOD_TEMPORARY] = StaleSerialError("stale")
    result = ceb.apply("desk", allow_missing=True)
    assert result.applied_display
    assert len(result.errors) == 1 and "audio server" in result.errors[0]


# ------------------------------------------------------------ display audio that appears late


def tv_profile(make_ceb):
    """A profile with only the TV on and its HDMI audio; saved while both were present."""
    ceb = make_ceb(reply([mon("DP-2", PROJECTOR), mon("HDMI-1", LENOVO)], [(0, 0, True, ["DP-2"])]),
                   audio=FakeAudio(default_output=HDMI_OUT.name))
    ceb.save_current("tv")
    ceb.display.calls.clear()
    ceb.audio.set_calls.clear()
    # Now the TV is connected but off, and its HDMI audio output is gone.
    ceb.display.set(reply([mon("DP-2", PROJECTOR), mon("HDMI-1", LENOVO)], [(0, 0, True, ["HDMI-1"])]))
    ceb.audio.state.outputs = [UNITEK_OUT]
    ceb.audio.state.default_output = UNITEK_OUT.name
    return ceb


def test_hdmi_audio_that_appears_after_the_screen_turns_on_is_used(make_ceb):
    ceb = tv_profile(make_ceb)

    def appear(n):
        if ceb.display.applied() and n >= 3:   # a little after the screens changed
            ceb.audio.state.outputs = [UNITEK_OUT, HDMI_OUT]
    ceb.audio.on_snapshot = appear
    result = ceb.apply("tv")
    assert result.ok, result.summary_lines()
    assert result.applied_display and ("output", HDMI_OUT.name) in ceb.audio.set_calls
    assert any("may appear" in n for n in result.notes)


def test_hdmi_audio_that_never_appears_is_reported_not_replaced(make_ceb):
    ceb = tv_profile(make_ceb)
    clock = iter(range(0, 1000))
    ceb._clock = lambda: next(clock)
    result = ceb.apply("tv")
    assert result.applied_display and not result.applied_output
    assert [c for c in ceb.audio.set_calls if c[0] == "output"] == []
    assert [p.kind for p in result.problems] == ["missing_output"]
    assert "waited" in result.problems[0].message


def test_missing_non_display_audio_still_refuses_before_any_change(make_ceb):
    ceb = make_ceb(reply([mon("DP-2", PROJECTOR), mon("HDMI-1", LENOVO)], [(0, 0, True, ["DP-2"])]))
    ceb.save_current("tv")                       # output: the USB adapter
    ceb.display.calls.clear()
    ceb.display.set(reply([mon("DP-2", PROJECTOR), mon("HDMI-1", LENOVO)], [(0, 0, True, ["HDMI-1"])]))
    ceb.audio.state.outputs = [HDMI_OUT]
    result = ceb.apply("tv")
    assert result.refused and ceb.display.calls == []


def test_hdmi_audio_missing_while_screens_stay_on_refuses(make_ceb):
    ceb = make_ceb(desk(1), audio=FakeAudio(default_output=HDMI_OUT.name))
    ceb.save_current("desk")
    ceb.display.calls.clear()
    ceb.audio.state.outputs = [UNITEK_OUT]       # nothing is turned on, so it will not appear
    result = ceb.apply("desk")
    assert result.refused and ceb.display.calls == []


def _names(ceb):
    return [p.name for p in ceb.profiles()]


def test_rename_keeps_the_position_and_writes_once(make_ceb, monkeypatch):
    ceb = make_ceb(desk(1))
    for name in ("a", "b", "c"):
        ceb.save_current(name)
    writes = []
    real = ceb.store.save_profiles
    monkeypatch.setattr(ceb.store, "save_profiles", lambda ps: (writes.append(1), real(ps)))
    ceb.rename_profile("b", "bee")
    assert _names(ceb) == ["a", "bee", "c"] and writes == [1]
    with pytest.raises(CebError):
        ceb.rename_profile("a", "c")
    with pytest.raises(CebError):
        ceb.rename_profile("nope", "x")
    assert _names(ceb) == ["a", "bee", "c"]


def test_put_profile_with_expected_refuses_a_newer_stored_version(make_ceb):
    import copy
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    shown = ceb.profile("desk")
    before = copy.deepcopy(shown)
    ceb.display.set(desk(2))
    ceb.save_current("desk", overwrite=True)          # `ceb save-current desk --force` meanwhile
    newer = ceb.profile("desk").to_json()
    shown.audio_output = None
    with pytest.raises(ProfileChanged):
        ceb.put_profile(shown, expected=before)
    assert ceb.profile("desk").to_json() == newer
    fresh = ceb.profile("desk")
    fresh_before = copy.deepcopy(fresh)
    fresh.audio_output = None
    ceb.put_profile(fresh, expected=fresh_before)
    assert ceb.profile("desk").audio_output is None


def test_forget_removes_a_screen_or_audio_device_from_the_history(make_ceb):
    ceb = make_ceb(reply([mon("HDMI-1", PROJECTOR)]))
    ceb.current()
    proj = ceb.known().screens[0].screen
    ceb.display.set(desk(1))
    ceb.current()
    assert proj in [s.screen for s in ceb.known().screens]
    assert ceb.forget_screen(proj) is True
    assert proj not in [s.screen for s in ceb.known().screens]
    assert ceb.forget_screen(proj) is False
    out = ceb.known().outputs[0].name
    assert ceb.forget_audio("output", out) is True
    assert out not in [a.name for a in ceb.known().outputs]
    assert ceb.known().inputs  # inputs untouched
    with pytest.raises(ValueError):
        ceb.forget_audio("speaker", out)


def test_identity_describe_does_not_repeat_the_vendor():
    from ceb_profiles.model import Identity
    assert Identity.of("LEN", "LEN LI2364d", "0x01010101").describe() == "LEN LI2364d, serial 0x01010101"
    assert Identity.of("AUS", "ROG PG248Q", "L8LMQS075392").describe() == "AUS ROG PG248Q, serial L8LMQS075392"
    assert Identity.of("STK", "", "").describe() == "STK, serial (none)"


def test_missing_screen_message_uses_the_remembered_name(make_ceb):
    from conftest import PROJECTOR, mon, reply
    ceb = make_ceb(reply([mon("DP-1", PROJECTOR)]))
    ceb.save_current("tv")
    ceb.display.set(desk(1))
    result = ceb.apply("tv")
    message = next(p.message for p in result.problems if p.kind == "missing_screen")
    assert message.startswith("S2-TEK TV is not connected")
    assert "serial" not in message and "STK" not in message
