import json
import threading
import unittest
from datetime import datetime, timedelta, timezone

from hwchecklib.plan import parse_plan
from hwchecklib.session import Session, SessionError
from hwchecklib.store import Store

from .fakes import BOOT_A, BOOT_B, FakeClock, FakeHibernate, FakeSpawn, fake_readers, fixture, scratch_dir

MANUAL = [{"id": c, "title": c, "kind": "manual"} for c in ("a", "b", "c")]
HIB = {"id": "hibernate", "title": "hibernate", "kind": "hibernate"}


class FakeNow:
    def __init__(self):
        self.t = datetime(2026, 10, 27, 11, 0, 0, tzinfo=timezone(timedelta(hours=-7)))

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t.isoformat(timespec="seconds")


class Harness:
    def __init__(self, test, checks, readers=None, hibernate=None):
        self.dir = scratch_dir(test)
        self.store = Store(self.dir / "state")
        self.plan = parse_plan({"checks": checks})
        self.readers = readers or fake_readers()
        self.hibernate = hibernate or FakeHibernate()
        self.now = FakeNow()
        self.clock = FakeClock(0.0)
        self.session = Session.new(self.plan, self.dir / "plan.json", self.store, self.readers,
                                   self.hibernate, now=self.now, sleep_clock=self.clock)

    def resume(self, readers=None):
        self.session = Session.resume(self.store.load_state(), self.plan, self.dir / "plan.json", self.store,
                                      readers or self.readers, self.hibernate, now=self.now,
                                      sleep_clock=self.clock)
        return self.session

    def results(self):
        return json.loads(self.store.results_path.read_text(encoding="utf-8"))

    def saved_state(self):
        return json.loads(self.store.state_path.read_text(encoding="utf-8"))

    def by_id(self):
        return {c["id"]: c for c in self.results()["checks"]}


class Answers(unittest.TestCase):
    def test_each_answer_kind_is_saved_with_its_note_after_every_answer(self):
        h = Harness(self, MANUAL)
        r = h.results()
        self.assertEqual((r["machine"], r["model"], r["boot_id"]), ("testbook", "LENOVO 21CD0019US ThinkPad X1 Yoga Gen 7", BOOT_A))
        self.assertEqual([c["answer"] for c in r["checks"]], [None, None, None])

        h.session.answer("a", "works", "  fine  ")
        a = h.by_id()["a"]
        self.assertEqual((a["answer"], a["note"]), ("works", "fine"))
        self.assertTrue(a["at"].startswith("2026-10-27T11:00:"))
        self.assertIsNone(h.by_id()["b"]["answer"])
        self.assertEqual(h.saved_state()["current"], "b")

        h.session.answer("b", "doesnt-work", "left speaker silent")
        self.assertEqual(h.by_id()["b"]["note"], "left speaker silent")
        self.assertFalse(h.session.finished.is_set())

        snap = h.session.answer("c", "skip", "no headphones here")
        got = h.by_id()
        self.assertEqual([got[x]["answer"] for x in "abc"], ["works", "doesnt-work", "skip"])
        self.assertEqual(got["c"]["note"], "no headphones here")
        self.assertTrue(h.session.finished.is_set())
        self.assertIsNotNone(h.results()["finished"])
        self.assertEqual(snap["counts"], {"works": 1, "doesnt-work": 1, "skip": 1, "left": 0})

    def test_refusals(self):
        h = Harness(self, MANUAL)
        for args, status in ((("a", "maybe"), 400), (("zz", "works"), 404), (("b", "works"), 409)):
            with self.assertRaises(SessionError) as ctx:
                h.session.answer(*args)
            self.assertEqual(ctx.exception.status, status)
        with self.assertRaises(SessionError):
            h.session.answer("a", "works", "x" * 2001)
        with self.assertRaises(SessionError):
            h.session.answer("a", "works", "", ["not", "an", "object"])
        for c in "abc":
            h.session.answer(c, "works")
        with self.assertRaises(SessionError) as ctx:
            h.session.answer("c", "works")
        self.assertEqual(ctx.exception.status, 409)

    def test_page_observations_are_kept_as_live(self):
        h = Harness(self, [{"id": "cam", "title": "camera", "kind": "camera"}, *MANUAL])
        h.session.answer("cam", "works", "", {"cameras_found": 2, "cameras_seen": ["front (1280×720)"]})
        self.assertEqual(h.by_id()["cam"]["live"],
                         {"source": "page", "cameras_found": 2, "cameras_seen": ["front (1280×720)"]})
        h.session.answer("a", "works", "", {"big": "x" * 9000})
        self.assertIn("too large", h.by_id()["a"]["live"]["error"])
        h.session.answer("b", "works")
        self.assertIsNone(h.by_id()["b"]["live"])


