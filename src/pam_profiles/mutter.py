"""Read and apply display layouts through GNOME's org.gnome.Mutter.DisplayConfig D-Bus API.

Talks to Mutter directly (no gdctl): gdctl is a thin client of this same API, and calling it would add
a subprocess, text parsing and a tool that is not in every distribution's GNOME packages.

Apply methods (the `method` argument of ApplyMonitorsConfig):
  0 = verify only: Mutter checks the layout and changes nothing;
  1 = temporary: applied at once, no "Keep changes / Revert" prompt, not written to monitors.xml;
  2 = persistent: GNOME shows the "Keep changes / Revert" prompt and stores it in monitors.xml.
PAM only ever uses 0 and 1. The layout stays until GNOME reconfigures the monitors (re-plugging,
reboot); then GNOME falls back to its own stored layout and the profile has to be applied again.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .model import ConnectedMonitor, DisplayState, Identity, LogicalMonitorState, Mode
from .ports import port_sort_key, split_connector

BUS_NAME = "org.gnome.Mutter.DisplayConfig"
OBJECT_PATH = "/org/gnome/Mutter/DisplayConfig"
INTERFACE = "org.gnome.Mutter.DisplayConfig"
DBUS_TIMEOUT_MS = 10000

METHOD_VERIFY = 0
METHOD_TEMPORARY = 1
_ALLOWED_METHODS = (METHOD_VERIFY, METHOD_TEMPORARY)  # never 2: it triggers the Keep/Revert prompt


class DisplayError(Exception):
    """Reading or applying the display configuration failed."""


class StaleSerialError(DisplayError):
    """The monitors changed between reading the state and applying it."""


@dataclass
class ApplyMonitor:
    connector: str
    mode_id: str


@dataclass
class ApplyLogicalMonitor:
    x: int
    y: int
    scale: float
    transform: int
    primary: bool
    monitors: list[ApplyMonitor]


class DisplayBackend(Protocol):
    def get_state(self) -> DisplayState: ...

    def apply(self, serial: int, method: int, logical_monitors: list[ApplyLogicalMonitor],
              properties: dict) -> None: ...


# ---------------------------------------------------------------- GPU of a port (sysfs)


def _sysfs_connector_names(connector: str) -> list[str]:
    """Mutter names HDMI-A ports 'HDMI-n'; the kernel names them 'HDMI-A-n'."""
    kind, nums = split_connector(connector)
    suffix = "-".join(str(n) for n in nums)
    names = [connector]
    if kind == "HDMI" and not connector.upper().startswith("HDMI-A"):
        names.append(f"HDMI-A-{suffix}")
    return names


def find_gpu(connector: str, sysfs_root: str | os.PathLike = "/sys/class/drm") -> str | None:
    """PCI address of the card that owns `connector`, or None if it cannot be told for sure.

    Looks for /sys/class/drm/card<N>-<connector> with status 'connected'. If no card or more than one
    card matches (two cards can both have an 'HDMI-A-1'), the answer is None, and port order falls
    back to the connector name alone."""
    root = Path(sysfs_root)
    if not root.is_dir():
        return None
    gpus = set()
    for name in _sysfs_connector_names(connector):
        for entry in root.glob(f"card*-{name}"):
            card = entry.name.split("-", 1)[0]
            if not card[4:].isdigit():
                continue
            try:
                if (entry / "status").read_text().strip() != "connected":
                    continue
                gpus.add(os.path.basename(os.path.realpath(root / card / "device")))
            except OSError:
                continue
    return gpus.pop() if len(gpus) == 1 else None


# ---------------------------------------------------------------- GetCurrentState


def parse_current_state(reply: tuple, gpu_of=lambda connector: None) -> DisplayState:
    """Turn the unpacked GetCurrentState reply into a DisplayState and rank identical twins.

    reply = (serial, monitors, logical_monitors, properties), as returned by GLib's unpack():
      monitors: [((connector, vendor, product, serial), [mode...], {props})]
      mode: (id, width, height, refresh, preferred_scale, [supported_scales], {props})
      logical_monitors: [(x, y, scale, transform, primary, [(connector, vendor, product, serial)], {})]
    """
    serial, monitors_raw, logical_raw, props = reply
    monitors: list[ConnectedMonitor] = []
    for (connector, vendor, product, mserial), modes_raw, mprops in monitors_raw:
        modes = [Mode(id=m[0], width=int(m[1]), height=int(m[2]), refresh=float(m[3]),
                      preferred_scale=float(m[4]), supported_scales=tuple(float(s) for s in m[5]),
                      is_current=bool(m[6].get("is-current", False)),
                      is_preferred=bool(m[6].get("is-preferred", False)))
                 for m in modes_raw]
        identity = Identity.of(vendor, product, mserial)
        monitors.append(ConnectedMonitor(
            connector=connector, identity=identity,
            display_name=str(mprops.get("display-name") or f"{vendor} {product}".strip()),
            modes=modes, is_builtin=bool(mprops.get("is-builtin", False)),
            gpu=gpu_of(connector)))
    rank_twins(monitors)
    logical = [LogicalMonitorState(x=int(x), y=int(y), scale=float(scale), transform=int(transform),
                                   primary=bool(primary), connectors=[spec[0] for spec in specs])
               for x, y, scale, transform, primary, specs, _lprops in logical_raw]
    layout_mode = props.get("layout-mode")
    return DisplayState(serial=int(serial), monitors=monitors, logical_monitors=logical,
                        layout_mode=int(layout_mode) if layout_mode is not None else None,
                        supports_changing_layout_mode=bool(props.get("supports-changing-layout-mode", False)))


def rank_twins(monitors: list[ConnectedMonitor]) -> None:
    """Give each monitor its twin rank: among connected monitors with the same identity, 0 is the one
    on the lowest port (by port_sort_key), 1 the next, and so on.

    The GPU is part of the key only when it is known for every twin in the group. If it is unknown
    for one of them (sysfs unreadable, status not 'connected' during a hotplug, a port name found
    on two cards), the whole group is ordered by port name alone. Mixing the two would let a failed
    lookup reorder the twins: an unknown GPU would sort before every known one."""
    groups: dict[Identity, list[ConnectedMonitor]] = {}
    for m in monitors:
        groups.setdefault(m.identity, []).append(m)
    for group in groups.values():
        use_gpu = all(m.gpu for m in group)
        group.sort(key=lambda m: port_sort_key(m.connector, m.gpu if use_gpu else None))
        for rank, m in enumerate(group):
            m.twin, m.twin_count = rank, len(group)
            m.twin_order_without_gpu = len(group) > 1 and not use_gpu and any(t.gpu for t in group)


# ---------------------------------------------------------------- the D-Bus backend


class MutterDisplayConfig:
    """The real backend. Imports gi lazily so the rest of the package works without it (tests)."""

    def __init__(self, sysfs_root: str = "/sys/class/drm"):
        self._sysfs_root = sysfs_root
        self._bus = None

    def _proxy_call(self, method: str, params, timeout_ms: int = DBUS_TIMEOUT_MS):
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except (ImportError, ValueError) as e:
            raise DisplayError(f"PyGObject (python3-gi) is not available: {e}") from e
        try:
            if self._bus is None:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            return self._bus.call_sync(BUS_NAME, OBJECT_PATH, INTERFACE, method, params, None,
                                       Gio.DBusCallFlags.NONE, timeout_ms, None)
        except GLib.Error as e:
            message = remote_error_message(e)
            if "stale" in message.lower():
                raise StaleSerialError(message) from e
            raise DisplayError(f"{method} failed: {message}") from e

    def get_state(self) -> DisplayState:
        reply = self._proxy_call("GetCurrentState", None)
        return parse_current_state(reply.unpack(), lambda c: find_gpu(c, self._sysfs_root))

    def apply(self, serial: int, method: int, logical_monitors: list[ApplyLogicalMonitor],
              properties: dict) -> None:
        if method not in _ALLOWED_METHODS:
            raise ValueError(f"PAM never applies with method {method}; only 0 (verify) and 1 (temporary)")
        from gi.repository import GLib
        self._proxy_call("ApplyMonitorsConfig", build_apply_variant(serial, method, logical_monitors,
                                                                    properties, GLib))


def remote_error_message(error, Gio=None) -> str:
    """The text of a D-Bus error without the 'GDBus.Error:org.freedesktop.DBus.Error.X: ' prefix.
    (Gio.DBusError.strip_remote_error does not change the Python exception's message.)"""
    return _REMOTE_PREFIX.sub("", str(error.message))


_REMOTE_PREFIX = re.compile(r"^GDBus\.Error:[\w.\-]+:\s*")


def build_apply_variant(serial: int, method: int, logical_monitors: list[ApplyLogicalMonitor],
                        properties: dict, GLib):
    lms = [(lm.x, lm.y, float(lm.scale), lm.transform, lm.primary,
            [(m.connector, m.mode_id, {}) for m in lm.monitors])
           for lm in logical_monitors]
    props = {}
    if "layout-mode" in properties:
        props["layout-mode"] = GLib.Variant("u", int(properties["layout-mode"]))
    return GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, method, lms, props))
