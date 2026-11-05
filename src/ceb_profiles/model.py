"""Data types shared by the display backend, the planner, the store and the GUI."""
from __future__ import annotations

from dataclasses import dataclass, field

REFRESH_TOLERANCE = 0.01  # Hz; Mutter reports e.g. 59.940 and 60.000 as distinct modes
SCALE_TOLERANCE = 1e-3


@dataclass(frozen=True)
class Identity:
    """What a monitor says it is (from its EDID, as Mutter reports it). Never includes the port.

    Whitespace around the fields is dropped: Mutter passes the EDID serial through as-is, and some
    monitors pad it (the ASUS ROG PG248Q reports 'L8LMQS075392 ')."""

    vendor: str
    product: str
    serial: str

    @classmethod
    def of(cls, vendor: str, product: str, serial: str) -> "Identity":
        return cls(vendor.strip(), product.strip(), serial.strip())

    def describe(self) -> str:
        # Some monitors repeat the vendor code in the product name ('LEN' + 'LEN LI2364d').
        product = self.product
        name = product if self.vendor and product.split(" ", 1)[0] == self.vendor else f"{self.vendor} {product}"
        return f"{name.strip()}, serial {self.serial or '(none)'}"


@dataclass(frozen=True)
class ScreenId:
    """One physical screen: its identity plus its twin rank.

    `twin` is 0 for any screen whose identity is unique among the connected screens. When several
    connected screens share an identity (identical twins, e.g. two monitors with the same EDID
    serial), they are ranked by port order (see ports.port_sort_key): 0 = lowest port, 1 = next..."""

    identity: Identity
    twin: int = 0

    def to_json(self) -> dict:
        return {"vendor": self.identity.vendor, "product": self.identity.product,
                "serial": self.identity.serial, "twin": self.twin}

    @classmethod
    def from_json(cls, d: dict) -> "ScreenId":
        return cls(Identity.of(d["vendor"], d["product"], d["serial"]), int(d.get("twin", 0)))


@dataclass(frozen=True)
class Mode:
    """A mode a connected monitor offers (Mutter's mode id is valid only for this session)."""

    id: str
    width: int
    height: int
    refresh: float
    preferred_scale: float = 1.0
    supported_scales: tuple[float, ...] = (1.0,)
    is_current: bool = False
    is_preferred: bool = False

    def spec(self) -> "ModeSpec":
        return ModeSpec(self.width, self.height, self.refresh)


@dataclass(frozen=True)
class ModeSpec:
    """A mode as stored in a profile: size and refresh rate, no session-specific id."""

    width: int
    height: int
    refresh: float

    def matches(self, mode: Mode) -> bool:
        return (mode.width == self.width and mode.height == self.height
                and abs(mode.refresh - self.refresh) < REFRESH_TOLERANCE)

    def describe(self) -> str:
        return f"{self.width}x{self.height}@{self.refresh:.3f}"

    def to_json(self) -> dict:
        return {"width": self.width, "height": self.height, "refresh": self.refresh}

    @classmethod
    def from_json(cls, d: dict) -> "ModeSpec":
        return cls(int(d["width"]), int(d["height"]), float(d["refresh"]))


@dataclass
class ConnectedMonitor:
    """A monitor connected right now."""

    connector: str
    identity: Identity
    display_name: str
    modes: list[Mode]
    is_builtin: bool = False
    gpu: str | None = None  # PCI address of its card, when it could be found
    twin: int = 0
    twin_count: int = 1
    # True when this monitor's twins were ordered by port name only because the GPU of some (not
    # all) of them could not be read; the planner reports it as a note.
    twin_order_without_gpu: bool = False

    @property
    def screen_id(self) -> ScreenId:
        return ScreenId(self.identity, self.twin)

    def current_mode(self) -> Mode | None:
        return next((m for m in self.modes if m.is_current), None)

    def preferred_mode(self) -> Mode | None:
        return next((m for m in self.modes if m.is_preferred), None)

    def find_mode(self, spec: ModeSpec) -> Mode | None:
        return next((m for m in self.modes if spec.matches(m)), None)


@dataclass
class LogicalMonitorState:
    """One logical monitor (a region of the desktop; more than one monitor in it = mirroring)."""

    x: int
    y: int
    scale: float
    transform: int
    primary: bool
    connectors: list[str]


