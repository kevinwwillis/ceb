"""Command-line interface: `pam list`, `pam show`, `pam save-current`, `pam apply`, `pam delete`.

`pam apply <name>` is what keyboard shortcuts run. Every failure of `apply` (and any unexpected error)
is shown as a desktop notification as well as printed, because a shortcut has no terminal.

Exit codes: 0 done; 1 failed or refused (nothing changed, or an error part-way); 3 applied with
--allow-missing but something was skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback

from . import __version__
from .core import ApplyResult, Pam, PamError
from .model import Profile
from .notify import notify as desktop_notify
from .planner import describe_screen
from .store import Store, StoreError

EXIT_OK, EXIT_FAILED, EXIT_PARTIAL = 0, 1, 3


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pam", description="Display and audio profiles for GNOME.")
    ap.add_argument("--version", action="version", version=f"pam {__version__}")
    ap.add_argument("--config-dir", metavar="DIR",
                    help="where profiles and history are kept (default $XDG_CONFIG_HOME/pam)")
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

    sub.add_parser("gui", help="open the PAM window (same as pam-gui; pam gui --help for its options)",
                   add_help=False)
    p = sub.add_parser("delete", help="delete a profile")
    p.add_argument("name")
    return ap


def main(argv=None, *, pam: Pam | None = None, notifier=desktop_notify) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["gui"]:  # `pam gui [--dry-run|--demo|...]` is `pam-gui`
        from .gui import main as gui_main
        return gui_main(args[1:])
    opts = build_parser().parse_args(args)
    notify_failures = opts.command == "apply" and not opts.no_notify and not opts.dry_run
    try:
        pam = pam or Pam(store=Store(opts.config_dir))
        handler = {"list": cmd_list, "show": cmd_show, "save-current": cmd_save,
                   "apply": cmd_apply, "delete": cmd_delete}[opts.command]
        return handler(pam, opts, notifier if notify_failures else None)
    except (PamError, StoreError) as e:
        return _fail(f"pam {opts.command} failed", str(e), notifier if notify_failures else None)
    except Exception as e:  # anything unexpected: still tell the user, with the type
        traceback.print_exc()
        title = f"pam {opts.command} failed"
        body = f"unexpected error: {type(e).__name__}: {e}"
        if opts.command == "apply" and not opts.no_notify:
            notifier(title, body)
        else:
            print(f"pam: {title}: {body}", file=sys.stderr)
        return EXIT_FAILED


def _fail(title: str, body: str, notifier) -> int:
    if notifier:
        notifier(title, body)  # also prints to stderr
    else:
        print(f"pam: {title}: {body}", file=sys.stderr)
    return EXIT_FAILED


# ---------------------------------------------------------------- list / show


def cmd_list(pam: Pam, opts, _notifier) -> int:
    profiles = pam.profiles()
    display_state, audio_state, audio_errors = pam.current()
    known = pam.known()
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
            "errors": audio_errors}, indent=2))
        return EXIT_OK

    print("Profiles:")
    if not profiles:
        print("  (none; save one with: pam save-current <name>)")
    for p in profiles:
        print(f"  {p.name}: {_profile_summary(p)}")

    print("\nScreens (every screen PAM has seen):")
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
        print(f"\n{title} (every device PAM has seen):")
        for a in entries:
            mark = "default" if a.name == default else ("present" if a.name in present else "absent ")
            print(f"  {mark}  {a.description or a.name}  [{a.name}]")
    for e in audio_errors:
        print(f"\npam: audio: {e}", file=sys.stderr)
    return EXIT_OK


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


def cmd_show(pam: Pam, opts, _notifier) -> int:
    p = pam.profile(opts.name)
    if opts.json:
        print(json.dumps(p.to_json(), indent=2))
        return EXIT_OK
    print(f"Profile {p.name}")
    if p.displays:
        known = pam.known()
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


def cmd_save(pam: Pam, opts, _notifier) -> int:
    p = pam.save_current(opts.name, overwrite=opts.force, include_audio=not opts.no_audio)
    print(f"saved profile {p.name}: {_profile_summary(p)}")
    return EXIT_OK


def cmd_delete(pam: Pam, opts, _notifier) -> int:
    pam.delete_profile(opts.name)
    print(f"deleted profile {opts.name}")
    return EXIT_OK


# ---------------------------------------------------------------- apply


def cmd_apply(pam: Pam, opts, notifier) -> int:
    result = pam.apply(opts.name, dry_run=opts.dry_run, allow_missing=opts.allow_missing)
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
            body += ". Use --allow-missing to apply the rest."
    if notifier:
        notifier(title, body)  # also prints to stderr
    else:
        print(f"pam: {title}: {body}", file=sys.stderr)
    return code


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