class LiveChecks(unittest.TestCase):
    def test_reading_and_what_was_seen_are_recorded(self):
        h = Harness(self, [{"id": "power", "title": "charger", "kind": "power"}, *MANUAL])
        h.session.poll_once()
        h.readers.run.table["upower -d"] = fixture("upower-battery.txt")  # the charger comes out
        h.session.poll_once()
        h.session.poll_once()
        snap = h.session.snapshot()
        self.assertEqual(snap["live"]["reading"]["summary"], "on battery, BAT0 84% discharging")
        self.assertEqual(len(snap["live"]["seen"]), 2)
        h.session.answer("power", "works", "switched both ways")
        live = h.by_id()["power"]["live"]
        self.assertTrue(live["read"])
        self.assertEqual(live["source"], "upower -d")
        self.assertEqual([s["summary"] for s in live["seen"]],
                         ["on mains, BAT0 85% charging", "on battery, BAT0 84% discharging"])
        self.assertIsNone(h.session.snapshot()["live"])  # the next check starts clean

    def test_answering_before_any_poll_reads_first(self):
        h = Harness(self, [{"id": "bt", "title": "bluetooth", "kind": "bluetooth"}, *MANUAL])
        h.session.answer("bt", "works")
        self.assertEqual(h.by_id()["bt"]["live"]["summary"], "2 connected: WH-1000XM4, MX Master 3")

    def test_a_missing_tool_leaves_the_check_manual(self):
        h = Harness(self, [{"id": "power", "title": "charger", "kind": "power"}, *MANUAL],
                    readers=fake_readers(table={}))
        h.session.poll_once()
        self.assertEqual(h.session.snapshot()["live"]["reading"]["summary"], "can't read: upower not found")
        h.session.answer("power", "doesnt-work", "no charger light")
        live = h.by_id()["power"]["live"]
        self.assertEqual((live["read"], live["error"]), (False, "upower not found"))
        self.assertEqual(h.by_id()["power"]["answer"], "doesnt-work")

    def test_the_rotation_tool_stops_when_its_check_ends(self):
        spawn = FakeSpawn(fixture("monitor-sensor-normal.txt"))
        h = Harness(self, [{"id": "rot", "title": "rotation", "kind": "rotation"}, *MANUAL],
                    readers=fake_readers(spawn=spawn))
        h.session.poll_once()
        self.assertEqual(h.readers.watching(), ["rotation"])
        h.session.answer("rot", "works")
        h.session.poll_once()
        self.assertEqual(h.readers.watching(), [])
        self.assertTrue(spawn.procs[0].stopped)

    def test_selecting_another_check(self):
        h = Harness(self, [*MANUAL, HIB])
        with self.assertRaises(SessionError) as ctx:
            h.session.select("hibernate")
        self.assertIn("runs last", str(ctx.exception))
        h.session.select("c")
        self.assertEqual(h.saved_state()["current"], "c")
        h.session.answer("c", "works")
        self.assertEqual(h.session.snapshot()["current"], "a")  # wraps to the first unanswered
        h.session.answer("a", "works")
        h.session.answer("b", "works")
        self.assertEqual(h.session.snapshot()["current"], "hibernate")
        statuses = {c["id"]: c["status"] for c in h.session.snapshot()["checks"]}
        self.assertEqual(statuses, {"a": "works", "b": "works", "c": "works", "hibernate": "running"})


