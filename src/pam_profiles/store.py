"""Profiles and the history of every screen and audio device PAM has seen.

Files, in $XDG_CONFIG_HOME/pam/ (default ~/.config/pam/):
  profiles.json  {"version": 1, "profiles": [Profile.to_json(), ...]}
  known.json     {"version": 1, "screens": [KnownScreen...], "audio": {"outputs": [...], "inputs": [...]}}

Writes go to a temporary file and are renamed into place, under a lock, so the GUI and a keyboard
shortcut running at the same moment cannot corrupt or lose each other's changes. A file that cannot
be parsed is reported and never overwritten.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .model import AudioDevice, AudioState, ConnectedMonitor, DisplayState, Identity, ModeSpec, Profile, ScreenId

SCHEMA_VERSION = 1


class StoreError(Exception):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "pam"


@dataclass
class KnownMode:
    width: int
    height: int
    refresh: float
    preferred_scale: float = 1.0
    supported_scales: tuple[float, ...] = (1.0,)
    is_preferred: bool = False

    def spec(self) -> ModeSpec:
        return ModeSpec(self.width, self.height, self.refresh)

    def to_json(self) -> dict:
        return {"width": self.width, "height": self.height, "refresh": self.refresh,
                "preferred_scale": self.preferred_scale,
                "supported_scales": list(self.supported_scales), "is_preferred": self.is_preferred}

    @classmethod
    def from_json(cls, d: dict) -> "KnownMode":
        return cls(int(d["width"]), int(d["height"]), float(d["refresh"]),
                   float(d.get("preferred_scale", 1.0)),
                   tuple(float(s) for s in d.get("supported_scales", [1.0])),
                   bool(d.get("is_preferred", False)))


@dataclass
class KnownScreen:
    """A screen PAM has seen at least once. For twins there is one entry per twin rank."""

    screen: ScreenId
    display_name: str
    modes: list[KnownMode]
    is_builtin: bool = False
    last_connector: str = ""
    last_gpu: str | None = None
    first_seen: str = ""
    last_seen: str = ""

    def preferred_mode(self) -> KnownMode | None:
        return next((m for m in self.modes if m.is_preferred), self.modes[0] if self.modes else None)

    def to_json(self) -> dict:
        d = self.screen.to_json()
        d.update({"display_name": self.display_name, "modes": [m.to_json() for m in self.modes],
                  "is_builtin": self.is_builtin, "last_connector": self.last_connector,
                  "last_gpu": self.last_gpu, "first_seen": self.first_seen, "last_seen": self.last_seen})
        return d

    @classmethod
    def from_json(cls, d: dict) -> "KnownScreen":
        return cls(ScreenId.from_json(d), d.get("display_name", ""),
                   [KnownMode.from_json(m) for m in d.get("modes", [])], bool(d.get("is_builtin", False)),
                   d.get("last_connector", ""), d.get("last_gpu"), d.get("first_seen", ""),
                   d.get("last_seen", ""))


@dataclass
class KnownAudio:
    name: str
    description: str
    first_seen: str = ""
    last_seen: str = ""

    def to_json(self) -> dict:
        return {"name": self.name, "description": self.description,
                "first_seen": self.first_seen, "last_seen": self.last_seen}

    @classmethod
    def from_json(cls, d: dict) -> "KnownAudio":
        return cls(d["name"], d.get("description", ""), d.get("first_seen", ""), d.get("last_seen", ""))


@dataclass
class Known:
    screens: list[KnownScreen] = field(default_factory=list)
    outputs: list[KnownAudio] = field(default_factory=list)
    inputs: list[KnownAudio] = field(default_factory=list)

    def screen(self, sid: ScreenId) -> KnownScreen | None:
        return next((s for s in self.screens if s.screen == sid), None)

    def twins_of(self, identity: Identity) -> list[KnownScreen]:
        return [s for s in self.screens if s.screen.identity == identity]

    def to_json(self) -> dict:
        return {"version": SCHEMA_VERSION, "screens": [s.to_json() for s in self.screens],
                "audio": {"outputs": [a.to_json() for a in self.outputs],
                          "inputs": [a.to_json() for a in self.inputs]}}

    @classmethod
    def from_json(cls, d: dict) -> "Known":
        audio = d.get("audio") or {}
        return cls([KnownScreen.from_json(s) for s in d.get("screens", [])],
                   [KnownAudio.from_json(a) for a in audio.get("outputs", [])],
                   [KnownAudio.from_json(a) for a in audio.get("inputs", [])])

    # -- merging what is connected now

    def observe_display(self, state: DisplayState, when: str | None = None) -> bool:
        """Record every connected screen. Returns True if anything changed."""
        when = when or now_iso()
        changed = False
        for m in state.monitors:
            changed |= self._observe_screen(m, when)
        return changed

    def _observe_screen(self, m: ConnectedMonitor, when: str) -> bool:
        modes = [KnownMode(x.width, x.height, x.refresh, x.preferred_scale, x.supported_scales,
                           x.is_preferred) for x in m.modes]
        entry = self.screen(m.screen_id)
        if entry is None:
            self.screens.append(KnownScreen(m.screen_id, m.display_name, modes, m.is_builtin,
                                            m.connector, m.gpu, when, when))
            return True
        before = entry.to_json()
        entry.display_name, entry.is_builtin = m.display_name, m.is_builtin
        entry.modes = modes or entry.modes
        entry.last_connector, entry.last_gpu = m.connector, m.gpu
        entry.last_seen = when
        return entry.to_json() != before

    def observe_audio(self, state: AudioState, when: str | None = None) -> bool:
        when = when or now_iso()
        changed = _merge_audio(self.outputs, state.outputs, when)
        changed |= _merge_audio(self.inputs, state.inputs, when)
        return changed


def _merge_audio(known: list[KnownAudio], present: list[AudioDevice], when: str) -> bool:
    changed = False
    for dev in present:
        entry = next((k for k in known if k.name == dev.name), None)
        if entry is None:
            known.append(KnownAudio(dev.name, dev.description, when, when))
            changed = True
        else:
            changed |= entry.description != dev.description or entry.last_seen != when
            entry.description, entry.last_seen = dev.description or entry.description, when
    return changed


class Store:
    def __init__(self, config_dir: str | os.PathLike | None = None):
        self.dir = Path(config_dir) if config_dir else default_config_dir()

    @property
    def profiles_path(self) -> Path:
        return self.dir / "profiles.json"

    @property
    def known_path(self) -> Path:
        return self.dir / "known.json"

    @contextlib.contextmanager
    def locked(self):
        """Hold the store lock for a read-modify-write."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self, path: Path) -> dict | None:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            raise StoreError(f"could not read {path}: {e}") from e
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise StoreError(f"{path} is not valid JSON ({e}); fix or move it, PAM will not overwrite it") from e
        if not isinstance(data, dict):
            raise StoreError(f"{path} does not hold a JSON object; PAM will not overwrite it")
        version = data.get("version", SCHEMA_VERSION)
        if version > SCHEMA_VERSION:
            raise StoreError(f"{path} was written by a newer PAM (schema {version}); PAM will not overwrite it")
        return data

    def _write(self, path: Path, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=self.dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    # -- profiles

    def load_profiles(self) -> list[Profile]:
        data = self._read(self.profiles_path)
        if data is None:
            return []
        try:
            return [Profile.from_json(p) for p in data.get("profiles", [])]
        except (KeyError, TypeError, ValueError) as e:
            raise StoreError(f"{self.profiles_path} has an invalid profile ({e!r}); PAM will not overwrite it") from e

    def save_profiles(self, profiles: list[Profile]) -> None:
        self._write(self.profiles_path, {"version": SCHEMA_VERSION,
                                         "profiles": [p.to_json() for p in profiles]})

    # -- known screens and audio devices

    def load_known(self) -> Known:
        data = self._read(self.known_path)
        if data is None:
            return Known()
        try:
            return Known.from_json(data)
        except (KeyError, TypeError, ValueError) as e:
            raise StoreError(f"{self.known_path} has an invalid entry ({e!r}); PAM will not overwrite it") from e

    def save_known(self, known: Known) -> None:
        self._write(self.known_path, known.to_json())
