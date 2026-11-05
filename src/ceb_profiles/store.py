"""Profiles and the history of every screen and audio device CEB has seen.

Files, in $XDG_CONFIG_HOME/ceb/ (default ~/.config/ceb/):
  profiles.json  {"version": 1, "profiles": [Profile.to_json(), ...]}
  known.json     {"version": 1, "screens": [KnownScreen...], "audio": {"outputs": [...], "inputs": [...]}}

Writes go to a temporary file and are renamed into place, under a lock, so the GUI and a keyboard
shortcut running at the same moment cannot corrupt or lose each other's changes. If a file is a
symlink (dotfiles kept in a repo), the write goes to the file it points to and the link stays. A file
that cannot be parsed, or holds values of the wrong type, is reported and never overwritten.

`last_seen` in known.json is kept to LAST_SEEN_RESOLUTION_S: seeing the same screens again does not
rewrite the file unless something else changed or the stored time is older than that, so a keyboard
shortcut does not write the history on every press.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .model import AudioDevice, AudioState, ConnectedMonitor, DisplayState, Identity, ModeSpec, Profile, ScreenId

SCHEMA_VERSION = 1
LAST_SEEN_RESOLUTION_S = 3600  # known.json is not rewritten just to move last_seen by less than this


class StoreError(Exception):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _age_s(earlier: str, later: str) -> float | None:
    """Seconds from `earlier` to `later` (ISO times), or None if either cannot be read."""
    try:
        return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()
    except (TypeError, ValueError):
        return None


def _seen_again(old_last_seen: str, when: str) -> str:
    """The last_seen to store when an entry is seen again with nothing else changed."""
    age = _age_s(old_last_seen, when)
    return old_last_seen if age is not None and 0 <= age < LAST_SEEN_RESOLUTION_S else when


def open_lock_file(path: str | os.PathLike):
    """Open (creating if needed) a lock file safely: never follows a symlink, never truncates, and
    refuses a file owned by another user. Returns a file object; raises StoreError with a clear
    message otherwise."""
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise StoreError(f"lock file {path} is a symlink; CEB will not use it (remove it)") from e
        raise StoreError(f"could not open the lock file {path}: {e.strerror or e}") from e
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        os.close(fd)
        raise StoreError(f"lock file {path} is not a regular file owned by you; CEB will not use it")
    return os.fdopen(fd, "r+b")


def default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "ceb"


def explicit_config_dir(config_dir: str | os.PathLike | None) -> str | None:
    """The config folder as an absolute path when it is not the default one, else None.

    Commands CEB writes for later (keyboard shortcuts, the background watcher's autostart entry)
    carry `--config-dir` only when this is not None, so a store outside the default folder is the
    one they use."""
    if not config_dir:
        return None
    path = Path(config_dir).expanduser().absolute()
    try:
        if path.resolve() == default_config_dir().resolve():
            return None
    except OSError:
        pass
    return str(path)


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
    """A screen CEB has seen at least once. For twins there is one entry per twin rank."""

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
        last_gpu = d.get("last_gpu")
        return cls(ScreenId.from_json(d), _str(d, "display_name"),
                   [KnownMode.from_json(m) for m in _list(d, "modes")], bool(d.get("is_builtin", False)),
                   _str(d, "last_connector"), None if last_gpu is None else _str(d, "last_gpu"),
                   _str(d, "first_seen"), _str(d, "last_seen"))


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
        name = d["name"]
        if not isinstance(name, str):
            raise TypeError(f"audio device name {name!r} is not a string")
        return cls(name, _str(d, "description"), _str(d, "first_seen"), _str(d, "last_seen"))


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
        if not isinstance(audio, dict):
            raise TypeError("'audio' is not an object")
        return cls([KnownScreen.from_json(s) for s in _list(d, "screens")],
                   [KnownAudio.from_json(a) for a in _list(audio, "outputs")],
                   [KnownAudio.from_json(a) for a in _list(audio, "inputs")])

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
        entry.last_seen = when if entry.to_json() != before else _seen_again(entry.last_seen, when)
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
            description = dev.description or entry.description
            if description != entry.description:
                entry.description, entry.last_seen, changed = description, when, True
            else:
                last_seen = _seen_again(entry.last_seen, when)
                changed |= last_seen != entry.last_seen
                entry.last_seen = last_seen
    return changed


def _str(d: dict, key: str) -> str:
    value = d.get(key, "")
    if not isinstance(value, str):
        raise TypeError(f"{key!r} is {value!r}, not a string")
    return value


def _list(d: dict, key: str) -> list:
    value = d.get(key, [])
    if not isinstance(value, list):
        raise TypeError(f"{key!r} is not a list")
    return value


_INVALID = (KeyError, TypeError, ValueError, AttributeError)  # what a hand-edited file can raise


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
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StoreError(f"could not create {self.dir}: {e.strerror or e}") from e
        with open_lock_file(self.dir / ".lock") as handle:
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
            raise StoreError(f"{path} is not valid JSON ({e}); fix or move it, CEB will not overwrite it") from e
        if not isinstance(data, dict):
            raise StoreError(f"{path} does not hold a JSON object; CEB will not overwrite it")
        version = data.get("version", SCHEMA_VERSION)
        if not isinstance(version, int) or isinstance(version, bool):
            raise StoreError(f"{path} has version {version!r}, not a number; CEB will not overwrite it")
        if version > SCHEMA_VERSION:
            raise StoreError(f"{path} was written by a newer CEB (schema {version}); CEB will not overwrite it")
        return data

    def _write(self, path: Path, data: dict) -> None:
        """Atomic write. A symlinked file is written through: the temporary file goes next to the
        link's target and replaces the target, so the link stays a link. OSError is raised as
        StoreError."""
        try:
            self._write_atomic(path, data)
        except OSError as e:
            raise StoreError(f"could not write {path}: {e.strerror or e}") from e

    def _write_atomic(self, path: Path, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        target = Path(os.path.realpath(path)) if path.is_symlink() else path
        fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            with contextlib.suppress(OSError):  # keep the old file's permissions (e.g. 0644)
                os.chmod(tmp, stat.S_IMODE(os.stat(target).st_mode))
            os.replace(tmp, target)
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
            return [Profile.from_json(p) for p in _list(data, "profiles")]
        except _INVALID as e:
            raise StoreError(f"{self.profiles_path} has an invalid profile ({e!r}); CEB will not overwrite it") from e

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
        except _INVALID as e:
            raise StoreError(f"{self.known_path} has an invalid entry ({e!r}); CEB will not overwrite it") from e

    def save_known(self, known: Known) -> None:
        self._write(self.known_path, known.to_json())
