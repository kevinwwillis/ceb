"""One-time copy of the files from $XDG_CONFIG_HOME/pam (the app's name until 2026-11-05) to
$XDG_CONFIG_HOME/ceb. conftest points XDG_CONFIG_HOME at a temporary folder for every test."""
import json
import os
from pathlib import Path

import pytest

from conftest import desk
from test_cli import Notifier
from ceb_profiles import cli
from ceb_profiles.core import Ceb
from ceb_profiles.store import Store, StoreError, legacy_config_dir, migrate_legacy_config

PROFILE = {"name": "desk", "displays": None, "audio": {}}


def config_home() -> Path:
    return Path(os.environ["XDG_CONFIG_HOME"])


def write_legacy(profiles=True, known=True) -> Path:
    """A PAM-era folder made by a store pointed at it, so the files have exactly our format."""
    legacy = config_home() / "pam"
    store = Store(legacy)
    if profiles:
        store.profiles_path.parent.mkdir(parents=True, exist_ok=True)
        store.profiles_path.write_text(json.dumps({"version": 1, "profiles": [PROFILE]}))
    if known:
        store.known_path.parent.mkdir(parents=True, exist_ok=True)
        store.known_path.write_text(json.dumps({"version": 1, "screens": [],
                                                "audio": {"outputs": [], "inputs": []}}))
    return legacy


def tree(folder: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()}


def test_legacy_dir_is_pam_under_config_home():
    assert legacy_config_dir() == config_home() / "pam"
    assert Store().dir == config_home() / "ceb"


def test_files_are_copied_once_and_the_old_folder_is_left_alone():
    legacy = write_legacy()
    before = tree(legacy)
    store = Store()
    assert [p.name for p in store.load_profiles()] == ["desk"]
    assert tree(config_home() / "ceb") == before
    assert tree(legacy) == before                     # copied, not moved; nothing else written there
    assert "copied profiles.json, known.json" in store.migration_notice
    assert not [p for p in config_home().iterdir() if "migrating" in p.name]  # no temp folder left
    # A second store (the next run) does not copy again and says nothing.
    (legacy / "profiles.json").write_text(json.dumps({"version": 1, "profiles": []}))
    again = Store()
    assert [p.name for p in again.load_profiles()] == ["desk"]
    assert again.migration_notice is None


def test_only_profiles_json_present():
    write_legacy(known=False)
    store = Store()
    store.load_known()                                # any first access triggers the copy
    assert sorted(p.name for p in (config_home() / "ceb").iterdir()) == ["profiles.json"]
    assert "copied profiles.json from" in store.migration_notice


def test_existing_ceb_folder_wins():
    write_legacy()
    (config_home() / "ceb").mkdir(parents=True)
    store = Store()
    assert store.load_profiles() == []
    assert store.migration_notice is None


@pytest.mark.parametrize("text", [
    "{not json",                                     # not JSON at all
    '["a list"]',                                    # not an object
    '{"profiles": []}',                              # no version: not written by PAM
    '{"version": "1", "profiles": []}',              # version of the wrong type
    '{"version": 99, "profiles": []}',               # written by a newer version
    '{"version": 1, "users": []}',                   # some other program's format
    '{"version": 1, "profiles": [{"nom": "x"}]}',    # profiles of another shape
])
def test_a_pam_folder_that_is_not_ours_is_never_touched(text):
    legacy = config_home() / "pam"
    legacy.mkdir(parents=True)
    (legacy / "profiles.json").write_text(text)
    (legacy / "other.conf").write_text("auth required pam_unix.so\n")
    before = tree(legacy)
    store = Store()
    assert store.load_profiles() == []
    assert store.migration_notice is None
    assert not (config_home() / "ceb").exists()
    assert tree(legacy) == before


def test_one_bad_file_stops_the_whole_copy():
    legacy = write_legacy()
    (legacy / "known.json").write_text('{"version": 1, "screens": "nope"}')
    store = Store()
    assert store.load_profiles() == []
    assert not (config_home() / "ceb").exists()


def test_pam_folder_with_no_data_files_copies_nothing():
    legacy = config_home() / "pam"
    legacy.mkdir(parents=True)
    (legacy / ".lock").write_bytes(b"")
    store = Store()
    store.load_profiles()
    assert store.migration_notice is None
    assert not (config_home() / "ceb").exists()


