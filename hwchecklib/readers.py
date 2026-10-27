"""system readers: what the server reads from the machine and pushes to the page.

every reader goes through three injectable seams so tests can use recorded outputs:
  run(argv, timeout) -> Result          one-shot tools (upower, kscreen-doctor, bluetoothctl, ...)
  spawn(argv) -> process                long-running tools (monitor-sensor), read under a pty
  read_file(path) -> str                /proc and /sys files
  read_switches(path) -> int            the evdev switch bitmap of /dev/input/eventN
a tool that is missing or fails gives a reading with read=False and a plain reason,
and the check stays manual.
"""

from __future__ import annotations

import codecs
import fcntl
import os
import pty
import re
import select
import socket
import subprocess
import threading
import time
from dataclasses import dataclass

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]")
SW_TABLET_MODE = 1
EVENT_RE = re.compile(r"^event\d+$")
KERNEL_LOG_RE = re.compile(r"PM: |hibernat|swsusp|\bresume\b", re.IGNORECASE)
DMI_JUNK = {
    "", "none", "n/a", "not applicable", "default string", "to be filled by o.e.m.",
    "system product name", "system version", "system manufacturer", "not specified",
    "type1productconfigid",
}
BUILTIN_PREFIXES = ("eDP", "LVDS", "DSI")
KSCREEN_TYPES = {
    "Unknown", "VGA", "DVI", "DVII", "DVIA", "DVID", "HDMI", "Panel", "TV", "TVComposite",
    "TVSVideo", "TVComponent", "TVSCART", "TVC4", "DisplayPort", "UnmappedOutputType",
}


class ToolMissing(Exception):
    """the tool isn't installed (or can't be run)."""

    def __init__(self, tool: str):
        self.tool = tool
        super().__init__(f"{tool} not found")


@dataclass
class Result:
    returncode: int | None
    output: str
    timed_out: bool = False


def clean(text: str) -> str:
    """drop colour codes and carriage returns."""
    return ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "")


def tool_env() -> dict:
    env = dict(os.environ)
    env["LC_ALL"] = "C.UTF-8"
    env["NO_COLOR"] = "1"
    return env


def run_command(argv: list[str], timeout: float = 5.0) -> Result:
    """run a one-shot tool and return what it printed (stdout and stderr together)."""
    try:
        cp = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, env=tool_env(), start_new_session=True,
        )
    except (FileNotFoundError, PermissionError):
        raise ToolMissing(argv[0]) from None
    except subprocess.TimeoutExpired as exc:
        out = exc.output or b""
        return Result(None, clean(out.decode("utf-8", "replace")), True)
    return Result(cp.returncode, clean(cp.stdout.decode("utf-8", "replace")), False)


