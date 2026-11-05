import os

import pytest

from conftest import LENOVO, PROJECTOR, ROG, desk, mon, reply
from pam_profiles.model import Identity
from pam_profiles.mutter import (ApplyLogicalMonitor, ApplyMonitor, MutterDisplayConfig, find_gpu,
                                 parse_current_state, remote_error_message)


def test_parse_kevins_desk():
    state = parse_current_state(desk(1))
    assert state.serial == 1 and state.layout_mode == 1 and state.supports_changing_layout_mode
    rog = state.by_connector("HDMI-3")
    assert rog.identity == Identity("AUS", "ROG PG248Q", "L8LMQS075392")  # trailing space dropped
    assert rog.display_name == 'ASUSTek COMPUTER INC 24"'
    assert rog.current_mode().id == "1920x1080@60.000"
    assert (rog.twin, rog.twin_count) == (0, 1)
    assert [(m.connector, m.twin, m.twin_count) for m in state.monitors if m.identity.vendor == "LEN"] \
        == [("HDMI-1", 0, 2), ("HDMI-2", 1, 2)]
    assert [(lm.x, lm.primary, lm.connectors) for lm in state.logical_monitors] == \
        [(0, False, ["HDMI-1"]), (1920, True, ["HDMI-3"]), (3840, False, ["HDMI-2"])]


@pytest.mark.parametrize("base", [1, 2])
@pytest.mark.parametrize("shuffle", [False, True])
def test_twin_rank_follows_port_order_not_reply_order(base, shuffle):
    state = parse_current_state(desk(base, shuffle=shuffle))
    ranks = {m.connector: m.twin for m in state.monitors if m.identity.vendor == "LEN"}
    assert ranks == {f"HDMI-{base}": 0, f"HDMI-{base + 1}": 1}


def test_twin_rank_hdmi_9_before_hdmi_10():
    state = parse_current_state(reply([mon("HDMI-10", LENOVO), mon("HDMI-9", LENOVO)]))
    assert state.by_connector("HDMI-9").twin == 0 and state.by_connector("HDMI-10").twin == 1


def test_twin_rank_uses_gpu_before_port_name():
    r = reply([mon("HDMI-1", LENOVO), mon("HDMI-2", LENOVO)])
    gpus = {"HDMI-1": "0000:2e:00.0", "HDMI-2": "0000:2d:00.0"}
    state = parse_current_state(r, gpus.get)
    assert state.by_connector("HDMI-2").twin == 0


def _fake_sysfs(root, cards):
    """cards: {card_name: (pci_address, {connector: status})}"""
    devices = root / "devices"
    for card, (pci, connectors) in cards.items():
        (devices / pci).mkdir(parents=True)
        (root / card).mkdir()
        os.symlink(devices / pci, root / card / "device")
        for conn, status in connectors.items():
            d = root / f"{card}-{conn}"
            d.mkdir()
            (d / "status").write_text(status + "\n")
    return root


def test_find_gpu_maps_mutter_hdmi_name_to_kernel_hdmi_a(tmp_path):
    # Layout seen on KEVIN-PC-1: NVIDIA numbers ports across both cards.
    root = _fake_sysfs(tmp_path / "drm", {
        "card0": ("0000:2d:00.0", {"HDMI-A-1": "connected", "HDMI-A-2": "connected", "DP-1": "disconnected"}),
        "card1": ("0000:2e:00.0", {"HDMI-A-4": "connected", "DP-4": "disconnected"}),
    })
    assert find_gpu("HDMI-1", root) == "0000:2d:00.0"
    assert find_gpu("HDMI-4", root) == "0000:2e:00.0"
    assert find_gpu("DP-1", root) is None          # not connected
    assert find_gpu("HDMI-7", root) is None        # no such port


def test_find_gpu_ambiguous_name_on_two_cards_is_unknown(tmp_path):
    root = _fake_sysfs(tmp_path / "drm", {
        "card0": ("0000:00:02.0", {"HDMI-A-1": "connected"}),
        "card1": ("0000:01:00.0", {"HDMI-A-1": "connected"}),
    })
    assert find_gpu("HDMI-1", root) is None


def test_find_gpu_without_sysfs(tmp_path):
    assert find_gpu("HDMI-1", tmp_path / "missing") is None


def test_backend_refuses_persistent_method_without_touching_dbus():
    backend = MutterDisplayConfig()
    backend._proxy_call = lambda *a, **k: pytest.fail("D-Bus must not be called")
    with pytest.raises(ValueError):
        backend.apply(1, 2, [], {})


def test_remote_error_message_strips_dbus_prefix():
    class E:
        message = "GDBus.Error:org.freedesktop.DBus.Error.InvalidArgs: Logical monitors not adjacent"
    assert remote_error_message(E()) == "Logical monitors not adjacent"


def test_apply_variant_signature():
    gi = pytest.importorskip("gi")
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    from pam_profiles.mutter import build_apply_variant
    v = build_apply_variant(7, 1, [ApplyLogicalMonitor(1920, 0, 1.0, 0, True,
                                                       [ApplyMonitor("HDMI-3", "1920x1080@60.000")])],
                            {"layout-mode": 1}, GLib)
    assert v.get_type_string() == "(uua(iiduba(ssa{sv}))a{sv})"
    serial, method, lms, props = v.unpack()
    assert (serial, method) == (7, 1)
    assert lms == [(1920, 0, 1.0, 0, True, [("HDMI-3", "1920x1080@60.000", {})])]
    assert props == {"layout-mode": 1}


def test_projector_fixture_is_its_own_identity():
    state = parse_current_state(reply([mon("DP-2", PROJECTOR), mon("HDMI-3", ROG)]))
    assert {m.identity.vendor for m in state.monitors} == {"STK", "AUS"}
