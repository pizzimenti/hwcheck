import sys
import unittest

from hwchecklib import readers as R
from hwchecklib.readers import (PtyProcess, Result, StreamWatch, ToolMissing, filter_kernel_log,
                                parse_bt_devices, parse_bt_show, parse_input_devices, parse_kscreen,
                                parse_monitor_sensor, parse_upower, run_command)

from .fakes import BOOT_A, FakeClock, FakeSpawn, default_table, fake_readers, fixture, wait_for


class MonitorSensor(unittest.TestCase):
    def test_parse_initial_state(self):
        p = parse_monitor_sensor(fixture("monitor-sensor-normal.txt"))
        self.assertEqual(p, {"proxy": True, "accelerometer": True, "orientation": "normal", "tilt": "vertical"})

    def test_parse_changes_keep_the_latest(self):
        text = fixture("monitor-sensor-normal.txt") + fixture("monitor-sensor-rotated.txt")
        p = parse_monitor_sensor(text)
        self.assertEqual((p["orientation"], p["tilt"]), ("bottom-up", "tilted-down"))

    def test_parse_old_format_and_no_accelerometer(self):
        self.assertEqual(parse_monitor_sensor(fixture("monitor-sensor-old.txt"))["orientation"], "right-up")
        p = parse_monitor_sensor(fixture("monitor-sensor-none.txt"))
        self.assertIs(p["accelerometer"], False)
        p = parse_monitor_sensor(fixture("monitor-sensor-waiting.txt"))
        self.assertFalse(p["proxy"])

    def test_vanished_proxy_forgets_orientation(self):
        text = fixture("monitor-sensor-normal.txt") + "--- iio-sensor-proxy vanished, waiting for it to appear\n"
        self.assertIsNone(parse_monitor_sensor(text)["orientation"])

    def test_reader_follows_rotation_and_stops_the_tool(self):
        spawn = FakeSpawn(fixture("monitor-sensor-normal.txt"))
        rd = fake_readers(spawn=spawn)
        r = wait_for(lambda: (x := rd.rotation())["state"]["orientation"] and x)
        self.assertTrue(r["read"])
        self.assertEqual(r["summary"], "orientation: normal")
        self.assertEqual(spawn.argvs, [["monitor-sensor", "--accel"]])
        spawn.procs[0].push(fixture("monitor-sensor-rotated.txt"))
        wait_for(lambda: rd.rotation()["summary"] == "orientation: bottom-up")
        self.assertEqual(len(spawn.procs), 1)  # one tool kept running, not one per read
        self.assertEqual(rd.watching(), ["rotation"])
        rd.release()
        self.assertTrue(spawn.procs[0].stopped)
        self.assertTrue(spawn.procs[0].closed)
        self.assertEqual(rd.watching(), [])

    def test_reader_no_accelerometer(self):
        rd = fake_readers(spawn=FakeSpawn(fixture("monitor-sensor-none.txt")))
        r = wait_for(lambda: (x := rd.rotation())["summary"] != "waiting for the first reading" and x)
        self.assertTrue(r["read"])
        self.assertEqual(r["summary"], "no accelerometer")
        rd.release()

    def test_reader_missing_tool(self):
        rd = fake_readers(spawn=FakeSpawn(None))
        r = rd.rotation()
        self.assertFalse(r["read"])
        self.assertIn("monitor-sensor not found", r["error"])
        self.assertTrue(r["summary"].startswith("can't read"))

    def test_reader_proxy_not_running(self):
        clock = FakeClock(100.0)
        rd = fake_readers(spawn=FakeSpawn(fixture("monitor-sensor-waiting.txt")), monotonic=clock)
        self.assertEqual(rd.rotation()["summary"], "waiting for the first reading")
        clock.value += 2.5
        r = wait_for(lambda: (x := rd.rotation()) and not x["read"] and x)
        self.assertEqual(r["error"], "iio-sensor-proxy isn't running")
        rd.release()

    def test_reader_tool_ended(self):
        rd = fake_readers(spawn=FakeSpawn(fixture("monitor-sensor-claim-failed.txt"), end=True))
        r = wait_for(lambda: (x := rd.rotation()) and not x["read"] and x)
        self.assertIn("Failed to claim accelerometer", r["error"])
        rd.release()


