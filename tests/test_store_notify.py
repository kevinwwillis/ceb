import pytest

from conftest import desk
from pam_profiles import notify
from pam_profiles.store import Store, StoreError


def test_corrupt_profiles_file_is_reported_and_kept(make_pam, tmp_path):
    pam = make_pam(desk(1))
    pam.store.dir.mkdir(parents=True)
    pam.store.profiles_path.write_text("{not json")
    with pytest.raises(StoreError):
        pam.save_current("desk")
    assert pam.store.profiles_path.read_text() == "{not json"


def test_newer_schema_is_not_overwritten(tmp_path):
    store = Store(tmp_path)
    store.profiles_path.write_text('{"version": 99, "profiles": []}')
    with pytest.raises(StoreError):
        store.load_profiles()


def test_profile_roundtrip(make_pam):
    pam = make_pam(desk(1))
    saved = pam.save_current("desk")
    assert pam.profile("desk").to_json() == saved.to_json()


def test_no_temp_files_left(make_pam):
    pam = make_pam(desk(1))
    pam.save_current("desk")
    assert sorted(p.name for p in pam.store.dir.iterdir()) == [".lock", "known.json", "profiles.json"]


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
    assert "pam: t: b" in capsys.readouterr().err


def test_notify_send_missing_binary(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("notify-send")
    monkeypatch.setattr(notify.subprocess, "run", missing)
    assert notify._notify_send("t", "b", notify.URGENCY_CRITICAL) is False
