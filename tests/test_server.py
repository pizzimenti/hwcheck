"""browser-free tests: drive hwcheck's http endpoints the way the page does."""

import contextlib
import http.client
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest

from hwchecklib.cli import main
from hwchecklib.plan import load_plan

from .fakes import (BOOT_B, EXAMPLE_PLAN, ROOT, FakeClock, FakeHibernate, FakeMachine, fake_readers,
                    scratch_dir, wait_for)

MANUAL2 = {"checks": [{"id": "a", "title": "a", "kind": "manual"}, {"id": "b", "title": "b", "kind": "manual"}]}
WITH_HIB = {"checks": [{"id": "a", "title": "a", "kind": "manual"},
                       {"id": "hibernate", "title": "hibernate", "kind": "hibernate"}]}

# what the person "answers" in the whole-plan run: (answer, note, what the page saw)
ANSWERS = {
    "speakers": ("works", "both sides clear", {"tones_played": ["left", "right", "both"]}),
    "microphone": ("works", "", {"device": "Built-in Audio Analog Stereo", "peak_level": 0.71,
                                 "recorded": True, "played_back": True}),
    "headphones": ("skip", "no wired headphones to hand", {"tones_played": []}),
    "camera": ("works", "", {"cameras_seen": ["Integrated Camera (1280×720)"], "cameras_found": 2}),
    "touch": ("works", "", {"max_touches_seen": 10, "reported_max_touch_points": 10,
                            "reached_target": True, "dragged_with": "touch"}),
    "pen": ("works", "", {"pointer_types": ["pen"], "max_pressure": 0.98, "pressure_levels_seen": 87,
                          "max_tilt_degrees": 52, "eraser_seen": False, "side_button_seen": True,
                          "hover_seen": True}),
    "keys": ("doesnt-work", "f12 does nothing", {"keys_seen": 84, "presses": 97, "codes": ["KeyA"]}),
    "touchpad": ("works", "", None),
    "tablet-mode": ("works", "", None),
    "rotation": ("works", "", None),
    "power": ("works", "", None),
    "lid": ("works", "", None),
    "external-display": ("works", "", None),
    "bluetooth": ("works", "", None),
    "wifi": ("works", "", None),
    "hibernate": ("works", "came back with wifi and sound", None),
}


class Run:
    """hwcheck's main() in a thread, on 127.0.0.1 with a free port and fake readers."""

    def __init__(self, test, plan_path, state_dir, readers=None, hibernate=None, args=(), clock=None,
                 hibernate_delay=0, detector=None):
        self.readers = readers or fake_readers()
        self.hibernate = hibernate or FakeHibernate()
        self.clock = clock or FakeClock(0.0)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.server = self.session = self.code = None
        self.ready = threading.Event()
        self.hibernate_delay = hibernate_delay
        self.detector = detector
        plan_args = ["--plan", str(plan_path)] if plan_path else []  # none: detect
        argv = [*plan_args, "--port", "0", "--state-dir", str(state_dir), *args]
        self.thread = threading.Thread(target=self._main, args=(argv,), daemon=True)
        self.thread.start()
        wait_for(lambda: self.ready.is_set() or not self.thread.is_alive(), timeout=10)
        test.addCleanup(self.close)

    def _main(self, argv):
        def on_ready(server, session):
            self.server, self.session = server, session
            self.ready.set()

        self.code = main(argv, readers=self.readers, hibernate=self.hibernate, detector=self.detector,
                         out=self.out, err=self.err,
                         on_ready=on_ready, grace=0.05, poll_interval=0.02,
                         hibernate_delay=self.hibernate_delay,
                         session_options={"sleep_clock": self.clock})

    @property
    def port(self):
        return self.server.server_address[1]

    def request(self, method, path, body=None, headers=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"X-Hwcheck-Token": self.server.token} if token else {}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body)
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp, raw

    def get(self, path="/api/state"):
        resp, raw = self.request("GET", path)
        assert resp.status == 200, (resp.status, raw)
        return json.loads(raw)

    def post(self, path, body):
        resp, raw = self.request("POST", path, body)
        return resp.status, json.loads(raw)

    def wait(self, timeout=10):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "hwcheck didn't exit"
        return self.code

    def close(self):
        if self.session is not None:
            self.session.stop()
        self.thread.join(10)


