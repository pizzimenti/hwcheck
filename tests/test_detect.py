"""detection on recorded machines: a 2-in-1, a desktop, and the 2-in-1 with its pen missing."""

import json
import unittest

from hwchecklib.detect import (ExpectError, build_plan, load_expect, matches, parse_asound_cards,
                               parse_asound_pcm, parse_swaps, summary)
from hwchecklib.plan import parse_plan
from hwchecklib.session import Session
from hwchecklib.store import Store

from .fakes import ROOT, FakeMachine, fake_readers, scratch_dir

EXPECT_2IN1 = ROOT / "examples" / "expect-2in1.json"


def plan_for(machine, expect=None):
    detected = machine.detector().detect()
    return detected, parse_plan(build_plan(detected, expect))


def by_id(plan):
    return {c["id"]: c for c in plan["checks"]}


class TwoInOne(unittest.TestCase):
    def setUp(self):
        self.detected, self.plan = plan_for(FakeMachine("machine-2in1.json"))
        self.checks = by_id(self.plan)

    def test_a_check_for_everything_it_has_in_a_followable_order(self):
        self.assertEqual(list(self.checks), [
            "speakers", "microphone", "headphones", "camera-1", "camera-2", "touch", "pen", "keys",
            "touchpad", "fingerprint", "tablet-mode", "rotation", "power", "lid", "external-display",
            "bluetooth", "wifi", "hibernate"])
        self.assertEqual(self.detected["notes"], [])

    def test_each_check_carries_its_device(self):
        c = self.checks
        self.assertEqual((c["pen"]["device"]["name"], c["pen"]["device"]["path"]),
                         ("Wacom Pen and multitouch sensor Pen", "/dev/input/event16"))
        self.assertIn("i2c-WACF2200:00", c["pen"]["device"]["ids"])
        self.assertEqual(c["touch"]["device"]["name"], "Wacom Pen and multitouch sensor Finger")
        self.assertEqual(c["touchpad"]["device"]["name"], "SYNA8018:00 06CB:CEB3 Touchpad")
        self.assertEqual(c["keys"]["device"]["name"], "AT Translated Set 2 keyboard")
        self.assertEqual(c["headphones"]["device"]["name"], "sof-hda-dsp Headphone")
        self.assertEqual(c["tablet-mode"]["device"]["name"], "Intel HID switches")
        self.assertEqual(c["rotation"]["device"]["name"], "accel_3d")
        self.assertEqual(c["fingerprint"]["device"],
                         {"name": "Synaptics Sensors", "path": "/net/reactivated/Fprint/Device/0",
                          "ids": ["Synaptics Sensors", "/net/reactivated/Fprint/Device/0"]})
        self.assertEqual(c["lid"]["device"]["path"], "/proc/acpi/button/lid/LID")
        self.assertEqual(c["power"]["device"]["name"], "BAT0 (SMP 5B10W13975)")  # not the mouse's battery
        self.assertEqual(c["bluetooth"]["device"]["name"], "hci0")
        self.assertIn("8087:0033", c["bluetooth"]["device"]["ids"])
        self.assertEqual(c["wifi"]["device"]["name"], "wlan0 (iwlwifi)")
        self.assertEqual(c["hibernate"]["device"]["path"], "/dev/nvme0n1p3")
        self.assertIsNone(c["hibernate"]["preset"])

    def test_one_check_per_camera_not_per_video_node(self):
        cams = [c for c in self.plan["checks"] if c["kind"] == "camera"]
        self.assertEqual([(c["title"], c["device"]["path"]) for c in cams],
                         [("camera 1: Integrated Camera: Integrated C", "/dev/video0"),
                          ("camera 2: Integrated Camera: Integrated I", "/dev/video2")])
        self.assertIn("04f2:b6ea", cams[0]["device"]["ids"])

    def test_audio_skips_hdmi_and_prefers_the_built_in_microphone(self):
        self.assertEqual(self.checks["speakers"]["device"]["name"], "card 0 sofhdadsp: HDA Analog")
        self.assertEqual(self.checks["microphone"]["device"]["name"], "card 0 sofhdadsp: DMIC Raw")
        names = [d["name"] for d in self.detected["devices"]["speakers"]]
        self.assertFalse(any("HDMI" in n for n in names))

    def test_external_display_lists_the_ports_but_not_the_panel(self):
        dev = self.checks["external-display"]["device"]
        self.assertEqual(dev["name"], "ports: DP-1, DP-2, HDMI-A-1")
        self.assertNotIn("eDP-1", " ".join(dev["ids"]))

    def test_every_expected_device_is_found(self):
        _, plan = plan_for(FakeMachine("machine-2in1.json"), load_expect(EXPECT_2IN1))
        self.assertFalse([c for c in plan["checks"] if c["id"].startswith("missing-")])

    def test_either_tablet_signal_brings_both_checks(self):
        m = FakeMachine("machine-2in1.json").remove("/sys/bus/iio/devices")  # no accelerometer driver
        _, plan = plan_for(m)
        c = by_id(plan)
        self.assertEqual(c["tablet-mode"]["device"]["name"], "Intel HID switches")
        self.assertIsNone(c["rotation"]["device"])  # checked anyway, so the missing sensor shows