class Hibernate(unittest.TestCase):
    def ready(self, **kw):
        h = Harness(self, [MANUAL[0], HIB], **kw)
        h.session.answer("a", "works")
        return h

    def test_pending_and_boot_id_are_on_disk_before_hibernating(self):
        seen = {}
        h = self.ready()
        h.hibernate.then = lambda: seen.update(h.saved_state()["hibernate"])
        with self.assertRaises(SessionError) as ctx:
            h.session.answer("hibernate", "works")
        self.assertIn("run hibernate first", str(ctx.exception))
        h.session.start_hibernate()
        h.session.run_hibernate(0)
        self.assertEqual(h.hibernate.calls, 1)
        self.assertEqual((seen["phase"], seen["boot_id"]), ("pending", BOOT_A))
        self.assertEqual(h.saved_state()["hibernate"]["phase"], "hibernating")

    def test_works_needs_a_sleep_actually_seen(self):
        h = self.ready()
        h.session.start_hibernate()
        with self.assertRaises(SessionError) as ctx:
            h.session.answer("hibernate", "works")  # pending
        self.assertIn("hasn't hibernated", str(ctx.exception))
        h.session.run_hibernate(0)
        self.assertEqual(h.session.snapshot()["hibernate"]["phase"], "hibernating")
        with self.assertRaises(SessionError):
            h.session.answer("hibernate", "works", "nothing happened")  # systemctl returned, no sleep
        self.assertFalse(h.session.finished.is_set())
        # just after a real resume, before the poller has looked, works is accepted
        h.clock.value += 20
        h.session.answer("hibernate", "works", "back")
        live = h.by_id()["hibernate"]["live"]
        self.assertEqual((live["phase"], live["resumed"], live["slept_seconds"]), ("resumed", True, 20))

    def test_doesnt_work_while_hibernating(self):
        h = self.ready()
        h.session.start_hibernate()
        h.session.run_hibernate(0)
        h.session.answer("hibernate", "doesnt-work", "screen went black and stayed on")
        live = h.by_id()["hibernate"]["live"]
        self.assertEqual((live["phase"], live["resumed"]), ("hibernating", None))

    def test_an_answer_during_the_delay_cancels_the_hibernate(self):
        h = self.ready()
        h.session.start_hibernate()
        h.session.answer("hibernate", "skip", "not today")
        self.assertTrue(h.session.finished.is_set())
        h.session.run_hibernate(0)
        self.assertEqual(h.hibernate.calls, 0)

    def test_a_run_finished_in_the_delay_is_left_as_it_was(self):
        h = self.ready()
        h.session.start_hibernate()
        h.session.answer("hibernate", "skip", "not today")
        before = h.store.state_path.read_bytes()
        h.session.stop()  # what serve does when the run is over
        h.session.run_hibernate(3600)  # the waiting hibernate wakes to the stop
        self.assertEqual(h.hibernate.calls, 0)
        self.assertEqual(h.store.state_path.read_bytes(), before)  # nothing written after the end

    def test_an_answer_while_systemctl_runs_ends_the_writing(self):
        for ok in (True, False):  # systemctl coming back fine, or with an error
            with self.subTest(systemctl_ok=ok):
                h = self.ready(hibernate=FakeHibernate(ok, "" if ok else "Call to Hibernate failed"))
                seen = {}

                def the_person_answers():  # while systemctl hibernate is still running
                    h.session.answer("hibernate", "skip", "changed my mind")
                    seen["state"] = h.store.state_path.read_bytes()
                    seen["results"] = h.store.results_path.read_bytes()

                h.hibernate.then = the_person_answers
                h.session.start_hibernate()
                h.session.run_hibernate(0)
                self.assertEqual(h.hibernate.calls, 1)
                self.assertTrue(h.session.finished.is_set())
                # the finished run's files are as the answer left them: no "hibernating", no "failed"
                self.assertEqual(h.store.state_path.read_bytes(), seen["state"])
                self.assertEqual(h.store.results_path.read_bytes(), seen["results"])
                self.assertEqual(h.saved_state()["hibernate"]["phase"], "pending")
                self.assertEqual(h.by_id()["hibernate"]["answer"], "skip")

    def test_the_action_marker_is_on_disk_before_systemctl_runs(self):
        h = self.ready()
        on_disk = {}
        h.hibernate.then = lambda: on_disk.update(h.saved_state()["hibernate"])
        h.session.start_hibernate()
        self.assertIsNone(h.saved_state()["hibernate"]["action_started"])  # not at the press
        h.session.run_hibernate(0)
        self.assertEqual(on_disk["phase"], "pending")
        self.assertTrue(on_disk["action_started"])

    def test_a_suspend_in_the_delay_is_not_a_hibernate(self):
        h = self.ready()
        h.session.start_hibernate()  # the delay: systemctl not yet called
        h.clock.value += 30  # the lid closed and the machine suspended, say
        h.session.poll_once()
        self.assertEqual(h.session.snapshot()["hibernate"]["phase"], "pending")
        with self.assertRaises(SessionError):
            h.session.answer("hibernate", "works")

    def test_the_sleep_mark_is_taken_with_the_action_mark(self):
        h = self.ready()
        h.clock.value = 7.5
        h.session.start_hibernate()
        self.assertIsNone(h.saved_state()["hibernate"]["sleep_mark"])  # not at the press
        h.clock.value = 9.0
        h.session.run_hibernate(0)
        self.assertEqual(h.saved_state()["hibernate"]["sleep_mark"], 9.0)

    def test_a_suspend_in_the_delay_doesnt_count_toward_the_sleep(self):
        h = self.ready()
        h.session.start_hibernate()
        h.clock.value += 30  # suspended in the second before systemctl
        h.session.run_hibernate(0)  # systemctl accepted the job
        h.session.poll_once()
        self.assertEqual(h.session.snapshot()["hibernate"]["phase"], "hibernating")  # not "resumed"
        with self.assertRaises(SessionError):
            h.session.answer("hibernate", "works")
        h.clock.value += 40  # now it really hibernates and comes back
        h.session.poll_once()
        self.assertEqual(h.session.snapshot()["hibernate"]["slept_seconds"], 40)

    def test_a_refused_systemctl_doesnt_leave_resumed_standing(self):
        h = self.ready(hibernate=FakeHibernate(False, "Call to Hibernate failed: Sleep verb not supported"))

        def a_sleep_seen_while_systemctl_ran():
            h.clock.value += 30
            h.session.poll_once()
            self.assertEqual(h.session.snapshot()["hibernate"]["phase"], "resumed")

        h.hibernate.then = a_sleep_seen_while_systemctl_ran
        h.session.start_hibernate()
        h.session.run_hibernate(0)
        snap = h.session.snapshot()["hibernate"]
        self.assertEqual(snap["phase"], "failed")
        self.assertIn("not supported", snap["error"])
        self.assertEqual(h.saved_state()["hibernate"]["phase"], "failed")
        with self.assertRaises(SessionError):
            h.session.answer("hibernate", "works")

    def test_a_mark_that_cant_be_saved_fails_hibernate_and_it_can_be_retried(self):
        h = self.ready()
        h.session.start_hibernate()
        real_save = h.store.save
        h.store.save = lambda state, results: (_ for _ in ()).throw(OSError(28, "No space left on device"))
        h.session.run_hibernate(0)
        self.assertEqual(h.hibernate.calls, 0)  # not started without its mark
        snap = h.session.snapshot()["hibernate"]
        self.assertEqual(snap["phase"], "failed")
        self.assertIn("couldn't be saved", snap["error"])
        h.store.save = real_save  # space freed
        h.session.start_hibernate()  # retried, not refused as "already pending"
        h.session.run_hibernate(0)
        self.assertEqual(h.hibernate.calls, 1)
        self.assertEqual(h.saved_state()["hibernate"]["phase"], "hibernating")

    def test_resume_in_the_same_process(self):
        h = self.ready()
        h.session.start_hibernate()
        h.session.run_hibernate(0)
        h.session.poll_once()
        self.assertEqual(h.session.snapshot()["hibernate"]["phase"], "hibernating")  # no sleep yet
        h.clock.value += 42.4  # boottime ran on while monotonic stood still
        h.session.poll_once()
        self.assertEqual(h.session.snapshot()["hibernate"]["phase"], "resumed")
        h.session.answer("hibernate", "works", "wifi and sound came back")
        hib = h.by_id()["hibernate"]
        self.assertEqual((hib["answer"], hib["note"]), ("works", "wifi and sound came back"))
        live = hib["live"]
        self.assertEqual((live["phase"], live["resumed"], live["slept_seconds"]), ("resumed", True, 42))
        self.assertEqual(live["boot_id_before"], live["boot_id_after"])
        self.assertIn("PM: hibernation: hibernation exit", live["kernel_log"]["this_boot"]["lines"][-1])
        self.assertTrue(h.session.finished.is_set())

    def test_a_resume_seen_while_systemctl_is_still_returning_is_kept(self):
        h = self.ready()

        def slow_systemctl():
            h.clock.value += 30
            h.session.poll_once()

        h.hibernate.then = slow_systemctl
        h.session.start_hibernate()
        h.session.run_hibernate(0)
        self.assertEqual(h.session.snapshot()["hibernate"]["phase"], "resumed")

    def test_hibernate_that_fails_to_start(self):
        h = self.ready(hibernate=FakeHibernate(False, "Call to Hibernate failed: Not enough suitable swap space"))
        h.session.start_hibernate()
        h.session.run_hibernate(0)
        snap = h.session.snapshot()
        self.assertEqual(snap["hibernate"]["phase"], "failed")
        self.assertIn("swap space", snap["hibernate"]["error"])
        with self.assertRaises(SessionError):
            h.session.answer("hibernate", "works")
        h.session.start_hibernate()  # try again is allowed
        h.session.run_hibernate(0)
        h.session.answer("hibernate", "doesnt-work", "says no swap")
        live = h.by_id()["hibernate"]["live"]
        self.assertEqual((live["phase"], live["resumed"]), ("failed", False))
        self.assertIn("swap space", live["error"])

    def test_stopping_before_the_action_runs_never_hibernates(self):
        h = self.ready()
        h.session.start_hibernate()
        before = h.store.state_path.read_bytes()
        h.session.stop()
        h.session.run_hibernate(5)
        self.assertEqual(h.hibernate.calls, 0)
        # nothing written once stopping: the press's "pending" stays, and --resume sees no sleep
        self.assertEqual(h.session.snapshot()["hibernate"]["phase"], "pending")
        self.assertEqual(h.store.state_path.read_bytes(), before)

    def test_a_skipped_hibernate_never_starts(self):
        h = Harness(self, [MANUAL[0], dict(HIB, preset={"answer": "skip", "note": "not enough swap to hibernate"})])
        # however it came to be on screen, a hibernate detection skipped is never started
        h.session.state["current"], h.session.state["finished"] = "hibernate", None
        with self.assertRaises(SessionError) as ctx:
            h.session.start_hibernate()
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("not enough swap", str(ctx.exception))
        self.assertEqual(h.session.state["hibernate"]["phase"], "idle")
        h.session.run_hibernate(0)
        self.assertEqual(h.hibernate.calls, 0)
        self.assertEqual(h.by_id()["hibernate"]["live"], {"source": "preset", "evidence": "not enough swap to hibernate"})

    def test_start_hibernate_only_when_it_is_on_screen(self):
        h = Harness(self, [MANUAL[0], HIB])
        with self.assertRaises(SessionError):
            h.session.start_hibernate()


