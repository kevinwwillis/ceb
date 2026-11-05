from conftest import HDMI_OUT, PROJECTOR, desk, mon, reply
from ceb_profiles import cli


class Notifier:
    def __init__(self):
        self.sent = []
        self.critical = []

    def __call__(self, title, body, *, critical=True):
        self.sent.append((title, body))
        self.critical.append(critical)
        return True


class _NoShortcuts:
    available = False


def run(ceb, *argv, notifier=None, on_terminal=True, shortcuts=None):
    """shortcuts: what `delete` uses (default: none available, so the user's are never touched)."""
    n = notifier or Notifier()
    s = _NoShortcuts() if shortcuts in (None, False) else shortcuts
    return cli.main(list(argv), ceb=ceb, notifier=n, on_terminal=on_terminal, shortcuts=s), n.sent


def test_apply_success_is_silent(make_ceb):
    ceb = make_ceb(desk(1))
    assert run(ceb, "save-current", "desk")[0] == 0
    ceb.display.set(desk(2))
    assert run(ceb, "apply", "desk") == (0, [])


def test_apply_refusal_notifies_with_the_reason(make_ceb):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    ceb.audio.state.outputs = [HDMI_OUT]
    code, sent = run(ceb, "apply", "desk")
    assert code == 1 and len(sent) == 1
    title, body = sent[0]
    assert "not applied" in title and "Nothing was changed" in body and "not present" in body
    assert "--allow-missing" in body


def test_allow_missing_partial_notifies_and_exits_3(make_ceb):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    ceb.audio.state.outputs = [HDMI_OUT]
    code, sent = run(ceb, "apply", "desk", "--allow-missing")
    assert code == 3 and "skipped" in sent[0][0]


def test_unknown_profile_notifies(make_ceb):
    code, sent = run(make_ceb(desk(1)), "apply", "nope")
    assert code == 1 and "nope" in sent[0][1]


def test_unexpected_error_notifies(make_ceb):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")

    def boom():
        raise RuntimeError("something odd")
    ceb.display.get_state = boom
    code, sent = run(ceb, "apply", "desk")
    assert code == 1 and "something odd" in sent[0][1]


def test_gnome_refusal_notifies(make_ceb):
    from ceb_profiles.mutter import METHOD_VERIFY, DisplayError
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    ceb.display.fail_with[METHOD_VERIFY] = DisplayError("Logical monitors not adjacent")
    code, sent = run(ceb, "apply", "desk")
    assert code == 1 and "not adjacent" in sent[0][1]


def test_no_notify_and_dry_run_do_not_notify(make_ceb):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    ceb.audio.state.outputs = [HDMI_OUT]
    assert run(ceb, "apply", "desk", "--no-notify") == (1, [])
    assert run(ceb, "apply", "desk", "--dry-run") == (1, [])


def test_dry_run_prints_plan(make_ceb, capsys):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    ceb.display.set(desk(2))
    assert run(ceb, "apply", "desk", "--dry-run")[0] == 0
    out = capsys.readouterr().out
    assert "screen HDMI-4 (1920x1080@60.000) at (1920, 0) scale 1 primary" in out
    assert "nothing was changed" in out


def test_list_shows_unplugged_screens(make_ceb, capsys):
    ceb = make_ceb(reply([mon("DP-2", PROJECTOR)]))
    run(ceb, "list")
    ceb.display.set(desk(1))
    capsys.readouterr()
    assert run(ceb, "list")[0] == 0
    out = capsys.readouterr().out
    assert "not connected" in out and "S2-TEK TV" in out
    assert "identical twin 2 of 2" in out


def test_save_twice_fails_without_force(make_ceb, capsys):
    ceb = make_ceb(desk(1))
    assert run(ceb, "save-current", "desk")[0] == 0
    assert run(ceb, "save-current", "desk") == (1, [])  # not apply: printed, not notified
    assert "already exists" in capsys.readouterr().err
    assert run(ceb, "save-current", "desk", "--force")[0] == 0