class Desktop(unittest.TestCase):
    def test_only_what_a_desktop_has(self):
        detected, plan = plan_for(FakeMachine("machine-desktop.json"))
        c = by_id(plan)
        self.assertEqual(list(c), ["speakers", "microphone", "headphones", "keys", "external-display", "hibernate"])
        self.assertEqual(c["speakers"]["device"]["name"], "card 0 PCH: ALC897 Analog")
        self.assertEqual(c["keys"]["device"]["name"], "Logitech USB Receiver")
        self.assertEqual(c["external-display"]["device"]["name"], "ports: DP-1, DP-2, DVI-D-1, HDMI-A-1")
        self.assertEqual(c["hibernate"]["preset"]["answer"], "skip")
        self.assertIn("no disk swap", c["hibernate"]["preset"]["note"])
        for absent in ("touch", "pen", "camera", "power", "lid", "bluetooth", "wifi", "fingerprint", "rotation"):
            self.assertEqual(detected["devices"][absent], [], absent)

    def test_headphones_from_pactl_when_no_jack_switch(self):
        m = FakeMachine("machine-desktop.json")
        m.files["/proc/bus/input/devices"] = m.files["/proc/bus/input/devices"].replace(
            "Front Headphone", "Front Speaker").replace("B: SW=4\n", "B: SW=0\n")
        _, plan = plan_for(m)
        self.assertNotIn("headphones", by_id(plan))  # no switch and no pactl: not detected
        m.commands["pactl list sinks"] = (
            "Sink #49\n\tName: alsa_output.pci-0000_00_1f.3.analog-stereo\n\tPorts:\n"
            "\t\tanalog-output-lineout: Line Out (type: Line, priority: 9000, availability unknown)\n"
            "\t\tanalog-output-headphones: Headphones (type: Headphones, priority: 9900, not available)\n"
            "\tActive Port: analog-output-lineout\n")
        _, plan = plan_for(m)
        self.assertEqual(by_id(plan)["headphones"]["device"]["name"], "Headphones")

    def test_not_enough_swap(self):
        m = FakeMachine("machine-desktop.json")
        m.files["/proc/swaps"] += "/swap/swapfile                          file\t\t4194300\t\t0\t\t-2\n"
        _, plan = plan_for(m)
        self.assertEqual(by_id(plan)["hibernate"]["preset"]["note"],
                         "not enough swap to hibernate: 4.0 GiB of disk swap for 31.2 GiB of memory")

    def test_unreadable_memory_lets_the_person_try(self):
        m = FakeMachine("machine-desktop.json").remove("/proc/meminfo")
        detected, plan = plan_for(m)
        self.assertIsNone(by_id(plan)["hibernate"]["preset"])
        self.assertIn("can't read /proc/meminfo", " ".join(detected["notes"]))

    def test_nothing_readable_still_gives_keys_display_and_hibernate(self):
        m = FakeMachine("machine-desktop.json")
        m.files, m.links, m.commands = {}, {}, {}
        detected, plan = plan_for(m)
        self.assertEqual(list(by_id(plan)), ["keys", "external-display", "hibernate"])
        self.assertIsNone(by_id(plan)["keys"]["device"])
        self.assertIn("can't read /proc/bus/input/devices", " ".join(detected["notes"]))