class Presets(unittest.TestCase):
    def test_a_preset_can_be_reanswered_but_not_as_works_and_keeps_its_evidence(self):
        h = Harness(self, [*MANUAL[:2], {"id": "missing-pen", "title": "expected but not detected: pen WACF2200",
                                         "kind": "manual", "preset": {"answer": "doesnt-work",
                                                                      "note": "expected but not detected: pen WACF2200"}}])
        miss = h.by_id()["missing-pen"]
        self.assertEqual((miss["answer"], miss["live"]["evidence"]),
                         ("doesnt-work", "expected but not detected: pen WACF2200"))
        self.assertEqual(h.session.snapshot()["current"], "a")  # starts on the first open check
        h.session.select("missing-pen")
        with self.assertRaises(SessionError) as ctx:
            h.session.answer("missing-pen", "works", "it's there now")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("doesn't work or skip", str(ctx.exception))
        h.session.answer("missing-pen", "skip", "this unit came without the pen")
        miss = h.by_id()["missing-pen"]
        self.assertEqual((miss["answer"], miss["note"]), ("skip", "this unit came without the pen"))
        self.assertEqual(miss["live"], {"source": "preset", "evidence": "expected but not detected: pen WACF2200"})


class Stopping(unittest.TestCase):
    """once stop() has run, nothing is written: hwcheck may already be giving up its lock."""

    def files(self, h):
        return h.store.state_path.read_bytes(), h.store.results_path.read_bytes()

    def test_requests_are_refused(self):
        h = Harness(self, [*MANUAL, HIB])
        h.session.answer("a", "works")
        before = self.files(h)
        h.session.stop()
        for call in (lambda: h.session.answer("b", "works"), lambda: h.session.select("c"),
                     h.session.start_hibernate):
            with self.assertRaises(SessionError) as ctx:
                call()
            self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(self.files(h), before)

    def test_a_resume_seen_after_stopping_is_not_written(self):
        h = Harness(self, [MANUAL[0], HIB])
        h.session.answer("a", "works")
        h.session.start_hibernate()
        h.session.run_hibernate(0)
        before = self.files(h)
        h.session.stop()
        h.clock.value += 30  # it slept; the poller would notice
        h.session.poll_once()
        self.assertEqual(self.files(h), before)

    def test_the_hibernate_thread_is_joined_with_a_bound_and_writes_nothing_after(self):
        h = Harness(self, [MANUAL[0], HIB])
        h.session.answer("a", "works")
        in_systemctl, let_go = threading.Event(), threading.Event()

        def slow_systemctl():
            in_systemctl.set()
            let_go.wait(10)
            return True, ""

        h.session.hibernate_action = slow_systemctl
        h.session.start_hibernate()
        h.session.launch_hibernate(0)
        self.assertTrue(in_systemctl.wait(10))
        before = self.files(h)
        h.session.stop()
        h.session.join_background(0.05)  # bounded: systemctl is still running
        self.assertTrue(h.session._hibernate_thread.is_alive())
        let_go.set()
        h.session._hibernate_thread.join(10)
        self.assertFalse(h.session._hibernate_thread.is_alive())
        self.assertEqual(self.files(h), before)