def test_show_and_json(make_ceb, capsys):
    import json
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    capsys.readouterr()
    assert run(ceb, "show", "desk", "--json")[0] == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "desk" and len(data["displays"]["logical_monitors"]) == 3
    assert run(ceb, "list", "--json")[0] == 0
    data = json.loads(capsys.readouterr().out)
    assert [s["twin"] for s in data["connected_screens"] if s["vendor"] == "LEN"] == [0, 1]


# ------------------------------------------------------------ review fixes


def test_partial_success_is_normal_urgency_failure_is_critical(make_ceb):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    ceb.audio.state.outputs = [HDMI_OUT]
    n = Notifier()
    assert run(ceb, "apply", "desk", "--allow-missing", notifier=n)[0] == 3
    assert run(ceb, "apply", "desk", notifier=n)[0] == 1
    assert n.critical == [False, True]


def test_dry_run_unexpected_error_only_prints(make_ceb, capsys):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")

    def boom():
        raise RuntimeError("something odd")
    ceb.display.get_state = boom
    code, sent = run(ceb, "apply", "desk", "--dry-run")
    assert code == 1 and sent == []
    assert "something odd" in capsys.readouterr().err


def test_usage_error_from_a_shortcut_notifies(make_ceb, capsys):
    ceb = make_ceb(desk(1))
    code, sent = run(ceb, "apply", on_terminal=False)
    assert code == 2 and len(sent) == 1 and "name" in sent[0][1]
    code, sent = run(ceb, "aply", "desk", on_terminal=False)
    assert code == 2 and "aply" in sent[0][1]


def test_usage_error_on_a_terminal_only_prints(make_ceb, capsys):
    code, sent = run(make_ceb(desk(1)), "apply", on_terminal=True)
    assert code == 2 and sent == []
    assert "usage:" in capsys.readouterr().err
    assert run(make_ceb(desk(1)), "apply", "--dry-run", on_terminal=False) == (2, [])


def test_list_and_save_without_mutter_say_so_cleanly(make_ceb, capsys):
    from ceb_profiles.mutter import DisplayError
    ceb = make_ceb(DisplayError("org.gnome.Mutter.DisplayConfig was not provided by any .service files"))
    for argv in (["list"], ["save-current", "desk"]):
        capsys.readouterr()
        assert run(ceb, *argv) == (1, [])
        err = capsys.readouterr().err
        assert "could not read the screens" in err and "Traceback" not in err


def test_list_with_corrupt_history_still_lists_screens(make_ceb, capsys):
    ceb = make_ceb(desk(1))
    ceb.store.dir.mkdir(parents=True)
    ceb.store.known_path.write_text("{broken")
    assert run(ceb, "list")[0] == 1
    out, err = capsys.readouterr()
    assert out.count("connected      HDMI-") == 3
    assert "not valid JSON" in err and "Traceback" not in err
    assert ceb.store.known_path.read_text() == "{broken"


def test_list_with_corrupt_profiles_still_lists_screens(make_ceb, capsys):
    ceb = make_ceb(desk(1))
    ceb.store.dir.mkdir(parents=True)
    ceb.store.profiles_path.write_text("[]")
    assert run(ceb, "list")[0] == 1
    out, err = capsys.readouterr()
    assert "connected      HDMI-1" in out and "does not hold a JSON object" in err


def test_usage_error_message_has_one_ceb_prefix(make_ceb, capsys):
    from ceb_profiles import notify
    assert run(make_ceb(desk(1)), "aply", on_terminal=True)[0] == 2
    err = capsys.readouterr().err
    assert "ceb: ceb:" not in err and "ceb: wrong command: " in err.lower()
    shown = []

    def notifier(title, body, *, critical=True):
        shown.append(title)
        return notify.notify(title, body, critical=critical, senders=[])
    assert cli.main(["aply"], ceb=make_ceb(desk(1)), notifier=notifier, on_terminal=False) == 2
    assert "ceb: ceb:" not in capsys.readouterr().err and shown == ["Wrong command"]


# ---------------------------------------------------------------- review fixes, 2026-11-05


def test_gui_after_global_options_goes_to_the_window(monkeypatch):
    import ceb_profiles.gui as gui
    seen = []
    monkeypatch.setattr(gui, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["--config-dir", "/tmp/x", "gui", "--demo"]) == 0
    assert cli.main(["--config-dir=/tmp/y", "gui"]) == 0
    assert cli.main(["gui", "--dry-run"]) == 0
    assert seen == [["--demo", "--config-dir", "/tmp/x"], ["--config-dir=/tmp/y"], ["--dry-run"]]


