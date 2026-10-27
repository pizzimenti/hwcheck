"""detection: build the plan from what this machine actually has.

everything here only reads, as the person running it (no root): /proc, /sys and
two optional tools over the user's own rights (busctl for fprintd, pactl). every
source goes through an injectable seam so tests run on recorded machines:
  read_file(path) -> str     listdir(path) -> [names]     realpath(path) -> str
  run(argv, timeout) -> Result
a source that can't be read counts as "nothing there", and is noted.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .readers import Result, ToolMissing, clean, parse_input_devices, read_text_file, run_command

# evdev numbers (linux/input-event-codes.h)
EV_KEY, EV_ABS, EV_SW = 1, 3, 5
KEY_Q, KEY_A, KEY_Z, KEY_SPACE = 16, 30, 44, 57
BTN_TOOL_PEN, BTN_TOOL_FINGER, BTN_TOUCH = 0x140, 0x145, 0x14A
ABS_X, ABS_TILT_X, ABS_MT_POSITION_X = 0x00, 0x1A, 0x35
INPUT_PROP_POINTER, INPUT_PROP_DIRECT = 0, 1
SW_LID, SW_TABLET_MODE, SW_HEADPHONE_INSERT = 0, 1, 2

BUILTIN_CONNECTORS = ("eDP", "LVDS", "DSI", "Writeback", "Virtual")
HDMI_PCM_RE = re.compile(r"hdmi|displayport|\bdp\b", re.IGNORECASE)
PEN_NAME_RE = re.compile(r"\b(pen|stylus)\s*$", re.IGNORECASE)
VIRTUAL_CAMERA_RE = re.compile(r"loopback|dummy|virtual", re.IGNORECASE)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
MAX_IDS = 40          # the plan takes at most 50
MAX_TITLE = 120       # the plan's limit
MAX_MATCH = 200
LAPTOP_ALWAYS = ("speakers", "microphone", "camera")  # a laptop without one of these lost a driver

# what a device kind is called in --expect (and the aliases it accepts)
DEVICE_KINDS = (
    "speakers", "microphone", "headphones", "camera", "touch", "pen", "keys", "touchpad",
    "fingerprint", "tablet-mode", "rotation", "power", "lid", "external-display", "bluetooth",
    "wifi", "hibernate",
)
ALIASES = {
    "touchscreen": "touch", "stylus": "pen", "keyboard": "keys", "webcam": "camera",
    "accelerometer": "rotation", "battery": "power", "display": "external-display",
    "swap": "hibernate", "mic": "microphone", "headphone": "headphones",
}

# the text each detected check carries: (title, kind, instructions, expect)
TEXT = {
    "speakers": ("speakers", "speakers",
                 "turn the volume to about half, then play the left, right and both tones.",
                 "left only from the left speaker, right only from the right, both from both, with no crackle"),
    "microphone": ("microphone", "microphone",
                   "start the microphone and speak, then record 3 s and listen to it played back.",
                   "the bar moves when you speak and the recording is clear"),
    "headphones": ("headphone jack", "headphones",
                   "plug wired headphones into the jack and play the tones again.",
                   "the sound moves to the headphones when they're plugged in and back to the speakers when they're not"),
    "camera": ("camera", "camera",
               "start the camera; if this machine has more than one, use next camera until this one shows.",
               "a live, clear picture"),
    "touch": ("touchscreen", "touch",
              "drag the box to the target with a finger, then rest several fingers on the grey area.",
              "the box follows your finger and several touches are counted"),
    "pen": ("pen", "pen",
            "draw on the pad with the pen, lightly and then hard, and tilt it.",
            "lines get thicker as you press harder and the tilt numbers change"),
    "keys": ("keyboard", "keys",
             "press every key, including the function row and the arrows. try the volume and brightness keys too.",
             "every key shows up; volume and brightness change on screen"),
    "touchpad": ("touchpad", "manual",
                 "move the pointer, tap to click, scroll with two fingers and right-click.",
                 "all of it works smoothly"),
    "fingerprint": ("fingerprint reader", "manual",
                    "enrol a finger in the system settings (users), then lock the screen and unlock it with that finger.",
                    "the finger enrols and unlocks the screen"),
    "tablet-mode": ("tablet mode", "tablet-mode",
                    "fold the screen all the way back into tablet mode, then open it back to a laptop.",
                    "tablet mode turns on when folded and off when opened; the keyboard stops typing while folded"),
    "rotation": ("screen rotation", "rotation",
                 "in tablet mode, turn the machine onto each side and upside down.",
                 "the orientation follows the machine and the screen turns with it"),
    "power": ("charger", "power",
              "unplug the charger, wait a few seconds, then plug it back in.",
              "it goes on battery when unplugged and back to mains, charging, when plugged in"),
    "lid": ("lid", "lid",
            "close the lid for a few seconds, then open it. if the machine sleeps, wake it.",
            "the lid is seen closed then open, and the screen comes back"),
    "external-display": ("external display", "external-display",
                         "plug an external display into each port you want to check (usb-c or hdmi).",
                         "the display is seen as connected and shows a picture"),
    "bluetooth": ("bluetooth", "bluetooth",
                  "connect a bluetooth device (headphones or a mouse) from the system's bluetooth settings.",
                  "the device shows as connected and works"),
    "wifi": ("wifi", "manual",
             "look at the network icon in the panel.",
             "wifi is connected with a good signal"),
    "hibernate": ("hibernate", "hibernate",
                  "this runs last. the machine hibernates and powers off; turn it back on with the power button.",
                  "it comes back where it left off, with this page still open"),
}
ORDER = ("speakers", "microphone", "headphones", "camera", "touch", "pen", "keys", "touchpad",
         "fingerprint", "tablet-mode", "rotation", "power", "lid", "external-display", "bluetooth", "wifi")


class ExpectError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def plain(text) -> str:
    """text from the machine, safe to show anywhere: no colour codes or control characters."""
    return CONTROL_RE.sub("", clean(str(text or ""))).strip()


def fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def device(name: str, path: str | None = None, ids=()) -> dict:
    ids = [i for i in dict.fromkeys(fit(plain(i), 300) for i in ids) if i]
    if len(ids) > MAX_IDS:  # keep a count of the rest rather than refuse the plan
        ids = ids[: MAX_IDS - 1] + [f"(+{len(ids) - MAX_IDS + 1} more)"]
    return {"name": fit(plain(name), 200) or "unnamed", "path": fit(plain(path), 300) if path else None,
            "ids": ids}


def _bit(n: int, i: int) -> bool:
    return bool((n >> i) & 1)


def _gib(kib: int) -> str:
    return f"{kib / 1048576:.1f} GiB"


def _usb_id(modalias: str) -> str:
    m = re.search(r"v([0-9A-Fa-f]{4})p([0-9A-Fa-f]{4})", modalias)
    return f"{m.group(1).lower()}:{m.group(2).lower()}" if m else ""


def parse_asound_cards(text: str) -> dict:
    """/proc/asound/cards: card number -> (id, driver, long name)."""
    cards = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(\d+)\s+\[(.*?)\s*\]:\s*(.*?)\s+-\s+(.*)$", line)
        if m:
            longname = lines[i + 1].strip() if i + 1 < len(lines) else ""
            cards[int(m.group(1))] = {"id": m.group(2), "driver": m.group(3), "name": m.group(4).strip(),
                                      "longname": longname}
    return cards


def parse_asound_pcm(text: str) -> list[dict]:
    """/proc/asound/pcm: one line per pcm device, with its playback and capture streams."""
    pcms = []
    for line in text.splitlines():
        m = re.match(r"^(\d+)-(\d+):\s*(.*)$", line)
        if not m:
            continue
        parts = [p.strip() for p in m.group(3).split(" : ")]
        streams = " ".join(parts[2:]) if len(parts) > 2 else ""
        pcms.append({"card": int(m.group(1)), "device": int(m.group(2)),
                     "id": re.sub(r"\s*\(\*\)$", "", parts[0]),
                     "name": parts[1] if len(parts) > 1 else "",
                     "playback": "playback" in streams, "capture": "capture" in streams})
    return pcms


def parse_swaps(text: str) -> list[dict]:
    swaps = []
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 3 and cols[2].isdigit():
            swaps.append({"path": cols[0], "type": cols[1], "kib": int(cols[2])})
    return swaps


def parse_memtotal(text: str) -> int | None:
    m = re.search(r"^MemTotal:\s+(\d+)\s*kB", text, re.M)
    return int(m.group(1)) if m else None


class Detector:
    """reads what this machine has; detect() returns {kind: [device, ...]} and notes."""

    def __init__(self, read_file=read_text_file, listdir=os.listdir, realpath=os.path.realpath,
                 run=run_command):
        self.read_file = read_file
        self.listdir = listdir
        self.realpath = realpath
        self.run = run
        self.notes: list[str] = []

    # -- seams that turn "can't read" into "nothing there", with a note
    def _read(self, path: str, note: bool = False) -> str | None:
        try:
            return self.read_file(path)
        except OSError as exc:
            if note:
                self.notes.append(f"can't read {path}: {exc.strerror or exc}")
            return None

    def _ls(self, path: str) -> list[str]:
        try:
            return sorted(self.listdir(path))
        except OSError:
            return []

    def _run(self, argv: list[str]) -> Result | None:
        try:
            r = self.run(argv, 5.0)
        except ToolMissing:
            return None
        return None if r.timed_out or r.returncode != 0 else r

    # -- the whole machine
    def detect(self) -> dict:
        self.notes = []
        self.evidence: dict[str, str] = {}
        found: dict[str, list[dict]] = {k: [] for k in DEVICE_KINDS}
        inputs = parse_input_devices(self._read("/proc/bus/input/devices", note=True) or "")
        self._inputs(inputs, found)
        self._audio(inputs, found)
        found["camera"] = self._cameras()
        found["fingerprint"] = self._fingerprints()
        found["rotation"] = self._accelerometers()
        acpi_lids = self._lids_acpi()  # the acpi button first; else the lid switch among the inputs
        if acpi_lids:
            found["lid"] = acpi_lids
        found["power"] = self._batteries()
        found["external-display"] = self._display_ports()
        found["bluetooth"] = self._bluetooth()
        found["wifi"] = self._wifi()
        found["hibernate"], hibernate_skip = self._swap()
        return {"devices": found, "hibernate_skip": hibernate_skip, "notes": list(self.notes),
                "evidence": dict(self.evidence)}

    def _inputs(self, inputs: list[dict], found: dict) -> None:
        for d in inputs:
            b = d["bits"]
            ev, key, absb, prop, sw = (b.get("EV", 0), b.get("KEY", 0), b.get("ABS", 0),
                                       b.get("PROP", 0), b.get("SW", 0))
            dev = device(d["name"], f"/dev/input/{d['event']}" if d["event"] else None,
                         [d["name"], d["phys"], d["sysfs"], d["id"]])
            pen = _bit(key, BTN_TOOL_PEN) or (_bit(ev, EV_ABS) and _bit(absb, ABS_TILT_X)) \
                or bool(PEN_NAME_RE.search(d["name"]))
            if pen:
                found["pen"].append(dev)
            elif _bit(prop, INPUT_PROP_DIRECT) and _bit(ev, EV_ABS) and \
                    (_bit(absb, ABS_MT_POSITION_X) or _bit(absb, ABS_X)):
                found["touch"].append(dev)
            elif _bit(prop, INPUT_PROP_POINTER) and _bit(key, BTN_TOOL_FINGER) and _bit(absb, ABS_MT_POSITION_X):
                found["touchpad"].append(dev)
            if _bit(ev, EV_KEY) and all(_bit(key, k) for k in (KEY_Q, KEY_A, KEY_Z, KEY_SPACE)):
                found["keys"].append(dev)
            if _bit(ev, EV_SW) or sw:
                if _bit(sw, SW_TABLET_MODE):
                    found["tablet-mode"].append(dev)
                if _bit(sw, SW_LID):
                    found["lid"].append(dev)
                if _bit(sw, SW_HEADPHONE_INSERT):
                    found["headphones"].append(dev)

    def _audio(self, inputs: list[dict], found: dict) -> None:
        cards_text = self._read("/proc/asound/cards")
        pcm_text = self._read("/proc/asound/pcm")
        cards = parse_asound_cards(cards_text or "")
        pcms = parse_asound_pcm(pcm_text or "")
        # what to say if a laptop turns out to have no speakers or microphone
        if cards_text is None:
            card_words = "can't read /proc/asound/cards"
        elif not cards:
            card_words = "no sound card in /proc/asound/cards"
        else:
            card_words = "sound cards: " + ", ".join(f"{n} {c['id']} ({c['driver']})" for n, c in sorted(cards.items()))
        pcm_where = "/proc/asound/pcm" if pcm_text is not None else "/proc/asound/pcm (unreadable)"
        self.evidence["speakers"] = f"no playback device in {pcm_where}; {card_words}"
        self.evidence["microphone"] = f"no capture device in {pcm_where}; {card_words}"
        for p in pcms:
            card = cards.get(p["card"], {})
            label = f"card {p['card']} {card.get('id') or '?'}: {p['id']}"
            dev = device(label, f"/dev/snd/pcmC{p['card']}D{p['device']}",
                         [p["id"], p["name"], card.get("id", ""), card.get("driver", ""),
                          card.get("name", ""), card.get("longname", "")])
            hdmi = bool(HDMI_PCM_RE.search(f"{p['id']} {p['name']}"))
            if p["playback"] and not hdmi:
                found["speakers"].append(dict(dev, path=dev["path"] + "p"))
            if p["capture"]:
                found["microphone"].append(dict(dev, path=dev["path"] + "c"))
        # a laptop's built-in microphone is its dmic; the analog capture is the headset jack
        found["microphone"].sort(key=lambda d: 0 if "dmic" in d["name"].lower() else 1)
        if not found["headphones"]:  # no jack switch in the input devices: ask pulseaudio/pipewire
            r = self._run(["pactl", "list", "sinks"])
            for line in (r.output if r else "").splitlines():
                m = re.match(r"^\s*([\w.-]*headphone[\w.-]*):\s*(.*?)(\s*\(.*)?$", line, re.IGNORECASE)
                if m:
                    found["headphones"].append(device(m.group(2) or m.group(1), None, [m.group(1), m.group(2)]))
                    break

    def _cameras(self) -> list[dict]:
        cams, seen, left_out = [], set(), []
        base = "/sys/class/video4linux"
        for node in sorted(self._ls(base), key=lambda n: (len(n), n)):
            if not re.match(r"^video\d+$", node):
                continue
            index = (self._read(f"{base}/{node}/index") or "0").strip()
            if index != "0":  # metadata and extra nodes of a camera already counted
                continue
            name = (self._read(f"{base}/{node}/name") or node).strip()
            # v4l2loopback and the like: no hardware behind them
            if "/devices/virtual/" in self.realpath(f"{base}/{node}") or VIRTUAL_CAMERA_RE.search(name):
                left_out.append(f"{node} ({plain(name)})")
                continue
            parent = self.realpath(f"{base}/{node}/device")
            if parent in seen:
                continue
            seen.add(parent)
            modalias = (self._read(f"{base}/{node}/device/modalias") or "").strip()
            cams.append(device(name, f"/dev/{node}", [name, parent, modalias, _usb_id(modalias)]))
        self.evidence["camera"] = "no camera in /sys/class/video4linux" + (
            f" (left out as virtual: {', '.join(left_out)})" if left_out else "")
        return cams

    def _fingerprints(self) -> list[dict]:
        r = self._run(["busctl", "--system", "call", "net.reactivated.Fprint", "/net/reactivated/Fprint/Manager",
                       "net.reactivated.Fprint.Manager", "GetDevices"])
        if not r:
            return []
        out = []
        for path in re.findall(r'"(/net/reactivated/Fprint/Device/\d+)"', r.output):
            n = self._run(["busctl", "--system", "get-property", "net.reactivated.Fprint", path,
                           "net.reactivated.Fprint.Device", "name"])
            m = re.match(r'^s\s+"(.*)"\s*$', n.output.strip()) if n else None
            name = m.group(1) if m else "fingerprint reader"
            out.append(device(name, path, [name, path]))
        return out

    def _accelerometers(self) -> list[dict]:
        out = []
        base = "/sys/bus/iio/devices"
        for node in self._ls(base):
            files = self._ls(f"{base}/{node}")
            if not any(f.startswith("in_accel_") for f in files):
                continue
            name = (self._read(f"{base}/{node}/name") or node).strip()
            out.append(device(name, f"{base}/{node}", [name, self.realpath(f"{base}/{node}")]))
        return out

    def _lids_acpi(self) -> list[dict]:
        base = "/proc/acpi/button/lid"
        return [device(n, f"{base}/{n}", [n]) for n in self._ls(base)]

    def _batteries(self) -> list[dict]:
        out = []
        base = "/sys/class/power_supply"
        for n in self._ls(base):
            if (self._read(f"{base}/{n}/type") or "").strip() != "Battery":
                continue
            if (self._read(f"{base}/{n}/scope") or "").strip() == "Device":  # a mouse's or a pen's
                continue
            maker = (self._read(f"{base}/{n}/manufacturer") or "").strip()
            model = (self._read(f"{base}/{n}/model_name") or "").strip()
            label = f"{n} ({' '.join(x for x in (maker, model) if x)})" if maker or model else n
            out.append(device(label, f"{base}/{n}", [n, maker, model]))
        return out

    def _display_ports(self) -> list[dict]:
        out = []
        for n in self._ls("/sys/class/drm"):
            m = re.match(r"^card\d+-(.+)$", n)
            if not m or m.group(1).startswith(BUILTIN_CONNECTORS):
                continue
            out.append(device(m.group(1), f"/sys/class/drm/{n}", [m.group(1), n]))
        return out

    def _bluetooth(self) -> list[dict]:
        out = []
        for n in self._ls("/sys/class/bluetooth"):
            if not re.match(r"^hci\d+$", n):
                continue
            modalias = (self._read(f"/sys/class/bluetooth/{n}/device/modalias") or "").strip()
            out.append(device(n, f"/sys/class/bluetooth/{n}", [n, modalias, _usb_id(modalias)]))
        return out

    def _wifi(self) -> list[dict]:
        out = []
        for n in self._ls("/sys/class/net"):
            entries = self._ls(f"/sys/class/net/{n}")
            if "wireless" not in entries and "phy80211" not in entries:
                continue
            uevent = self._read(f"/sys/class/net/{n}/device/uevent") or ""
            ids = re.findall(r"^(?:DRIVER|PCI_ID|MODALIAS)=(.*)$", uevent, re.M)
            driver = next(iter(re.findall(r"^DRIVER=(.*)$", uevent, re.M)), "")
            out.append(device(f"{n} ({driver})" if driver else n, f"/sys/class/net/{n}", [n, *ids]))
        return out

    def _swap(self):
        """disk swap at least as big as memory: hibernate can hold its image; zram can't."""
        swaps = parse_swaps(self._read("/proc/swaps", note=True) or "")
        mem = parse_memtotal(self._read("/proc/meminfo", note=True) or "")
        disk = [s for s in swaps if not s["path"].startswith("/dev/zram")]
        total = sum(s["kib"] for s in disk)
        devs = [device(f"{s['path']} ({s['type']}, {_gib(s['kib'])})", s["path"], [s["path"], s["type"]])
                for s in disk]
        if mem is None:
            return devs, None  # can't tell: let the person try
        if not disk:
            return [], f"no disk swap to hibernate to (zram can't hold the image); {_gib(mem)} of memory"
        if total < mem:
            return devs, f"not enough swap to hibernate: {_gib(total)} of disk swap for {_gib(mem)} of memory"
        return devs, None