def read_text_file(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _eviocgsw(length: int) -> int:
    # _IOC(_IOC_READ, 'E', 0x1b, length)
    return (2 << 30) | (length << 16) | (ord("E") << 8) | 0x1B


def evdev_switches(path: str) -> int:
    """the switch bitmap of an evdev node; PermissionError when the user may not read it."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        buf = bytearray(8)
        fcntl.ioctl(fd, _eviocgsw(len(buf)), buf, True)
        return int.from_bytes(buf, "little")
    finally:
        os.close(fd)


class PtyProcess:
    """a long-running tool whose output is read through a pty, so it prints line by line."""

    def __init__(self, argv: list[str]):
        master, slave = pty.openpty()
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                env=tool_env(), close_fds=True, start_new_session=True,
            )
        except (FileNotFoundError, PermissionError):
            os.close(master)
            raise ToolMissing(argv[0]) from None
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.fd: int | None = master

    def read(self, timeout: float) -> bytes | None:
        """bytes printed so far; b"" when nothing came in time; None once the tool has gone."""
        fd = self.fd
        if fd is None:
            return None
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return b""
            data = os.read(fd, 4096)
        except (OSError, ValueError):
            return None
        return data or None

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class StreamWatch:
    """keeps one long-running tool going and holds what it has printed."""

    MAX_CHARS = 65536

    def __init__(self, proc, started: float):
        self.proc = proc
        self.started = started
        self.ended = False
        self._stop = False
        self._buf = ""
        self._lock = threading.Lock()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._thread = threading.Thread(target=self._pump, name="hwcheck-watch", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        while True:
            chunk = self.proc.read(0.25)
            if chunk is None:
                break
            if chunk:
                text = self._decoder.decode(chunk)
                with self._lock:
                    self._buf = (self._buf + text)[-self.MAX_CHARS:]
            elif self._stop:
                break
        with self._lock:
            self.ended = True

    def text(self) -> str:
        with self._lock:
            return clean(self._buf)

    def stop(self) -> None:
        self._stop = True
        try:
            self.proc.stop()
        finally:
            self._thread.join(3)
            close = getattr(self.proc, "close", None)
            # closing the fd under a reader still in select() could hand it someone else's fd
            if close and not self._thread.is_alive():
                close()


# ---------------------------------------------------------------- parsers (pure)

def last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def parse_monitor_sensor(text: str) -> dict:
    """fold monitor-sensor output into its latest accelerometer state."""
    out = {"proxy": False, "accelerometer": None, "orientation": None, "tilt": None}
    for raw in clean(text).splitlines():
        line = raw.strip()
        if line.startswith("+++ iio-sensor-proxy appeared"):
            out["proxy"] = True
        elif line.startswith("--- iio-sensor-proxy vanished"):
            out.update(proxy=False, accelerometer=None, orientation=None, tilt=None)
        elif line.startswith("=== Has accelerometer"):
            out["proxy"] = True
            out["accelerometer"] = True
            m = re.search(r"orientation:\s*([^,)]+)", line)
            if m:
                out["orientation"] = m.group(1).strip()
            m = re.search(r"tilt:\s*([^,)]+)", line)
            if m:
                out["tilt"] = m.group(1).strip()
        elif line.startswith("=== No accelerometer"):
            out["proxy"] = True
            out.update(accelerometer=False, orientation=None, tilt=None)
        elif line.startswith("Accelerometer orientation changed:"):
            out["proxy"] = True
            out["accelerometer"] = True
            out["orientation"] = line.split(":", 1)[1].strip()
        elif line.startswith("Tilt changed:"):
            out["tilt"] = line.split(":", 1)[1].strip()
        elif line.startswith("--- Accelerometer disappeared"):
            out.update(accelerometer=False, orientation=None, tilt=None)
        elif line.startswith("+++ Accelerometer appeared"):
            out["accelerometer"] = True
    return out


def _bitmap(value: str) -> int:
    n = 0
    for word in value.split():
        try:
            n = (n << 64) | int(word, 16)
        except ValueError:
            return 0
    return n


def parse_input_devices(text: str) -> list[dict]:
    """the blocks of /proc/bus/input/devices."""
    devices = []
    for block in re.split(r"\n\s*\n", text.strip()):
        dev = {"name": "", "handlers": [], "event": None, "bits": {}}
        for line in block.splitlines():
            if line.startswith("N: Name="):
                dev["name"] = line[len("N: Name="):].strip().strip('"')
            elif line.startswith("H: Handlers="):
                dev["handlers"] = line[len("H: Handlers="):].split()
            elif line.startswith("B: ") and "=" in line:
                key, value = line[3:].split("=", 1)
                dev["bits"][key.strip()] = _bitmap(value)
        dev["event"] = next((h for h in dev["handlers"] if EVENT_RE.match(h)), None)
        if dev["name"] or dev["handlers"]:
            devices.append(dev)
    return devices


def parse_upower(text: str) -> dict:
    """upower -d: its devices (with the section type) and the daemon section."""
    devices: list[dict] = []
    daemon: dict | None = None
    current: dict | None = None
    for raw in clean(text).splitlines():
        if not raw.strip():
            continue
        if raw.startswith("Device:"):
            current = {"path": raw.split(":", 1)[1].strip(), "type": None, "props": {}}
            devices.append(current)
            continue
        if raw.startswith("Daemon:"):
            daemon = {}
            current = None
            continue
        if not raw[0].isspace():
            current = None
            continue
        line = raw.strip()
        target = daemon if current is None else current["props"]
        if target is None:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            target.setdefault(key.strip(), value.strip().strip("'"))
        elif current is not None and current["type"] is None:
            current["type"] = line
    return {"devices": devices, "daemon": daemon}


def _mode_text(token: str) -> str:
    mode = token.split(":", 1)[-1].rstrip("*!")
    if "@" in mode:
        size, rate = mode.split("@", 1)
        try:
            rate = f"{float(rate):.2f}".rstrip("0").rstrip(".")
        except ValueError:
            pass
        mode = f"{size}@{rate}"
    return mode


def parse_kscreen(text: str) -> list[dict]:
    """kscreen-doctor -o, in the one-line (plasma 5) or one-attribute-per-line (plasma 6) form."""
    outputs = []
    blocks = re.split(r"(?m)^(?=Output:)", clean(text))
    for block in blocks:
        if not block.startswith("Output:"):
            continue
        tokens = block.split()
        if len(tokens) < 3:
            continue
        head = tokens[: tokens.index("Modes:")] if "Modes:" in tokens else tokens
        out = {"id": tokens[1], "name": tokens[2], "type": None, "enabled": None,
               "connected": None, "mode": None, "geometry": None}
        if "enabled" in head:
            out["enabled"] = True
        elif "disabled" in head:
            out["enabled"] = False
        if "connected" in head:
            out["connected"] = True
        elif "disconnected" in head:
            out["connected"] = False
        out["type"] = next((t for t in head[3:] if t in KSCREEN_TYPES), None)
        for tok in tokens:
            if re.match(r"^\d+:\d+x\d+@[\d.]+\*!?$", tok):
                out["mode"] = _mode_text(tok)
                break
        if "Geometry:" in tokens:
            i = tokens.index("Geometry:")
            out["geometry"] = " ".join(tokens[i + 1:i + 3])
        out["builtin"] = out["type"] == "Panel" or out["name"].startswith(BUILTIN_PREFIXES)
        outputs.append(out)
    return outputs


def parse_bt_show(text: str) -> dict | None:
    """bluetoothctl show: the default controller, or None when there is none."""
    text = clean(text)
    if "No default controller available" in text:
        return None
    ctrl = {"name": None, "powered": None}
    found = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Controller "):
            found = True
        elif line.startswith("Name:") and ctrl["name"] is None:
            ctrl["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Powered:"):
            ctrl["powered"] = line.split(":", 1)[1].strip() == "yes"
    return ctrl if found else None


def parse_bt_devices(text: str) -> list[dict]:
    devices = []
    for raw in clean(text).splitlines():
        m = re.search(r"(?:^|\s)Device ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\s*(.*)$", raw.strip())
        if m:
            devices.append({"address": m.group(1).upper(), "name": m.group(2).strip() or m.group(1).upper()})
    return devices


def parse_kwin_tablet(text: str) -> bool | None:
    m = re.match(r"^\s*b\s+(true|false)\s*$", text)
    return None if not m else m.group(1) == "true"


def filter_kernel_log(text: str, keep: int = 20) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines() if KERNEL_LOG_RE.search(ln)][-keep:]


# ---------------------------------------------------------------- readings

def ok(source: str, summary: str, lines: list[str], state: dict) -> dict:
    return {"read": True, "source": source, "summary": summary, "lines": lines, "state": state}


def cant(source: str, error: str) -> dict:
    return {"read": False, "source": source, "summary": f"can't read: {error}", "lines": [],
            "state": None, "error": error}


def _yes(value) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() == "yes"


class Readers:
    """everything the server reads from the machine."""

    def __init__(self, run=run_command, spawn=PtyProcess, read_file=read_text_file,
                 read_switches=evdev_switches, hostname=socket.gethostname, monotonic=time.monotonic):
        self.run = run
        self.spawn = spawn
        self.read_file = read_file
        self.read_switches = read_switches
        self.hostname = hostname
        self.monotonic = monotonic
        self._watches: dict[str, StreamWatch] = {}
        self._lock = threading.Lock()

    def read(self, kind: str) -> dict:
        fn = {
            "rotation": self.rotation,
            "tablet-mode": self.tablet_mode,
            "lid": self.lid,
            "power": self.power,
            "external-display": self.external_display,
            "bluetooth": self.bluetooth,
        }[kind]
        try:
            return fn()
        except Exception as exc:  # a reader must never take the server down
            return cant(kind, f"reader failed: {exc}")

    def release(self) -> None:
        """stop every long-running tool (called when a check ends and on exit)."""
        with self._lock:
            watches, self._watches = list(self._watches.values()), {}
        for w in watches:
            w.stop()

    def watching(self) -> list[str]:
        with self._lock:
            return sorted(self._watches)

    # -- rotation (monitor-sensor, kept running while the check is on screen)
    def rotation(self) -> dict:
        source = "monitor-sensor"
        with self._lock:
            w = self._watches.get("rotation")
            if w is None:
                try:
                    proc = self.spawn(["monitor-sensor", "--accel"])
                except ToolMissing:
                    return cant(source, "monitor-sensor not found (it comes with iio-sensor-proxy)")
                except OSError as exc:
                    return cant(source, f"monitor-sensor didn't start: {exc.strerror or exc}")
                w = StreamWatch(proc, self.monotonic())
                self._watches["rotation"] = w
        text = w.text()
        p = parse_monitor_sensor(text)
        if p["accelerometer"] is False:
            return ok(source, "no accelerometer", ["iio-sensor-proxy reports no accelerometer"],
                      {"accelerometer": False, "orientation": None, "tilt": None})
        if p["orientation"]:
            lines = [f"orientation: {p['orientation']}"]
            if p["tilt"]:
                lines.append(f"tilt: {p['tilt']}")
            return ok(source, f"orientation: {p['orientation']}", lines,
                      {"accelerometer": True, "orientation": p["orientation"], "tilt": p["tilt"]})
        if w.ended:
            last = last_line(text)
            return cant(source, f"monitor-sensor stopped: {last}" if last else "monitor-sensor stopped")
        if not p["proxy"] and self.monotonic() - w.started >= 2.0:
            return cant(source, "iio-sensor-proxy isn't running")
        return ok(source, "waiting for the first reading", ["waiting for monitor-sensor"],
                  {"accelerometer": None, "orientation": None, "tilt": None})

    # -- tablet mode (the switch in /proc/bus/input/devices, and plasma's view if it has one)
    def tablet_mode(self) -> dict:
        source = "/proc/bus/input/devices"
        try:
            text = self.read_file("/proc/bus/input/devices")
        except OSError as exc:
            return cant(source, f"can't open /proc/bus/input/devices: {exc.strerror or exc}")
        switches = [d for d in parse_input_devices(text)
                    if (d["bits"].get("SW", 0) >> SW_TABLET_MODE) & 1]
        lines: list[str] = []
        found = []
        switch_on = None
        for dev in switches:
            entry = {"name": dev["name"], "event": dev["event"], "on": None}
            if dev["event"]:
                try:
                    bits = self.read_switches(f"/dev/input/{dev['event']}")
                    entry["on"] = bool((bits >> SW_TABLET_MODE) & 1)
                    switch_on = entry["on"] if switch_on is None else (switch_on or entry["on"])
                    lines.append(f"{dev['name']}: tablet mode {'on' if entry['on'] else 'off'}")
                except PermissionError:
                    lines.append(f"{dev['name']} ({dev['event']}): present; its state can't be read without access to /dev/input")
                except OSError as exc:
                    lines.append(f"{dev['name']} ({dev['event']}): present; can't read its state: {exc.strerror or exc}")
            else:
                lines.append(f"{dev['name']}: present, no event device")
            found.append(entry)
        if not switches:
            lines.append("no tablet-mode switch found")
        kwin = self._kwin_tablet()
        if kwin is not None:
            lines.append(f"plasma says tablet mode is {'on' if kwin else 'off'}")
        if switch_on is not None:
            summary = f"tablet mode: {'on' if switch_on else 'off'}"
        elif kwin is not None:
            summary = f"tablet mode: {'on' if kwin else 'off'} (plasma)"
        elif switches:
            summary = "tablet-mode switch present; state can't be read"
        else:
            summary = "no tablet-mode switch found"
        return ok(source, summary, lines, {"switches": found, "switch_on": switch_on, "plasma": kwin})

    def _kwin_tablet(self) -> bool | None:
        try:
            r = self.run(["busctl", "--user", "get-property", "org.kde.KWin", "/org/kde/KWin",
                          "org.kde.KWin.TabletModeManager", "tabletMode"], 2.0)
        except ToolMissing:
            return None
        if r.timed_out or r.returncode != 0:
            return None
        return parse_kwin_tablet(r.output)

    # -- lid and power (upower -d)
    def _upower(self):
        try:
            r = self.run(["upower", "-d"], 5.0)
        except ToolMissing:
            return None, "upower not found"
        if r.timed_out:
            return None, "upower didn't answer in 5 s"
        if r.returncode != 0:
            return None, f"upower failed: {last_line(r.output) or f'exit {r.returncode}'}"
        info = parse_upower(r.output)
        if info["daemon"] is None:
            return None, "upower printed no daemon section (is upowerd running?)"
        return info, None

    def lid(self) -> dict:
        source = "upower -d"
        info, err = self._upower()
        if err:
            return cant(source, err)
        d = info["daemon"]
        present, closed = _yes(d.get("lid-is-present")), _yes(d.get("lid-is-closed"))
        if not present:
            return ok(source, "no lid reported", ["upower reports no lid"], {"present": False, "closed": None})
        summary = "lid closed" if closed else "lid open"
        return ok(source, summary, [summary], {"present": True, "closed": closed})

    def power(self) -> dict:
        source = "upower -d"
        info, err = self._upower()
        if err:
            return cant(source, err)
        on_battery = _yes(info["daemon"].get("on-battery"))
        ac = [d for d in info["devices"] if d["type"] == "line-power"]
        ac_online = None
        if ac:
            ac_online = any(_yes(d["props"].get("online")) for d in ac)
        batteries = []
        for d in info["devices"]:
            if d["type"] != "battery" or d["path"].endswith("/DisplayDevice"):
                continue
            if not _yes(d["props"].get("power supply")):
                continue
            pct = d["props"].get("percentage", "").rstrip("%")
            try:
                pct_v = round(float(pct))
            except ValueError:
                pct_v = None
            batteries.append({"name": d["props"].get("native-path") or d["path"].rsplit("/", 1)[-1],
                              "state": d["props"].get("state"), "percentage": pct_v})
        lines = [f"running on: {'battery' if on_battery else 'mains'}"]
        if ac_online is None:
            lines.append("charger: no line-power device seen")
        else:
            lines.append(f"charger: {'connected' if ac_online else 'not connected'}")
        for b in batteries:
            pct = f"{b['percentage']}%" if b["percentage"] is not None else "?%"
            lines.append(f"{b['name']}: {pct}, {b['state'] or 'unknown'}")
        if not batteries:
            lines.append("no battery seen")
        summary = "on battery" if on_battery else "on mains"
        if batteries:
            b = batteries[0]
            summary += f", {b['name']} {b['percentage'] if b['percentage'] is not None else '?'}% {b['state'] or ''}".rstrip()
        return ok(source, summary, lines, {"on_battery": on_battery, "charger": ac_online, "batteries": batteries})

    # -- external display (kscreen-doctor -o)
    def external_display(self) -> dict:
        source = "kscreen-doctor -o"
        try:
            r = self.run(["kscreen-doctor", "-o"], 5.0)
        except ToolMissing:
            return cant(source, "kscreen-doctor not found")
        if r.timed_out:
            return cant(source, "kscreen-doctor didn't answer in 5 s")
        outputs = parse_kscreen(r.output)
        if r.returncode != 0 or not outputs:
            return cant(source, f"kscreen-doctor: {last_line(r.output) or f'exit {r.returncode}'}")
        lines = []
        for o in outputs:
            where = "built in" if o["builtin"] else "external"
            parts = ["connected" if o["connected"] else "not connected"]
            if o["enabled"] is not None:
                parts.append("on" if o["enabled"] else "off")
            if o["connected"] and o["enabled"] and o["mode"]:
                parts.append(o["mode"])
            lines.append(f"{o['name']} ({where}): {', '.join(parts)}")
        external = [o for o in outputs if o["connected"] and not o["builtin"]]
        if external:
            e = external[0]
            summary = f"external display: {e['name']} {'on' if e['enabled'] else 'connected, off'}"
            if e["enabled"] and e["mode"]:
                summary += f", {e['mode']}"
            if len(external) > 1:
                summary += f" (+{len(external) - 1} more)"
        else:
            summary = "no external display connected"
        return ok(source, summary, lines, {"outputs": outputs, "external_connected": len(external)})

    # -- bluetooth (bluetoothctl show; bluetoothctl devices Connected)
    def bluetooth(self) -> dict:
        source = "bluetoothctl"
        try:
            show = self.run(["bluetoothctl", "show"], 4.0)
        except ToolMissing:
            return cant(source, "bluetoothctl not found")
        if show.timed_out:
            return cant(source, "bluetoothd isn't answering (is bluetooth.service running?)")
        ctrl = parse_bt_show(show.output)
        if ctrl is None:
            if "No default controller available" in show.output:
                return ok(source, "no bluetooth controller", ["no bluetooth controller found"],
                          {"controller": None, "connected": []})
            return cant(source, f"bluetoothctl show: {last_line(show.output) or f'exit {show.returncode}'}")
        try:
            devs = self.run(["bluetoothctl", "devices", "Connected"], 4.0)
        except ToolMissing:
            return cant(source, "bluetoothctl not found")
        if devs.timed_out:
            return cant(source, "bluetoothd isn't answering (is bluetooth.service running?)")
        if devs.returncode not in (0, None):
            return cant(source, f"bluetoothctl devices: {last_line(devs.output) or f'exit {devs.returncode}'}")
        connected = parse_bt_devices(devs.output)
        power = {True: "powered on", False: "powered off", None: "power unknown"}[ctrl["powered"]]
        lines = [f"controller: {ctrl['name'] or 'unnamed'} ({power})"]
        lines += [f"connected: {d['name']} ({d['address']})" for d in connected]
        if connected:
            summary = f"{len(connected)} connected: " + ", ".join(d["name"] for d in connected)
        elif ctrl["powered"] is False:
            summary = "controller powered off"
        else:
            summary = "controller on, nothing connected"
            lines.append("nothing connected")
        return ok(source, summary, lines, {"controller": ctrl, "connected": connected})

    # -- one-off facts
    def boot_id(self) -> str | None:
        try:
            value = self.read_file("/proc/sys/kernel/random/boot_id").strip()
        except OSError:
            return None
        return value or None

    def kernel_log(self, boot: str = "0") -> dict:
        argv = ["journalctl", "-k", "-b", boot, "--no-pager", "-q", "-o", "short-monotonic"]
        source = " ".join(argv[:4])
        try:
            r = self.run(argv, 10.0)
        except ToolMissing:
            return {"read": False, "source": source, "error": "journalctl not found", "lines": []}
        if r.timed_out:
            return {"read": False, "source": source, "error": "journalctl didn't finish in 10 s", "lines": []}
        if r.returncode != 0:
            return {"read": False, "source": source,
                    "error": last_line(r.output) or f"exit {r.returncode}", "lines": []}
        return {"read": True, "source": source, "lines": filter_kernel_log(r.output)}

    def machine(self) -> str:
        try:
            return self.hostname() or "unknown"
        except OSError:
            return "unknown"

    def model(self) -> str | None:
        parts: list[str] = []
        for name in ("sys_vendor", "product_name", "product_version"):
            try:
                value = self.read_file(f"/sys/class/dmi/id/{name}").strip()
            except OSError:
                continue
            if value.lower() in DMI_JUNK or re.match(r"^[\d.]+$", value):
                continue
            if any(value.lower() in p.lower() for p in parts):
                continue
            parts = [p for p in parts if p.lower() not in value.lower()]
            parts.append(value)
        return " ".join(parts) or None
