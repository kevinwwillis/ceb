import pytest

from conftest import (HDMI_OUT, LENOVO, PROJECTOR, ROG, UNITEK_IN, UNITEK_OUT, WEBCAM_IN, FakeAudio,
                      desk, mon, reply)
from pam_profiles.audio import AudioError
from pam_profiles.core import PamError
from pam_profiles.mutter import METHOD_TEMPORARY, METHOD_VERIFY, DisplayError, StaleSerialError

M60 = "1920x1080@60.000"


def layout(pam):
    return pam.display.applied()


# ------------------------------------------------------------ port numberings and twins


@pytest.mark.parametrize("saved_base,applied_base", [(1, 2), (2, 1), (1, 1), (2, 2)])
@pytest.mark.parametrize("shuffle", [False, True])
def test_same_screens_under_both_port_numberings(make_pam, saved_base, applied_base, shuffle):
    pam = make_pam(desk(saved_base))
    pam.save_current("desk")
    pam.display.set(desk(applied_base, shuffle=shuffle))
    result = pam.apply("desk")
    assert result.ok, result.summary_lines()
    b = applied_base
    assert sorted(layout(pam)) == sorted([
        (f"HDMI-{b}", M60, 0, 0, False),          # left Lenovo = lower port
        (f"HDMI-{b + 2}", M60, 1920, 0, True),    # ROG, primary
        (f"HDMI-{b + 1}", M60, 3840, 0, False),   # right Lenovo = higher port
    ])


def test_twins_keep_their_sides_when_the_profile_swaps_them(make_pam):
    # Saved with the lower-port Lenovo on the RIGHT; must stay on the right after renumbering.
    r = reply([mon("HDMI-1", LENOVO), mon("HDMI-2", LENOVO), mon("HDMI-3", ROG)],
              [(0, 0, False, ["HDMI-2"]), (1920, 0, True, ["HDMI-3"]), (3840, 0, False, ["HDMI-1"])])
    pam = make_pam(r)
    pam.save_current("swapped")
    pam.display.set(desk(2))
    assert pam.apply("swapped").ok
    assert ("HDMI-2", M60, 3840, 0, False) in layout(pam)
    assert ("HDMI-3", M60, 0, 0, False) in layout(pam)


def test_twins_on_different_connector_types(make_pam):
    # One twin on DP, one on HDMI; only the HDMI numbers shift (HDMI-1 -> HDMI-3). Comparing numbers
    # across types would swap them (1 < 2, then 2 < 3); type first (DP before HDMI) does not.
    r = reply([mon("HDMI-1", LENOVO), mon("DP-2", LENOVO)],
              [(0, 0, True, ["DP-2"]), (1920, 0, False, ["HDMI-1"])])
    pam = make_pam(r)
    pam.save_current("mixed")
    pam.display.set(reply([mon("HDMI-3", LENOVO), mon("DP-2", LENOVO)]))
    assert pam.apply("mixed").ok
    assert sorted(layout(pam)) == [("DP-2", M60, 0, 0, True), ("HDMI-3", M60, 1920, 0, False)]


def test_twins_on_two_gpus_follow_the_gpu_when_cards_swap_numbers(make_pam):
    r = reply([mon("HDMI-1", LENOVO), mon("HDMI-4", LENOVO)],
              [(0, 0, True, ["HDMI-1"]), (1920, 0, False, ["HDMI-4"])])
    pam = make_pam(r, gpus={"HDMI-1": "0000:2d:00.0", "HDMI-4": "0000:2e:00.0"})
    pam.save_current("two-gpus")
    # Next boot the second card loads first: its port is now HDMI-1, the first card's is HDMI-2.
    pam.display.set(reply([mon("HDMI-1", LENOVO), mon("HDMI-2", LENOVO)]))
    pam.display.gpus = {"HDMI-1": "0000:2e:00.0", "HDMI-2": "0000:2d:00.0"}
    assert pam.apply("two-gpus").ok
    assert sorted(layout(pam)) == [("HDMI-1", M60, 1920, 0, False), ("HDMI-2", M60, 0, 0, True)]