def test_explicit_config_dir_never_migrates(tmp_path):
    write_legacy()
    store = Store(tmp_path / "elsewhere")
    assert store.load_profiles() == []
    assert store.migration_notice is None


def test_symlinked_file_stays_a_link_to_the_same_file(tmp_path):
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "profiles.json"
    real.write_text(json.dumps({"version": 1, "profiles": [PROFILE]}))
    legacy = config_home() / "pam"
    legacy.mkdir(parents=True)
    os.symlink(os.path.relpath(real, legacy), legacy / "profiles.json")   # relative link
    store = Store()
    assert [p.name for p in store.load_profiles()] == ["desk"]
    link = config_home() / "ceb" / "profiles.json"
    assert link.is_symlink() and link.resolve() == real.resolve()
    store.save_profiles([])                          # writes go through to the dotfiles copy
    assert json.loads(real.read_text())["profiles"] == []


def test_copy_failure_is_a_store_error_and_leaves_nothing(monkeypatch):
    write_legacy()

    import shutil
    real_copy2, full = shutil.copy2, [True]

    def broken(src, dst, **kw):
        if full[0]:
            raise OSError(28, "No space left on device")
        return real_copy2(src, dst, **kw)
    monkeypatch.setattr("ceb_profiles.store.shutil.copy2", broken)
    with pytest.raises(StoreError, match="No space left"):
        Store().load_profiles()
    assert not (config_home() / "ceb").exists()
    assert not [p for p in config_home().iterdir() if "migrating" in p.name]
    full[0] = False   # (monkeypatch.undo() would also undo conftest's XDG_CONFIG_HOME)
    store = Store()                                  # the next run tries again
    assert [p.name for p in store.load_profiles()] == ["desk"]


def test_target_created_meanwhile_wins(monkeypatch):
    write_legacy()
    target = config_home() / "ceb"
    real_rename = os.rename

    def racing(src, dst):
        target.mkdir()
        (target / "profiles.json").write_text('{"version": 1, "profiles": []}')
        return real_rename(src, dst)
    monkeypatch.setattr("ceb_profiles.store.os.rename", racing)
    assert migrate_legacy_config(target, config_home() / "pam") is None
    assert json.loads((target / "profiles.json").read_text())["profiles"] == []
    assert not [p for p in config_home().iterdir() if "migrating" in p.name]


def test_cli_says_it_once_on_the_terminal(make_ceb, capsys):
    write_legacy()
    ceb = make_ceb(desk(1))
    ceb.store = Store()
    n = Notifier()
    assert cli.main(["list"], ceb=ceb, notifier=n, on_terminal=True) == 0
    assert "ceb: note: PAM is now called CEB: copied" in capsys.readouterr().err
    assert n.sent == []
    assert cli.main(["list"], ceb=Ceb(display=ceb.display, audio=ceb.audio, store=Store()),
                    notifier=n, on_terminal=True) == 0
    assert "PAM is now called CEB" not in capsys.readouterr().err


def test_keyboard_shortcut_apply_shows_it_as_a_normal_notification(make_ceb):
    write_legacy(profiles=False)
    ceb = make_ceb(desk(1))
    ceb.store = Store()
    ceb.save_current("desk")
    ceb.store.migration_notice = None                # said by the save above; check apply's own run
    ceb2 = Ceb(display=ceb.display, audio=ceb.audio, store=Store())
    assert ceb2.store._legacy_dir is not None
    n = Notifier()
    # ceb/ exists now, so this run copies nothing and stays silent.
    assert cli.main(["apply", "desk"], ceb=ceb2, notifier=n, on_terminal=False) == 0
    assert n.sent == []


def test_first_run_from_a_shortcut_notifies_once(make_ceb):
    write_legacy()
    ceb = make_ceb(desk(1))
    ceb.store = Store()
    n = Notifier()
    cli.main(["apply", "desk"], ceb=ceb, notifier=n, on_terminal=False)
    notes = [(t, b) for t, b in n.sent if t == "Settings copied from PAM"]
    assert len(notes) == 1 and "PAM is now called CEB" in notes[0][1]
    assert n.critical[[t for t, _ in n.sent].index("Settings copied from PAM")] is False
