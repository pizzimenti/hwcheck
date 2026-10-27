"""browser-free tests: drive hwcheck's http endpoints the way the page does."""

import http.client
import io
import json
import re
import socket
import subprocess
import sys
import threading
import unittest

from hwchecklib.cli import main
from hwchecklib.plan import load_plan

from .fakes import (BOOT_B, EXAMPLE_PLAN, ROOT, FakeClock, FakeHibernate, fake_readers, scratch_dir,
                    wait_for)

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

    def __init__(self, test, plan_path, state_dir, readers=None, hibernate=None, args=(), clock=None):
        self.readers = readers or fake_readers()
        self.hibernate = hibernate or FakeHibernate()
        self.clock = clock or FakeClock(0.0)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.server = self.session = self.code = None
        self.ready = threading.Event()
        argv = ["--plan", str(plan_path), "--port", "0", "--state-dir", str(state_dir), *args]
        self.thread = threading.Thread(target=self._main, args=(argv,), daemon=True)
        self.thread.start()
        wait_for(lambda: self.ready.is_set() or not self.thread.is_alive(), timeout=10)
        test.addCleanup(self.close)

    def _main(self, argv):
        def on_ready(server, session):
            self.server, self.session = server, session
            self.ready.set()

        self.code = main(argv, readers=self.readers, hibernate=self.hibernate, out=self.out, err=self.err,
                         on_ready=on_ready, grace=0.05, poll_interval=0.02, hibernate_delay=0,
                         session_options={"sleep_clock": self.clock})

    @property
    def port(self):
        return self.server.server_address[1]

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {}
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
        self.assertEqual(url, f"http://127.0.0.1:{run.port}/")

        # the page and its files: served locally, nothing fetched from elsewhere
        resp, body = run.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/html"))
        self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))
        self.assertIn(b'<script src="/app.js"', body)
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
        conn.request("GET", "/api/events")
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
        return plan, readers

    def test_same_boot_resume(self):
        d = scratch_dir(self)
        plan, readers = self.go_down_while_hibernating(d)
        run = Run(self, plan, d / "state", readers=readers, args=["--resume"])
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
        self.assertEqual(hib["live"]["boot_id_before"], hib["live"]["boot_id_after"])

    def test_fresh_boot_resume_after_a_failed_hibernate(self):
        d = scratch_dir(self)
        plan, readers = self.go_down_while_hibernating(d)
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