def test_screens_not_in_the_profile_are_turned_off(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    pam.display.set(desk(2, extra=[mon("DP-2", PROJECTOR)]))
    result = pam.apply("desk")
    assert result.ok
    assert "DP-2" not in [c[0] for c in layout(pam)]
    assert [m.connector for m in result.display_plan.turned_off] == ["DP-2"]


def test_only_verify_and_temporary_methods_are_used(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    pam.apply("desk")
    assert [c[0] for c in pam.display.calls] == [0, 1]  # verify, then temporary; never 2


# ------------------------------------------------------------ unplugged screens


def projector_profile(make_pam):
    pam = make_pam(reply([mon("DP-2", PROJECTOR), mon("HDMI-1", LENOVO), mon("HDMI-2", LENOVO),
                          mon("HDMI-3", ROG)], [(0, 0, True, ["DP-2"])]))
    pam.save_current("projector")
    return pam


def test_unplugged_screen_refuses_and_changes_nothing(make_pam):
    pam = projector_profile(make_pam)
    pam.audio.set_calls.clear()
    pam.display.set(desk(1))
    result = pam.apply("projector")
    assert not result.ok and result.refused
    assert pam.display.applied() is None and pam.audio.set_calls == []
    assert any("S2-TEK TV" in line and "not connected" in line for line in result.summary_lines())


def test_unplugged_screen_stays_in_the_history(make_pam):
    pam = projector_profile(make_pam)
    pam.display.set(desk(1))
    pam.current()
    known = pam.known()
    assert {s.screen.identity.vendor for s in known.screens} == {"STK", "LEN", "AUS"}
    stk = next(s for s in known.screens if s.screen.identity.vendor == "STK")
    assert stk.last_connector == "DP-2" and stk.display_name == "S2-TEK TV"
    assert [(m.width, m.height) for m in stk.modes][:2] == [(1920, 1080), (1920, 1080)]


def test_twins_get_one_history_entry_each(make_pam):
    pam = make_pam(desk(1))
    pam.current()
    lenovos = pam.known().twins_of(pam.display.get_state().by_connector("HDMI-1").identity)
    assert sorted((s.screen.twin, s.last_connector) for s in lenovos) == [(0, "HDMI-1"), (1, "HDMI-2")]


def test_one_twin_missing_says_which_one_cannot_be_told(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    pam.display.set(reply([mon("HDMI-2", LENOVO), mon("HDMI-3", ROG)]))
    result = pam.apply("desk")
    assert result.refused
    msg = " ".join(result.summary_lines())
    assert "twin 2 of 2" in msg and "cannot tell which" in msg


def test_allow_missing_applies_the_rest_and_reports(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    pam.display.set(reply([mon("HDMI-1", LENOVO), mon("HDMI-3", ROG)]))   # right Lenovo unplugged
    result = pam.apply("desk", allow_missing=True)
    assert result.applied_display and not result.ok and not result.refused
    assert sorted(layout(pam)) == [("HDMI-1", M60, 0, 0, False), ("HDMI-3", M60, 1920, 0, True)]
    assert [p.kind for p in result.problems] == ["missing_screen"]


def test_allow_missing_moves_primary_and_origin(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    # Left Lenovo and ROG gone: the right Lenovo alone, made primary and moved to (0, 0).
    pam.display.set(reply([mon("HDMI-5", LENOVO)]))
    result = pam.apply("desk", allow_missing=True)
    assert layout(pam) == [("HDMI-5", M60, 0, 0, True)]
    assert any("primary" in n for n in result.notes)


def test_nothing_left_to_show_never_applies(make_pam):
    pam = projector_profile(make_pam)
    pam.display.set(desk(1))
    result = pam.apply("projector", allow_missing=True)
    assert pam.display.applied() is None and pam.display.calls == []
    assert any(p.kind == "no_screens" for p in result.problems)


def test_layout_mode_mismatch_blocks_even_with_allow_missing(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    r = desk(2)
    pam.display.set((r[0], r[1], r[2], {"layout-mode": 2, "supports-changing-layout-mode": False}))
    for dry_run in (True, False):
        result = pam.apply("desk", allow_missing=True, dry_run=dry_run)
        assert not result.goes_ahead and result.refused
        assert pam.display.calls == []
        assert [p.kind for p in result.problems] == ["layout_mode"]


def test_missing_mode_refuses(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    no60 = reply([mon("HDMI-1", LENOVO, current="1920x1080@74.986"),
                  mon("HDMI-2", LENOVO), mon("HDMI-3", ROG)])
    # drop the 60 Hz mode from the first Lenovo
    no60[1][0] = (no60[1][0][0], [m for m in no60[1][0][1] if m[0] != M60], no60[1][0][2])
    pam.display.set(no60)
    result = pam.apply("desk")
    assert result.refused and result.problems[0].kind == "missing_mode"


# ------------------------------------------------------------ audio


def test_audio_is_saved_by_name_and_applied(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    p = pam.profile("desk")
    assert (p.audio_output.name, p.audio_input.name) == (UNITEK_OUT.name, WEBCAM_IN.name)
    pam.audio.state.default_output, pam.audio.state.default_input = HDMI_OUT.name, UNITEK_IN.name
    assert pam.apply("desk").ok
    assert pam.audio.set_calls == [("output", UNITEK_OUT.name), ("input", WEBCAM_IN.name)]


def test_missing_audio_output_refuses_everything(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    pam.audio.state.outputs = [HDMI_OUT]  # the USB adapter is unplugged
    result = pam.apply("desk")
    assert result.refused
    assert pam.display.applied() is None and pam.audio.set_calls == []
    assert "Unitek" in result.summary_lines()[0] and "not present" in result.summary_lines()[0]


def test_missing_audio_input_with_allow_missing(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    pam.audio.state.inputs = [UNITEK_IN]  # webcam unplugged
    result = pam.apply("desk", allow_missing=True)
    assert result.applied_display and result.applied_output and not result.applied_input
    assert pam.audio.set_calls == [("output", UNITEK_OUT.name)]
    assert [p.kind for p in result.problems] == ["missing_input"]


def test_audio_devices_are_remembered_when_gone(make_pam):
    pam = make_pam(desk(1))
    pam.current()
    pam.audio.state.inputs = [UNITEK_IN]
    pam.current()
    assert WEBCAM_IN.name in [a.name for a in pam.known().inputs]


def test_audio_server_down_refuses(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    pam.audio.down = True
    result = pam.apply("desk")
    assert not result.ok and pam.display.applied() is None
    assert "audio server" in result.errors[0]


def test_profile_without_audio_never_touches_audio(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("screens", include_audio=False)
    pam.audio.down = True
    snapshots = pam.audio.snapshots
    assert pam.apply("screens").ok
    assert pam.audio.snapshots == snapshots and pam.audio.set_calls == []


def test_audio_set_failure_is_reported(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")

    def boom(name):
        raise AudioError("could not set the default sink: lost connection")
    pam.audio.set_default_output = boom
    result = pam.apply("desk")
    assert result.applied_display and not result.ok
    assert "lost connection" in result.errors[0]


# ------------------------------------------------------------ dry run and GNOME errors


def test_dry_run_only_verifies(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    result = pam.apply("desk", dry_run=True)
    assert result.ok and not result.applied_display
    assert [c[0] for c in pam.display.calls] == [METHOD_VERIFY]
    assert pam.audio.set_calls == []


def test_gnome_refusal_stops_before_audio(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    pam.display.fail_with[METHOD_VERIFY] = DisplayError("Logical monitors not adjacent")
    result = pam.apply("desk")
    assert not result.applied_display and pam.audio.set_calls == []
    assert "not adjacent" in result.errors[0]


def test_stale_serial_rereads_once(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    pam.display.fail_with[METHOD_TEMPORARY] = StaleSerialError("stale")
    reads = pam.display.reads
    pam.display.set(desk(1))
    result = pam.apply("desk")
    assert result.ok and result.applied_display
    assert pam.display.reads == reads + 2


def test_read_failure_is_reported(make_pam):
    pam = make_pam(DisplayError("org.gnome.Mutter.DisplayConfig is not running"))
    pam.put_profile(__import__("pam_profiles.model", fromlist=["Profile"]).Profile(
        "x", __import__("pam_profiles.model", fromlist=["DisplayLayout"]).DisplayLayout([])))
    result = pam.apply("x")
    assert "could not read the screens" in result.errors[0]


# ------------------------------------------------------------ profiles


def test_save_refuses_to_overwrite_without_force(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    with pytest.raises(PamError):
        pam.save_current("desk")
    pam.save_current("desk", overwrite=True)
    assert [p.name for p in pam.profiles()] == ["desk"]


def test_unknown_profile(make_pam):
    with pytest.raises(PamError):
        make_pam(desk(1)).apply("nope")


def test_delete(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    pam.delete_profile("desk")
    assert pam.profiles() == []
    with pytest.raises(PamError):
        pam.delete_profile("desk")


def test_audio_down_still_lets_screens_be_listed(make_pam):
    pam = make_pam(desk(1), audio=FakeAudio())
    pam.audio.down = True
    state, audio, errors = pam.current()
    assert len(state.monitors) == 3 and audio is None and errors


# ------------------------------------------------------------ history is best-effort


def test_history_write_failure_does_not_block_apply(make_pam, monkeypatch):
    pam = make_pam(desk(1))
    pam.save_current("desk")

    def full(known):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pam.store, "save_known", full)
    pam.display.set(desk(2, extra=[mon("DP-2", PROJECTOR)]))   # a new screen: history must change
    result = pam.apply("desk")
    assert result.ok and result.applied_display
    assert any("history not updated" in n and "No space" in n for n in result.notes)


def test_read_only_config_folder_does_not_block_apply(make_pam, monkeypatch):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    pam.apply("desk")                       # creates the fallback lock file while writable
    pam.display.set(desk(2, extra=[mon("DP-2", PROJECTOR)]))
    pam.store.dir.chmod(0o500)
    try:
        result = pam.apply("desk")
    finally:
        pam.store.dir.chmod(0o700)
    assert result.applied_display and not result.errors
    assert any("history not updated" in n for n in result.notes)


def test_corrupt_history_does_not_block_listing(make_pam):
    pam = make_pam(desk(1))
    pam.store.dir.mkdir(parents=True)
    pam.store.known_path.write_text("{broken")
    state, audio, errors = pam.current()
    assert len(state.monitors) == 3 and errors == []
    assert "not valid JSON" in pam.last_history_error
    assert pam.store.known_path.read_text() == "{broken"


# ------------------------------------------------------------ the apply lock


def test_apply_lock_without_runtime_dir_stays_in_the_config_folder(make_pam, monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    pam = make_pam(desk(1))
    pam.save_current("desk")
    assert pam.apply("desk").ok
    assert (pam.store.dir / ".apply.lock").is_file()


@pytest.mark.parametrize("runtime", [True, False])
def test_apply_lock_symlink_is_refused_and_not_followed(make_pam, monkeypatch, tmp_path, private_dirs, runtime):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    if runtime:
        lock = private_dirs / "pam-apply.lock"
    else:
        monkeypatch.delenv("XDG_RUNTIME_DIR")
        lock = pam.store.dir / ".apply.lock"
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    lock.symlink_to(victim)
    result = pam.apply("desk")
    assert not result.ok and "symlink" in result.errors[0]
    assert pam.display.calls == [] and victim.read_text() == "keep me"


def test_apply_lock_owned_by_someone_else_is_refused(make_pam, monkeypatch, private_dirs):
    import os
    pam = make_pam(desk(1))
    pam.save_current("desk")
    (private_dirs / "pam-apply.lock").write_text("theirs")
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    result = pam.apply("desk")
    assert "not a regular file owned by you" in result.errors[0] and pam.display.calls == []
    assert (private_dirs / "pam-apply.lock").read_text() == "theirs"


def test_apply_lock_is_not_truncated(make_pam, private_dirs):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    (private_dirs / "pam-apply.lock").write_text("x")
    assert pam.apply("desk").ok
    assert (private_dirs / "pam-apply.lock").read_text() == "x"


# ------------------------------------------------------------ stale serial retry


def test_stale_retry_does_not_repeat_errors(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    pam.audio.down = True                      # an error on each pass, allowed by allow_missing
    pam.display.fail_with[METHOD_TEMPORARY] = StaleSerialError("stale")
    result = pam.apply("desk", allow_missing=True)
    assert result.applied_display
    assert len(result.errors) == 1 and "audio server" in result.errors[0]


# ------------------------------------------------------------ display audio that appears late


def tv_profile(make_pam):
    """A profile with only the TV on and its HDMI audio; saved while both were present."""
    pam = make_pam(reply([mon("DP-2", PROJECTOR), mon("HDMI-1", LENOVO)], [(0, 0, True, ["DP-2"])]),
                   audio=FakeAudio(default_output=HDMI_OUT.name))
    pam.save_current("tv")
    pam.display.calls.clear()
    pam.audio.set_calls.clear()
    # Now the TV is connected but off, and its HDMI audio output is gone.
    pam.display.set(reply([mon("DP-2", PROJECTOR), mon("HDMI-1", LENOVO)], [(0, 0, True, ["HDMI-1"])]))
    pam.audio.state.outputs = [UNITEK_OUT]
    pam.audio.state.default_output = UNITEK_OUT.name
    return pam


def test_hdmi_audio_that_appears_after_the_screen_turns_on_is_used(make_pam):
    pam = tv_profile(make_pam)

    def appear(n):
        if pam.display.applied() and n >= 3:   # a little after the screens changed
            pam.audio.state.outputs = [UNITEK_OUT, HDMI_OUT]
    pam.audio.on_snapshot = appear
    result = pam.apply("tv")
    assert result.ok, result.summary_lines()
    assert result.applied_display and ("output", HDMI_OUT.name) in pam.audio.set_calls
    assert any("may appear" in n for n in result.notes)


def test_hdmi_audio_that_never_appears_is_reported_not_replaced(make_pam):
    pam = tv_profile(make_pam)
    clock = iter(range(0, 1000))
    pam._clock = lambda: next(clock)
    result = pam.apply("tv")
    assert result.applied_display and not result.applied_output
    assert [c for c in pam.audio.set_calls if c[0] == "output"] == []
    assert [p.kind for p in result.problems] == ["missing_output"]
    assert "waited" in result.problems[0].message


def test_missing_non_display_audio_still_refuses_before_any_change(make_pam):
    pam = make_pam(reply([mon("DP-2", PROJECTOR), mon("HDMI-1", LENOVO)], [(0, 0, True, ["DP-2"])]))
    pam.save_current("tv")                       # output: the USB adapter
    pam.display.calls.clear()
    pam.display.set(reply([mon("DP-2", PROJECTOR), mon("HDMI-1", LENOVO)], [(0, 0, True, ["HDMI-1"])]))
    pam.audio.state.outputs = [HDMI_OUT]
    result = pam.apply("tv")
    assert result.refused and pam.display.calls == []


def test_hdmi_audio_missing_while_screens_stay_on_refuses(make_pam):
    pam = make_pam(desk(1), audio=FakeAudio(default_output=HDMI_OUT.name))
    pam.save_current("desk")
    pam.display.calls.clear()
    pam.audio.state.outputs = [UNITEK_OUT]       # nothing is turned on, so it will not appear
    result = pam.apply("desk")
    assert result.refused and pam.display.calls == []
