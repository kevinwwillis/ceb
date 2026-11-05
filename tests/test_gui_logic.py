"""The GUI's pure logic: snapping, settling a dropped screen, editing a profile, screen names."""
from conftest import LENOVO, PROJECTOR, ROG, desk, mon, reply
from ceb_profiles.gui import editor
from ceb_profiles.gui.geometry import Rect, adjacent, close_gap, is_valid, nudge, settle, snap
from ceb_profiles.model import Identity, ModeSpec

HD = (1920, 1080)


def r(x, y, w=1920, h=1080):
    return Rect(x, y, w, h)


# ------------------------------------------------------------ geometry


def test_adjacent_needs_a_shared_edge_not_a_corner():
    assert adjacent(r(0, 0), r(1920, 0))
    assert adjacent(r(0, 0), r(1920, 1079))
    assert not adjacent(r(0, 0), r(1920, 1080))   # corner only
    assert not adjacent(r(0, 0), r(1921, 0))      # 1 px gap


def test_is_valid_rejects_overlap_and_islands():
    assert is_valid([r(0, 0), r(1920, 0), r(3840, 0)])
    assert not is_valid([r(0, 0), r(1900, 0)])
    assert not is_valid([r(0, 0), r(1920, 0), r(5760, 0)])  # third one floats
    assert is_valid([r(0, 0)])
    assert is_valid([])


def test_snap_pulls_to_neighbour_edges():
    others = [r(0, 0)]
    assert snap(1950, 40, *HD, others) == (1920, 0)     # left edge to right edge, top to top
    assert snap(-1900, 1100, *HD, others) == (-1920, 1080)  # right edge to left edge, top to bottom
    assert snap(2400, 600, *HD, others) == (2400, 600)  # too far: unchanged


def test_snap_aligns_bottom_edges_and_right_edges():
    tall = [r(0, 0, 1920, 1200)]
    assert snap(1920, 150, *HD, tall) == (1920, 120)        # bottom to bottom (1200)
    wide = [r(0, 0, 2560, 1440)]
    assert snap(700, 1440, *HD, wide) == (640, 1440)       # right to right (2560)


def test_snap_takes_the_nearest_edge_pair():
    # 15 px from left-aligned, 5 px from right-aligned: right-aligned wins.
    assert snap(15, 0, 1900, 1080, [r(0, 1080)]) == (20, 0)


def test_nudge_moves_by_steps_and_stays_valid():
    rects = [r(0, 0), r(1920, 0)]
    down = nudge(rects, 1, 0, 1)
    assert down is not None and is_valid(down) and down[1].y == 200 and down[1].x == 1920
    assert nudge(rects, 1, 1, 0) is None              # away from its only neighbour: no place
    rects = [r(0, 0), r(1920, 0), r(3840, 0)]
    moved = nudge(rects, 0, 1, 0)                     # left screen moves right: past the others
    assert moved is not None and is_valid(moved) and moved[0].x > 0
    assert nudge([r(0, 0), r(1920, 900)], 1, 0, 1) is None  # further down only the corner touches


def test_settle_keeps_a_valid_drop():
    rects = [r(0, 0), r(1920, 300)]
    assert settle(rects, 1, r(1920, 0)) == (rects, True)


def test_settle_moves_an_island_to_touch_its_neighbour():
    rects = [r(0, 0), r(2500, 200)]
    new, as_dropped = settle(rects, 1, r(1920, 0))
    assert not as_dropped
    assert is_valid(new)
    assert new[1] == r(1920, 200)  # nearest: slid left, height kept


def test_settle_moves_out_of_an_overlap():
    rects = [r(0, 0), r(1920, 0), r(2000, 50)]
    new, _ = settle(rects, 2, r(3840, 0))
    assert is_valid(new)


def test_settle_falls_back_when_the_bridge_screen_is_moved_away():
    # A - B - C in a row; dragging B away leaves A and C apart wherever B goes, except back between them.
    rects = [r(0, 0), r(1920, 5000), r(3840, 0)]
    new, as_dropped = settle(rects, 1, r(1920, 0))
    assert is_valid(new)
    assert new[1] == r(1920, 0)


def test_close_gap_after_taking_out_the_middle_screen():
    assert close_gap([r(0, 0), r(3840, 0)], r(1920, 0)) == [r(0, 0), r(1920, 0)]
    assert close_gap([r(0, 0), r(0, 2160)], r(0, 1080)) == [r(0, 0), r(0, 1080)]


# ------------------------------------------------------------ editing a profile


