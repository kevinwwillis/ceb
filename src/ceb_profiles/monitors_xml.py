"""Write a profile into GNOME's saved-layout file, monitors.xml, for the screens connected now.

GNOME (Mutter) reads monitors.xml when it starts, at the login screen and at login, and uses the
saved layout whose set of screens is exactly the set connected. Each layout is keyed by every
screen's port and EDID strings (connector, vendor, product, serial), so on a machine whose ports are
renumbered between boots one layout is needed per numbering. `ceb login-layout` writes the layout for
the current numbering; running it at every login covers each numbering the first time it is seen.

Only the layout for the exact set of screens connected now is added or replaced. Every other saved
layout is kept unchanged, so with other screens connected GNOME does what it did before.
"""
from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .model import DisplayState
from .planner import DisplayPlan

# Mutter's transform numbers (DisplayConfig) -> monitors.xml <rotation>, <flipped>.
_TRANSFORMS = {0: ("normal", "no"), 1: ("left", "no"), 2: ("upside_down", "no"), 3: ("right", "no"),
               4: ("normal", "yes"), 5: ("left", "yes"), 6: ("upside_down", "yes"), 7: ("right", "yes")}
_LAYOUT_MODES = {1: "logical", 2: "physical"}


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "monitors.xml"


def _text(parent: ET.Element, tag: str, value: str) -> ET.Element:
    el = ET.SubElement(parent, tag)
    el.text = value
    return el


def _monitorspec(parent: ET.Element, spec: tuple[str, str, str, str]) -> None:
    ms = ET.SubElement(parent, "monitorspec")
    for tag, value in zip(("connector", "vendor", "product", "serial"), spec):
        _text(ms, tag, value)


def configuration_element(plan: DisplayPlan, state: DisplayState) -> ET.Element:
    """The <configuration> for `plan` (from plan_display, without problems) on the screens in `state`.
    Screens the plan turns off are listed as disabled, so the layout's key is every connected screen."""
    if plan.problems:
        raise ValueError("the plan has problems; nothing to write")
    conf = ET.Element("configuration")
    if state.layout_mode in _LAYOUT_MODES:
        _text(conf, "layoutmode", _LAYOUT_MODES[state.layout_mode])
    for lm in plan.logical_monitors:
        lel = ET.SubElement(conf, "logicalmonitor")
        _text(lel, "x", str(lm.x))
        _text(lel, "y", str(lm.y))
        _text(lel, "scale", f"{lm.scale:.17g}")
        if lm.transform:
            rotation, flipped = _TRANSFORMS[lm.transform]
            tel = ET.SubElement(lel, "transform")
            _text(tel, "rotation", rotation)
            _text(tel, "flipped", flipped)
        if lm.primary:
            _text(lel, "primary", "yes")
        for am in lm.monitors:
            cm = state.by_connector(am.connector)
            mode = next(m for m in cm.modes if m.id == am.mode_id)
            mel = ET.SubElement(lel, "monitor")
            _monitorspec(mel, cm.raw_spec)
            modeel = ET.SubElement(mel, "mode")
            _text(modeel, "width", str(mode.width))
            _text(modeel, "height", str(mode.height))
            _text(modeel, "rate", f"{mode.refresh:.3f}")
    if plan.turned_off:
        disabled = ET.SubElement(conf, "disabled")
        for cm in plan.turned_off:
            _monitorspec(disabled, cm.raw_spec)
    return conf


def configuration_key(conf: ET.Element) -> frozenset[tuple[str, ...]]:
    """Every screen a <configuration> names (on or disabled), as Mutter matches it."""
    return frozenset(tuple((ms.findtext(t) or "") for t in ("connector", "vendor", "product", "serial"))
                     for ms in conf.iter("monitorspec"))


def merge(path: str | os.PathLike, conf: ET.Element) -> bool:
    """Put `conf` into the monitors.xml at `path`, replacing any saved layout for the same set of
    screens and keeping all others. Returns True when the file changed. Written atomically."""
    path = Path(path)
    if path.exists():
        root = ET.parse(path).getroot()
        if root.tag != "monitors":
            raise ValueError(f"{path} is not a GNOME monitors.xml (root element <{root.tag}>)")
    else:
        root = ET.Element("monitors", {"version": "2"})
    before = ET.tostring(root)
    key = configuration_key(conf)
    for old in [c for c in root.findall("configuration") if configuration_key(c) == key]:
        root.remove(old)
    root.append(conf)
    ET.indent(root, space="  ")
    data = ET.tostring(root, encoding="unicode") + "\n"
    if path.exists() and ET.tostring(ET.fromstring(data)) == before:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".monitors.xml.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return True