@dataclass
class DisplayState:
    """Everything GetCurrentState says, in CEB's terms."""

    serial: int
    monitors: list[ConnectedMonitor]
    logical_monitors: list[LogicalMonitorState]
    layout_mode: int | None = None  # 1 = logical (positions in scaled pixels), 2 = physical
    supports_changing_layout_mode: bool = False

    def by_connector(self, connector: str) -> ConnectedMonitor | None:
        return next((m for m in self.monitors if m.connector == connector), None)

    def by_screen(self) -> dict[ScreenId, ConnectedMonitor]:
        return {m.screen_id: m for m in self.monitors}


# ---------------------------------------------------------------- profiles


@dataclass
class ProfileScreen:
    screen: ScreenId
    mode: ModeSpec
    connector_when_saved: str = ""  # information only; never used to find the screen

    def to_json(self) -> dict:
        d = self.screen.to_json()
        d["mode"] = self.mode.to_json()
        if self.connector_when_saved:
            d["connector_when_saved"] = self.connector_when_saved
        return d

    @classmethod
    def from_json(cls, d: dict) -> "ProfileScreen":
        return cls(ScreenId.from_json(d), ModeSpec.from_json(d["mode"]),
                   d.get("connector_when_saved", ""))


@dataclass
class ProfileLogicalMonitor:
    x: int
    y: int
    scale: float
    transform: int
    primary: bool
    screens: list[ProfileScreen]

    def to_json(self) -> dict:
        return {"x": self.x, "y": self.y, "scale": self.scale, "transform": self.transform,
                "primary": self.primary, "screens": [s.to_json() for s in self.screens]}

    @classmethod
    def from_json(cls, d: dict) -> "ProfileLogicalMonitor":
        return cls(int(d["x"]), int(d["y"]), float(d.get("scale", 1.0)), int(d.get("transform", 0)),
                   bool(d.get("primary", False)), [ProfileScreen.from_json(s) for s in d["screens"]])


@dataclass
class DisplayLayout:
    """The screens a profile turns on, where, and which is primary. Every connected screen not
    listed here is turned off when the profile is applied."""

    logical_monitors: list[ProfileLogicalMonitor]
    layout_mode: int | None = None

    def screens(self) -> list[ProfileScreen]:
        return [s for lm in self.logical_monitors for s in lm.screens]

    def to_json(self) -> dict:
        return {"layout_mode": self.layout_mode,
                "logical_monitors": [lm.to_json() for lm in self.logical_monitors]}

    @classmethod
    def from_json(cls, d: dict) -> "DisplayLayout":
        return cls([ProfileLogicalMonitor.from_json(lm) for lm in d["logical_monitors"]],
                   d.get("layout_mode"))


@dataclass(frozen=True)
class AudioRef:
    """An audio device as stored: its PulseAudio/PipeWire node name (stable across reboots, unlike
    the numeric index) and the description shown to people."""

    name: str
    description: str = ""

    def describe(self) -> str:
        return f"{self.description} ({self.name})" if self.description else self.name

    def to_json(self) -> dict:
        return {"name": self.name, "description": self.description}

    @classmethod
    def from_json(cls, d: dict | None) -> "AudioRef | None":
        return None if d is None else cls(d["name"], d.get("description", ""))


@dataclass
class Profile:
    """A named profile. `displays`, `audio_output` or `audio_input` set to None means the profile
    leaves that part unchanged."""

    name: str
    displays: DisplayLayout | None = None
    audio_output: AudioRef | None = None
    audio_input: AudioRef | None = None

    def to_json(self) -> dict:
        return {"name": self.name,
                "displays": self.displays.to_json() if self.displays else None,
                "audio": {"output": self.audio_output.to_json() if self.audio_output else None,
                          "input": self.audio_input.to_json() if self.audio_input else None}}

    @classmethod
    def from_json(cls, d: dict) -> "Profile":
        audio = d.get("audio") or {}
        if not isinstance(d["name"], str):
            raise TypeError(f"profile name {d['name']!r} is not a string")
        return cls(d["name"],
                   DisplayLayout.from_json(d["displays"]) if d.get("displays") else None,
                   AudioRef.from_json(audio.get("output")), AudioRef.from_json(audio.get("input")))


# ---------------------------------------------------------------- audio state


@dataclass(frozen=True)
class AudioDevice:
    name: str
    description: str

    def ref(self) -> AudioRef:
        return AudioRef(self.name, self.description)


@dataclass
class AudioState:
    outputs: list[AudioDevice] = field(default_factory=list)
    inputs: list[AudioDevice] = field(default_factory=list)
    default_output: str | None = None
    default_input: str | None = None