def desk_profile(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    return ceb, ceb.profile("desk")


def test_remove_middle_screen_closes_the_gap_and_moves_primary(make_ceb):
    ceb, p = desk_profile(make_ceb)
    rog = editor.primary_screen(p)
    editor.remove(p, rog)
    assert len(p.displays.logical_monitors) == 2
    assert [(lm.x, lm.y) for lm in p.displays.logical_monitors] == [(0, 0), (1920, 0)]
    assert sum(lm.primary for lm in p.displays.logical_monitors) == 1
    ceb.put_profile(p)
    ceb.display.set(desk(2))
    assert ceb.apply("desk", dry_run=True).ok


def test_add_goes_right_of_the_rightmost_and_first_is_primary(make_ceb):
    ceb, p = desk_profile(make_ceb)
    proj = mon("DP-1", PROJECTOR)
    ceb.display.set(reply([*desk(1)[1], proj]))
    state = ceb.display.get_state()
    sid = state.by_connector("DP-1").screen_id
    mode, scale = editor.mode_for(sid, state, ceb.known())
    editor.add(p, sid, mode, scale, 1)
    lm = p.displays.logical_monitors[-1]
    assert (lm.x, lm.y, lm.primary) == (5760, 0, False)

    empty = type(p)("new")
    editor.add(empty, sid, mode, scale, 1)
    assert empty.displays.logical_monitors[0].primary
    assert (empty.displays.logical_monitors[0].x, empty.displays.logical_monitors[0].y) == (0, 0)


def test_mode_for_a_remembered_screen_comes_from_the_history(make_ceb):
    ceb = make_ceb(reply([mon("HDMI-1", ROG), mon("DP-1", PROJECTOR)]))
    ceb.current()
    ceb.display.set(reply([mon("HDMI-1", ROG)]))
    state = ceb.display.get_state()
    proj = next(k.screen for k in ceb.known().screens if k.screen.identity.vendor == "STK")
    assert editor.mode_for(proj, state, ceb.known()) == (ModeSpec(1920, 1080, 60.0), 1.0)


def test_move_shifts_the_layout_to_start_at_zero(make_ceb):
    ceb, p = desk_profile(make_ceb)
    left = p.displays.logical_monitors[0].screens[0].screen
    editor.move(p, left, -1920, 300)
    xs = sorted((lm.x, lm.y) for lm in p.displays.logical_monitors)
    assert min(x for x, _ in xs) == 0 and min(y for _, y in xs) == 0


def test_set_primary(make_ceb):
    ceb, p = desk_profile(make_ceb)
    left = p.displays.logical_monitors[0].screens[0].screen
    editor.set_primary(p, left)
    assert editor.primary_screen(p) == left
    assert sum(lm.primary for lm in p.displays.logical_monitors) == 1


def test_scaled_and_rotated_sizes():
    from ceb_profiles.model import ProfileLogicalMonitor, ProfileScreen, ScreenId
    sid = ScreenId(Identity.of("A", "B", "C"))
    lm = ProfileLogicalMonitor(0, 0, 2.0, 1, True, [ProfileScreen(sid, ModeSpec(3840, 2160, 60.0))])
    assert editor.lm_rect(lm, 1) == Rect(0, 0, 1080, 1920)   # logical: halved, turned
    assert editor.lm_rect(lm, 2) == Rect(0, 0, 2160, 3840)   # physical


# ------------------------------------------------------------ names


def test_screen_titles():
    assert editor.screen_title('Lenovo Group Limited 23"', Identity.of(*LENOVO[:3])) == "Lenovo LI2364d"
    assert editor.screen_title('ASUSTek COMPUTER INC 24"', Identity.of(*ROG[:3])) == "ASUSTek ROG PG248Q"
    assert editor.screen_title("S2-TEK TV", Identity.of(*PROJECTOR[:3])) == "S2-TEK TV"
    assert editor.screen_size('Lenovo Group Limited 23"') == '23"'


def test_twin_labels_say_which_port(make_ceb):
    ceb = make_ceb(desk(1))
    state, _, _ = ceb.current()
    lower = state.by_connector("HDMI-1").screen_id
    higher = state.by_connector("HDMI-2").screen_id
    assert "twin 1 of 2 (lower port)" in editor.label(lower, ceb.known(), state).detail
    assert "twin 2 of 2 (higher port)" in editor.label(higher, ceb.known(), state).detail
    rog = state.by_connector("HDMI-3").screen_id
    assert editor.label(rog, ceb.known(), state).detail == '24" · serial L8LMQS075392'
    assert editor.label(rog, ceb.known(), None).connected is False


def test_version_check_names_the_libadwaita_needed():
    from ceb_profiles.gui import version_problem
    assert version_problem((1, 8, 0)) is None and version_problem((1, 9, 1)) is None
    assert "1.8" in version_problem((1, 7, 4)) and "1.7.4" in version_problem((1, 7, 4))
    assert "gir1.2-adw-1" in version_problem(None)


def test_ceb_gui_exits_with_a_message_on_old_libadwaita(monkeypatch, capsys):
    import ceb_profiles.gui as gui
    monkeypatch.setattr(gui, "_adw_version", lambda: (1, 7, 0))
    assert gui.main([]) == 1  # before anything real is opened
    assert "libadwaita 1.8" in capsys.readouterr().err


def test_missing_pygobject_message_says_the_cli_needs_it_too():
    from ceb_profiles.gui import version_problem
    text = version_problem(None, have_gi=False)
    assert "works without" not in text
    assert "Both `ceb` and `ceb-gui` need PyGObject" in text
    assert "sudo apt install python3-gi" in text and "sudo dnf install python3-gobject" in text


def test_demo_removes_its_temp_folder_on_exit(tmp_path, monkeypatch):
    """`ceb-gui --demo` with GTK stubbed out: the folder it makes is gone when it returns."""
    import tempfile

    import ceb_profiles.gui as gui
    made = []
    real = tempfile.mkdtemp

    def mkdtemp(**kw):
        made.append(real(dir=tmp_path, **kw))
        return made[-1]
    monkeypatch.setattr(gui.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(gui, "_adw_version", lambda: (1, 8, 0))

    class App:
        def __init__(self, **kw):
            pass

        def connect(self, *a):
            pass

        def run(self, argv):
            import os
            assert os.path.isdir(made[0])
            return 0

        def quit(self):
            pass
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw
    monkeypatch.setattr(Adw, "Application", App)
    assert gui.main(["--demo"]) == 0
    assert made and not __import__("os").path.exists(made[0])
