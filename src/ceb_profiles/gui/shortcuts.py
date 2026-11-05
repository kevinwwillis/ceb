"""Per-profile keyboard shortcuts as GNOME custom shortcuts.

GNOME keeps custom shortcuts in gsettings: the list
  org.gnome.settings-daemon.plugins.media-keys custom-keybindings
holds one path per shortcut, and each path has the relocatable schema
  org.gnome.settings-daemon.plugins.media-keys.custom-keybinding
with `name`, `command` and `binding` (e.g. '<Super>F1'). They show up and can be edited in
GNOME Settings > Keyboard > Custom Shortcuts.

CEB's shortcuts run `/absolute/path/to/ceb [--config-dir DIR] apply -- <profile>`: the absolute path
because GNOME runs the command without the user's shell PATH, `--config-dir` only when the profiles
are not in the default folder, and `--` so a name that starts with "-" is not read as an option. A
shortcut belongs to a profile when its command is exactly that (the older form without `--` is
recognised too); CEB never touches other custom shortcuts. Every function takes a Gio settings
backend so tests (and `ceb-gui --demo`) can use an in-memory backend instead of the user's real
settings.
"""
from __future__ import annotations

import os
import shlex

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM = MEDIA_KEYS + ".custom-keybinding"
CUSTOM_DIR = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
NAME_PREFIX = "CEB: "

# Schemas whose keys hold GNOME's own shortcuts, checked for clashes.
SYSTEM_SCHEMAS = ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings",
                  "org.gnome.mutter.keybindings", "org.gnome.mutter.wayland.keybindings",
                  MEDIA_KEYS)


class ShortcutError(Exception):
    pass


NOT_FOUND = ("CEB could not find its installed `ceb` command, which keyboard shortcuts run. Install "
             "CEB (see the README) so that `ceb` is on your PATH, then try again.")


def ceb_command() -> str | None:
    """The absolute path of the `ceb` executable a shortcut should run: the one installed next to
    this program, else next to this Python, else the one on PATH. None if there is none: a bare
    'ceb' is never used, because GNOME may not find it (its PATH is not the shell's)."""
    from ..desktop import find_executable
    return find_executable("ceb")


def _schema(schema_id: str):
    source = Gio.SettingsSchemaSource.get_default()
    return source.lookup(schema_id, True) if source else None


class Shortcuts:
    def __init__(self, backend: Gio.SettingsBackend | None = None, command: str | None = None,
                 config_dir: str | None = None):
        """command: the `ceb` to run (default: ceb_command(); None when it cannot be found, and then
        a shortcut cannot be set). config_dir: the non-default config folder the profiles are in
        (store.explicit_config_dir), which the shortcut's command passes as --config-dir; only
        shortcuts for that folder are this object's."""
        self.backend = backend
        self.command = command or ceb_command()
        self.config_dir = config_dir or None
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
        if not self.command:
            raise ShortcutError(NOT_FOUND)
        argv = [self.command] + (["--config-dir", self.config_dir] if self.config_dir else [])
        return " ".join(shlex.quote(a) for a in argv + ["apply", "--", profile])

    @staticmethod
    def parse(command: str) -> tuple[str | None, str] | None:
        """(config dir or None, profile) of a CEB shortcut's command, or None if it is not one.
        Accepts `ceb [--config-dir DIR] apply [--] NAME` with nothing else."""
        try:
            ok, argv = GLib.shell_parse_argv(command)
        except GLib.Error:
            return None
        if not ok or not argv or os.path.basename(argv[0]) != "ceb":
            return None
        rest, config_dir = list(argv[1:]), None
        if rest[:1] == ["--config-dir"] and len(rest) >= 2:
            config_dir, rest = rest[1], rest[2:]
        elif rest and rest[0].startswith("--config-dir="):
            config_dir, rest = rest[0].split("=", 1)[1], rest[1:]
        if rest[:1] != ["apply"]:
            return None
        rest = rest[1:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        elif rest and rest[0].startswith("-"):
            return None  # an option, not a name (the old form had no "--")
        if len(rest) != 1:
            return None
        return (config_dir or None), rest[0]

    @classmethod
    def profile_of(cls, command: str) -> str | None:
        """The profile a custom shortcut's command applies, if it is a CEB shortcut (any folder)."""
        parsed = cls.parse(command)
        return parsed[1] if parsed else None

    def _mine(self, command: str) -> str | None:
        """The profile of a shortcut for this object's config folder, or None."""
        parsed = self.parse(command)
        if parsed is None:
            return None
        folder, name = parsed
        same = (folder is None and self.config_dir is None) or (
            folder is not None and self.config_dir is not None
            and os.path.abspath(folder) == os.path.abspath(self.config_dir))
        return name if same else None

    def _ceb_entries(self) -> dict[str, tuple[str, Gio.Settings]]:
        out = {}
        for path in self._list():
            entry = self._entry(path)
            name = self._mine(entry.get_string("command"))
            if name is not None and name not in out:
                out[name] = (path, entry)
        return out

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
        command = self.command_for(profile)  # raises before anything is written
        if found:
            path, entry = found
        else:
            path = self._free_path()
            entry = self._entry(path)
        entry.set_string("name", NAME_PREFIX + profile)
        entry.set_string("command", command)
        entry.set_string("binding", accel)
        if not found:
            media = self._settings(MEDIA_KEYS)
            media.set_strv("custom-keybindings", self._list() + [path])
        Gio.Settings.sync()

    def rename(self, old: str, new: str) -> None:
        found = self._ceb_entries().get(old)
        if found:
            path, entry = found
            command = self.command_for(new)
            entry.set_string("name", NAME_PREFIX + new)
            entry.set_string("command", command)
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
            if self._mine(entry.get_string("command")) == profile:
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


def delete_profile(ceb, shortcuts: "Shortcuts | None", name: str) -> str | None:
    """Delete a profile and its keyboard shortcut (the window and `ceb delete` both use this).
    Raises what Ceb.delete_profile raises; nothing is touched then. Returns why the shortcut could
    not be removed, or None. Other custom shortcuts are never touched."""
    ceb.delete_profile(name)
    if shortcuts is None or not shortcuts.available:
        return None
    try:
        shortcuts.set(name, None)
    except (ShortcutError, GLib.Error) as e:
        return f"its keyboard shortcut could not be removed: {e}"
    return None