def write_plan(directory, obj):
    path = directory / "plan.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def read_results(state_dir):
    return json.loads((state_dir / "results.json").read_text(encoding="utf-8"))


class WholePlan(unittest.TestCase):
    def test_a_whole_2in1_plan_through_the_endpoints(self):
        d = scratch_dir(self)
        state_dir = d / "state"
        run = Run(self, EXAMPLE_PLAN, state_dir)
        self.assertEqual(run.server.server_address[0], "127.0.0.1")
        url = run.out.getvalue().splitlines()[0]
        self.assertEqual(url, f"http://127.0.0.1:{run.port}/?t={run.server.token}")
        self.assertGreaterEqual(len(run.server.token), 20)

        # the page and its files: served locally, nothing fetched from elsewhere
        resp, body = run.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/html"))
        self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))
        self.assertIn(b'<script src="/app.js"', body)
        self.assertIn(b'<link rel="icon" href="data:,">', body)  # no favicon request to 404
        for path in ("/", "/app.js", "/style.css"):
            resp, body = run.request("GET", path)
            self.assertEqual(resp.status, 200, path)
            self.assertIsNone(re.search(rb"https?://", body), f"{path} names a remote url")

        plan = load_plan(EXAMPLE_PLAN)
        kinds = {c["id"]: c["kind"] for c in plan["checks"]}
        answered = []
        while True:
            state = run.get()
            if state["finished"]:
                break
            cur = state["current"]
            check = next(c for c in state["checks"] if c["id"] == cur)
            self.assertEqual(check["status"], "running")
            if check["where"] == "live":
                wait_for(lambda: (s := run.get())["live"] and s["live"]["check"] == cur
                         and s["live"]["reading"]
                         and not s["live"]["reading"]["summary"].startswith("waiting"))
            if check["kind"] == "hibernate":
                status, s = run.post("/api/hibernate", {})
                self.assertEqual((status, s["hibernate"]["phase"]), (200, "pending"))
                saved = json.loads((state_dir / "state.json").read_text())
                self.assertEqual(saved["hibernate"]["phase"], "pending")  # on disk before it sleeps
                wait_for(lambda: run.get()["hibernate"]["phase"] == "hibernating")
                self.assertEqual(run.hibernate.calls, 1)
                # systemctl has returned but nothing has slept: works isn't accepted yet
                status, body = run.post("/api/answer", {"id": cur, "answer": "works", "note": "too soon"})
                self.assertEqual(status, 409, body)
                run.clock.value += 31  # the machine slept
                wait_for(lambda: run.get()["hibernate"]["phase"] == "resumed")
            answer, note, observed = ANSWERS[cur]
            status, s = run.post("/api/answer", {"id": cur, "answer": answer, "note": note, "observed": observed})
            self.assertEqual(status, 200, s)
            answered.append(cur)
            if len(answered) == 1:  # saved after every answer, not at the end
                self.assertEqual(read_results(state_dir)["checks"][0]["answer"], answer)

        self.assertEqual(answered, [c["id"] for c in plan["checks"]])
        self.assertEqual(run.wait(), 0)  # it exits after the last answer
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", run.port), timeout=1).close()

        results = read_results(state_dir)
        self.assertEqual((results["machine"], results["model"]), ("testbook", "LENOVO 21CD0019US ThinkPad X1 Yoga Gen 7"))
        self.assertIsNotNone(results["finished"])
        got = {c["id"]: c for c in results["checks"]}
        for cid, (answer, note, observed) in ANSWERS.items():
            c = got[cid]
            self.assertEqual((c["answer"], c["note"]), (answer, note), cid)
            self.assertRegex(c["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
            if observed:
                self.assertEqual(c["live"], {"source": "page", **observed}, cid)
        expect_live = {
            "tablet-mode": "tablet mode: on",
            "rotation": "orientation: normal",
            "power": "on mains, BAT0 85% charging",
            "lid": "lid open",
            "external-display": "external display: HDMI-A-1 on, 1920x1080@60",
            "bluetooth": "2 connected: WH-1000XM4, MX Master 3",
        }
        for cid, summary in expect_live.items():
            self.assertEqual(kinds[cid], cid)
            self.assertTrue(got[cid]["live"]["read"], cid)
            self.assertEqual(got[cid]["live"]["summary"], summary)
            self.assertEqual(got[cid]["live"]["seen"][-1]["summary"], summary)
        hib = got["hibernate"]["live"]
        self.assertEqual((hib["phase"], hib["resumed"], hib["slept_seconds"]), ("resumed", True, 31))
        self.assertIsNone(got["touchpad"]["live"])
        self.assertEqual(run.readers.watching(), [])  # monitor-sensor stopped with its check
        self.assertTrue(run.readers.spawn.procs[0].stopped)


class Endpoints(unittest.TestCase):
    def test_events_push_the_state(self):
        d = scratch_dir(self)
        run = Run(self, write_plan(d, MANUAL2), d / "state")
        conn = http.client.HTTPConnection("127.0.0.1", run.port, timeout=10)
        conn.request("GET", f"/api/events?t={run.server.token}")
        resp = conn.getresponse()
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")

        def next_event():
            while True:
                line = resp.fp.readline().decode()
                if line.startswith("data: "):
                    return json.loads(line[6:])

        first = next_event()
        self.assertEqual(first["current"], "a")
        run.post("/api/answer", {"id": "a", "answer": "works", "note": ""})
        second = next_event()
        self.assertGreater(second["version"], first["version"])
        self.assertEqual(second["current"], "b")
        conn.close()

    def test_refusals(self):
        d = scratch_dir(self)
        run = Run(self, write_plan(d, WITH_HIB), d / "state")
        resp, _ = run.request("GET", "/", headers={"Host": f"evil.example:{run.port}"})
        self.assertEqual(resp.status, 403)
        resp, _ = run.request("POST", "/api/answer", {"id": "a", "answer": "works"},
                              headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status, 403)
        resp, _ = run.request("POST", "/api/answer", '{"id": "a", "answer": "works"}',
                              headers={"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415)
        resp, _ = run.request("POST", "/api/answer", "{nope")
        self.assertEqual(resp.status, 400)
        resp, _ = run.request("POST", "/api/answer", "[1]")
        self.assertEqual(resp.status, 400)
        resp, _ = run.request("POST", "/api/answer", "x" * 70000)
        self.assertEqual(resp.status, 413)
        resp, _ = run.request("GET", "/etc/passwd")
        self.assertEqual(resp.status, 404)
        resp, _ = run.request("GET", "/../hwchecklib/cli.py")
        self.assertEqual(resp.status, 404)
        status, body = run.post("/api/answer", {"id": "hibernate", "answer": "works"})
        self.assertEqual(status, 409)
        self.assertIn("isn't the one on screen", body["error"])
        status, body = run.post("/api/select", {"id": "hibernate"})
        self.assertEqual(status, 409)
        status, body = run.post("/api/hibernate", {})
        self.assertEqual(status, 409)
        status, body = run.post("/api/answer", {"id": "a", "answer": "great"})
        self.assertEqual(status, 400)
        self.assertEqual(run.get()["counts"]["left"], 2)  # nothing refused was recorded
        # a same-origin request from the page itself is fine
        resp, _ = run.request("POST", "/api/answer", {"id": "a", "answer": "works"},
                              headers={"Origin": f"http://127.0.0.1:{run.port}"})
        self.assertEqual(resp.status, 200)
        run.session.stop()
        self.assertEqual(run.wait(), 143)


class Token(unittest.TestCase):
    def test_every_api_request_needs_this_runs_token(self):
        d = scratch_dir(self)
        run = Run(self, write_plan(d, WITH_HIB), d / "state")
        wrong = {"X-Hwcheck-Token": "not-the-token"}
        for path in ("/api/state", "/api/events"):
            for kw in ({"token": False}, {"token": False, "headers": wrong}):
                resp, raw = run.request("GET", path, **kw)
                self.assertEqual(resp.status, 403, (path, kw))
                self.assertIn("?t=", json.loads(raw)["error"])
        for path, body in (("/api/answer", {"id": "a", "answer": "works", "note": "x"}),
                           ("/api/select", {"id": "a"}), ("/api/hibernate", {})):
            resp, _ = run.request("POST", path, body, token=False)
            self.assertEqual(resp.status, 403, path)
            resp, _ = run.request("POST", path, body, token=False, headers=wrong)
            self.assertEqual(resp.status, 403, path)
        resp, _ = run.request("GET", f"/api/state?t={run.server.token}", token=False)
        self.assertEqual(resp.status, 403)  # in the query only the event stream may carry it
        self.assertEqual(run.get()["counts"]["left"], 2)  # nothing was recorded
        # the page itself carries no secrets and loads without it; it reads the token from its url
        resp, _ = run.request("GET", "/", token=False)
        self.assertEqual(resp.status, 200)
        resp, _ = run.request("GET", "/app.js", token=False)
        self.assertEqual(resp.status, 200)
        # with the token in the event stream's query
        conn = http.client.HTTPConnection("127.0.0.1", run.port, timeout=10)
        conn.request("GET", f"/api/events?t={run.server.token}")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()

    def test_a_private_page_hands_the_address_to_a_browser_and_goes_with_the_run(self):
        d = scratch_dir(self)
        run = Run(self, write_plan(d, MANUAL2), d / "state")
        handoff = d / "state" / "open.html"
        self.assertEqual(oct(handoff.stat().st_mode & 0o777), "0o600")
        page = handoff.read_text(encoding="utf-8")
        self.assertIn(f'content="0;url={run.server.url}"', page)
        self.assertIn(f"file://{handoff}", run.err.getvalue())
        run.session.stop()
        run.wait()
        self.assertFalse(handoff.exists())

    def test_each_run_has_its_own_token(self):
        d = scratch_dir(self)
        plan = write_plan(d, MANUAL2)
        first = Run(self, plan, d / "a")
        second = Run(self, plan, d / "b")
        self.assertNotEqual(first.server.token, second.server.token)


class HibernateOverHttp(unittest.TestCase):
    def test_answering_in_the_delay_means_it_never_hibernates(self):
        d = scratch_dir(self)
        run = Run(self, write_plan(d, WITH_HIB), d / "state", hibernate_delay=0.5)
        run.post("/api/answer", {"id": "a", "answer": "works"})
        status, s = run.post("/api/hibernate", {})
        self.assertEqual((status, s["hibernate"]["phase"]), (200, "pending"))
        status, _ = run.post("/api/answer", {"id": "hibernate", "answer": "skip", "note": "changed my mind"})
        self.assertEqual(status, 200)
        self.assertEqual(run.wait(), 0)
        time.sleep(0.7)  # past the delay: the action must not have run
        self.assertEqual(run.hibernate.calls, 0)


class CommandLine(unittest.TestCase):
    def test_timeout_stops_and_keeps_what_was_answered(self):
        d = scratch_dir(self)
        run = Run(self, write_plan(d, MANUAL2), d / "state", args=["--timeout", "0.5"])
        run.post("/api/answer", {"id": "a", "answer": "works", "note": "fine"})
        self.assertEqual(run.wait(), 3)
        self.assertIn("--resume", run.err.getvalue())
        results = read_results(d / "state")
        self.assertEqual([c["answer"] for c in results["checks"]], ["works", None])
        self.assertIsNone(results["finished"])

    def test_an_unfinished_run_needs_resume_or_new(self):
        d = scratch_dir(self)
        plan = write_plan(d, MANUAL2)
        state_dir = d / "state"
        self.assertEqual(Run(self, plan, state_dir, args=["--timeout", "0.1"]).wait(), 3)
        again = Run(self, plan, state_dir)
        self.assertEqual(again.wait(), 2)
        self.assertIn("use --resume to carry on or --new to start over", again.err.getvalue())
        fresh = Run(self, plan, state_dir, args=["--new", "--timeout", "0.1"])
        self.assertEqual(fresh.wait(), 3)
        self.assertEqual(len(list(state_dir.glob("state-*.json"))), 1)
        self.assertEqual(len(list(state_dir.glob("results-*.json"))), 1)

    def test_resume_with_nothing_saved_or_a_finished_run(self):
        d = scratch_dir(self)
        plan = write_plan(d, {"checks": [{"id": "a", "title": "a", "kind": "manual"}]})
        self.assertEqual(Run(self, plan, d / "state", args=["--resume"]).wait(), 2)
        run = Run(self, plan, d / "state")
        run.post("/api/answer", {"id": "a", "answer": "works"})
        self.assertEqual(run.wait(), 0)
        again = Run(self, plan, d / "state", args=["--resume"])
        self.assertEqual(again.wait(), 0)
        self.assertIn("finished", again.err.getvalue())
        # a finished run is kept aside when the next one starts
        nxt = Run(self, plan, d / "state", args=["--timeout", "0.1"])
        self.assertEqual(nxt.wait(), 3)
        self.assertEqual(len(list((d / "state").glob("results-*.json"))), 1)

    def test_a_busy_port(self):
        d = scratch_dir(self)
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            err = io.StringIO()
            code = main(["--plan", str(write_plan(d, MANUAL2)), "--port", str(port),
                         "--state-dir", str(d / "state")], readers=fake_readers(), err=err, out=io.StringIO())
        self.assertEqual(code, 1)
        self.assertIn("can't listen", err.getvalue())

    def test_bad_plan_from_the_real_command(self):
        d = scratch_dir(self)
        bad = write_plan(d, {"checks": [{"id": "a", "title": "a", "kind": "smell"},
                                        {"id": "hibernate", "title": "h", "kind": "hibernate"},
                                        {"id": "b", "title": "b", "kind": "manual"}]})
        r = subprocess.run([sys.executable, str(ROOT / "hwcheck.py"), "--plan", str(bad),
                            "--state-dir", str(d / "state")], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("bad plan", r.stderr)
        self.assertIn("unknown kind \"smell\"", r.stderr)
        self.assertIn("hibernate check must be the last", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertFalse((d / "state").exists())
        r = subprocess.run([sys.executable, str(ROOT / "hwcheck.py"), "--version"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "hwcheck " + (ROOT / "VERSION").read_text().strip())


class ResumeFromTheCommandLine(unittest.TestCase):
    def go_down_while_hibernating(self, d):
        """answer up to hibernate, press it, and let the 'machine' go down with the server."""
        plan = write_plan(d, WITH_HIB)
        readers = fake_readers()
        holder = {}
        hibernate = FakeHibernate(then=lambda: holder["run"].session.stop())
        run = Run(self, plan, d / "state", readers=readers, hibernate=hibernate)
        holder["run"] = run
        run.post("/api/answer", {"id": "a", "answer": "works", "note": "fine"})
        status, s = run.post("/api/hibernate", {})
        self.assertEqual(status, 200)
        self.assertEqual(run.wait(), 143)
        wait_for(lambda: run.session.state["hibernate"]["phase"] == "hibernating")
        saved = json.loads((d / "state" / "state.json").read_text())
        self.assertIn(saved["hibernate"]["phase"], ("pending", "hibernating"))
        return plan, readers, run.clock

    def test_same_boot_resume(self):
        d = scratch_dir(self)
        plan, readers, clock = self.go_down_while_hibernating(d)
        clock.value += 55  # the machine hibernated and came back in the same boot
        run = Run(self, plan, d / "state", readers=readers, args=["--resume"], clock=clock)
        state = run.get()
        self.assertEqual((state["current"], state["hibernate"]["phase"]), ("hibernate", "resumed"))
        resp, _ = run.request("GET", "/")
        self.assertEqual(resp.status, 200)
        status, _ = run.post("/api/answer", {"id": "hibernate", "answer": "works", "note": "all came back"})
        self.assertEqual(status, 200)
        self.assertEqual(run.wait(), 0)
        got = {c["id"]: c for c in read_results(d / "state")["checks"]}
        self.assertEqual((got["a"]["answer"], got["a"]["note"]), ("works", "fine"))
        hib = got["hibernate"]
        self.assertEqual((hib["answer"], hib["note"]), ("works", "all came back"))
        self.assertTrue(hib["live"]["resumed"])
        self.assertEqual(hib["live"]["slept_seconds"], 55)
        self.assertEqual(hib["live"]["boot_id_before"], hib["live"]["boot_id_after"])

    def test_same_boot_without_a_sleep_is_not_a_resume(self):
        d = scratch_dir(self)
        plan, readers, clock = self.go_down_while_hibernating(d)
        run = Run(self, plan, d / "state", readers=readers, args=["--resume"], clock=clock)
        state = run.get()
        self.assertEqual(state["hibernate"]["phase"], "failed")
        self.assertIn("didn't sleep", state["hibernate"]["error"])
        status, _ = run.post("/api/answer", {"id": "hibernate", "answer": "works"})
        self.assertEqual(status, 409)
        status, s = run.post("/api/hibernate", {})  # try again
        self.assertEqual((status, s["hibernate"]["phase"]), (200, "pending"))

    def test_fresh_boot_resume_after_a_failed_hibernate(self):
        d = scratch_dir(self)
        plan, readers, _ = self.go_down_while_hibernating(d)
        readers.files["/proc/sys/kernel/random/boot_id"] = BOOT_B + "\n"
        run = Run(self, plan, d / "state", readers=readers, args=["--resume"])
        state = run.get()
        self.assertEqual((state["current"], state["hibernate"]["phase"]), ("hibernate", "fresh-boot"))
        hib = {c["id"]: c for c in read_results(d / "state")["checks"]}["hibernate"]
        self.assertEqual(hib["answer"], "doesnt-work")  # recorded before the person says anything
        status, body = run.post("/api/answer", {"id": "hibernate", "answer": "works"})
        self.assertEqual(status, 409)
        self.assertIn("booted fresh", body["error"])
        status, _ = run.post("/api/answer", {"id": "hibernate", "answer": "doesnt-work",
                                             "note": "powered off, came back at the login screen"})
        self.assertEqual(status, 200)
        self.assertEqual(run.wait(), 0)
        results = read_results(d / "state")
        hib = {c["id"]: c for c in results["checks"]}["hibernate"]
        self.assertEqual((hib["answer"], hib["note"]),
                         ("doesnt-work", "powered off, came back at the login screen"))
        self.assertEqual((hib["live"]["resumed"], hib["live"]["boot_id_after"]), (False, BOOT_B))
        self.assertEqual(results["boot_id"], BOOT_B)


if __name__ == "__main__":
    unittest.main()


class Detection(unittest.TestCase):
    def test_detect_is_the_default_and_its_plan_is_served(self):
        d = scratch_dir(self)
        run = Run(self, None, d / "state", detector=FakeMachine("machine-desktop.json").detector())
        err = run.err.getvalue()
        self.assertIn("detected 6 checks", err)
        self.assertIn("hibernate (skip: no disk swap", err)
        state = run.get()
        self.assertEqual([c["id"] for c in state["checks"]],
                         ["speakers", "microphone", "headphones", "keys", "external-display", "hibernate"])
        keys = next(c for c in state["checks"] if c["id"] == "keys")
        self.assertEqual(keys["device"]["name"], "Logitech USB Receiver")  # the page shows what it's for
        self.assertEqual(state["counts"]["skip"], 1)  # hibernate, skipped with its reason from the start
        for cid in ("speakers", "microphone", "headphones", "keys", "external-display"):
            status, _ = run.post("/api/answer", {"id": cid, "answer": "works", "note": ""})
            self.assertEqual(status, 200)
        self.assertEqual(run.wait(), 0)
        results = read_results(d / "state")
        self.assertEqual(results["plan"], "detected")
        got = {c["id"]: c for c in results["checks"]}
        self.assertEqual(got["external-display"]["device"]["name"], "ports: DP-1, DP-2, DVI-D-1, HDMI-A-1")
        self.assertEqual(got["speakers"]["device"]["path"], "/dev/snd/pcmC0D0p")
        self.assertEqual(got["hibernate"]["answer"], "skip")
        self.assertIn("no disk swap", got["hibernate"]["note"])

    def test_print_plan_prints_and_serves_nothing(self):
        d = scratch_dir(self)
        out, err = io.StringIO(), io.StringIO()
        code = main(["--detect", "--print-plan", "--expect", str(ROOT / "examples" / "expect-2in1.json"),
                     "--state-dir", str(d / "state")],
                    detector=FakeMachine("machine-2in1-no-pen.json").detector(), out=out, err=err)
        self.assertEqual(code, 0)
        plan = load_plan_text(out.getvalue())
        ids = [c["id"] for c in plan["checks"]]
        self.assertIn("missing-pen", ids)
        self.assertNotIn("pen", ids)
        self.assertFalse((d / "state").exists())
        # the printed plan is a plan: it runs as --plan, presets and devices included
        path = d / "detected.json"
        path.write_text(out.getvalue(), encoding="utf-8")
        run = Run(self, path, d / "state")
        miss = next(c for c in run.get()["checks"] if c["id"] == "missing-pen")
        self.assertEqual((miss["status"], miss["preset"]), ("doesnt-work", True))

    def test_usage_refusals(self):
        d = scratch_dir(self)
        plan = write_plan(d, MANUAL2)
        bad_expect = d / "expect.json"
        bad_expect.write_text('[{"kind": "smell", "match": "x"}]', encoding="utf-8")
        machine = FakeMachine("machine-desktop.json")
        for argv, fragment in (
                (["--plan", str(plan), "--expect", str(bad_expect)], "go with detection"),
                (["--plan", str(plan), "--print-plan"], "go with detection"),
                (["--resume", "--detect"], "doesn't detect again"),
                (["--resume", "--expect", str(bad_expect)], "doesn't detect again"),
                (["--print-plan", "--new"], "only prints"),
                (["--expect", str(bad_expect)], "unknown kind")):
            err = io.StringIO()
            code = main([*argv, "--state-dir", str(d / "state")], detector=machine.detector(),
                        out=io.StringIO(), err=err)
            self.assertEqual(code, 2, argv)
            self.assertIn(fragment, err.getvalue(), argv)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(["--plan", str(plan), "--detect"], out=io.StringIO(), err=io.StringIO())  # one or the other

    def test_resume_uses_the_saved_plan_without_detecting_again(self):
        d = scratch_dir(self)
        first = Run(self, None, d / "state", args=["--timeout", "0.3"],
                    detector=FakeMachine("machine-desktop.json").detector())
        first.post("/api/answer", {"id": "speakers", "answer": "works", "note": ""})
        self.assertEqual(first.wait(), 3)

        class NoDetecting:
            def detect(self):
                raise AssertionError("--resume must not detect")

        run = Run(self, None, d / "state", args=["--resume"], detector=NoDetecting())
        state = run.get()
        self.assertEqual(state["current"], "microphone")
        self.assertEqual(state["checks"][0]["device"]["name"], "card 0 PCH: ALC897 Analog")
        for cid in ("microphone", "headphones", "keys", "external-display"):
            run.post("/api/answer", {"id": cid, "answer": "skip", "note": ""})
        self.assertEqual(run.wait(), 0)
        self.assertEqual(read_results(d / "state")["plan"], "detected")


def load_plan_text(text):
    from hwchecklib.plan import parse_plan
    return parse_plan(json.loads(text))


@unittest.skipUnless(threading.current_thread() is threading.main_thread(), "signals need the main thread")
class Signals(unittest.TestCase):
    def test_sighup_and_sigterm_stop_it_cleanly_and_remove_the_handoff(self):
        for sig, code in ((signal.SIGHUP, 129), (signal.SIGTERM, 143)):
            with self.subTest(signal=sig.name):
                d = scratch_dir(self)
                seen = {}

                def on_ready(server, session, sig=sig, d=d):
                    seen["handoff"] = (d / "state" / "open.html").exists()
                    threading.Timer(0.2, os.kill, (os.getpid(), sig)).start()

                # a safety net: if hwcheck failed to take the signal, it must not end the test run
                safety = lambda *_: seen.setdefault("missed", True)  # noqa: E731
                before = signal.signal(sig, safety)
                try:
                    err = io.StringIO()
                    got = main(["--plan", str(write_plan(d, MANUAL2)), "--port", "0", "--state-dir",
                                str(d / "state"), "--timeout", "10"],
                               readers=fake_readers(), hibernate=FakeHibernate(), out=io.StringIO(), err=err,
                               on_ready=on_ready, grace=0.05, poll_interval=0.02)
                    self.assertIs(signal.getsignal(sig), safety)  # its handler was put back
                finally:
                    signal.signal(sig, before)
                self.assertEqual(got, code)
                self.assertTrue(seen["handoff"])
                self.assertNotIn("missed", seen)
                self.assertFalse((d / "state" / "open.html").exists())
                self.assertIn("--resume", err.getvalue())
                self.assertEqual([c["answer"] for c in read_results(d / "state")["checks"]], [None, None])

    def test_a_stale_handoff_is_removed_at_start(self):
        d = scratch_dir(self)
        (d / "state").mkdir()
        stale = d / "state" / "open.html"
        stale.write_text("<meta http-equiv=refresh content='0;url=http://127.0.0.1:8770/?t=old'>", encoding="utf-8")
        code = main(["--plan", str(write_plan(d, MANUAL2)), "--resume", "--state-dir", str(d / "state")],
                    readers=fake_readers(), out=io.StringIO(), err=io.StringIO())
        self.assertEqual(code, 2)  # nothing to resume, but the dead secret is gone
        self.assertFalse(stale.exists())
