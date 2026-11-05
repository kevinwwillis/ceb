from conftest import HDMI_OUT, PROJECTOR, desk, mon, reply
from pam_profiles import cli


class Notifier:
    def __init__(self):
        self.sent = []

    def __call__(self, title, body, **kw):
        self.sent.append((title, body))
        return True


def run(pam, *argv):
    n = Notifier()
    return cli.main(list(argv), pam=pam, notifier=n), n.sent


def test_apply_success_is_silent(make_pam):
    pam = make_pam(desk(1))
    assert run(pam, "save-current", "desk")[0] == 0
    pam.display.set(desk(2))
    assert run(pam, "apply", "desk") == (0, [])


def test_apply_refusal_notifies_with_the_reason(make_pam):
    pam = make_pam(desk(1))
    run(pam, "save-current", "desk")
    pam.audio.state.outputs = [HDMI_OUT]
    code, sent = run(pam, "apply", "desk")
    assert code == 1 and len(sent) == 1
    title, body = sent[0]
    assert "not applied" in title and "Nothing was changed" in body and "not present" in body
    assert "--allow-missing" in body


def test_allow_missing_partial_notifies_and_exits_3(make_pam):
    pam = make_pam(desk(1))
    run(pam, "save-current", "desk")
    pam.audio.state.outputs = [HDMI_OUT]
    code, sent = run(pam, "apply", "desk", "--allow-missing")
    assert code == 3 and "skipped" in sent[0][0]


def test_unknown_profile_notifies(make_pam):
    code, sent = run(make_pam(desk(1)), "apply", "nope")
    assert code == 1 and "nope" in sent[0][1]


def test_unexpected_error_notifies(make_pam):
    pam = make_pam(desk(1))
    run(pam, "save-current", "desk")

    def boom():
        raise RuntimeError("something odd")
    pam.display.get_state = boom
    code, sent = run(pam, "apply", "desk")
    assert code == 1 and "something odd" in sent[0][1]


def test_gnome_refusal_notifies(make_pam):
    from pam_profiles.mutter import METHOD_VERIFY, DisplayError
    pam = make_pam(desk(1))
    run(pam, "save-current", "desk")
    pam.display.fail_with[METHOD_VERIFY] = DisplayError("Logical monitors not adjacent")
    code, sent = run(pam, "apply", "desk")
    assert code == 1 and "not adjacent" in sent[0][1]


def test_no_notify_and_dry_run_do_not_notify(make_pam):
    pam = make_pam(desk(1))
    run(pam, "save-current", "desk")
    pam.audio.state.outputs = [HDMI_OUT]
    assert run(pam, "apply", "desk", "--no-notify") == (1, [])
    assert run(pam, "apply", "desk", "--dry-run") == (1, [])


def test_dry_run_prints_plan(make_pam, capsys):
    pam = make_pam(desk(1))
    run(pam, "save-current", "desk")
    pam.display.set(desk(2))
    assert run(pam, "apply", "desk", "--dry-run")[0] == 0
    out = capsys.readouterr().out
    assert "screen HDMI-4 (1920x1080@60.000) at (1920, 0) scale 1 primary" in out
    assert "nothing was changed" in out


def test_list_shows_unplugged_screens(make_pam, capsys):
    pam = make_pam(reply([mon("DP-2", PROJECTOR)]))
    run(pam, "list")
    pam.display.set(desk(1))
    capsys.readouterr()
    assert run(pam, "list")[0] == 0
    out = capsys.readouterr().out
    assert "not connected" in out and "S2-TEK TV" in out
    assert "identical twin 2 of 2" in out


def test_save_twice_fails_without_force(make_pam, capsys):
    pam = make_pam(desk(1))
    assert run(pam, "save-current", "desk")[0] == 0
    assert run(pam, "save-current", "desk") == (1, [])  # not apply: printed, not notified
    assert "already exists" in capsys.readouterr().err
    assert run(pam, "save-current", "desk", "--force")[0] == 0


def test_show_and_json(make_pam, capsys):
    import json
    pam = make_pam(desk(1))
    run(pam, "save-current", "desk")
    capsys.readouterr()
    assert run(pam, "show", "desk", "--json")[0] == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "desk" and len(data["displays"]["logical_monitors"]) == 3
    assert run(pam, "list", "--json")[0] == 0
    data = json.loads(capsys.readouterr().out)
    assert [s["twin"] for s in data["connected_screens"] if s["vendor"] == "LEN"] == [0, 1]