class TabletMode(unittest.TestCase):
    def test_parse_switch_bitmaps(self):
        devs = {d["name"]: d for d in parse_input_devices(fixture("input-devices-2in1.txt"))}
        self.assertEqual(devs["Intel HID switches"]["event"], "event14")
        self.assertEqual(devs["Intel HID switches"]["bits"]["SW"], 2)
        self.assertEqual(devs["Lid Switch"]["bits"]["SW"], 1)
        self.assertEqual(devs["ThinkPad Extra Buttons"]["event"], "event9")

    def test_switch_state_read(self):
        rd = fake_readers(switches={"/dev/input/event14": 0b10})
        r = rd.tablet_mode()
        self.assertTrue(r["read"])
        self.assertEqual(r["summary"], "tablet mode: on")
        self.assertEqual(r["state"]["switches"], [{"name": "Intel HID switches", "event": "event14", "on": True}])
        rd = fake_readers(switches={"/dev/input/event14": 0})
        self.assertEqual(rd.tablet_mode()["summary"], "tablet mode: off")

    def test_switch_without_permission_falls_back_to_plasma(self):
        table = default_table()
        table["busctl"] = "b true\n"
        rd = fake_readers(table=table, switches={"/dev/input/event14": PermissionError(13, "Permission denied")})
        r = rd.tablet_mode()
        self.assertEqual(r["summary"], "tablet mode: on (plasma)")
        self.assertTrue(any("can't be read without access" in line for line in r["lines"]))

    def test_switch_present_state_unknown(self):
        rd = fake_readers(switches={"/dev/input/event14": PermissionError(13, "Permission denied")})
        r = rd.tablet_mode()
        self.assertTrue(r["read"])
        self.assertEqual(r["summary"], "tablet-mode switch present; state can't be read")

    def test_clamshell_has_no_switch(self):
        files = {"/proc/bus/input/devices": fixture("input-devices-clamshell.txt")}
        r = fake_readers(files=files).tablet_mode()
        self.assertEqual(r["summary"], "no tablet-mode switch found")

    def test_proc_unreadable(self):
        r = fake_readers(files={}).tablet_mode()
        self.assertFalse(r["read"])
        self.assertIn("/proc/bus/input/devices", r["error"])


class Upower(unittest.TestCase):
    def test_parse(self):
        info = parse_upower(fixture("upower-ac.txt"))
        self.assertEqual(info["daemon"]["lid-is-present"], "yes")
        types = [d["type"] for d in info["devices"]]
        self.assertEqual(types, ["line-power", "battery", "mouse", "battery"])

    def test_power_on_mains(self):
        r = fake_readers().power()
        self.assertTrue(r["read"])
        self.assertEqual(r["summary"], "on mains, BAT0 85% charging")
        self.assertEqual(r["state"]["batteries"], [{"name": "BAT0", "state": "charging", "percentage": 85}])
        self.assertIs(r["state"]["charger"], True)

    def test_power_on_battery(self):
        table = default_table()
        table["upower -d"] = fixture("upower-battery.txt")
        r = fake_readers(table=table).power()
        self.assertEqual(r["summary"], "on battery, BAT0 84% discharging")
        self.assertIs(r["state"]["charger"], False)

    def test_lid(self):
        self.assertEqual(fake_readers().lid()["summary"], "lid open")
        table = default_table()
        table["upower -d"] = fixture("upower-lid-closed.txt")
        r = fake_readers(table=table).lid()
        self.assertEqual(r["state"], {"present": True, "closed": True})

    def test_desktop_has_no_lid_or_battery(self):
        table = default_table()
        table["upower -d"] = fixture("upower-desktop.txt")
        rd = fake_readers(table=table)
        self.assertEqual(rd.lid()["summary"], "no lid reported")
        p = rd.power()
        self.assertEqual(p["summary"], "on mains")
        self.assertIn("no battery seen", p["lines"])

    def test_missing_slow_and_broken(self):
        r = fake_readers(table={}).power()
        self.assertEqual((r["read"], r["error"]), (False, "upower not found"))
        r = fake_readers(table={"upower -d": Result(None, "", True)}).lid()
        self.assertIn("didn't answer", r["error"])
        r = fake_readers(table={"upower -d": Result(1, "Failed to get daemon\n")}).lid()
        self.assertEqual(r["error"], "upower failed: Failed to get daemon")
        r = fake_readers(table={"upower -d": "Device: /x\n  native-path: x\n"}).lid()
        self.assertIn("no daemon section", r["error"])


