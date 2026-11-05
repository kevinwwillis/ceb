"""GNOME custom shortcuts, against an in-memory gsettings backend (never the user's settings)."""
import os

import pytest

# Safety net: nothing in these tests may reach the user's real settings.
os.environ["GSETTINGS_BACKEND"] = "memory"

gi = pytest.importorskip("gi")
from gi.repository import Gio  # noqa: E402

from pam_profiles.gui.shortcuts import CUSTOM, MEDIA_KEYS, Shortcuts  # noqa: E402

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
    s = Shortcuts(backend, command="/usr/bin/pam")
    s.set("Desk — 3 monitors", "<Super>F1")
    paths = media(backend).get_strv("custom-keybindings")
    assert len(paths) == 1
    e = entry(backend, paths[0])
    assert e.get_string("binding") == "<Super>F1"
    assert e.get_string("command") == "/usr/bin/pam apply 'Desk — 3 monitors'"
    assert e.get_string("name") == "PAM: Desk — 3 monitors"
    assert s.all() == {"Desk — 3 monitors": "<Super>F1"}


def test_change_rename_and_remove_keep_other_shortcuts(backend):
    other = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
    e = entry(backend, other)
    e.set_string("name", "Terminal")
    e.set_string("command", "kgx")
    e.set_string("binding", "<Super>t")
    media(backend).set_strv("custom-keybindings", [other])

    s = Shortcuts(backend, command="pam")
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


def test_profile_of_only_matches_pam_apply():
    assert Shortcuts.profile_of("/home/k/.venv/bin/pam apply 'a b'") == "a b"
    assert Shortcuts.profile_of("pam apply desk --dry-run") is None
    assert Shortcuts.profile_of("spam apply desk") is None
    assert Shortcuts.profile_of("pam list") is None
    assert Shortcuts.profile_of("'unterminated") is None


def test_clash_with_another_custom_shortcut(backend):
    s = Shortcuts(backend, command="pam")
    s.set("a", "<Super><Alt>F1")
    assert "PAM: a" in s.clash("<Super><Alt>F1", "b")
    assert s.clash("<Super><Alt>F1", "a") is None   # its own shortcut is no clash
    assert s.clash("<Super><Alt>F12", "b") is None


def test_clash_with_gnome_default(backend):
    s = Shortcuts(backend, command="pam")
    # <Super>l locks the screen by default (media-keys "screensaver"); GNOME's default help
    # shortcut is <Super>F1, the key the mockup gave the first profile.
    assert s.clash("<Super>l", "a") is not None
    assert "help" in s.clash("<Super>F1", "a").lower()
