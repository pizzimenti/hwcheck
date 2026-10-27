"""python3 hwcheck.py [--detect [--expect E] [--print-plan] | --plan P] [--resume | --new] [--port N] [--timeout S]"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading
import time

from . import __version__
from .detect import Detector, ExpectError, build_plan, load_expect, summary
from .plan import PlanError, load_plan, parse_plan
from .readers import Readers, clean
from .server import Server, serve
from .session import Session, SessionError, systemctl_hibernate
from .store import Store, StoreError

DEFAULT_PORT = 8770

EXIT_DONE = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
EXIT_INTERRUPTED = 130  # ctrl-c
EXIT_STOPPED = 143      # sigterm (a signal adds its number to 128: sighup is 129)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hwcheck",
        description="an on-screen hardware check. serves one page on 127.0.0.1 that walks a person "
                    "through each check and saves every answer under ~/.local/state/hwcheck/.",
    )
    source = p.add_mutually_exclusive_group()
    source.add_argument("--detect", action="store_true",
                        help="build the plan from the hardware this machine has (the default without --plan)")
    source.add_argument("--plan", help="use this check plan (json) instead of detecting")
    p.add_argument("--expect", metavar="FILE",
                   help="with detection: a json list of devices this model should have, "
                        "[{\"kind\": \"pen\", \"match\": \"WACF2200\"}]; each one not detected becomes a failing check")
    p.add_argument("--print-plan", action="store_true",
                   help="with detection: print the plan it builds and exit, serving nothing")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true",
                      help="carry on with the saved run where it stopped (after hibernate, a timeout or a stop)")
    mode.add_argument("--new", action="store_true",
                      help="start over even though an unfinished run is saved (its files are kept, renamed)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"port on 127.0.0.1 (default {DEFAULT_PORT}; 0 picks a free one)")
    p.add_argument("--timeout", type=float, default=0.0,
                   help="stop after this many seconds awake, answered or not (default: no limit)")
    p.add_argument("--state-dir", help="where state and results go (default ~/.local/state/hwcheck)")
    p.add_argument("--version", action="version", version=f"hwcheck {__version__}")
    return p


class SignalStop:
    """the sigterm and sighup handler: it only records which signal came, and the serve loop,
    which looks every 0.2 s, does the stopping. stop() takes locks (the session's, and the
    stopping event's own); a handler taking one that the interrupted main thread holds would
    hang it."""

    def __init__(self):
        self.signals: list[int] = []

    def __call__(self, signum, _frame) -> None:
        self.signals.append(signum)

    def requested(self) -> bool:
        return bool(self.signals)


def terminal_safe(text) -> str:
    """device names come from the machine: drop escape codes and control characters before printing."""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", clean(str(text)))


def _stamp(started) -> str:
    digits = re.sub(r"[^0-9T]", "", str(started or ""))[:15]
    return digits or "old"


def main(argv=None, *, readers=None, hibernate=None, detector=None, out=None, err=None, on_ready=None,
         grace: float = 1.5, poll_interval: float = 1.0, hibernate_delay: float = 1.0,
         session_options: dict | None = None, clock=None) -> int:
    """run hwcheck. the keyword arguments are seams for tests (fake readers and detector, a
    fake hibernate, a fake sleep clock through session_options, a fake clock for --timeout);
    the command line never sets them."""
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    err = err or sys.stderr

    def say(msg: str) -> None:
        print(f"hwcheck: {terminal_safe(msg)}", file=err, flush=True)

    if not 0 <= args.port <= 65535:
        say("--port must be between 0 and 65535")
        return EXIT_USAGE
    if args.timeout < 0:
        say("--timeout can't be negative")
        return EXIT_USAGE
    if args.plan and (args.expect or args.print_plan):
        say("--expect and --print-plan go with detection, not --plan")
        return EXIT_USAGE
    if args.resume and (args.detect or args.expect or args.print_plan):
        say("--resume carries on with the saved run's own plan; it doesn't detect again")
        return EXIT_USAGE
    if args.print_plan and args.new:
        say("--print-plan only prints; it starts no run")
        return EXIT_USAGE

    def problems(title: str, items) -> int:
        say(title)
        for item in items:
            print(f"  - {terminal_safe(item)}", file=err, flush=True)
        return EXIT_USAGE

    plan, plan_label = None, args.plan
    if args.plan:
        try:
            plan = load_plan(args.plan)
        except PlanError as exc:
            return problems(f"bad plan {args.plan}:", exc.problems)
    elif not args.resume:
        try:
            expect = load_expect(args.expect) if args.expect else []
        except ExpectError as exc:
            return problems(f"bad expect file {args.expect}:", exc.problems)
        detected = (detector or Detector()).detect()
        try:
            plan = parse_plan(build_plan(detected, expect))
        except PlanError as exc:  # a detection bug, never the person's
            return problems("the detected plan is invalid:", exc.problems)
        if args.print_plan:
            print(json.dumps(plan, indent=2, ensure_ascii=False), file=out, flush=True)
            return EXIT_DONE
        plan_label = "detected" + (f", expecting {args.expect}" if args.expect else "")
        say(f"detected {len(plan['checks'])} checks:")
        for line in summary(detected, plan):
            print(f"  {terminal_safe(line)}", file=err, flush=True)

    store = Store(args.state_dir)
    try:
        store.lock()  # one run per state directory, from here until this run ends (or dies)
    except StoreError as exc:
        say(str(exc))  # another hwcheck owns it: touch nothing of its
        return EXIT_ERROR
    except OSError as exc:
        say(f"can't use {store.dir}: {exc.strerror or exc}")
        return EXIT_ERROR
    try:
        return _owned(args, store, plan, plan_label, say, problems, readers=readers, hibernate=hibernate,
                      out=out, on_ready=on_ready, grace=grace, poll_interval=poll_interval,
                      hibernate_delay=hibernate_delay, session_options=session_options, clock=clock)
    finally:
        store.unlock()


def _owned(args, store, plan, plan_label, say, problems, *, readers, hibernate, out, on_ready, grace,
           poll_interval, hibernate_delay, session_options, clock=None) -> int:
    """the run, once this process holds the state directory's lock."""
    # no other hwcheck holds this directory, so an open.html here is from a run that's gone
    # (a crash, or a machine that booted fresh): its secret is dead
    store.remove_handoff()
    readers = readers or Readers()
    hibernate = hibernate or systemctl_hibernate
    try:
        saved = store.load_state()
        if args.resume:
            if saved is None:
                say(f"nothing to resume in {store.dir}")
                return EXIT_USAGE
            if saved.get("finished"):
                say(f"that run finished at {saved['finished']}; its results are in {store.results_path}")
                return EXIT_DONE
            session = Session.resume(saved, plan, plan_label, store, readers, hibernate,
                                     **(session_options or {}))
        else:
            if saved and not saved.get("finished") and not args.new:
                total = len((saved.get("plan") or {}).get("checks") or [])
                done = sum(1 for a in (saved.get("answers") or {}).values() if a.get("final", True))
                say(f"an unfinished run from {saved.get('started')} ({done} of {total} answered) is saved "
                    f"in {store.dir}; use --resume to carry on or --new to start over")
                return EXIT_USAGE
            if saved:
                moved = store.archive(_stamp(saved.get("started")))
                if moved:
                    say("kept the earlier run as " + ", ".join(p.name for p in moved))
            session = Session.new(plan, plan_label, store, readers, hibernate, **(session_options or {}))
    except PlanError as exc:
        return problems("the saved run's plan is unreadable:", exc.problems)
    except (StoreError, SessionError) as exc:
        say(str(exc))
        return EXIT_USAGE if isinstance(exc, SessionError) and exc.status < 500 else EXIT_ERROR
    except OSError as exc:
        say(f"can't use {store.dir}: {exc.strerror or exc}")
        return EXIT_ERROR

    try:
        server = Server(session, args.port, hibernate_delay=hibernate_delay)
    except OSError as exc:
        say(f"can't listen on 127.0.0.1:{args.port}: {exc.strerror or exc}; pass another --port")
        readers.release()
        return EXIT_ERROR

    # sigterm, and sighup (the terminal or tmux pane closing), stop it the way an answer would:
    # cleanly, saved, the hand-off page removed. installed before that page is written.
    stop_signal = SignalStop()
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old_handlers[sig] = signal.signal(sig, stop_signal)
    served = False
    try:
        print(server.url, file=out, flush=True)
        try:
            handoff = store.write_handoff(server.url)
        except OSError as exc:
            handoff = None
            say(f"couldn't write {store.handoff_path}: {exc.strerror or exc}")
        say(f"open {server.url} in this machine's browser; results go to {store.results_path}")
        if handoff:
            say(f"to keep the secret off the browser's command line, open file://{handoff} instead")
        if on_ready:
            on_ready(server, session)
        served = True
        outcome = serve(session, server, timeout=args.timeout or None, grace=grace,
                        poll_interval=poll_interval, clock=clock or time.monotonic,
                        stop_requested=stop_signal.requested)
    finally:
        for sig, old in old_handlers.items():
            signal.signal(sig, old)
        store.remove_handoff()  # its secret dies with this run
        if not served:
            server.server_close()
            readers.release()

    if outcome == "finished":
        say(f"all checks answered; results are in {store.results_path}")
        return EXIT_DONE
    left = session.snapshot()["counts"]["left"]
    if outcome == "timeout":
        say(f"stopped after --timeout with {left} check(s) left; run again with --resume to carry on")
        return EXIT_TIMEOUT
    say(f"stopped with {left} check(s) left; run again with --resume to carry on")
    if outcome == "interrupted":
        return EXIT_INTERRUPTED
    return 128 + stop_signal.signals[0] if stop_signal.signals else EXIT_STOPPED