class Kscreen(unittest.TestCase):
    def test_parse_plasma6_with_colour_codes(self):
        outs = parse_kscreen(fixture("kscreen-plasma6-external.txt"))
        self.assertEqual([o["name"] for o in outs], ["eDP-1", "HDMI-A-1", "DP-1"])
        edp, hdmi, dp = outs
        self.assertEqual((edp["type"], edp["builtin"], edp["mode"]), ("Panel", True, "2880x1800@120"))
        self.assertEqual((hdmi["connected"], hdmi["enabled"], hdmi["mode"]), (True, True, "1920x1080@60"))
        self.assertEqual(hdmi["geometry"], "1440,0 1920x1080")
        self.assertEqual((dp["connected"], dp["enabled"], dp["mode"]), (False, False, None))

    def test_parse_plasma5_one_line(self):
        outs = parse_kscreen(fixture("kscreen-plasma5.txt"))
        self.assertEqual([(o["name"], o["type"], o["mode"]) for o in outs],
                         [("eDP-1", "Panel", "1920x1080@60"), ("HDMI-1", "HDMI", "2560x1440@59.95")])

    def test_external_connected(self):
        r = fake_readers().external_display()
        self.assertEqual(r["summary"], "external display: HDMI-A-1 on, 1920x1080@60")
        self.assertEqual(r["state"]["external_connected"], 1)
        self.assertIn("DP-1 (external): not connected, off", r["lines"])

    def test_builtin_only(self):
        table = default_table()
        table["kscreen-doctor -o"] = fixture("kscreen-plasma6-builtin.txt")
        r = fake_readers(table=table).external_display()
        self.assertEqual(r["summary"], "no external display connected")

    def test_cant_read(self):
        r = fake_readers(table={}).external_display()
        self.assertEqual(r["error"], "kscreen-doctor not found")
        table = {"kscreen-doctor -o": Result(1, "kscreen-doctor: could not connect to the display server\n")}
        r = fake_readers(table=table).external_display()
        self.assertFalse(r["read"])
        self.assertIn("could not connect", r["error"])