def test_names_starting_with_dash_work_after_double_dash(make_ceb):
    ceb = make_ceb(desk(1))
    assert run(ceb, "save-current", "--", "-work")[0] == 0
    assert run(ceb, "show", "--", "-work")[0] == 0
    assert run(ceb, "apply", "--dry-run", "--", "-work")[0] == 0
    assert run(ceb, "apply", "--", "-work")[0] == 0
    assert [p.name for p in ceb.profiles()] == ["-work"]
    assert run(ceb, "delete", "--", "-work", shortcuts=False)[0] == 0
    assert ceb.profiles() == []


def test_dash_name_without_double_dash_explains(make_ceb, capsys):
    code, _ = run(make_ceb(desk(1)), "apply", "-work")
    assert code == 2
    assert "goes after --" in capsys.readouterr().err


def test_refusal_notification_says_how_to_apply_the_rest(make_ceb):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    ceb.audio.state.outputs = [HDMI_OUT]
    _code, sent = run(ceb, "apply", "desk")
    body = sent[0][1]
    assert "Use --allow-missing" not in body
    assert "Apply the Rest" in body and "apply --allow-missing -- desk" in body


class FakeShortcuts:
    available = True

    def __init__(self, fail=None):
        self.removed, self.fail = [], fail

    def set(self, name, accel):
        if self.fail:
            from ceb_profiles.gui.shortcuts import ShortcutError
            raise ShortcutError(self.fail)
        self.removed.append((name, accel))


def test_delete_removes_the_profiles_shortcut(make_ceb):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    s = FakeShortcuts()
    assert run(ceb, "delete", "desk", shortcuts=s)[0] == 0
    assert s.removed == [("desk", None)] and ceb.profiles() == []


def test_delete_reports_a_shortcut_it_could_not_remove(make_ceb, capsys):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    assert run(ceb, "delete", "desk", shortcuts=FakeShortcuts(fail="settings are read-only"))[0] == 1
    assert ceb.profiles() == [] and "read-only" in capsys.readouterr().err


def test_delete_with_real_shortcut_code_in_memory(make_ceb):
    pytest = __import__("pytest")
    gi = pytest.importorskip("gi")
    from gi.repository import Gio
    from ceb_profiles.gui.shortcuts import CUSTOM, Shortcuts
    if Gio.SettingsSchemaSource.get_default().lookup(CUSTOM, True) is None:
        pytest.skip("GNOME's media-keys schemas are not installed")
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    s = Shortcuts(Gio.memory_settings_backend_new(), command="/usr/bin/ceb")
    s.set("desk", "<Super>F7")
    assert run(ceb, "delete", "desk", shortcuts=s)[0] == 0
    assert s.all() == {}


def test_install_desktop_writes_menu_entry_and_icon(tmp_path, monkeypatch, capsys):
    from ceb_profiles import desktop
    monkeypatch.setattr(desktop, "find_executable", lambda name: f"/opt/my apps/bin/{name}")
    assert cli.main(["install-desktop"]) == 0
    entry, icon = desktop.desktop_paths()
    assert str(entry).startswith(str(tmp_path / "data"))
    assert 'Exec="/opt/my apps/bin/ceb-gui"' in entry.read_text()
    assert icon.read_bytes().startswith(b"<")
    assert cli.main(["install-desktop", "--uninstall"]) == 0
    assert not entry.exists() and not icon.exists()


def test_install_desktop_without_ceb_gui_says_so(monkeypatch, capsys):
    from ceb_profiles import desktop
    monkeypatch.setattr(desktop, "find_executable", lambda name: None)
    assert cli.main(["install-desktop"]) == 1
    assert "could not find the installed ceb-gui" in capsys.readouterr().err


def test_unknown_option_gets_no_dash_name_hint(make_ceb, capsys):
    code, _ = run(make_ceb(desk(1)), "apply", "desk", "--bogus")
    assert code == 2 and "goes after --" not in capsys.readouterr().err
