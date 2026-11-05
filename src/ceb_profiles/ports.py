"""Port (connector) ordering, used only to tell identical twin monitors apart.

Why the order is built this way:

- Port *names* are not stable. On a two-GPU machine the driver can number the ports in the order the
  cards finish loading, so the same cables appear as HDMI-1/2/3 on one boot and HDMI-2/3/4 on the
  next. The *relative order* of the ports on one card does not change, because the whole card shifts
  by the same offset.
- So the order key is: GPU (its PCI address, when it can be found), then connector type, then the
  connector's numbers compared as numbers (HDMI-9 before HDMI-10).
- The type comes before the number. Comparing numbers across types would break under a shift:
  HDMI-1 vs DP-2 (HDMI first) can become HDMI-2 vs DP-2 (a tie) on the next boot. Types never change,
  so "all DP ports before all HDMI ports" is stable.
- Kernel and Mutter spell some types differently (kernel "HDMI-A-1", Mutter "HDMI-1"); both normalize
  to the same type name.
"""
from __future__ import annotations

import re

_TYPE_ALIASES = {
    "HDMI-A": "HDMI",
    "HDMIA": "HDMI",
    "DISPLAYPORT": "DP",
    "DVI-D": "DVI",
    "DVI-I": "DVI",
    "DVI-A": "DVI",
}

_NAME = re.compile(r"^(?P<type>.*?)-?(?P<nums>\d+(?:-\d+)*)$")


def split_connector(connector: str) -> tuple[str, tuple[int, ...]]:
    """'HDMI-A-2' -> ('HDMI', (2,)); 'DP-1-3' (MST) -> ('DP', (1, 3)); 'eDP-1' -> ('EDP', (1,)).
    A name without trailing numbers gets an empty number tuple."""
    name = connector.strip()
    m = _NAME.match(name)
    if not m:
        kind, nums = name, ()
    else:
        kind = m.group("type")
        nums = tuple(int(n) for n in m.group("nums").split("-"))
    kind = kind.rstrip("-").upper()
    return _TYPE_ALIASES.get(kind, kind), nums


def port_sort_key(connector: str, gpu: str | None = None) -> tuple:
    """Sort key for ordering twins. `gpu` is the PCI address of the card the port belongs to, or
    None when unknown (unknown sorts before known, consistently)."""
    kind, nums = split_connector(connector)
    return (gpu or "", kind, nums, connector)
