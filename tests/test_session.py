import json
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

    def test_the_sleep_mark_is_saved_at_the_press(self):
        h = self.ready()
        h.clock.value = 7.5
        h.session.start_hibernate()
        self.assertEqual(h.saved_state()["hibernate"]["sleep_mark"], 7.5)

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
        h.session.stop()
        h.session.run_hibernate(5)
        self.assertEqual(h.hibernate.calls, 0)
        self.assertEqual(h.session.snapshot()["hibernate"]["phase"], "failed")

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