def load_expect(path) -> list[dict]:
    """the --expect file: [{"kind": "pen", "match": "WACF2200"}, ...]; match is a case-insensitive regex."""
    path = Path(path)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ExpectError([f"can't read {path}: {exc.strerror or exc}"]) from None
    except json.JSONDecodeError as exc:
        raise ExpectError([f"{path} is not valid json: {exc.msg} at line {exc.lineno}"]) from None
    if not isinstance(obj, list):
        raise ExpectError(["the expect file must be a json list of {\"kind\", \"match\"} objects"])
    problems, out = [], []
    for n, e in enumerate(obj, start=1):
        if not isinstance(e, dict) or set(e) - {"kind", "match"}:
            problems.append(f"entry {n} must be an object with only \"kind\" and \"match\"")
            continue
        kind = ALIASES.get(e.get("kind"), e.get("kind"))
        match = e.get("match")
        if kind not in DEVICE_KINDS:
            problems.append(f"entry {n}: unknown kind {e.get('kind')!r} (known: {', '.join(DEVICE_KINDS)})")
        if not isinstance(match, str) or not match.strip():
            problems.append(f"entry {n}: \"match\" must be a non-empty string")
            continue
        if len(match) > MAX_MATCH:
            problems.append(f"entry {n}: \"match\" is longer than {MAX_MATCH} characters")
            continue
        try:
            re.compile(match)
        except re.error as exc:
            problems.append(f"entry {n}: \"match\" isn't a valid pattern: {exc}")
            continue
        out.append({"kind": kind, "match": match})
    if problems:
        raise ExpectError(problems)
    return out


