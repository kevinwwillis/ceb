"""Files CEB puts in the user's desktop: the application menu entry and icon (`ceb install-desktop`)
and the autostart entry of the background watcher (`ceb watch`; the window's "Remember Screens in
the Background").

Everything goes under the user's own folders ($XDG_DATA_HOME, default ~/.local/share, and
$XDG_CONFIG_HOME/autostart, default ~/.config/autostart). Commands written into these files use the
absolute path of the installed `ceb`/`ceb-gui`, because programs started by GNOME do not get the
PATH of the user's shell. No gi import here: the CLI uses this module too.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import signal
import subprocess
import sys
from importlib import resources
from pathlib import Path

from .gui import APP_ID

WATCH_ID = APP_ID + ".Watch"
_RESERVED = set(' \t\n"\'\\><~|&;$*?#()`')


def find_executable(name: str) -> str | None:
    """Absolute path of an installed CEB command (`ceb`, `ceb-gui`): the one next to the running
    program, else next to this Python, else the one on PATH; None if there is none."""
    for folder in (os.path.dirname(os.path.abspath(sys.argv[0] or "")), os.path.dirname(sys.executable)):
        here = os.path.join(folder, name)
        if os.path.isfile(here) and os.access(here, os.X_OK):
            return here
    found = shutil.which(name)
    return os.path.abspath(found) if found else None


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"))


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"))


def exec_line(argv: list[str]) -> str:
    """argv as the value of a desktop entry's Exec key (Desktop Entry Specification, "The Exec
    key"): an argument with a reserved character is double-quoted, with ", `, $ and \\ escaped by a
    backslash; % is doubled; then the value's own escaping doubles every backslash."""
    out = []
    for arg in argv:
        arg = arg.replace("%", "%%")
        if arg == "" or any(ch in _RESERVED for ch in arg):
            arg = '"' + "".join("\\" + ch if ch in '"`$\\' else ch for ch in arg) + '"'
        out.append(arg)
    return " ".join(out).replace("\\", "\\\\")


# ---------------------------------------------------------------- application menu entry


def _data_file(*parts: str) -> bytes:
    return resources.files("ceb_profiles.gui").joinpath("data", *parts).read_bytes()


def desktop_paths(base: Path | None = None) -> tuple[Path, Path]:
    base = base or data_home()
    return (base / "applications" / f"{APP_ID}.desktop",
            base / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg")


def install_desktop(base: Path | None = None, gui: str | None = None) -> list[str]:
    """Write the menu entry (Exec = the absolute path of ceb-gui) and the icon. Returns what was
    done, one line each. RuntimeError if ceb-gui cannot be found."""
    gui = gui or find_executable("ceb-gui")
    if not gui:
        raise RuntimeError("could not find the installed ceb-gui command; install CEB first (see the README)")
    entry_path, icon_path = desktop_paths(base)
    text = _data_file(f"{APP_ID}.desktop").decode()
    lines = [f"Exec={exec_line([gui])}" if line.startswith("Exec=") else line for line in text.splitlines()]
    for path, data in ((entry_path, ("\n".join(lines) + "\n").encode()),
                       (icon_path, _data_file("icons", "hicolor", "scalable", "apps", f"{APP_ID}.svg"))):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return [f"wrote {entry_path} (runs {gui})", f"wrote {icon_path}",
            "CEB is now in the applications menu (log out and in if it does not show at once)"]


def uninstall_desktop(base: Path | None = None) -> list[str]:
    out = []
    for path in desktop_paths(base):
        if path.is_file():
            path.unlink()
            out.append(f"removed {path}")
    return out or ["nothing to remove"]


# ---------------------------------------------------------------- background watcher


def autostart_path(base: Path | None = None) -> Path:
    return (base or config_home()) / "autostart" / f"{WATCH_ID}.desktop"


def watch_argv(config_dir: str | None, ceb: str | None = None) -> list[str]:
    """`ceb [--config-dir DIR] watch` with the absolute path of ceb. RuntimeError if not found."""
    ceb = ceb or find_executable("ceb")
    if not ceb:
        raise RuntimeError("could not find the installed `ceb` command, which the background watcher runs; "
                           "install CEB (see the README) so that `ceb` is on your PATH")
    return [ceb] + (["--config-dir", config_dir] if config_dir else []) + ["watch"]


def autostart_enabled(config_dir: str | None, base: Path | None = None) -> bool:
    """True when the autostart entry exists and runs the watcher for this config folder."""
    path = autostart_path(base)
    try:
        text = path.read_text()
    except OSError:
        return False
    exec_ = next((line[5:] for line in text.splitlines() if line.startswith("Exec=")), "")
    if config_dir is None:
        return "--config-dir" not in exec_ and exec_.endswith(" watch")
    return exec_.endswith(" " + exec_line(["--config-dir", config_dir, "watch"]))


def enable_autostart(config_dir: str | None, base: Path | None = None, ceb: str | None = None) -> Path:
    """Write the autostart entry that runs `ceb watch` at login. Returns its path."""
    argv = watch_argv(config_dir, ceb)
    path = autostart_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("[Desktop Entry]\n"
            "Type=Application\n"
            "Name=CEB Screen Memory\n"
            "Comment=Remembers every screen and audio device that is connected, for CEB's profiles\n"
            f"Exec={exec_line(argv)}\n"
            f"Icon={APP_ID}\n"
            "NoDisplay=true\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return path


def disable_autostart(base: Path | None = None) -> bool:
    """Remove the autostart entry. Returns False if there was none."""
    path = autostart_path(base)
    if path.is_file() or path.is_symlink():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------- the running watcher


def watch_lock_path(config_dir: str | os.PathLike) -> Path:
    """One watcher per config folder: $XDG_RUNTIME_DIR/ceb-watch-<hash of the folder>.lock (else in
    the config folder). The file holds the watcher's process id."""
    folder = os.path.abspath(os.fspath(config_dir))
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return Path(runtime) / f"ceb-watch-{hashlib.sha1(folder.encode()).hexdigest()[:12]}.lock"
    return Path(folder) / ".watch.lock"


def running_watcher(config_dir: str | os.PathLike) -> int | None:
    """Process id of the watcher running for this folder, or None."""
    path = watch_lock_path(config_dir)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:  # held: a watcher runs
            try:
                return int(os.pread(fd, 32, 0).decode().strip() or 0) or None
            except ValueError:
                return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def start_watcher(config_dir: str | None, store_dir: str | os.PathLike, ceb: str | None = None) -> bool:
    """Start `ceb watch` for this folder now, detached from this program, unless one runs.
    Returns True if one was started."""
    if running_watcher(store_dir):
        return False
    subprocess.Popen(watch_argv(config_dir, ceb), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    return True


def stop_watcher(store_dir: str | os.PathLike) -> bool:
    """Ask the watcher running for this folder to end (SIGTERM). Returns True if there was one."""
    pid = running_watcher(store_dir)
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True
