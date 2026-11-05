import itertools

import pytest

from pam_profiles.ports import port_sort_key, split_connector


@pytest.mark.parametrize("name,expected", [
    ("HDMI-1", ("HDMI", (1,))),
    ("HDMI-A-1", ("HDMI", (1,))),      # kernel spelling of the same port type
    ("DP-12", ("DP", (12,))),
    ("DP-1-3", ("DP", (1, 3))),        # MST branch
    ("eDP-1", ("EDP", (1,))),
    ("DVI-I-2", ("DVI", (2,))),
    ("DVI-D-1", ("DVI", (1,))),
    ("VGA-1", ("VGA", (1,))),
    ("Virtual", ("VIRTUAL", ())),
])
def test_split_connector(name, expected):
    assert split_connector(name) == expected


def order(connectors, gpus=None):
    gpus = gpus or {}
    return sorted(connectors, key=lambda c: port_sort_key(c, gpus.get(c)))


def test_numbers_compare_as_numbers():
    assert order(["HDMI-10", "HDMI-9", "HDMI-2"]) == ["HDMI-2", "HDMI-9", "HDMI-10"]


def test_mst_ports_compare_as_numbers():
    assert order(["DP-1-10", "DP-1-2", "DP-2"]) == ["DP-1-2", "DP-1-10", "DP-2"]


def test_order_survives_any_uniform_shift_within_and_across_types():
    """Twins on DP-2 and HDMI-1. A boot that shifts every port of the card by k (per type, as the
    NVIDIA race does) must not change which twin comes first. Comparing numbers across types would
    fail here: HDMI-1 < DP-2, but after HDMI shifts by 1 it is HDMI-2 vs DP-2."""
    base = order(["HDMI-1", "DP-2"])
    for dp_shift, hdmi_shift in itertools.product(range(4), range(4)):
        shifted = order([f"HDMI-{1 + hdmi_shift}", f"DP-{2 + dp_shift}"])
        assert [c.split("-")[0] for c in shifted] == [c.split("-")[0] for c in base]


def test_kernel_and_mutter_spellings_sort_the_same():
    assert port_sort_key("HDMI-A-2")[:3] == port_sort_key("HDMI-2")[:3]


def test_gpu_comes_first():
    """Twins on two cards. When the cards swap load order, the port numbers swap between cards, but
    the PCI addresses do not, so the twin on the card at the lower PCI address stays first."""
    boot1 = {"HDMI-1": "0000:2d:00.0", "HDMI-4": "0000:2e:00.0"}
    boot2 = {"HDMI-2": "0000:2d:00.0", "HDMI-1": "0000:2e:00.0"}
    assert [boot1[c] for c in order(list(boot1), boot1)] == ["0000:2d:00.0", "0000:2e:00.0"]
    assert [boot2[c] for c in order(list(boot2), boot2)] == ["0000:2d:00.0", "0000:2e:00.0"]
