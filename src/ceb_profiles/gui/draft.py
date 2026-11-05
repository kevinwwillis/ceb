"""The unsaved edits of one profile, with undo and redo. Pure Python (no GTK).

The window edits a Draft, never profiles.json and never the live screens or audio: an edit is kept
in memory until Save (Draft.save), and Discard (Draft.discard) drops it. The draft holds the
profile and its keyboard shortcut (a GNOME custom shortcut, written on Save as well). Undo and Redo
step through the edits made since the draft was last saved or discarded.
"""
from __future__ import annotations

import copy
from typing import Callable

from ..model import Profile

UNDO_LIMIT = 200


class Draft:
    def __init__(self, saved: Profile, accel: str | None):
        """saved: the profile as profiles.json holds it; accel: its shortcut as GNOME holds it."""
        self.saved = copy.deepcopy(saved)
        self.saved_accel = accel
        self.profile = copy.deepcopy(saved)
        self.accel = accel
        self._undo: list[tuple[Profile, str | None]] = []
        self._redo: list[tuple[Profile, str | None]] = []

    @property
    def name(self) -> str:
        return self.saved.name

    def _state(self) -> tuple[Profile, str | None]:
        return copy.deepcopy(self.profile), self.accel

    @staticmethod
    def _same(a: tuple[Profile, str | None], b: tuple[Profile, str | None]) -> bool:
        return a[0].to_json() == b[0].to_json() and (a[1] or None) == (b[1] or None)

    @property
    def dirty(self) -> bool:
        """The draft differs from what is saved."""
        return not self._same((self.profile, self.accel), (self.saved, self.saved_accel))

    @property
    def profile_dirty(self) -> bool:
        return self.profile.to_json() != self.saved.to_json()

    # ------------------------------------------------------------ editing

    def edit(self, change: Callable[[Profile], None]) -> bool:
        """Change the profile with `change(profile)`. Returns False when nothing changed (no undo
        step is added then)."""
        before = self._state()
        change(self.profile)
        return self._record(before)

    def set_accel(self, accel: str | None) -> bool:
        before = self._state()
        self.accel = accel or None
        return self._record(before)

    def _record(self, before) -> bool:
        if self._same(before, (self.profile, self.accel)):
            return False
        self._undo.append(before)
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()
        return True

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._state())
        self.profile, self.accel = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._state())
        self.profile, self.accel = self._redo.pop()
        return True

    def discard(self) -> None:
        """Back to what is saved; the undo history is cleared."""
        self.profile = copy.deepcopy(self.saved)
        self.accel = self.saved_accel
        self._undo.clear()
        self._redo.clear()

    # ------------------------------------------------------------ saving

    def save(self, ceb, set_shortcut: Callable[[str, str | None], str | None], *, force: bool = False) -> str | None:
        """Write the profile to profiles.json, then the shortcut to GNOME if it changed.

        The profile is written only if profiles.json still holds the version this draft started
        from; otherwise core.ProfileChanged is raised and nothing is written (force=True writes it
        anyway). StoreError and CebError are raised as they come.

        set_shortcut(name, accel) returns an error message or None. When it fails, the profile is
        saved and the shortcut stays an unsaved edit; the message is returned."""
        if self.profile_dirty or force:
            ceb.put_profile(self.profile, expected=None if force else self.saved)
        self.saved = copy.deepcopy(self.profile)
        problem = None
        if (self.accel or None) != (self.saved_accel or None):
            problem = set_shortcut(self.name, self.accel)
            if problem is None:
                self.saved_accel = self.accel
        self._undo.clear()
        self._redo.clear()
        return problem
