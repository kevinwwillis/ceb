"""`ceb login-layout`: a profile written as GNOME's saved layout (monitors.xml) for the screens
connected now, keyed exactly as Mutter matches it."""
import xml.etree.ElementTree as ET

from conftest import PROJECTOR, ROG, desk, mon, reply
from test_cli import run

from ceb_profiles.monitors_xml import configuration_key

TV = mon("DP-2", PROJECTOR)


def _configs(path):
    return ET.parse(path).getroot().findall("configuration")


def _lm(conf):
    """[(x, y, primary, connector, serial)] for each logical monitor."""
    out = []
    for lm in conf.findall("logicalmonitor"):
        ms = lm.find("monitor/monitorspec")
        out.append((int(lm.findtext("x")), int(lm.findtext("y")), lm.findtext("primary") == "yes",
                    ms.findtext("connector"), ms.findtext("serial")))
    return sorted(out)


def test_writes_the_profile_with_the_tv_disabled(make_ceb, tmp_path):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    ceb.display.set(desk(2, extra=[TV]))          # boot race numbering, TV connected too
    f = tmp_path / "monitors.xml"
    code, _ = run(ceb, "login-layout", "desk", "--file", str(f))
    assert code == 0
    (conf,) = _configs(f)
    assert conf.findtext("layoutmode") == "logical"
    assert _lm(conf) == [(0, 0, False, "HDMI-2", "0x01010101"),
                         (1920, 0, True, "HDMI-4", "L8LMQS075392 "),   # serial kept byte for byte
                         (3840, 0, False, "HDMI-3", "0x01010101")]
    assert [ms.findtext("connector") for ms in conf.findall("disabled/monitorspec")] == ["DP-2"]
    assert conf.find("logicalmonitor/monitor/mode/rate").text == "60.000"


def test_keeps_other_layouts_and_replaces_the_same_screen_set(make_ceb, tmp_path):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    f = tmp_path / "monitors.xml"
    run(ceb, "login-layout", "desk", "--file", str(f))           # 3 screens, numbering 1
    ceb.display.set(desk(2, extra=[TV]))
    run(ceb, "login-layout", "desk", "--file", str(f))           # 4 screens, numbering 2
    assert len(_configs(f)) == 2
    before = f.read_bytes()
    code, _ = run(ceb, "login-layout", "desk", "--file", str(f))  # same again: no change
    assert code == 0 and f.read_bytes() == before
    keys = [configuration_key(c) for c in _configs(f)]
    assert len(set(keys)) == 2


def test_missing_screen_writes_nothing(make_ceb, tmp_path):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    ceb.display.set(reply([mon("HDMI-3", ROG), TV]))  # only the ROG and the TV
    f = tmp_path / "monitors.xml"
    code, _ = run(ceb, "login-layout", "desk", "--file", str(f))
    assert code == 1 and not f.exists()


def test_rejects_a_file_that_is_not_monitors_xml(make_ceb, tmp_path):
    ceb = make_ceb(desk(1))
    run(ceb, "save-current", "desk")
    f = tmp_path / "monitors.xml"
    f.write_text("<other/>")
    code, _ = run(ceb, "login-layout", "desk", "--file", str(f))
    assert code == 1 and f.read_text() == "<other/>"
