"""Per-profile keyboard shortcuts as GNOME custom shortcuts.

GNOME keeps custom shortcuts in gsettings: the list
  org.gnome.settings-daemon.plugins.media-keys custom-keybindings
holds one path per shortcut, and each path has the relocatable schema
  org.gnome.settings-daemon.plugins.media-keys.custom-keybinding
with `name`, `command` and `binding` (e.g. '<Super>F1'). They show up and can be edited in
GNOME Settings > Keyboard > Custom Shortcuts.

CEB's shortcuts run `ceb apply <profile>`. A shortcut belongs to a profile when its command is
exactly that; CEB never touches other custom shortcuts. Every function takes a Gio settings backend
so tests (and `ceb-gui --demo`) can use an in-memory backend instead of the user's real settings.

Shortcuts made while the app was called PAM (until 2026-11-05) run `pam apply <profile>`. One counts
as ours when its command is exactly that and either its name is still "PAM: <profile>" or it sits at
a path the app made (custom-keybindings/pam<N>/); a `pam apply` shortcut with neither is someone
else's and is left alone. Ours are treated as the profile's shortcut, and `upgrade_legacy()` (run
when the window opens) rewrites them in place to `ceb apply <profile>` with the name "CEB: <profile>",
keeping the key and the path.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM = MEDIA_KEYS + ".custom-keybinding"
CUSTOM_DIR = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
NAME_PREFIX = "CEB: "
LEGACY_EXECUTABLE = "pam"            # the command's name until the rename to CEB, 2026-11-05
LEGACY_NAME_PREFIX = "PAM: "
_LEGACY_PATH = re.compile(re.escape(CUSTOM_DIR) + r"pam\d+/\Z")

# Schemas whose keys hold GNOME's own shortcuts, checked for clashes.
SYSTEM_SCHEMAS = ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings",
                  "org.gnome.mutter.keybindings", "org.gnome.mutter.wayland.keybindings",
                  MEDIA_KEYS)


class ShortcutError(Exception):
    pass


def ceb_command() -> str:
    """The `ceb` executable a shortcut should run: the one installed next to this program, else the
    one on PATH, else plain 'ceb'."""
    here = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0] or "")), "ceb")
    if os.path.isfile(here) and os.access(here, os.X_OK):
        return here
    return shutil.which("ceb") or "ceb"


def _schema(schema_id: str):
    source = Gio.SettingsSchemaSource.get_default()
    return source.lookup(schema_id, True) if source else None


class Shortcuts:
    def __init__(self, backend: Gio.SettingsBackend | None = None, command: str | None = None):
        self.backend = backend
        self.command = command or ceb_command()
        self.available = _schema(MEDIA_KEYS) is not None and _schema(CUSTOM) is not None

    # -------------------------------------------------------------- settings objects

    def _settings(self, schema_id: str, path: str | None = None) -> Gio.Settings:
        schema = _schema(schema_id)
        if schema is None:
            raise ShortcutError(f"GNOME's shortcut settings ({schema_id}) are not installed")
        return Gio.Settings.new_full(schema, self.backend, path)

    def _list(self) -> list[str]:
        return list(self._settings(MEDIA_KEYS).get_strv("custom-keybindings"))

    def _entry(self, path: str) -> Gio.Settings:
        return self._settings(CUSTOM, path)

    # -------------------------------------------------------------- reading

    def command_for(self, profile: str) -> str:
        return " ".join(shlex.quote(a) for a in (self.command, "apply", profile))

    @staticmethod
    def profile_of(command: str, executable: str = "ceb") -> str | None:
        """The profile a custom shortcut's command applies, if it is `<executable> apply <profile>`."""
        try:
            ok, argv = GLib.shell_parse_argv(command)
        except GLib.Error:
            return None
        if ok and len(argv) == 3 and os.path.basename(argv[0]) == executable and argv[1] == "apply":
            return argv[2]
        return None

    @classmethod
    def legacy_profile_of(cls, path: str, name: str, command: str) -> str | None:
        """The profile of a shortcut the app made while it was called PAM, or None (module docstring)."""
        profile = cls.profile_of(command, LEGACY_EXECUTABLE)
        if profile is None:
            return None
        if name == LEGACY_NAME_PREFIX + profile or _LEGACY_PATH.match(path):
            return profile
        return None

    def _owner(self, path: str, entry: Gio.Settings) -> tuple[str | None, bool]:
        """(profile, is_legacy) for one custom shortcut; profile is None if it is not ours."""
        command = entry.get_string("command")
        profile = self.profile_of(command)
        if profile is not None:
            return profile, False
        return self.legacy_profile_of(path, entry.get_string("name"), command), True

    def _ceb_entries(self) -> dict[str, tuple[str, Gio.Settings]]:
        """Profile -> (path, entry) of its shortcut. A current `ceb apply` entry wins over a PAM-era one."""
        out, legacy = {}, {}
        for path in self._list():
            entry = self._entry(path)
            name, is_legacy = self._owner(path, entry)
            if name is not None:
                (legacy if is_legacy else out).setdefault(name, (path, entry))
        for name, found in legacy.items():
            out.setdefault(name, found)
        return out

    def upgrade_legacy(self) -> int:
        """Rewrite the PAM-era shortcuts in place to run `ceb apply <profile>`, keeping each key and
        path. One whose profile already has a `ceb apply` shortcut is left as it is. Returns how many
        were rewritten."""
        if not self.available:
            return 0
        current, legacy = set(), []
        for path in self._list():
            entry = self._entry(path)
            name, is_legacy = self._owner(path, entry)
            if name is None:
                continue
            if is_legacy:
                legacy.append((name, entry))
            else:
                current.add(name)
        done = 0
        for name, entry in legacy:
            if name in current:
                continue
            entry.set_string("name", NAME_PREFIX + name)
            entry.set_string("command", self.command_for(name))
            current.add(name)
            done += 1
        if done:
            Gio.Settings.sync()
        return done

    def all(self) -> dict[str, str]:
        """Profile name -> accelerator (e.g. '<Super>F1'), for profiles that have a shortcut."""
        if not self.available:
            return {}
        return {name: entry.get_string("binding") for name, (_p, entry) in self._ceb_entries().items()
                if entry.get_string("binding")}

    def get(self, profile: str) -> str | None:
        return self.all().get(profile)

    # -------------------------------------------------------------- changing

    def set(self, profile: str, accel: str | None) -> None:
        """Give a profile a shortcut, or remove it (accel None or '')."""
        entries = self._ceb_entries()
        found = entries.get(profile)
        if not accel:
            if found:
                self._remove(found[0], found[1])
            return
        if found:
            path, entry = found
        else:
            path = self._free_path()
            entry = self._entry(path)
        entry.set_string("name", NAME_PREFIX + profile)
        entry.set_string("command", self.command_for(profile))
        entry.set_string("binding", accel)
        if not found:
            media = self._settings(MEDIA_KEYS)
            media.set_strv("custom-keybindings", self._list() + [path])
        Gio.Settings.sync()

    def rename(self, old: str, new: str) -> None:
        found = self._ceb_entries().get(old)
        if found:
            path, entry = found
            entry.set_string("name", NAME_PREFIX + new)
            entry.set_string("command", self.command_for(new))
            Gio.Settings.sync()

    def _remove(self, path: str, entry: Gio.Settings) -> None:
        media = self._settings(MEDIA_KEYS)
        media.set_strv("custom-keybindings", [p for p in self._list() if p != path])
        for key in ("name", "command", "binding"):
            entry.reset(key)
        Gio.Settings.sync()

    def _free_path(self) -> str:
        used = set(self._list())
        n = 0
        while f"{CUSTOM_DIR}ceb{n}/" in used:
            n += 1
        return f"{CUSTOM_DIR}ceb{n}/"

    # -------------------------------------------------------------- clashes

    def clash(self, accel: str, profile: str, same=None) -> str | None:
        """What already uses this accelerator (a description), or None. `same(a, b)` compares two
        accelerators; the GUI passes one that parses them with GTK so '<Super>f1' == '<Super>F1'."""
        same = same or (lambda a, b: a.lower() == b.lower())
        for path in self._list():
            entry = self._entry(path)
            if self._owner(path, entry)[0] == profile:
                continue
            if entry.get_string("binding") and same(entry.get_string("binding"), accel):
                return f"the custom shortcut “{entry.get_string('name')}”"
        for schema_id in SYSTEM_SCHEMAS:
            schema = _schema(schema_id)
            if schema is None:
                continue
            settings = Gio.Settings.new_full(schema, self.backend, None)
            for key in schema.list_keys():
                kind = schema.get_key(key).get_value_type().dup_string()
                if kind == "as":
                    values = settings.get_strv(key)
                elif kind == "s" and key not in ("custom-keybindings",):
                    values = [settings.get_string(key)]
                else:
                    continue
                if any(v and same(v, accel) for v in values):
                    return f"GNOME's “{schema.get_key(key).get_summary() or key}”"
        return None
