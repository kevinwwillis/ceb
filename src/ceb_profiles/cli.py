"""Command-line interface: `ceb list`, `ceb show`, `ceb save-current`, `ceb apply`, `ceb delete`,
`ceb watch`, `ceb install-desktop`, `ceb gui`.

Profile names may start with "-"; on the command line such a name goes after `--`
(`ceb apply -- -work`), as with any Unix tool. Keyboard shortcuts always write the `--`.

`ceb apply <name>` is what keyboard shortcuts run. Every failure of `apply` (and any unexpected error)
is shown as a desktop notification as well as printed, because a shortcut has no terminal.

Exit codes: 0 done; 1 failed or refused (nothing changed, or an error part-way); 2 wrong command
line; 3 applied with --allow-missing but something was skipped.

Notifications: failures of `apply` are notified at critical urgency, an `--allow-missing` partial
success (exit 3) at normal urgency. A wrong command line is notified when stderr is not a terminal
(a keyboard shortcut). `--dry-run` and `--no-notify` only print.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback

from . import __version__
from .audio import AudioError
from .core import ApplyResult, Ceb, CebError
from .model import Profile
from .mutter import DisplayError
from .notify import notify as desktop_notify
from .planner import describe_screen
from .store import Known, Store, StoreError, explicit_config_dir

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_PARTIAL = 0, 1, 2, 3


class UsageError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str):
        super().__init__(message)
        self.parser = parser


class _Parser(argparse.ArgumentParser):
    """argparse prints and exits on a bad command line; raise instead so main() can also notify
    (a keyboard shortcut has no terminal to print to)."""

    def error(self, message):
        raise UsageError(self, message)


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="ceb", description="CEB (Combined Environment Bundles): display and audio profiles for GNOME.")
    ap.add_argument("--version", action="version", version=f"ceb {__version__}")
    ap.add_argument("--config-dir", metavar="DIR",
                    help="where profiles and history are kept (default $XDG_CONFIG_HOME/ceb)")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list profiles, every screen ever seen, and audio devices")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("show", help="show one profile")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("save-current", help="save the current screens and default audio devices as a profile")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="replace a profile with this name")
    p.add_argument("--no-audio", action="store_true", help="save the screens only; apply leaves audio alone")

    p = sub.add_parser("apply", help="switch to a profile")
    p.add_argument("name")
    p.add_argument("--dry-run", action="store_true",
                   help="plan and let GNOME verify the layout; change nothing")
    p.add_argument("--allow-missing", action="store_true",
                   help="apply what is present and skip missing screens or audio devices (reported)")
    p.add_argument("--no-notify", action="store_true", help="no desktop notification on failure")

    sub.add_parser("gui", help="open the CEB window (same as ceb-gui; ceb gui --help for its options)",
                   add_help=False)
    p = sub.add_parser("delete", help="delete a profile and its keyboard shortcut")
    p.add_argument("name")

    sub.add_parser("watch", help="keep running and remember every screen and audio device that is "
                                 "connected (what the window's 'Remember Screens in the Background' runs)")
    p = sub.add_parser("install-desktop",
                       help="add CEB to the applications menu (~/.local/share: .desktop file and icon)")
    p.add_argument("--uninstall", action="store_true", help="remove them again")
    return ap


def _split_gui(args: list[str]) -> tuple[list[str], list[str]] | None:
    """`ceb [--config-dir DIR] gui [...]` -> (global options, gui arguments), else None."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--config-dir" and i + 1 < len(args):
            i += 2
        elif a.startswith("--config-dir="):
            i += 1
        elif a == "gui":
            return args[:i], args[i + 1:]
        else:
            return None
    return None