def matches(dev: dict, pattern: str) -> bool:
    text = " ".join([dev["name"], dev.get("path") or "", *dev.get("ids", [])])
    return re.search(pattern, text, re.IGNORECASE) is not None


def build_plan(detected: dict, expect: list[dict] | None = None) -> dict:
    """a plan (the same format as --plan) from detect()'s result."""
    found = detected["devices"]
    evidence = detected.get("evidence", {})
    laptop = bool(found.get("power") or found.get("lid"))
    checks = []

    def add(cid: str, key: str, dev: dict | None, title: str | None = None, preset=None):
        t_title, kind, instructions, expect_text = TEXT[key]
        c = {"id": cid, "title": fit(title or t_title, MAX_TITLE), "kind": kind, "instructions": instructions,
             "expect": expect_text, "device": dev}
        if preset:
            c["preset"] = preset
        checks.append(c)

    for key in ORDER:
        devs = found.get(key, [])
        if not devs and laptop and key in LAPTOP_ALWAYS:
            # a laptop has these; none at all usually means a driver or its firmware didn't load
            title = TEXT[key][0]
            why = evidence.get(key) or "nothing found"
            checks.append({
                "id": key, "title": f"{title}: none detected", "kind": "manual",
                "instructions": f"this is a laptop (it has a battery or a lid), which should have a {title}, "
                                f"but none was detected ({why}). a driver or its firmware not loading is the "
                                "usual cause; add anything you know.",
                "expect": "", "device": None,
                "preset": {"answer": "doesnt-work", "note": f"none detected: {why}"},
            })
        elif key == "camera":
            for i, cam in enumerate(devs, start=1):
                many = len(devs) > 1
                add(f"camera-{i}" if many else "camera", "camera", cam,
                    f"camera {i}: {cam['name']}" if many else None)
        elif key in ("tablet-mode", "rotation"):
            # a 2-in-1 shows itself by either one; check both so a missing driver for the other shows
            if found.get("tablet-mode") or found.get("rotation"):
                add(key, key, devs[0] if devs else None)
        elif key in ("keys", "external-display"):
            if key == "external-display" and devs:
                shown = ", ".join(d["name"] for d in devs[:12])
                more = f" (+{len(devs) - 12} more)" if len(devs) > 12 else ""
                dev = device(f"ports: {shown}{more}", "/sys/class/drm", [d["name"] for d in devs])
            else:
                dev = devs[0] if devs else None
            add(key, key, dev)  # always: every machine has a keyboard and should drive a display
        elif devs:
            add(key, key, devs[0])

    seen_ids: dict[str, int] = {}
    for e in expect or []:
        if any(matches(d, e["match"]) for d in found.get(e["kind"], [])):
            continue
        base = f"missing-{e['kind']}"
        seen_ids[base] = seen_ids.get(base, 0) + 1
        cid = base if seen_ids[base] == 1 else f"{base}-{seen_ids[base]}"
        what = f"expected but not detected: {e['kind']} {e['match']}"
        checks.append({
            "id": cid[:48], "title": fit(what, MAX_TITLE), "kind": "manual",
            "instructions": f"this model should have a {e['kind']} device matching \"{e['match']}\", but none "
                            "was detected. a missing driver or firmware is the usual cause; add anything you know.",
            "expect": "", "device": None,
            "preset": {"answer": "doesnt-work", "note": what},
        })

    hib_devs = found.get("hibernate", [])
    skip = detected.get("hibernate_skip")
    add("hibernate", "hibernate", hib_devs[0] if hib_devs else None,
        preset={"answer": "skip", "note": skip} if skip else None)
    return {"checks": checks}


def summary(detected: dict, plan: dict) -> list[str]:
    """plain lines for the terminal: what was found and what is missing."""
    lines = []
    for c in plan["checks"]:
        dev = c.get("device")
        preset = c.get("preset")
        tail = f": {dev['name']}" if dev else ""
        if preset:
            tail += f" ({preset['answer']}: {preset['note']})"
        lines.append(f"{c['id']}{tail}")
    lines += [f"note: {n}" for n in detected.get("notes", [])]
    return lines
