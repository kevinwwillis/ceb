import pytest

from conftest import desk
from ceb_profiles import notify
from ceb_profiles.store import Store, StoreError


def test_corrupt_profiles_file_is_reported_and_kept(make_ceb, tmp_path):
    ceb = make_ceb(desk(1))
    ceb.store.dir.mkdir(parents=True)
    ceb.store.profiles_path.write_text("{not json")
    with pytest.raises(StoreError):
        ceb.save_current("desk")
    assert ceb.store.profiles_path.read_text() == "{not json"


def test_newer_schema_is_not_overwritten(tmp_path):
    store = Store(tmp_path)
    store.profiles_path.write_text('{"version": 99, "profiles": []}')
    with pytest.raises(StoreError):
        store.load_profiles()


def test_profile_roundtrip(make_ceb):
    ceb = make_ceb(desk(1))
    saved = ceb.save_current("desk")
    assert ceb.profile("desk").to_json() == saved.to_json()


def test_no_temp_files_left(make_ceb):
    ceb = make_ceb(desk(1))
    ceb.save_current("desk")
    assert sorted(p.name for p in ceb.store.dir.iterdir()) == [".lock", "known.json", "profiles.json"]


def test_notify_falls_back_and_never_raises(capsys):
    calls = []

    def broken(title, body, urgency):
        calls.append("dbus")
        raise RuntimeError("no session bus")

    def works(title, body, urgency):
        calls.append("notify-send")
        return True
    assert notify.notify("t", "b", senders=(broken, works)) is True
    assert calls == ["dbus", "notify-send"]
    assert notify.notify("t", "b", senders=(broken,)) is False
    assert "ceb: t: b" in capsys.readouterr().err


def test_notify_send_missing_binary(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("notify-send")
    monkeypatch.setattr(notify.subprocess, "run", missing)
    assert notify._notify_send("t", "b", notify.URGENCY_CRITICAL) is False


# ------------------------------------------------------------ hand-edited files with wrong types


@pytest.mark.parametrize("which,text", [
    ("profiles", '{"version": "1", "profiles": []}'),
    ("profiles", '{"version": 1, "profiles": [5]}'),
    ("profiles", '{"version": 1, "profiles": {"name": "x"}}'),
    ("profiles", '{"version": 1, "profiles": [{"name": 3, "displays": null}]}'),
    ("profiles", '{"version": 1, "profiles": [{"name": "x", "audio": "x"}]}'),
    ("known", '{"version": 1, "audio": ["x"]}'),
    ("known", '{"version": 1, "screens": [{"vendor": 1, "product": "p", "serial": "s"}]}'),
    ("known", '{"version": 1, "screens": [{"vendor": "v", "product": "p", "serial": "s", "last_seen": 5}]}'),
    ("known", '{"version": 1, "audio": {"outputs": [{"name": ["x"]}]}}'),
])
def test_wrong_types_are_reported_not_crashing(tmp_path, which, text):
    store = Store(tmp_path)
    path = store.profiles_path if which == "profiles" else store.known_path
    path.write_text(text)
    with pytest.raises(StoreError, match="will not overwrite"):
        store.load_profiles() if which == "profiles" else store.load_known()
    assert path.read_text() == text


# ------------------------------------------------------------ symlinks and the lock file


def test_symlinked_profiles_file_is_written_through(make_ceb, tmp_path):
    ceb = make_ceb(desk(1))
    real = tmp_path / "dotfiles" / "ceb-profiles.json"
    real.parent.mkdir()
    real.write_text('{"version": 1, "profiles": []}')
    ceb.store.dir.mkdir(parents=True)
    ceb.store.profiles_path.symlink_to(real)
    ceb.save_current("desk")
    assert ceb.store.profiles_path.is_symlink()
    assert '"desk"' in real.read_text()
    assert [p.name for p in ceb.profiles()] == ["desk"]


def test_store_lock_never_follows_a_symlink(tmp_path):
    store = Store(tmp_path / "ceb")
    store.dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (store.dir / ".lock").symlink_to(victim)
    with pytest.raises(StoreError, match="symlink"):
        with store.locked():
            pass
    assert victim.read_text() == "keep me"


def test_write_failure_is_a_store_error(tmp_path, monkeypatch):
    store = Store(tmp_path)

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr("ceb_profiles.store.tempfile.mkstemp", full)
    with pytest.raises(StoreError, match="No space left"):
        store.save_profiles([])


# ------------------------------------------------------------ last_seen does not force a rewrite


def _observe_count(ceb, monkeypatch):
    writes = []
    real = ceb.store.save_known
    monkeypatch.setattr(ceb.store, "save_known", lambda known: (writes.append(1), real(known)))
    return writes


def test_seeing_the_same_screens_again_does_not_rewrite_known(make_ceb, monkeypatch):
    from ceb_profiles import store as store_mod
    minutes = iter(range(10, 60))
    monkeypatch.setattr(store_mod, "now_iso", lambda: f"2026-11-05T10:{next(minutes)}:00-05:00")
    ceb = make_ceb(desk(1))
    writes = _observe_count(ceb, monkeypatch)
    ceb.current()
    assert len(writes) == 1
    ceb.current()
    ceb.current()
    assert len(writes) == 1


def test_old_last_seen_is_refreshed(make_ceb, monkeypatch):
    from ceb_profiles import store as store_mod
    ceb = make_ceb(desk(1))
    state = ceb.display.get_state()
    audio = ceb.audio.snapshot()
    known = store_mod.Known()
    known.observe_display(state, when="2026-11-05T10:00:00-05:00")
    known.observe_audio(audio, when="2026-11-05T10:00:00-05:00")
    assert not known.observe_display(state, when="2026-11-05T10:59:00-05:00")
    assert not known.observe_audio(audio, when="2026-11-05T10:59:00-05:00")
    assert {s.last_seen for s in known.screens} == {"2026-11-05T10:00:00-05:00"}
    assert known.observe_display(state, when="2026-11-05T11:00:01-05:00")
    assert known.observe_audio(audio, when="2026-11-05T11:00:01-05:00")
    assert {s.last_seen for s in known.screens} == {"2026-11-05T11:00:01-05:00"}
    assert {a.last_seen for a in known.outputs} == {"2026-11-05T11:00:01-05:00"}
