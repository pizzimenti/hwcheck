"""a run: the plan, the answers, the check on screen, hibernate, and the live readings.

state.json (hwcheck's own) and results.json (what the provisioner reads) are both
written after every answer and every hibernate step.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import secrets
import subprocess
import threading
import time
from datetime import datetime

from . import __version__
from .plan import parse_plan, signature, where

ANSWERS = ("works", "doesnt-work", "skip")
FORMAT = 1
MAX_NOTE = 2000
MAX_OBSERVED = 8192
MAX_SEEN = 30
SLEEP_GAP = 2.0  # seconds the machine must have slept before we call it a resume

# which answers each hibernate phase accepts. "works" needs a sleep actually seen:
# systemctl hibernate returns as soon as logind queues the job, long before any sleep.
HIBERNATE_ANSWERS = {
    "idle": {"doesnt-work", "skip"},
    "pending": {"doesnt-work", "skip"},
    "hibernating": {"doesnt-work", "skip"},
    "failed": {"doesnt-work", "skip"},
    "resumed": set(ANSWERS),
    "unknown": set(ANSWERS),
    "fresh-boot": {"doesnt-work"},
}


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sleep_offset() -> float:
    """seconds the machine has spent asleep since boot (boottime counts sleep, monotonic doesn't)."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


def systemctl_hibernate() -> tuple[bool, str]:
    """the real hibernate action; tests always replace it."""
    os.sync()
    try:
        r = subprocess.run(["systemctl", "hibernate"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return False, "systemctl not found"
    except subprocess.TimeoutExpired:
        return False, "systemctl hibernate didn't return in 60 s"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or f"systemctl hibernate exited {r.returncode}"
    return True, ""


class SessionError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.status = status
        super().__init__(message)


def _preset_live(check: dict) -> dict:
    """what detection saw, kept with a preset check however it is answered later."""
    return {"source": "preset", "evidence": check["preset"]["note"]}


def _new_hibernate() -> dict:
    return {"phase": "idle", "boot_id": None, "at": None, "error": None, "boot_id_after": None,
            "slept_seconds": None, "resumed_at": None, "kernel_log": None, "sleep_mark": None,
            "action_started": None}


class Session:
    def __init__(self, state: dict, store, readers, hibernate_action=systemctl_hibernate,
                 now=iso_now, sleep_clock=sleep_offset):
        self.state = state
        self.store = store
        self.readers = readers
        self.hibernate_action = hibernate_action
        self.now = now
        self.sleep_clock = sleep_clock
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.version = 0
        self.instance = secrets.token_hex(4)  # tells the page a restarted server from the old one
        self.stopping = threading.Event()
        self.finished = threading.Event()
        self.wake = threading.Event()
        self.live: dict | None = None
        self._poll_lock = threading.Lock()
        self._polled: str | None = None
        if state.get("finished"):
            self.finished.set()

    # ------------------------------------------------------------ starting

    @classmethod
    def new(cls, plan: dict, plan_path, store, readers, hibernate_action=systemctl_hibernate,
            now=iso_now, sleep_clock=sleep_offset) -> "Session":
        stamp = now()
        state = {
            "format": FORMAT,
            "hwcheck": __version__,
            "plan_path": str(plan_path),
            "plan": plan,
            "machine": plan.get("machine") or readers.machine(),
            "model": plan.get("model") or readers.model(),
            "boot_id": readers.boot_id(),
            "started": stamp,
            "updated": stamp,
            "finished": None,
            "current": None,
            "answers": {},
            "hibernate": _new_hibernate(),
        }
        for c in plan["checks"]:
            # answers the plan carries in (an expected device that wasn't found, hibernate with no swap)
            if c.get("preset"):
                state["answers"][c["id"]] = {"answer": c["preset"]["answer"], "note": c["preset"]["note"],
                                             "at": stamp, "live": _preset_live(c)}
        s = cls(state, store, readers, hibernate_action, now, sleep_clock)
        state["current"] = s._next_check(None)
        if state["current"] is None:
            state["finished"] = stamp
            s.finished.set()
        s._save()
        return s

    @classmethod
    def resume(cls, saved: dict, plan: dict | None, plan_path, store, readers,
               hibernate_action=systemctl_hibernate, now=iso_now, sleep_clock=sleep_offset) -> "Session":
        """carry on a saved run. plan None: the saved run's own plan (a detected plan isn't detected again)."""
        if plan is None:
            plan = parse_plan(saved.get("plan"))
            plan_path = saved.get("plan_path") or plan_path
        if signature(saved.get("plan") or {"checks": []}) != signature(plan):
            raise SessionError("the plan's checks (ids or kinds) changed since this run started; "
                               "use the same plan, or --new to start over")
        state = dict(saved)
        state["plan"] = plan
        state["plan_path"] = str(plan_path)
        state.setdefault("answers", {})
        state["hibernate"] = {**_new_hibernate(), **(state.get("hibernate") or {})}
        state["boot_id"] = readers.boot_id()
        s = cls(state, store, readers, hibernate_action, now, sleep_clock)
        s._resume_hibernate(state["boot_id"])
        if s._check(state.get("current"), missing_ok=True) is None or s._answered(state["current"]):
            state["current"] = s._next_check(state.get("current"))
        state["updated"] = now()
        s._save()
        return s

    def _resume_hibernate(self, boot_now: str | None) -> None:
        hib = self.state["hibernate"]
        if hib["phase"] not in ("pending", "hibernating"):
            return
        hid = self._hibernate_id()
        hib["boot_id_after"] = boot_now
        before = hib.get("boot_id")
        if hib["phase"] == "pending" and not hib.get("action_started"):
            # systemctl was never called: hwcheck stopped, crashed or lost power in the delay.
            # whatever the machine did since (a suspend overnight, say), hibernate didn't run.
            hib.update(phase="failed", error="hibernate never ran: hwcheck ended before it started "
                                             "(stopped, crashed or lost power); try it again")
        elif before and boot_now and before == boot_now:
            mark = hib.get("sleep_mark")
            slept = None if mark is None else self.sleep_clock() - mark
            if slept is not None and slept < SLEEP_GAP:
                # same boot and no time asleep since the press: it never hibernated
                hib.update(phase="failed", error="the machine didn't sleep before hwcheck stopped; "
                                                 "try hibernate again")
            else:
                hib["phase"] = "resumed"
                hib["resumed_at"] = self.now()
                if slept is not None:
                    hib["slept_seconds"] = round(slept)
            hib["kernel_log"] = {"this_boot": self.readers.kernel_log("0")}
        elif before and boot_now:
            # a new boot id: the machine started fresh instead of resuming.
            hib["phase"] = "fresh-boot"
            hib["kernel_log"] = {"this_boot": self.readers.kernel_log("0"),
                                 "last_boot": self.readers.kernel_log("-1")}
            if hid:
                self.state["answers"][hid] = {"answer": "doesnt-work", "note": "", "at": self.now(),
                                              "live": self._hibernate_live(), "final": False}
        else:
            hib["phase"] = "unknown"
            hib["kernel_log"] = {"this_boot": self.readers.kernel_log("0")}
        if hid:
            self.state["current"] = hid
        self.state["finished"] = None
        self.finished.clear()

    # ------------------------------------------------------------ helpers

    @property
    def checks(self) -> list[dict]:
        return self.state["plan"]["checks"]

    def _check(self, check_id, missing_ok: bool = False) -> dict | None:
        for c in self.checks:
            if c["id"] == check_id:
                return c
        if missing_ok:
            return None
        raise SessionError(f"no check with id {check_id!r}", 404)

    def _hibernate_id(self) -> str | None:
        return next((c["id"] for c in self.checks if c["kind"] == "hibernate"), None)

    def _answered(self, check_id) -> bool:
        a = self.state["answers"].get(check_id)
        return a is not None and a.get("final", True)

    def _others_done(self) -> bool:
        return all(self._answered(c["id"]) for c in self.checks if c["kind"] != "hibernate")

    def _next_check(self, after: str | None) -> str | None:
        others = [c["id"] for c in self.checks if c["kind"] != "hibernate"]
        start = others.index(after) + 1 if after in others else 0
        for cid in others[start:] + others[:start]:
            if not self._answered(cid):
                return cid
        hid = self._hibernate_id()
        if hid and not self._answered(hid):
            return hid
        return None

    def _hibernate_live(self) -> dict:
        hib = self.state["hibernate"]
        phase = hib["phase"]
        resumed = True if phase == "resumed" else False if phase in ("fresh-boot", "failed") else None
        return {
            "source": "hibernate",
            "phase": phase,
            "resumed": resumed,
            "boot_id_before": hib.get("boot_id"),
            "boot_id_after": hib.get("boot_id_after"),
            "hibernated_at": hib.get("at"),
            "action_started": hib.get("action_started"),
            "slept_seconds": hib.get("slept_seconds"),
            "error": hib.get("error"),
            "kernel_log": hib.get("kernel_log"),
        }

    def _live_for(self, check: dict, observed) -> dict | None:
        if check.get("preset"):
            return _preset_live(check)
        kind_where = where(check["kind"])
        if kind_where == "live":
            lv = self.live
            if not lv or lv["check"] != check["id"] or lv["reading"] is None:
                return {"read": False, "source": check["kind"], "error": "not read yet"}
            out = dict(lv["reading"])
            out["seen"] = list(lv["seen"])
            return out
        if kind_where == "hibernate":
            return self._hibernate_live()
        if observed:
            try:
                size = len(json.dumps(observed))
            except (TypeError, ValueError):
                size = MAX_OBSERVED + 1
            if size > MAX_OBSERVED:
                return {"source": "page", "error": "what the page saw was too large to keep"}
            return {"source": "page", **observed}
        return None

    def _open(self) -> bool:
        """call with the lock held: may this run still change what's on disk?"""
        return not self.stopping.is_set() and not self.state["finished"]

    @contextlib.contextmanager
    def _changing(self):
        """call with the lock held and change the state inside: the change is saved at the end,
        or, if the save is refused (stopping) or fails (a full disk), undone, so memory and
        disk never disagree."""
        state_before = copy.deepcopy(self.state)
        live_before = copy.deepcopy(self.live)
        try:
            yield
            self._changed(save=True)
        except BaseException:
            self.state.clear()
            self.state.update(state_before)
            self.live = live_before
            raise

    def _changed(self, save: bool) -> None:
        """call with the lock held."""
        if save:
            if self.stopping.is_set():
                # hwcheck is ending and may already be giving up its lock: nothing more is written
                raise SessionError("hwcheck is stopping; nothing more can be saved", 503)
            self.state["updated"] = self.now()
            self._save()
        self.version += 1
        self.cond.notify_all()

    def _save(self) -> None:
        try:
            self.store.save(self.state, self.results())
        except OSError as exc:
            raise SessionError(f"couldn't save to {self.store.dir}: {exc.strerror or exc}", 500) from None

    # ------------------------------------------------------------ views

    def results(self) -> dict:
        checks = []
        for c in self.checks:
            a = self.state["answers"].get(c["id"]) or {}
            checks.append({
                "id": c["id"], "title": c["title"], "kind": c["kind"], "device": c.get("device"),
                "answer": a.get("answer"), "note": a.get("note", ""),
                "at": a.get("at"), "live": a.get("live"),
            })
        return {
            "hwcheck": __version__,
            "machine": self.state.get("machine"),
            "model": self.state.get("model"),
            "boot_id": self.state.get("boot_id"),
            "plan": self.state.get("plan_path"),
            "started": self.state.get("started"),
            "updated": self.state.get("updated"),
            "finished": self.state.get("finished"),
            "checks": checks,
        }

    def snapshot(self) -> dict:
        with self.lock:
            cur = self.state["current"]
            fin = self.state["finished"]
            checks, counts = [], {"works": 0, "doesnt-work": 0, "skip": 0, "left": 0}
            for c in self.checks:
                a = self.state["answers"].get(c["id"])
                done = self._answered(c["id"])
                if c["id"] == cur and not fin:
                    status = "running"
                elif done:
                    status = a["answer"]
                else:
                    status = "waiting"
                if done:
                    counts[a["answer"]] += 1
                else:
                    counts["left"] += 1
                checks.append({
                    "id": c["id"], "title": c["title"], "kind": c["kind"], "where": where(c["kind"]),
                    "instructions": c["instructions"], "expect": c["expect"], "status": status,
                    "device": c.get("device"), "preset": bool(c.get("preset")),
                    "answer": a["answer"] if a else None, "note": a["note"] if a else "",
                })
            hib = self.state["hibernate"]
            return {
                "hwcheck": __version__,
                "instance": self.instance,
                "version": self.version,
                "machine": self.state.get("machine"),
                "model": self.state.get("model"),
                "boot_id": self.state.get("boot_id"),
                "results_path": str(self.store.results_path),
                "started": self.state.get("started"),
                "finished": fin,
                "current": None if fin else cur,
                "checks": checks,
                "counts": counts,
                "live": json.loads(json.dumps(self.live)) if self.live else None,
                "hibernate": {"phase": hib["phase"], "error": hib.get("error"),
                              "slept_seconds": hib.get("slept_seconds"),
                              "others_done": self._others_done()},
            }

    # ------------------------------------------------------------ actions

    def answer(self, check_id, answer, note="", observed=None) -> dict:
        if answer not in ANSWERS:
            raise SessionError("answer must be works, doesnt-work or skip")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise SessionError("note must be text")
        note = note.strip()
        if len(note) > MAX_NOTE:
            raise SessionError(f"the note is longer than {MAX_NOTE} characters")
        if observed is not None and not isinstance(observed, dict):
            raise SessionError("observed must be an object")
        self._check_resumed()  # an answer just after a real resume must see it
        with self.lock:
            check = self._guard_current(check_id)
            if check.get("preset") and answer == "works":
                raise SessionError(f"detection recorded this before the run began ({check['preset']['note']}); "
                                   "it can be doesn't work or skip", 409)
            kind = check["kind"]
            need_live = where(kind) == "live" and (
                not self.live or self.live["check"] != check_id or self.live["reading"] is None)
            hib = self.state["hibernate"]
            need_log = kind == "hibernate" and hib["phase"] != "idle" and hib.get("kernel_log") is None
            need_boot = kind == "hibernate" and hib["phase"] != "idle" and hib.get("boot_id_after") is None
            if kind == "hibernate" and answer not in HIBERNATE_ANSWERS[hib["phase"]]:
                raise SessionError(self._hibernate_refusal(hib["phase"], answer), 409)
        # readings can take a moment; take them without holding the lock
        if need_live:
            self.poll_once()
        log = self.readers.kernel_log("0") if need_log else None
        boot = self.readers.boot_id() if need_boot else None
        with self.lock:
            check = self._guard_current(check_id)
            hib = self.state["hibernate"]
            if kind == "hibernate" and answer not in HIBERNATE_ANSWERS[hib["phase"]]:
                raise SessionError(self._hibernate_refusal(hib["phase"], answer), 409)
            with self._changing():
                if kind == "hibernate":
                    if log is not None and hib.get("kernel_log") is None:
                        hib["kernel_log"] = {"this_boot": log}
                    if need_boot and hib.get("boot_id_after") is None:
                        hib["boot_id_after"] = boot
                self.state["answers"][check_id] = {"answer": answer, "note": note, "at": self.now(),
                                                   "live": self._live_for(check, observed)}
                nxt = self._next_check(check_id)
                if nxt is None:
                    self.state["finished"] = self.now()
                else:
                    self.state["current"] = nxt
                    self.live = None
            if nxt is None:
                self.finished.set()
            self.wake.set()
            return self.snapshot()

    def _guard_current(self, check_id) -> dict:
        if self.stopping.is_set():
            raise SessionError("hwcheck is stopping", 503)
        if self.state["finished"]:
            raise SessionError("this run is finished", 409)
        check = self._check(check_id)
        if check_id != self.state["current"]:
            raise SessionError("that check isn't the one on screen; reload the page", 409)
        return check

    @staticmethod
    def _hibernate_refusal(phase: str, answer: str) -> str:
        if phase == "fresh-boot":
            return "the machine booted fresh, so hibernate is recorded as doesn't work; add a note and save"
        if answer == "works" and phase in ("idle", "failed"):
            return "run hibernate first: press hibernate now"
        if answer == "works" and phase in ("pending", "hibernating"):
            return "the machine hasn't hibernated and come back yet"
        return f"{answer} can't be recorded while hibernate is {phase}"

    def select(self, check_id) -> dict:
        with self.lock:
            if self.stopping.is_set():
                raise SessionError("hwcheck is stopping", 503)
            if self.state["finished"]:
                raise SessionError("this run is finished", 409)
            check = self._check(check_id)
            hib = self.state["hibernate"]
            if check["kind"] == "hibernate" and not self._others_done():
                raise SessionError("hibernate runs last: answer the other checks first", 409)
            if hib["phase"] in ("pending", "hibernating") and check["kind"] != "hibernate":
                raise SessionError("hibernate is under way", 409)
            if check_id != self.state["current"]:
                with self._changing():
                    self.state["current"] = check_id
                    self.live = None
                self.wake.set()
            return self.snapshot()

    def start_hibernate(self) -> dict:
        with self.lock:
            if self.stopping.is_set():
                raise SessionError("hwcheck is stopping", 503)
            if self.state["finished"]:
                raise SessionError("this run is finished", 409)
            cur = self._check(self.state["current"], missing_ok=True)
            if not cur or cur["kind"] != "hibernate":
                raise SessionError("hibernate isn't the check on screen", 409)
            if cur.get("preset"):  # detection skipped it (too little swap, say): never start it
                raise SessionError(f"detection skipped hibernate: {cur['preset']['note']}", 409)
            hib = self.state["hibernate"]
            if hib["phase"] not in ("idle", "failed"):
                raise SessionError(f"hibernate is already {hib['phase']}", 409)
            with self._changing():  # on disk before the machine goes down
                hib.update(_new_hibernate())
                hib.update(phase="pending", boot_id=self.readers.boot_id(), at=self.now(),
                           sleep_mark=self.sleep_clock())
            return self.snapshot()

    def launch_hibernate(self, delay: float = 1.0) -> None:
        """run the hibernate in its own thread, once the page has shown 'hibernating'."""
        self._hibernate_thread = threading.Thread(target=self.run_hibernate, args=(delay,),
                                                  name="hwcheck-hibernate", daemon=True)
        self._hibernate_thread.start()

    def join_background(self, timeout: float = 2.0) -> None:
        """after stop(): wait a little for the hibernate thread (systemctl may still be running).
        whatever it does after this, it writes nothing: stop() closed every save path."""
        t = getattr(self, "_hibernate_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout)

    def run_hibernate(self, delay: float = 1.0) -> None:
        if self.stopping.wait(delay):
            # hwcheck is ending: never hibernate now, and write nothing. the state says pending;
            # --resume sees the machine never slept and offers to try again.
            return
        with self.lock:
            # the person may have answered (and finished the run) during the delay
            hid = self._hibernate_id()
            if (not self._open() or self.state["hibernate"]["phase"] != "pending"
                    or (hid and self._answered(hid))):
                return
            try:
                # on disk (fsynced) before systemctl runs: --resume trusts a pending hibernate
                # only with this, so one stopped, crashed or cut off in the delay reads as never run
                with self._changing():
                    self.state["hibernate"]["action_started"] = self.now()
            except SessionError:
                return  # couldn't record it: then don't hibernate
        try:
            ok, message = self.hibernate_action()
        except Exception as exc:  # the action must not kill the thread silently
            ok, message = False, f"hibernate failed: {exc}"
        with self.lock:
            hib = self.state["hibernate"]
            # while systemctl ran, the person may have answered (finishing the run), hwcheck may
            # be stopping, or the resume may already have been seen: then write nothing
            if not self._open() or (hid and self._answered(hid)) or hib["phase"] != "pending":
                return
            try:
                with self._changing():
                    if ok:
                        hib["phase"] = "hibernating"
                    else:
                        hib.update(phase="failed", error=message or "hibernate failed")
            except SessionError:
                return

    # ------------------------------------------------------------ polling

    def poll_once(self) -> None:
        """read the live state for the check on screen; notice a resume from hibernate."""
        with self._poll_lock:
            with self.lock:
                cid = None if self.state["finished"] else self.state["current"]
                check = self._check(cid, missing_ok=True) if cid else None
            if check is None or where(check["kind"]) != "live":
                if self._polled is not None:
                    self.readers.release()
                    self._polled = None
                if check is not None and check["kind"] == "hibernate":
                    self._check_resumed()
                return
            if self._polled != cid:
                self.readers.release()
                self._polled = cid
            reading = self.readers.read(check["kind"])
            reading["at"] = self.now()
            with self.lock:
                if self.state["current"] != cid or self.state["finished"]:
                    return
                if not self.live or self.live["check"] != cid:
                    self.live = {"check": cid, "reading": None, "seen": []}
                self.live["reading"] = reading
                seen = self.live["seen"]
                if not seen or seen[-1]["summary"] != reading["summary"]:
                    seen.append({"at": reading["at"], "summary": reading["summary"]})
                    del seen[:-MAX_SEEN]
                self._changed(save=False)

    def _check_resumed(self) -> None:
        with self.lock:
            hib = self.state["hibernate"]
            phase, mark, began = hib["phase"], hib.get("sleep_mark"), hib.get("action_started")
        # a sleep counts as a hibernate only once systemctl was called: a suspend in the delay doesn't
        if phase not in ("pending", "hibernating") or mark is None or not began:
            return
        gap = self.sleep_clock() - mark
        if gap < SLEEP_GAP:
            return
        boot = self.readers.boot_id()
        log = self.readers.kernel_log("0")
        with self.lock:
            hib = self.state["hibernate"]
            if not self._open() or hib["phase"] not in ("pending", "hibernating"):
                return
            try:
                with self._changing():
                    hib.update(phase="resumed", boot_id_after=boot, slept_seconds=round(gap),
                               resumed_at=self.now(), kernel_log={"this_boot": log})
            except SessionError:
                return

    def poll_loop(self, interval: float = 1.0) -> None:
        try:
            while not self.stopping.is_set():
                try:
                    self.poll_once()
                except Exception:  # keep polling; a bad read shows on the next one
                    pass
                self.wake.wait(interval)
                self.wake.clear()
        finally:
            self.readers.release()

    def stop(self) -> None:
        """end the run's writing. the flag is set under the lock: a change in progress finishes
        (saved) first, and every one after sees the flag before it changes anything."""
        with self.cond:
            self.stopping.set()
            self.wake.set()
            self.cond.notify_all()
