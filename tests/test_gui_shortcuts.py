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
    assert e.get_string("command") == "/usr/bin/ceb apply 'Desk — 3 monitors'"
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
    assert Shortcuts.profile_of("/home/k/.venv/bin/ceb apply 'a b'") == "a b"
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


# ---------------------------------------------------------------- shortcuts made while the app was PAM

DIR = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


def add(backend, path, name, command, binding):
    e = entry(backend, path)
    e.set_string("name", name)
    e.set_string("command", command)
    e.set_string("binding", binding)
    media(backend).set_strv("custom-keybindings", list(media(backend).get_strv("custom-keybindings")) + [path])


def test_profile_of_does_not_take_pam_apply_as_current():
    assert Shortcuts.profile_of("pam apply desk") is None
    assert Shortcuts.profile_of("/usr/bin/pam apply desk", "pam") == "desk"


def test_legacy_shortcut_needs_its_old_name_or_an_app_made_path():
    cmd = "/home/k/.venv/bin/pam apply 'a b'"
    assert Shortcuts.legacy_profile_of(DIR + "custom3/", "PAM: a b", cmd) == "a b"     # old name
    assert Shortcuts.legacy_profile_of(DIR + "pam2/", "renamed by hand", cmd) == "a b"  # app's path
    assert Shortcuts.legacy_profile_of(DIR + "custom3/", "My thing", cmd) is None        # neither
    assert Shortcuts.legacy_profile_of(DIR + "pam2/", "PAM: a b", "pam apply a b --x") is None
    assert Shortcuts.legacy_profile_of(DIR + "pamx/", "x", "pam apply 'a b'") is None


def test_upgrade_rewrites_old_shortcuts_in_place_and_leaves_others(backend):
    add(backend, DIR + "custom0/", "Terminal", "kgx", "<Super>t")
    add(backend, DIR + "pam0/", "PAM: desk", "/home/k/.venv/bin/pam apply desk", "<Super>F2")
    add(backend, DIR + "custom1/", "PAM: tv", "pam apply tv", "<Super>F3")              # moved by hand
    add(backend, DIR + "custom2/", "Someone else's", "pam apply x", "<Super>F4")         # not ours
    s = Shortcuts(backend, command="/usr/bin/ceb")
    assert s.all() == {"desk": "<Super>F2", "tv": "<Super>F3"}   # recognized before the upgrade
    assert s.upgrade_legacy() == 2
    assert s.upgrade_legacy() == 0
    assert list(media(backend).get_strv("custom-keybindings")) == [
        DIR + "custom0/", DIR + "pam0/", DIR + "custom1/", DIR + "custom2/"]
    e = entry(backend, DIR + "pam0/")
    assert (e.get_string("name"), e.get_string("command"), e.get_string("binding")) == (
        "CEB: desk", "/usr/bin/ceb apply desk", "<Super>F2")
    assert entry(backend, DIR + "custom1/").get_string("command") == "/usr/bin/ceb apply tv"
    assert entry(backend, DIR + "custom2/").get_string("command") == "pam apply x"
    assert entry(backend, DIR + "custom0/").get_string("command") == "kgx"
    assert s.all() == {"desk": "<Super>F2", "tv": "<Super>F3"}


def test_upgrade_leaves_an_old_shortcut_whose_profile_already_has_a_new_one(backend):
    add(backend, DIR + "pam0/", "PAM: desk", "pam apply desk", "<Super>F2")
    add(backend, DIR + "ceb0/", "CEB: desk", "ceb apply desk", "<Super>F5")
    s = Shortcuts(backend, command="ceb")
    assert s.all() == {"desk": "<Super>F5"}                      # the current one wins
    assert s.upgrade_legacy() == 0
    assert entry(backend, DIR + "pam0/").get_string("command") == "pam apply desk"
    assert s.clash("<Super>F2", "desk") is None                  # its own old shortcut is no clash


def test_set_and_remove_work_on_an_old_shortcut(backend):
    add(backend, DIR + "pam0/", "PAM: desk", "pam apply desk", "<Super>F2")
    s = Shortcuts(backend, command="ceb")
    s.set("desk", "<Super>F6")
    assert list(media(backend).get_strv("custom-keybindings")) == [DIR + "pam0/"]
    assert entry(backend, DIR + "pam0/").get_string("command") == "ceb apply desk"
    s.set("desk", None)
    assert list(media(backend).get_strv("custom-keybindings")) == []
