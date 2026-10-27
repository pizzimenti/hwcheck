"""python3 hwcheck.py --plan P [--resume | --new] [--port N] [--timeout S]"""

from __future__ import annotations

import argparse
import re
import signal
import sys
import threading

from . import __version__
from .plan import PlanError, load_plan
from .readers import Readers
from .server import Server, serve
from .session import Session, SessionError, systemctl_hibernate
from .store import Store, StoreError

DEFAULT_PORT = 8770

EXIT_DONE = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
EXIT_INTERRUPTED = 130  # ctrl-c
EXIT_STOPPED = 143      # sigterm


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hwcheck",
        description="an on-screen hardware check. serves one page on 127.0.0.1 that walks a person "
                    "through each check and saves every answer under ~/.local/state/hwcheck/.",
    )
    p.add_argument("--plan", required=True, help="the check plan (json)")
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


def _stamp(started) -> str:
    digits = re.sub(r"[^0-9T]", "", str(started or ""))[:15]
    return digits or "old"


def main(argv=None, *, readers=None, hibernate=None, out=None, err=None, on_ready=None,
         grace: float = 1.5, poll_interval: float = 1.0, hibernate_delay: float = 1.0,
         session_options: dict | None = None) -> int:
    """run hwcheck. the keyword arguments are seams for tests (fake readers, a fake
    hibernate, a fake clock through session_options); the command line never sets them."""
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    err = err or sys.stderr

    def say(msg: str) -> None:
        print(f"hwcheck: {msg}", file=err, flush=True)

    if not 0 <= args.port <= 65535:
        say("--port must be between 0 and 65535")
        return EXIT_USAGE
    if args.timeout < 0:
        say("--timeout can't be negative")
        return EXIT_USAGE
    try:
        plan = load_plan(args.plan)
    except PlanError as exc:
        say(f"bad plan {args.plan}:")
        for problem in exc.problems:
            print(f"  - {problem}", file=err, flush=True)
        return EXIT_USAGE

    store = Store(args.state_dir)
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
            session = Session.resume(saved, plan, args.plan, store, readers, hibernate,
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
            session = Session.new(plan, args.plan, store, readers, hibernate, **(session_options or {}))
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

    print(server.url, file=out, flush=True)
    say(f"open {server.url} in this machine's browser; results go to {store.results_path}")
    if on_ready:
        on_ready(server, session)

    old_term = None
    if threading.current_thread() is threading.main_thread():
        old_term = signal.signal(signal.SIGTERM, lambda *_: session.stop())
    try:
        outcome = serve(session, server, timeout=args.timeout or None, grace=grace,
                        poll_interval=poll_interval)
    finally:
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)

    if outcome == "finished":
        say(f"all checks answered; results are in {store.results_path}")
        return EXIT_DONE
    left = session.snapshot()["counts"]["left"]
    if outcome == "timeout":
        say(f"stopped after --timeout with {left} check(s) left; run again with --resume to carry on")
        return EXIT_TIMEOUT
    say(f"stopped with {left} check(s) left; run again with --resume to carry on")
    return EXIT_INTERRUPTED if outcome == "interrupted" else EXIT_STOPPED