class MissingPen(unittest.TestCase):
    def test_an_expected_pen_not_detected_becomes_a_failing_check(self):
        detected, plan = plan_for(FakeMachine("machine-2in1-no-pen.json"), load_expect(EXPECT_2IN1))
        c = by_id(plan)
        self.assertNotIn("pen", c)
        miss = c["missing-pen"]
        self.assertEqual(miss["title"], "expected but not detected: pen WACF2200")
        self.assertEqual(miss["preset"], {"answer": "doesnt-work", "note": "expected but not detected: pen WACF2200"})
        self.assertEqual(plan["checks"][-1]["id"], "hibernate")  # still last
        self.assertIn("missing-pen (doesnt-work: expected but not detected: pen WACF2200)",
                      summary(detected, plan))

    def test_the_failing_check_is_in_the_results_from_the_start(self):
        _, plan = plan_for(FakeMachine("machine-2in1-no-pen.json"), load_expect(EXPECT_2IN1))
        d = scratch_dir(self)
        store = Store(d / "state")
        s = Session.new(plan, "detected", store, fake_readers())
        results = json.loads(store.results_path.read_text())
        got = {c["id"]: c for c in results["checks"]}
        self.assertEqual((got["missing-pen"]["answer"], got["missing-pen"]["note"]),
                         ("doesnt-work", "expected but not detected: pen WACF2200"))
        self.assertEqual(got["missing-pen"]["live"], {"source": "preset"})
        self.assertEqual(got["touch"]["device"]["name"], "Wacom Pen and multitouch sensor Finger")
        self.assertEqual(s.snapshot()["current"], "speakers")  # the person starts on the first real check

    def test_two_missing_of_a_kind_get_their_own_ids(self):
        m = FakeMachine("machine-desktop.json")
        _, plan = plan_for(m, [{"kind": "camera", "match": "front"}, {"kind": "camera", "match": "rear"}])
        self.assertEqual([c["id"] for c in plan["checks"] if c["id"].startswith("missing-")],
                         ["missing-camera", "missing-camera-2"])


class ExpectFile(unittest.TestCase):
    def write(self, obj):
        path = scratch_dir(self) / "expect.json"
        path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
        return path

    def test_aliases_and_patterns(self):
        got = load_expect(self.write([{"kind": "battery", "match": "BAT\\d"}, {"kind": "stylus", "match": "wacf"}]))
        self.assertEqual(got, [{"kind": "power", "match": "BAT\\d"}, {"kind": "pen", "match": "wacf"}])
        dev = {"name": "Wacom Pen", "path": "/dev/input/event16", "ids": ["i2c-WACF2200:00"]}
        self.assertTrue(matches(dev, "wacf2200"))  # case-insensitive, over the ids too
        self.assertFalse(matches(dev, "ELAN"))

    def test_refusals(self):
        for obj, fragment in (({"kind": "pen"}, "json list"),
                              ([{"kind": "smell", "match": "x"}], "unknown kind"),
                              ([{"kind": "pen", "match": ""}], "non-empty"),
                              ([{"kind": "pen", "match": "("}], "valid pattern"),
                              ([{"kind": "pen", "match": "x", "extra": 1}], "only"),
                              ("[{", "not valid json")):
            with self.assertRaises(ExpectError) as ctx:
                load_expect(self.write(obj))
            self.assertIn(fragment, str(ctx.exception))
        with self.assertRaises(ExpectError):
            load_expect(scratch_dir(self) / "absent.json")


class Parsers(unittest.TestCase):
    def test_asound(self):
        m = FakeMachine("machine-2in1.json")
        cards = parse_asound_cards(m.files["/proc/asound/cards"])
        self.assertEqual(cards[0]["longname"], "LENOVO-21CD0019US-ThinkPadX1YogaGen7-21CD0019US")
        pcms = parse_asound_pcm(m.files["/proc/asound/pcm"])
        self.assertEqual(pcms[0], {"card": 0, "device": 0, "id": "HDA Analog", "name": "",
                                   "playback": True, "capture": True})

    def test_swaps(self):
        self.assertEqual(parse_swaps(FakeMachine("machine-2in1.json").files["/proc/swaps"]),
                         [{"path": "/dev/nvme0n1p3", "type": "partition", "kib": 33554428},
                          {"path": "/dev/zram0", "type": "partition", "kib": 8388604}])


if __name__ == "__main__":
    unittest.main()