def main(argv=None, *, ceb: Ceb | None = None, notifier=desktop_notify, on_terminal=None,
         shortcuts=None) -> int:
    """shortcuts: the gui.shortcuts.Shortcuts that `delete` removes a profile's shortcut from
    (default: GNOME's, for this config folder)."""
    args = list(sys.argv[1:] if argv is None else argv)
    gui = _split_gui(args)
    if gui is not None:  # `ceb [--config-dir DIR] gui [--dry-run|--demo|...]` is `ceb-gui`
        from .gui import main as gui_main
        global_opts, gui_args = gui
        return gui_main(gui_args + global_opts)
    try:
        opts = build_parser().parse_args(args)
    except UsageError as e:
        return _usage_error(e, args, notifier, on_terminal)
    notify_failures = opts.command == "apply" and not opts.no_notify and not opts.dry_run
    sink = notifier if notify_failures else None
    try:
        if opts.command == "install-desktop":
            return cmd_install_desktop(opts)
        if opts.command == "watch":
            from .watch import run as watch_run
            return watch_run(opts.config_dir, ceb=ceb)
        ceb = ceb or Ceb(store=Store(opts.config_dir))
        if opts.command == "delete":
            return cmd_delete(ceb, opts, shortcuts)
        handler = {"list": cmd_list, "show": cmd_show, "save-current": cmd_save,
                   "apply": cmd_apply}[opts.command]
        return handler(ceb, opts, sink)
    except (CebError, StoreError) as e:
        return _fail(f"ceb {opts.command} failed", str(e), sink)
    except DisplayError as e:
        return _fail(f"ceb {opts.command} failed", f"could not read the screens: {e}", sink)
    except AudioError as e:
        return _fail(f"ceb {opts.command} failed", str(e), sink)
    except Exception as e:  # anything unexpected: still tell the user, with the type
        traceback.print_exc()
        return _fail(f"ceb {opts.command} failed", f"unexpected error: {type(e).__name__}: {e}", sink)


def _usage_error(e: UsageError, args: list[str], notifier, on_terminal) -> int:
    e.parser.print_usage(sys.stderr)
    # No "ceb:" in the title: stderr adds it ("ceb: Wrong command: ..."); the notification shows "CEB".
    title, body = "Wrong command", f"{e} (command: ceb {' '.join(args)})".rstrip()
    if "argument" in str(e) and any(a.startswith("-") and a not in _KNOWN_OPTIONS for a in args):
        body += ". A profile name that starts with - goes after --, for example: ceb apply -- -work"
    terminal = sys.stderr.isatty() if on_terminal is None else on_terminal
    if terminal or "--no-notify" in args or "--dry-run" in args:
        print(f"ceb: {title}: {body}", file=sys.stderr)
    else:
        notifier(title, body)  # also prints to stderr
    return EXIT_USAGE


_KNOWN_OPTIONS = {"--config-dir", "--json", "--force", "--no-audio", "--dry-run", "--allow-missing",
                  "--no-notify", "--uninstall", "--help", "-h", "--version", "--"}


def _fail(title: str, body: str, notifier) -> int:
    if notifier:
        notifier(title, body)  # also prints to stderr
    else:
        print(f"ceb: {title}: {body}", file=sys.stderr)
    return EXIT_FAILED


# ---------------------------------------------------------------- list / show


