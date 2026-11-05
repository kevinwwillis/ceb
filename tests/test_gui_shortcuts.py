"""GNOME custom shortcuts, against an in-memory gsettings backend (never the user's settings)."""
import os

import pytest

# Safety net: nothing in these tests may reach the user's real settings.
os.environ["GSETTINGS_BACKEND"] = "memory"

gi = pytest.importorskip("gi")
from gi.repository import Gio  # noqa: E402

from ceb_profiles.gui.shortcuts import CUSTOM, MEDIA_KEYS, Shortcuts  # noqa: E402

pytestmark = pytest.mark.skipif(Gio.SettingsSchemaSource.get_default().lookup(CUSTOM, True) is None,
                                reason="GNOME's media-keys schemas are not installed")


@pytest.fixture
def backend():
    return Gio.memory_settings_backend_new()


def media(backend):
    schema = Gio.SettingsSchemaSource.get_default().lookup(MEDIA_KEYS, True)
    return Gio.Settings.new_full(schema, backend, None)


def entry(backend, path):
    schema = Gio.SettingsSchemaSource.get_default().lookup(CUSTOM, True)
    return Gio.Settings.new_full(schema, backend, path)


def test_set_creates_a_gnome_custom_shortcut(backend):
    s = Shortcuts(backend, command="/usr/bin/ceb")
    s.set("Desk — 3 monitors", "<Super>F1")
    paths = media(backend).get_strv("custom-keybindings")
    assert len(paths) == 1
    e = entry(backend, paths[0])
    assert e.get_string("binding") == "<Super>F1"
    assert e.get_string("command") == "/usr/bin/ceb apply -- 'Desk — 3 monitors'"
    assert e.get_string("name") == "CEB: Desk — 3 monitors"
    assert s.all() == {"Desk — 3 monitors": "<Super>F1"}


def test_change_rename_and_remove_keep_other_shortcuts(backend):
    other = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
    e = entry(backend, other)
    e.set_string("name", "Terminal")
    e.set_string("command", "kgx")
    e.set_string("binding", "<Super>t")
    media(backend).set_strv("custom-keybindings", [other])

    s = Shortcuts(backend, command="ceb")
    s.set("desk", "<Super>F1")
    s.set("desk", "<Super>F2")
    assert s.all() == {"desk": "<Super>F2"}
    assert len(media(backend).get_strv("custom-keybindings")) == 2
    s.rename("desk", "it's mine")
    assert s.all() == {"it's mine": "<Super>F2"}
    s.set("it's mine", None)
    assert s.all() == {}
    assert list(media(backend).get_strv("custom-keybindings")) == [other]
    assert entry(backend, other).get_string("command") == "kgx"


def test_profile_of_only_matches_ceb_apply():
    assert Shortcuts.profile_of("/home/k/.venv/bin/ceb apply 'a b'") == "a b"   # the older form
    assert Shortcuts.profile_of("/usr/bin/ceb apply -- -work") == "-work"
    assert Shortcuts.profile_of("/usr/bin/ceb --config-dir '/tmp/x y' apply -- desk") == "desk"
    assert Shortcuts.parse("/usr/bin/ceb --config-dir '/tmp/x y' apply -- desk") == ("/tmp/x y", "desk")
    assert Shortcuts.profile_of("ceb apply --dry-run desk") is None
    assert Shortcuts.profile_of("ceb apply -- a b") is None
    assert Shortcuts.profile_of("ceb apply desk --dry-run") is None
    assert Shortcuts.profile_of("spam apply desk") is None
    assert Shortcuts.profile_of("ceb list") is None
    assert Shortcuts.profile_of("'unterminated") is None


def test_clash_with_another_custom_shortcut(backend):
    s = Shortcuts(backend, command="ceb")
    s.set("a", "<Super><Alt>F1")
    assert "CEB: a" in s.clash("<Super><Alt>F1", "b")
    assert s.clash("<Super><Alt>F1", "a") is None   # its own shortcut is no clash
    assert s.clash("<Super><Alt>F12", "b") is None


def test_clash_with_gnome_default(backend):
    s = Shortcuts(backend, command="ceb")
    # <Super>l locks the screen by default (media-keys "screensaver"); GNOME's default help
    # shortcut is <Super>F1, the key the mockup gave the first profile.
    assert s.clash("<Super>l", "a") is not None
    assert "help" in s.clash("<Super>F1", "a").lower()


def test_name_starting_with_dash_is_written_after_double_dash(backend):
    s = Shortcuts(backend, command="/usr/bin/ceb")
    s.set("-work", "<Super>F5")
    path = media(backend).get_strv("custom-keybindings")[0]
    assert entry(backend, path).get_string("command") == "/usr/bin/ceb apply -- -work"
    assert s.all() == {"-work": "<Super>F5"}


def test_shortcut_carries_a_non_default_config_dir(backend):
    s = Shortcuts(backend, command="/usr/bin/ceb", config_dir="/home/k/my profiles")
    s.set("desk", "<Super>F5")
    path = media(backend).get_strv("custom-keybindings")[0]
    assert entry(backend, path).get_string("command") == \
        "/usr/bin/ceb --config-dir '/home/k/my profiles' apply -- desk"
    # Each config folder sees only its own shortcuts.
    default = Shortcuts(backend, command="/usr/bin/ceb")
    assert default.all() == {}
    default.set("desk", "<Super>F6")
    assert s.all() == {"desk": "<Super>F5"} and default.all() == {"desk": "<Super>F6"}
    default.set("desk", None)
    assert s.all() == {"desk": "<Super>F5"}


def test_no_installed_ceb_means_no_shortcut_and_says_why(backend, monkeypatch):
    from ceb_profiles.gui import shortcuts as mod
    monkeypatch.setattr(mod, "ceb_command", lambda: None)
    s = Shortcuts(backend)
    assert s.command is None
    with pytest.raises(mod.ShortcutError, match="could not find its installed `ceb`"):
        s.set("desk", "<Super>F5")
    assert list(media(backend).get_strv("custom-keybindings")) == []


def test_ceb_command_is_an_absolute_path():
    from ceb_profiles.gui.shortcuts import ceb_command
    found = ceb_command()
    assert found is None or os.path.isabs(found)


def test_delete_profile_removes_its_shortcut_only(backend, make_ceb):
    from conftest import desk
    from ceb_profiles.gui.shortcuts import delete_profile
    other = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
    e = entry(backend, other)
    e.set_string("name", "Terminal")
    e.set_string("command", "kgx")
    e.set_string("binding", "<Super>t")
    media(backend).set_strv("custom-keybindings", [other])
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    ceb.save_current("tv")
    s = Shortcuts(backend, command="/usr/bin/ceb")
    s.set("desk", "<Super>F5")
    s.set("tv", "<Super>F6")
    assert delete_profile(ceb, s, "desk") is None
    assert [p.name for p in ceb.profiles()] == ["tv"]
    assert s.all() == {"tv": "<Super>F6"}
    assert other in media(backend).get_strv("custom-keybindings")
    assert entry(backend, other).get_string("command") == "kgx"