class SavedOrUndone(unittest.TestCase):
    """an answer is saved, or memory is put back: never one without the other."""

    def test_a_stop_between_the_check_and_the_save_undoes_the_answer(self):
        h = Harness(self, [*MANUAL, HIB])
        h.session.answer("a", "works", "kept")
        disk = h.results()
        real_now = h.session.now
        armed = {"on": False}

        def now_and_maybe_stop():
            if armed["on"]:  # in the middle of recording the answer, hwcheck is told to stop
                armed["on"] = False
                h.session.stop()
            return real_now()

        h.session.now = now_and_maybe_stop
        armed["on"] = True
        with self.assertRaises(SessionError) as ctx:
            h.session.answer("b", "works", "never saved")
        self.assertEqual(ctx.exception.status, 503)
        self.assertFalse(armed["on"])  # the stop really came mid-answer
        self.assertEqual(h.results(), disk)  # nothing on disk
        self.assertEqual(h.session.results(), disk)  # and memory agrees with it
        snap = h.session.snapshot()
        self.assertEqual((snap["current"], snap["counts"]["left"], snap["counts"]["works"]), ("b", 3, 1))

    def test_a_failed_save_undoes_the_answer_and_the_next_one_works(self):
        h = Harness(self, MANUAL)
        real_save = h.store.save

        def disk_full(state, results):
            raise OSError(28, "No space left on device")

        h.store.save = disk_full
        with self.assertRaises(SessionError) as ctx:
            h.session.answer("a", "doesnt-work", "lost")
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(h.session.snapshot()["current"], "a")
        self.assertIsNone(h.session.results()["checks"][0]["answer"])
        h.store.save = real_save  # space freed
        h.session.answer("a", "works", "saved")
        self.assertEqual((h.by_id()["a"]["answer"], h.by_id()["a"]["note"]), ("works", "saved"))

    def test_a_full_disk_between_the_two_files_changes_neither(self):
        from hwchecklib import store as store_module
        h = Harness(self, MANUAL)
        h.session.answer("a", "works", "kept")
        state_before, results_before = h.store.state_path.read_bytes(), h.store.results_path.read_bytes()
        real_write = store_module._write_temp

        def results_dont_fit(path, data):
            if str(path).endswith("results.json"):
                raise OSError(28, "No space left on device")
            return real_write(path, data)

        store_module._write_temp = results_dont_fit
        try:
            with self.assertRaises(SessionError):
                h.session.answer("b", "works", "lost")
        finally:
            store_module._write_temp = real_write
        self.assertEqual(h.store.state_path.read_bytes(), state_before)
        self.assertEqual(h.store.results_path.read_bytes(), results_before)
        self.assertEqual(h.session.results(), json.loads(results_before))

    def test_a_refused_select_or_hibernate_changes_nothing(self):
        h = Harness(self, [MANUAL[0], HIB])
        h.session.answer("a", "works")
        before = h.session.results(), h.session.snapshot()["hibernate"], h.session.state["current"]
        h.session.store.save = lambda state, results: (_ for _ in ()).throw(OSError(5, "I/O error"))
        with self.assertRaises(SessionError):
            h.session.start_hibernate()
        self.assertEqual((h.session.results(), h.session.snapshot()["hibernate"], h.session.state["current"]),
                         before)