def cmd_list(ceb: Ceb, opts, _notifier) -> int:
    """Lists everything it can. An unreadable profiles.json or known.json is reported (exit 1) but
    does not hide the rest; without a readable history, the connected screens are still shown."""
    file_errors = []
    try:
        profiles = ceb.profiles()
    except StoreError as e:
        profiles, file_errors = [], [str(e)]
    display_state, audio_state, audio_errors = ceb.current()
    if ceb.last_history_error:
        file_errors.append(ceb.last_history_error)
    try:
        known = ceb.known()
    except StoreError as e:
        if str(e) not in " ".join(file_errors):
            file_errors.append(str(e))
        known = Known()
        known.observe_display(display_state)
        if audio_state is not None:
            known.observe_audio(audio_state)
    connected = {m.screen_id: m for m in display_state.monitors}
    if opts.json:
        print(json.dumps({
            "profiles": [p.to_json() for p in profiles],
            "known": known.to_json(),
            "connected_screens": [dict(m.screen_id.to_json(), connector=m.connector, gpu=m.gpu)
                                  for m in display_state.monitors],
            "audio": None if audio_state is None else {
                "outputs": [d.name for d in audio_state.outputs],
                "inputs": [d.name for d in audio_state.inputs],
                "default_output": audio_state.default_output, "default_input": audio_state.default_input},
            "errors": audio_errors + file_errors}, indent=2))
        for e in file_errors:
            print(f"ceb: {e}", file=sys.stderr)
        return EXIT_FAILED if file_errors else EXIT_OK

    print("Profiles:")
    if not profiles:
        print("  (none; save one with: ceb save-current <name>)")
    for p in profiles:
        print(f"  {p.name}: {_profile_summary(p)}")

    print("\nScreens (every screen CEB has seen):")
    for ks in sorted(known.screens, key=lambda s: (s.screen not in connected, s.display_name, s.screen.twin)):
        cm = connected.get(ks.screen)
        twins = len(known.twins_of(ks.screen.identity))
        label = describe_screen(ks.screen, twins, ks.display_name)
        if cm:
            print(f"  connected      {cm.connector:<9} {label}")
        else:
            print(f"  not connected  {'':<9} {label}; last seen {ks.last_seen} on {ks.last_connector}")

    present_out = {d.name for d in audio_state.outputs} if audio_state else set()
    present_in = {d.name for d in audio_state.inputs} if audio_state else set()
    for title, entries, present, default in (
            ("Audio outputs", known.outputs, present_out, audio_state.default_output if audio_state else None),
            ("Audio inputs", known.inputs, present_in, audio_state.default_input if audio_state else None)):
        print(f"\n{title} (every device CEB has seen):")
        for a in entries:
            mark = "default" if a.name == default else ("present" if a.name in present else "absent ")
            print(f"  {mark}  {a.description or a.name}  [{a.name}]")
    for e in audio_errors:
        print(f"\nceb: audio: {e}", file=sys.stderr)
    for e in file_errors:
        print(f"\nceb: {e}", file=sys.stderr)
    return EXIT_FAILED if file_errors else EXIT_OK


def _profile_summary(p: Profile) -> str:
    parts = []
    if p.displays:
        n = len(p.displays.screens())
        parts.append(f"{n} screen{'s' if n != 1 else ''} on")
    else:
        parts.append("screens unchanged")
    parts.append(f"output: {p.audio_output.description or p.audio_output.name}" if p.audio_output
                 else "output unchanged")
    parts.append(f"input: {p.audio_input.description or p.audio_input.name}" if p.audio_input
                 else "input unchanged")
    return ", ".join(parts)


def cmd_show(ceb: Ceb, opts, _notifier) -> int:
    p = ceb.profile(opts.name)
    if opts.json:
        print(json.dumps(p.to_json(), indent=2))
        return EXIT_OK
    print(f"Profile {p.name}")
    if p.displays:
        try:
            known = ceb.known()
        except StoreError as e:
            print(f"ceb: {e}", file=sys.stderr)
            known = Known()
        for lm in p.displays.logical_monitors:
            for s in lm.screens:
                twins = len(known.twins_of(s.screen.identity))
                entry = known.screen(s.screen)
                print(f"  screen at ({lm.x}, {lm.y}) scale {lm.scale:g}{' primary' if lm.primary else ''}: "
                      f"{describe_screen(s.screen, twins, entry.display_name if entry else '')} "
                      f"{s.mode.describe()} (was on {s.connector_when_saved or '?'})")
        print("  every other screen: off")
    else:
        print("  screens: unchanged")
    print(f"  audio output: {p.audio_output.describe() if p.audio_output else 'unchanged'}")
    print(f"  audio input: {p.audio_input.describe() if p.audio_input else 'unchanged'}")
    return EXIT_OK


# ---------------------------------------------------------------- save / delete


def cmd_save(ceb: Ceb, opts, _notifier) -> int:
    p = ceb.save_current(opts.name, overwrite=opts.force, include_audio=not opts.no_audio)
    print(f"saved profile {p.name}: {_profile_summary(p)}")
    if ceb.last_history_error:
        print(f"ceb: {ceb.last_history_error}", file=sys.stderr)
    return EXIT_OK