class Bluetooth(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(parse_bt_show(fixture("bluetoothctl-show.txt")), {"name": "testbook", "powered": True})
        self.assertIsNone(parse_bt_show(fixture("bluetoothctl-no-controller.txt")))
        self.assertEqual(parse_bt_devices(fixture("bluetoothctl-connected.txt")),
                         [{"address": "38:18:4C:00:00:02", "name": "WH-1000XM4"},
                          {"address": "D4:2F:00:00:00:03", "name": "MX Master 3"}])

    def test_connected(self):
        r = fake_readers().bluetooth()
        self.assertEqual(r["summary"], "2 connected: WH-1000XM4, MX Master 3")
        self.assertEqual(r["lines"][0], "controller: testbook (powered on)")

    def test_nothing_connected_off_and_absent(self):
        table = default_table()
        table["bluetoothctl devices Connected"] = ""
        self.assertEqual(fake_readers(table=table).bluetooth()["summary"], "controller on, nothing connected")
        table["bluetoothctl show"] = fixture("bluetoothctl-show-off.txt")
        self.assertEqual(fake_readers(table=table).bluetooth()["summary"], "controller powered off")
        table["bluetoothctl show"] = Result(1, fixture("bluetoothctl-no-controller.txt"))
        r = fake_readers(table=table).bluetooth()
        self.assertEqual((r["read"], r["summary"]), (True, "no bluetooth controller"))

    def test_missing_and_daemon_down(self):
        r = fake_readers(table={}).bluetooth()
        self.assertEqual(r["error"], "bluetoothctl not found")
        r = fake_readers(table={"bluetoothctl show": Result(None, "Waiting to connect to bluetoothd...", True)}).bluetooth()
        self.assertIn("bluetoothd isn't answering", r["error"])


class OneOffFacts(unittest.TestCase):
    def test_boot_id(self):
        self.assertEqual(fake_readers().boot_id(), BOOT_A)
        self.assertIsNone(fake_readers(files={}).boot_id())

    def test_kernel_log(self):
        log = fake_readers().kernel_log("0")
        self.assertTrue(log["read"])
        self.assertIn("PM: hibernation: hibernation exit", log["lines"][-1])
        self.assertFalse(any("usb 3-3" in line for line in log["lines"]))
        self.assertEqual(len(filter_kernel_log("PM: x\n" * 50)), 20)

    def test_kernel_log_unavailable(self):
        table = {"journalctl": Result(1, "Data from the specified boot (-1) is not available: No such boot ID in journal\n")}
        log = fake_readers(table=table).kernel_log("-1")
        self.assertFalse(log["read"])
        self.assertIn("not available", log["error"])
        self.assertEqual(fake_readers(table={}).kernel_log()["error"], "journalctl not found")

    def test_machine_and_model(self):
        rd = fake_readers()
        self.assertEqual(rd.machine(), "testbook")
        self.assertEqual(rd.model(), "LENOVO 21CD0019US ThinkPad X1 Yoga Gen 7")
        rd = fake_readers(files={"/sys/class/dmi/id/sys_vendor": "HP\n",
                                 "/sys/class/dmi/id/product_name": "HP Spectre x360 Convertible 14\n",
                                 "/sys/class/dmi/id/product_version": "Type1ProductConfigId\n"})
        self.assertEqual(rd.model(), "HP Spectre x360 Convertible 14")
        rd = fake_readers(files={"/sys/class/dmi/id/sys_vendor": "Dell Inc.\n",
                                 "/sys/class/dmi/id/product_name": "XPS 13 9310 2-in-1\n",
                                 "/sys/class/dmi/id/product_version": "To Be Filled By O.E.M.\n"})
        self.assertEqual(rd.model(), "Dell Inc. XPS 13 9310 2-in-1")
        self.assertIsNone(fake_readers(files={}).model())

    def test_a_crashing_reader_reads_as_cant_read(self):
        rd = fake_readers(table={"upower -d": ValueError("boom")})
        r = rd.read("power")
        self.assertFalse(r["read"])
        self.assertIn("boom", r["error"])


class RealSeams(unittest.TestCase):
    """the seams themselves, run against python, never against a system tool."""

    def test_run_command(self):
        r = run_command([sys.executable, "-c", "print('hello \\x1b[01;32mworld\\x1b[0m')"], 10)
        self.assertEqual((r.returncode, r.output, r.timed_out), (0, "hello world\n", False))
        r = run_command([sys.executable, "-c", "import sys; sys.exit(3)"], 10)
        self.assertEqual(r.returncode, 3)
        with self.assertRaises(ToolMissing):
            run_command(["hwcheck-no-such-tool-here"], 1)
        r = run_command([sys.executable, "-c", "import time; print('a', flush=True); time.sleep(30)"], 0.5)
        self.assertTrue(r.timed_out)
        self.assertIsNone(r.returncode)

    def test_pty_stream_reads_line_by_line_and_stops(self):
        # python block-buffers a pipe; under a pty it flushes each line, like monitor-sensor needs
        code = "import time\nprint('=== Has accelerometer (orientation: normal, tilt: vertical)')\ntime.sleep(30)"
        proc = PtyProcess([sys.executable, "-c", code])
        watch = StreamWatch(proc, 0.0)
        wait_for(lambda: "orientation: normal" in watch.text(), timeout=10)
        watch.stop()
        self.assertIsNotNone(proc.proc.returncode)
        self.assertIsNone(proc.fd)
        with self.assertRaises(ToolMissing):
            PtyProcess(["hwcheck-no-such-tool-here"])

    def test_eviocgsw_number(self):
        # _IOC(_IOC_READ, 'E', 0x1b, 8) as the kernel headers spell it
        self.assertEqual(R._eviocgsw(8), 0x8008451B)


if __name__ == "__main__":
    unittest.main()