class Resume(unittest.TestCase):
    def pending(self, **kw):
        h = Harness(self, [MANUAL[0], MANUAL[1], HIB], **kw)
        h.session.answer("a", "works", "ok")
        h.session.answer("b", "skip")
        h.session.start_hibernate()
        h.session.run_hibernate(0)
        return h  # the process "dies" here: only the files remain

    def test_same_boot_means_it_resumed(self):
        h = self.pending()
        h.clock.value += 64  # it slept
        s = h.resume()
        snap = s.snapshot()
        self.assertEqual((snap["current"], snap["hibernate"]["phase"]), ("hibernate", "resumed"))
        self.assertIsNone(h.by_id()["hibernate"]["answer"])
        s.answer("hibernate", "works", "all came back")
        hib = h.by_id()["hibernate"]
        self.assertEqual((hib["answer"], hib["note"]), ("works", "all came back"))
        self.assertEqual((hib["live"]["boot_id_before"], hib["live"]["boot_id_after"]), (BOOT_A, BOOT_A))
        self.assertTrue(hib["live"]["resumed"])
        self.assertEqual(hib["live"]["slept_seconds"], 64)
        self.assertEqual(h.by_id()["a"]["note"], "ok")  # earlier answers survive
        self.assertTrue(s.finished.is_set())

    def test_a_hibernate_that_never_ran_is_never_a_resume(self):
        """pressed, then hwcheck ended in the delay; the machine did anything at all after."""
        for how in ("stopped, then suspended overnight", "crashed", "power lost, then booted"):
            with self.subTest(how=how):
                h = Harness(self, [MANUAL[0], HIB])
                h.session.answer("a", "works")
                h.session.start_hibernate()
                if how.startswith("stopped"):
                    h.session.stop()
                    h.session.run_hibernate(3600)  # wakes to the stop: never calls systemctl
                h.clock.value += 8 * 3600  # time asleep since the press
                if how.startswith("power"):
                    h.readers.files["/proc/sys/kernel/random/boot_id"] = BOOT_B + "\n"
                self.assertEqual(h.saved_state()["hibernate"]["phase"], "pending")
                s = h.resume()
                snap = s.snapshot()
                self.assertEqual(h.hibernate.calls, 0)
                self.assertEqual((snap["current"], snap["hibernate"]["phase"]), ("hibernate", "failed"))
                self.assertIn("never ran", snap["hibernate"]["error"])
                self.assertIsNone(h.by_id()["hibernate"]["answer"])  # not a fresh-boot doesn't-work either
                with self.assertRaises(SessionError):
                    s.answer("hibernate", "works")
                # and it can be tried again, properly this time
                s.start_hibernate()
                s.run_hibernate(0)
                self.assertEqual(h.hibernate.calls, 1)
                h.clock.value += 40
                s.answer("hibernate", "works", "came back")
                live = h.by_id()["hibernate"]["live"]
                self.assertEqual((live["phase"], live["resumed"]), ("resumed", True))
                self.assertTrue(live["action_started"])

    def test_same_boot_and_no_sleep_means_it_never_hibernated(self):
        h = self.pending()
        s = h.resume()
        snap = s.snapshot()
        self.assertEqual((snap["current"], snap["hibernate"]["phase"]), ("hibernate", "failed"))
        self.assertIn("didn't sleep", snap["hibernate"]["error"])
        with self.assertRaises(SessionError):
            s.answer("hibernate", "works")
        s.start_hibernate()  # it can be tried again
        s.run_hibernate(0)
        h.clock.value += 30
        s.answer("hibernate", "works", "second try came back")
        self.assertTrue(s.finished.is_set())

    def test_same_boot_from_an_older_state_without_a_mark_is_a_resume(self):
        h = self.pending()
        state = h.store.load_state()
        del state["hibernate"]["sleep_mark"]
        h.store.save(state, h.session.results())
        self.assertEqual(h.resume().snapshot()["hibernate"]["phase"], "resumed")

    def test_new_boot_means_it_booted_fresh(self):
        h = self.pending()
        h.readers.files["/proc/sys/kernel/random/boot_id"] = BOOT_B + "\n"
        h.readers.run.table["journalctl -k -b 0 --no-pager -q -o short-monotonic"] = fixture("journal-fresh-boot.txt")
        s = h.resume()
        # recorded as doesn't work straight away, before anyone answers
        hib = h.by_id()["hibernate"]
        self.assertEqual(hib["answer"], "doesnt-work")
        self.assertEqual((hib["live"]["phase"], hib["live"]["resumed"]), ("fresh-boot", False))
        self.assertEqual((hib["live"]["boot_id_before"], hib["live"]["boot_id_after"]), (BOOT_A, BOOT_B))
        self.assertIn("Image not found", " ".join(hib["live"]["kernel_log"]["this_boot"]["lines"]))
        self.assertIn("Cannot find swap device", " ".join(hib["live"]["kernel_log"]["last_boot"]["lines"]))
        self.assertEqual(h.results()["boot_id"], BOOT_B)
        snap = s.snapshot()
        self.assertEqual((snap["current"], snap["finished"]), ("hibernate", None))
        for refused in ("works", "skip"):
            with self.assertRaises(SessionError):
                s.answer("hibernate", refused)
        s.answer("hibernate", "doesnt-work", "it powered off, then started at the login screen")
        hib = h.by_id()["hibernate"]
        self.assertEqual((hib["answer"], hib["note"]),
                         ("doesnt-work", "it powered off, then started at the login screen"))
        self.assertTrue(s.finished.is_set())

    def test_unreadable_boot_id_asks_the_person(self):
        h = self.pending()
        del h.readers.files["/proc/sys/kernel/random/boot_id"]
        s = h.resume()
        self.assertEqual(s.snapshot()["hibernate"]["phase"], "unknown")
        s.answer("hibernate", "works")
        self.assertTrue(s.finished.is_set())

    def test_resume_without_hibernate_carries_on_where_it_stopped(self):
        h = Harness(self, [*MANUAL, HIB])
        h.session.answer("a", "works")
        s = h.resume()
        self.assertEqual(s.snapshot()["current"], "b")
        self.assertEqual(s.snapshot()["hibernate"]["phase"], "idle")

    def test_resume_with_a_changed_plan_is_refused(self):
        h = Harness(self, MANUAL)
        other = parse_plan({"checks": [{"id": "a", "title": "a", "kind": "camera"}]})
        with self.assertRaises(SessionError) as ctx:
            Session.resume(h.store.load_state(), other, "p", h.store, h.readers)
        self.assertIn("changed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
