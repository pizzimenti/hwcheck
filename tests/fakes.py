"""fakes for every system reader, built on recorded outputs in tests/fixtures/."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from hwchecklib.detect import Detector
from hwchecklib.readers import Readers, Result, ToolMissing

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE_PLAN = ROOT / "examples" / "plan-2in1.json"

BOOT_A = "6f1d2c3b-4a59-4e8f-9d7c-1b2a3c4d5e6f"
BOOT_B = "0a9b8c7d-6e5f-4a3b-8c2d-1e0f9a8b7c6d"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeRun:
    """stands in for run_command: answers from a table keyed by the full command line or the tool name.

    a value may be text (exit 0), a Result, an exception to raise, or a callable giving one of those.
    a tool not in the table is missing.
    """

    def __init__(self, table: dict | None = None):
        self.table = dict(table or {})
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=5.0):
        self.calls.append(list(argv))
        for key in (" ".join(argv), argv[0]):
            if key in self.table:
                value = self.table[key]
                if callable(value) and not isinstance(value, type):
                    value = value()
                if isinstance(value, BaseException):
                    raise value
                if isinstance(value, Result):
                    return value
                return Result(0, value)
        raise ToolMissing(argv[0])


class FakeProc:
    """stands in for PtyProcess: hands out recorded chunks, then waits (or ends)."""

    def __init__(self, text: str = "", end: bool = False):
        self.chunks = [text.encode()] if text else []
        self.end = end
        self.stopped = False
        self.closed = False

    def push(self, text: str) -> None:
        self.chunks.append(text.encode())

    def read(self, timeout):
        if self.stopped:
            return None
        if self.chunks:
            return self.chunks.pop(0)
        if self.end:
            return None
        time.sleep(min(timeout, 0.01))
        return b""

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeSpawn:
    def __init__(self, text: str | None = "", end: bool = False):
        self.text = text  # None: the tool is missing
        self.end = end
        self.procs: list[FakeProc] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv):
        self.argvs.append(list(argv))
        if self.text is None:
            raise ToolMissing(argv[0])
        proc = FakeProc(self.text, self.end)
        self.procs.append(proc)
        return proc


class FakeClock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeHibernate:
    """the hibernate action; never the real systemctl."""

    def __init__(self, ok: bool = True, message: str = "", then=None):
        self.ok = ok
        self.message = message
        self.then = then
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.then:
            self.then()
        return self.ok, self.message


class FakeMachine:
    """a recorded machine for detection: files, directories and links under /proc and /sys, and
    the output of the few commands detection runs. a fixture may build on another ("base")."""

    def __init__(self, name: str):
        spec = json.loads(fixture(name))
        base = FakeMachine(spec["base"]) if "base" in spec else None
        self.files = dict(base.files) if base else {}
        self.links = dict(base.links) if base else {}
        self.commands = dict(base.commands) if base else {}
        for path, fname in spec.get("include", {}).items():
            self.files[path] = fixture(fname)
        self.files.update(spec.get("files", {}))
        self.links.update(spec.get("links", {}))
        for cmd, out in spec.get("commands", {}).items():
            self.commands[cmd] = Result(out["rc"], out["out"]) if isinstance(out, dict) else out

    def remove(self, prefix: str) -> "FakeMachine":
        """forget a file, or everything under a directory, as if the machine didn't have it."""
        for table in (self.files, self.links):
            for path in [p for p in table if p == prefix or p.startswith(prefix.rstrip("/") + "/")]:
                del table[path]
        return self

    def read_file(self, path: str) -> str:
        if path in self.files:
            value = self.files[path]
            if isinstance(value, BaseException):
                raise value
            return value
        raise FileNotFoundError(2, "No such file or directory", path)

    def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        names = {p[len(prefix):].split("/")[0] for p in (*self.files, *self.links) if p.startswith(prefix)}
        if not names:
            raise FileNotFoundError(2, "No such file or directory", path)
        return sorted(names)

    def realpath(self, path: str) -> str:
        return self.links.get(path, path)

    def detector(self) -> Detector:
        self.run = FakeRun(self.commands)
        return Detector(read_file=self.read_file, listdir=self.listdir, realpath=self.realpath, run=self.run)


def default_files(boot_id: str = BOOT_A) -> dict:
    return {
        "/proc/sys/kernel/random/boot_id": boot_id + "\n",
        "/proc/bus/input/devices": fixture("input-devices-2in1.txt"),
        "/sys/class/dmi/id/sys_vendor": "LENOVO\n",
        "/sys/class/dmi/id/product_name": "21CD0019US\n",
        "/sys/class/dmi/id/product_version": "ThinkPad X1 Yoga Gen 7\n",
    }


def default_table() -> dict:
    return {
        "upower -d": fixture("upower-ac.txt"),
        "kscreen-doctor -o": fixture("kscreen-plasma6-external.txt"),
        "bluetoothctl show": fixture("bluetoothctl-show.txt"),
        "bluetoothctl devices Connected": fixture("bluetoothctl-connected.txt"),
        "journalctl -k -b 0 --no-pager -q -o short-monotonic": fixture("journal-resumed.txt"),
        "journalctl -k -b -1 --no-pager -q -o short-monotonic": fixture("journal-last-boot.txt"),
        "busctl": Result(1, "Failed to get property tabletMode: Unknown object '/org/kde/KWin'.\n"),
    }


def fake_readers(table=None, files=None, switches=None, spawn=None, monotonic=None) -> Readers:
    files = default_files() if files is None else files
    switches = {"/dev/input/event14": 0b10} if switches is None else switches

    def read_file(path):
        if path not in files:
            raise FileNotFoundError(2, "No such file or directory", path)
        value = files[path]
        if isinstance(value, BaseException):
            raise value
        return value

    def read_switches(path):
        if path not in switches:
            raise FileNotFoundError(2, "No such file or directory", path)
        value = switches[path]
        if isinstance(value, BaseException):
            raise value
        return value

    readers = Readers(
        run=FakeRun(default_table() if table is None else table),
        spawn=spawn or FakeSpawn(fixture("monitor-sensor-normal.txt")),
        read_file=read_file,
        read_switches=read_switches,
        hostname=lambda: "testbook",
        monotonic=monotonic or time.monotonic,
    )
    readers.files = files  # tests change the boot id through this
    return readers


def scratch_dir(test) -> Path:
    """a directory under $TMPDIR removed when the test ends."""
    path = Path(tempfile.mkdtemp(prefix="hwcheck-test-"))
    test.addCleanup(shutil.rmtree, path, True)
    return path


def wait_for(predicate, timeout: float = 5.0, step: float = 0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out waiting")


__all__ = [
    "BOOT_A", "BOOT_B", "EXAMPLE_PLAN", "FakeClock", "FakeMachine", "FakeHibernate", "FakeProc", "FakeRun",
    "FakeSpawn", "fake_readers", "fixture", "scratch_dir", "wait_for",
]