def cmd_delete(ceb: Ceb, opts, shortcuts=None) -> int:
    """Deletes the profile and its GNOME keyboard shortcut, as the window does (same code)."""
    try:
        from .gui.shortcuts import Shortcuts, delete_profile
    except (ImportError, ValueError) as e:  # no PyGObject/Gio: the profile can still go
        ceb.delete_profile(opts.name)
        print(f"deleted profile {opts.name}")
        print(f"ceb: its keyboard shortcut was not checked (GNOME's settings are not readable: {e})",
              file=sys.stderr)
        return EXIT_FAILED
    if shortcuts is None:
        shortcuts = Shortcuts(config_dir=explicit_config_dir(ceb.store.dir))
    problem = delete_profile(ceb, shortcuts, opts.name)
    print(f"deleted profile {opts.name}")
    if problem:
        print(f"ceb: {problem}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def cmd_install_desktop(opts) -> int:
    from .desktop import install_desktop, uninstall_desktop
    try:
        lines = uninstall_desktop() if opts.uninstall else install_desktop()
    except OSError as e:
        print(f"ceb: install-desktop failed: {e}", file=sys.stderr)
        return EXIT_FAILED
    except RuntimeError as e:
        print(f"ceb: {e}", file=sys.stderr)
        return EXIT_FAILED
    for line in lines:
        print(line)
    return EXIT_OK


# ---------------------------------------------------------------- apply


def cmd_apply(ceb: Ceb, opts, notifier) -> int:
    result = ceb.apply(opts.name, dry_run=opts.dry_run, allow_missing=opts.allow_missing)
    _print_result(result)
    if result.ok:
        return EXIT_OK
    detail = "; ".join(result.summary_lines())
    blocked = any(p.blocking_always for p in result.problems)
    if (opts.allow_missing and not result.errors and not blocked
            and (result.dry_run or _anything_applied(result))):
        verb = "would be applied" if result.dry_run else "applied"
        title, code = f"Profile {result.profile} {verb} with parts skipped", EXIT_PARTIAL
        body = "Skipped: " + detail
    elif _anything_applied(result):
        title, code = f"Profile {result.profile} only partly applied", EXIT_FAILED
        body = detail
    else:
        title = f"Profile {result.profile} not applied" + (" (dry run)" if result.dry_run else "")
        code, body = EXIT_FAILED, "Nothing was changed. " + detail
        if result.problems and not opts.allow_missing and not blocked:
            body += (". To apply the rest, press Apply in CEB's window and choose Apply the Rest, or run "
                     f"in a terminal: ceb{_dir_option(ceb)} apply --allow-missing -- {_quote(result.profile)}")
    if notifier:
        # Partial success is information, not an alarm: normal urgency.
        notifier(title, body, critical=code != EXIT_PARTIAL)  # also prints to stderr
    else:
        print(f"ceb: {title}: {body}", file=sys.stderr)
    return code


def _quote(name: str) -> str:
    import shlex
    return shlex.quote(name)


def _dir_option(ceb: Ceb) -> str:
    folder = explicit_config_dir(getattr(ceb.store, "dir", None))
    return f" --config-dir {_quote(folder)}" if folder else ""


def _anything_applied(r: ApplyResult) -> bool:
    return r.applied_display or r.applied_output or r.applied_input


def _print_result(r: ApplyResult) -> None:
    if not r.goes_ahead:
        if r.dry_run:
            print("dry run: nothing was changed")
        return
    if r.display_plan:
        dp = r.display_plan
        for lm in dp.logical_monitors:
            for m in lm.monitors:
                print(f"screen {m.connector} ({m.mode_id}) at ({lm.x}, {lm.y}) scale {lm.scale:g}"
                      f"{' primary' if lm.primary else ''}")
        for m in dp.turned_off:
            print(f"screen {m.connector} off ({m.display_name})")
    ap = r.audio_plan
    if ap:
        if ap.output:
            print(f"audio output {ap.output.describe()}")
        if ap.input:
            print(f"audio input {ap.input.describe()}")
    for n in r.notes:
        print(f"note: {n}")
    if r.dry_run:
        if r.ok:
            print("dry run: GNOME accepted the layout (verify only); nothing was changed")
        else:
            print("dry run: nothing was changed")
    elif r.ok:
        print(f"applied profile {r.profile}")


if __name__ == "__main__":
    sys.exit(main())
