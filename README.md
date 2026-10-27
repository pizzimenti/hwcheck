# hwcheck

an on-screen hardware check for the start of a provisioning round. it serves one
page on 127.0.0.1, opened in the browser of the machine being provisioned, that
walks a person through each check (plays the test sound, shows the camera, asks
them to drag a box, reads the lid and charger state) and records whether it
works, doesn't work or was skipped, with a note. the provisioner reads the
results file afterwards.

by default it builds its checks from the hardware the machine actually has: a
touchscreen check only if there is a touchscreen, one camera check per camera,
and so on. given a list of what the model should have (`--expect`), anything
expected that isn't detected becomes a failing check, so a missing driver shows
up. an explicit plan (`--plan`) replaces detection.

current version: `0.1.0` · see [CHANGELOG.md](CHANGELOG.md)

## run it

```sh
git clone https://github.com/pizzimenti/hwcheck
cd hwcheck
python3 hwcheck.py                                       # detect, then check what was found
python3 hwcheck.py --expect examples/expect-2in1.json    # ...and fail what should be there but isn't
python3 hwcheck.py --detect --print-plan > plan.json     # just show the plan detection builds
python3 hwcheck.py --plan examples/plan-2in1.json        # a plan of your own instead
```

the first line it prints is the page's address (`http://127.0.0.1:8770/?t=…`);
open it in the machine's own browser, in the person's desktop session. the page
fetches nothing from anywhere else, and the camera and microphone work because
`127.0.0.1` counts as a secure origin.

the `t` is a secret made for this run: the page sends it with every request,
and hwcheck refuses any request without it, so another local user can't read
the notes, answer, or set off a hibernate. on first load the page moves it from
the address bar into the tab's session storage, so it isn't left on show or in
a copied address.

how it's handed to the browser matters. a command line is readable by every
local user (`/proc/<pid>/cmdline`, unless `/proc` is mounted with `hidepid`),
so `chromium 'http://127.0.0.1:8770/?t=…'` shows the secret to them for as long
as that process runs. instead, open the page hwcheck writes for this, which
only its owner can read and which forwards to the address:

```sh
chromium "file://$HOME/.local/state/hwcheck/open.html"
```

it is written (mode 0600) when the server starts and removed when it stops,
including on sigterm or sighup; one left by a crash or a fresh boot is removed
by the next run that takes the directory's lock.
pasting the address into a browser that's already open works as well. the
limits, plainly: the secret is on hwcheck's own output, so anyone who can see
that terminal has it; and it does nothing against programs running as the same
user, which can read the state files and that page anyway.

| option | what it does |
|---|---|
| `--detect` | build the plan from the hardware this machine has (below). the default without `--plan`. |
| `--expect FILE` | with detection: a json list of devices this model should have; each one not detected becomes a failing check. |
| `--print-plan` | with detection: print the plan it builds (a valid `--plan` file) and exit, serving nothing and writing no state. |
| `--plan P` | use this check plan (json, below) instead of detecting. |
| `--resume` | carry on with the saved run where it stopped: after hibernate, a timeout or a stop. without `--plan` it uses the saved run's own plan and doesn't detect again (a fresh boot could detect differently). |
| `--new` | start over although an unfinished run is saved; its files are kept, renamed `state-<started>.json` and `results-<started>.json`. |
| `--port N` | port on 127.0.0.1 (default 8770; `0` picks a free one). |
| `--timeout S` | stop after S seconds awake, answered or not (default: no limit). time asleep doesn't count. |
| `--state-dir D` | where state and results go (default `~/.local/state/hwcheck/`, or `$XDG_STATE_HOME/hwcheck/`). |

it exits by itself after the last answer. exit codes: `0` every check answered,
`1` couldn't start (port busy, state unreadable, another hwcheck running with
the same state directory), `2` bad plan or usage
(including an unfinished run without `--resume` or `--new`), `3` stopped by
`--timeout`, `129` sighup (its terminal or tmux pane closed), `130` ctrl-c,
`143` sigterm. on any of these it saves, removes `open.html` and exits
cleanly; after `3`, `129`, `130` or `143`, run it again
with `--resume`.

files, under `~/.local/state/hwcheck/` (never /tmp, which a boot clears), both
written atomically after every answer and every hibernate step:

- `results.json`: what the provisioner reads (format below).
- `state.json`: hwcheck's own: the plan, the check on screen, the answers and the
  hibernate state.
- `open.html`: the private hand-off page above, only while the server runs.
- `lock`: held for the whole run (the kernel drops it when the process ends,
  however it ends), so a second hwcheck on the same directory refuses at once
  and touches nothing: not the running one's `open.html`, state or results.

## what it checks

| kind | where | what happens |
|---|---|---|
| `speakers` | page | left, right and both tones (440, 660, 550 hz), panned. |
| `headphones` | page | the same tones, with the person asked to plug in headphones. |
| `microphone` | page | a live level meter; record 3 s and play it back. |
| `camera` | page | a live preview; "next camera" steps through every camera found. |
| `touch` | page | drag a box to a target; touches now and most at once are counted. |
| `pen` | page | a drawing pad; line width follows pressure; pressure, tilt, twist and buttons shown. |
| `keys` | page | every key press shown live, with the set of keys seen so far. |
| `rotation` | server | `monitor-sensor --accel`, kept running only while the check is on screen. |
| `tablet-mode` | server | the tablet-mode switch in `/proc/bus/input/devices`, its state read over evdev when the user may, else plasma's view (`busctl --user … TabletModeManager tabletMode`). |
| `lid` | server | `upower -d`: lid present, open or closed. |
| `power` | server | `upower -d`: on battery or mains, charger, each battery's level and state. |
| `external-display` | server | `kscreen-doctor -o`: every output, connected and on, with its mode. |
| `bluetooth` | server | `bluetoothctl show` and `bluetoothctl devices Connected`. |
| `hibernate` | both | always the last check; see below. |
| `manual` | page | instructions only; the person looks and answers. |

the server reads its checks about once a second while the check is on screen
and pushes them to the page; every change it sees during the check is kept and
saved with the answer, so "unplug the charger, then plug it back in" leaves
`on mains → on battery → on mains` in the results. when a tool is missing or
fails, the page shows "can't read" and why, and the check stays manual: the
person answers by what they see.

the camera and microphone streams stop when their check ends (an answer, or
choosing another check) and when the page closes. the person can choose any
check from the list to answer it again; hibernate opens only when every other
check has an answer.

## detection

detection only reads, as the person running it (no root): `/proc`, `/sys`, and
two tools over the user's own rights. a source it can't read counts as nothing
there, and is noted in the terminal. every check it makes carries the device it
was made for (`device`: name, path and the ids `--expect` matches against), on
the page and in the results. names are stripped of escape codes and control
characters before they're shown or printed.

on a laptop (a battery or a lid), speakers, microphone and camera checks are
always made: if none is detected, the check is titled "speakers: none
detected" and starts as doesn't work, with the evidence (for example
`no playback device in /proc/asound/pcm; no sound card in /proc/asound/cards`),
because a laptop without them has usually lost a driver or its firmware.

asking fprintd which readers it has goes over d-bus, which can start fprintd on
demand; it stops itself when idle. if fprintd isn't installed, there is no
fingerprint check (list the reader in `--expect` to have its absence fail).

| check | source | made when |
|---|---|---|
| speakers | `/proc/asound/pcm`, `/proc/asound/cards` | a playback pcm that isn't hdmi or displayport |
| microphone | `/proc/asound/pcm` | a capture pcm (a laptop's dmic is preferred as its device) |
| headphones | the jack switch in `/proc/bus/input/devices` (`SW_HEADPHONE_INSERT`), else `pactl list sinks` | a headphone jack switch, or a sink port named headphones |
| camera (one per camera) | `/sys/class/video4linux/video*` (`name`, `index`, `device`) | a capture node (index 0), one per physical device, so metadata nodes don't count twice; virtual nodes (v4l2loopback and the like) left out |
| touch | `/proc/bus/input/devices` | a direct (`INPUT_PROP_DIRECT`) absolute device that isn't a pen |
| pen | `/proc/bus/input/devices` | `BTN_TOOL_PEN`, `ABS_TILT_X`, or a name ending in pen or stylus |
| keys | `/proc/bus/input/devices` | always; its device is the first input with q, a, z and space |
| touchpad | `/proc/bus/input/devices` | `INPUT_PROP_POINTER` with `BTN_TOOL_FINGER` and multitouch |
| fingerprint | `busctl --system call net.reactivated.Fprint … GetDevices` (d-bus; may start fprintd) | fprintd lists a reader; skipped if fprintd is absent |
| tablet-mode, rotation | the tablet-mode switch (`SW_TABLET_MODE`) in `/proc/bus/input/devices`; an accelerometer in `/sys/bus/iio/devices` (`in_accel_*`) | either one: both checks then, so a 2-in-1 missing the driver for the other shows it |
| power (charger) | `/sys/class/power_supply/*/type` | a battery that isn't a peripheral's (`scope` not `Device`) |
| lid | `/proc/acpi/button/lid/*`, else the lid switch (`SW_LID`) | a lid |
| external-display | `/sys/class/drm/card*-*` | always; its device lists the ports (`eDP`, `LVDS`, `DSI` left out) |
| bluetooth | `/sys/class/bluetooth/hci*` | a controller |
| wifi | `/sys/class/net/*/` (`wireless` or `phy80211`) | a wireless interface |
| hibernate | `/proc/swaps`, `/proc/meminfo` | always, last; skipped from the start, saying why, unless disk swap (zram doesn't count) is at least the size of memory |

touchpad and wifi are manual checks (look, then answer); the rest run as their
kinds below.

### expected devices

```json
[
  {"kind": "pen", "match": "WACF2200"},
  {"kind": "camera", "match": "Integrated I"},
  {"kind": "battery", "match": "BAT0"}
]
```

`kind` is a check's name from the table (or `touchscreen`, `stylus`,
`keyboard`, `webcam`, `accelerometer`, `battery`, `display`, `swap`, `mic`).
`match` is a case-insensitive regular expression searched in every detected
device of that kind: its name, path and ids (for an input device, its phys
and sysfs path and `vendor:product`; for a usb camera or bluetooth controller,
its `vendor:product`). each entry no device matches becomes a check titled
`expected but not detected: pen WACF2200`, recorded as doesn't work from the
start. a title longer than 120 characters is shortened; the note keeps the
whole pattern, which may be up to 200 characters.
[examples/expect-2in1.json](examples/expect-2in1.json) lists the sample
2-in-1's devices. `--detect --print-plan` shows every detected device's ids,
which is the easiest way to write one.

## hibernate and --resume

hibernate runs last. pressing "hibernate now" saves `hibernate pending`, the
boot id (`/proc/sys/kernel/random/boot_id`) and how long the machine has slept
since boot to disk, then runs `systemctl hibernate` a second later (not at all
if the person answers in that second). `systemctl` returns as soon as the job
is queued, so until a sleep has actually been seen the answer can only be
doesn't work or skip.

- **it resumes.** the server and the page come back with everything else. the
  server sees the machine slept (boot time ran on while monotonic time stood
  still) and the page asks whether everything came back: wifi, sound, keyboard,
  touch, pen.
- **it boots fresh.** start it again with `--resume`. a new boot id means the
  hibernate image wasn't restored: the check is recorded as doesn't work at
  once, and the page asks the person for a note on what they saw.
- **the server was stopped but the machine didn't reboot.** `--resume` sees the
  same boot id and compares the time asleep with what was saved at the press:
  if the machine slept, it asks whether everything came back; if it didn't,
  hibernate never happened, and the page says so and offers to try again.
- **`systemctl hibernate` refuses** (no swap, an inhibitor): the page shows its
  message and offers "try hibernate again"; the answer can then only be doesn't
  work or skip.

in every case the results keep the boot ids before and after, the seconds
slept when the server saw it, and the kernel log's hibernation lines from this
boot (`journalctl -k -b 0`) and, after a fresh boot, from the boot before
(`-b -1`).

## the plan

```json
{
  "machine": "optional: the name to record (default: the hostname)",
  "model": "optional: the model to record (default: read from /sys/class/dmi/id)",
  "checks": [
    {
      "id": "speakers",
      "title": "speakers",
      "kind": "speakers",
      "instructions": "what the person should do",
      "expect": "what should happen"
    }
  ]
}
```

- `id`: lowercase letters, digits, `-` or `_`, at most 48; unique.
- `title`: shown in the list and as the heading.
- `kind`: one of the kinds above.
- `instructions`, `expect`: optional text shown with the check.
- `device`: optional; what the check was made for, `{"name", "path", "ids"}`.
  detection fills it in.
- `preset`: optional; an answer recorded when the run starts,
  `{"answer": "doesnt-work" or "skip", "note": "why"}`. detection uses it for
  expected devices it didn't find, a laptop's missing speakers, microphone or
  camera, and hibernate without enough swap. a plan can't preset works, and the
  person can answer a preset check again only as doesn't work or skip; the
  preset's evidence stays in its `live` either way.
- at most one `hibernate` check, and it must be the last.

a bad plan is refused before anything starts, with every problem listed
(unknown keys too, so a typo like `"instruction"` doesn't pass silently).
`--resume` needs the same checks (ids and kinds) as the saved run; the texts
may change.

## the results

```json
{
  "hwcheck": "0.1.0",
  "machine": "the hostname, or the plan's machine",
  "model": "vendor, product name and version from dmi, or the plan's model",
  "boot_id": "the boot this run is in now",
  "plan": "the plan's path, or \"detected\"",
  "started": "2026-10-27T14:02:11-07:00",
  "updated": "…",
  "finished": "… or null while checks are left",
  "checks": [
    {
      "id": "power",
      "title": "charger",
      "kind": "power",
      "device": "what the check was made for ({name, path, ids}), or null",
      "answer": "works | doesnt-work | skip | null (not answered yet)",
      "note": "what the person typed",
      "at": "when they answered",
      "live": "what hwcheck saw while the check ran, or null"
    }
  ]
}
```

every check in the plan is listed, in plan order. `live` depends on the kind
(a preset check has `{"source": "preset", "evidence": "…"}`):

- **page checks**: `{"source": "page", …}` with what the page measured: the
  tones played, the microphone's device and peak level, cameras found, the most
  touches at once, pen pressure and tilt, the keys seen.
- **server checks**: the last reading (`read`, `source`, `summary`, `lines`,
  `state`, `at`, or `error` when it couldn't read) and `seen`: each change
  during the check with its time.
- **hibernate**: `phase` (`resumed`, `fresh-boot`, `failed`, `unknown`,
  `hibernating`, `idle`), `resumed`, `boot_id_before`, `boot_id_after`,
  `hibernated_at`, `slept_seconds`, `error` and `kernel_log`.

## a sample plan for a 2-in-1

this is [examples/plan-2in1.json](examples/plan-2in1.json), written by hand:
every kind, in an order a person can follow with the machine on a desk, then
folded, then hibernated. detection on the same machine builds much the same
plan, with one check per camera and a device on each.

<details>
<summary>examples/plan-2in1.json</summary>

```json
{
  "checks": [
    {
      "id": "speakers",
      "title": "speakers",
      "kind": "speakers",
      "instructions": "turn the volume to about half, then play the left, right and both tones.",
      "expect": "left only from the left speaker, right only from the right, both from both, with no crackle"
    },
    {
      "id": "microphone",
      "title": "microphone",
      "kind": "microphone",
      "instructions": "start the microphone and speak, then record 3 s and listen to it played back.",
      "expect": "the bar moves when you speak and the recording is clear"
    },
    {
      "id": "headphones",
      "title": "headphone jack",
      "kind": "headphones",
      "instructions": "plug wired headphones into the jack and play the tones again.",
      "expect": "the sound moves to the headphones when they're plugged in and back to the speakers when they're not"
    },
    {
      "id": "camera",
      "title": "camera",
      "kind": "camera",
      "instructions": "start the camera. if there is a second camera, switch to it too.",
      "expect": "a live, clear picture from each camera"
    },
    {
      "id": "touch",
      "title": "touchscreen",
      "kind": "touch",
      "instructions": "drag the box to the target with a finger, then rest several fingers on the grey area.",
      "expect": "the box follows your finger and at least 5 touches are counted"
    },
    {
      "id": "pen",
      "title": "pen",
      "kind": "pen",
      "instructions": "draw on the pad with the pen, lightly and then hard, and tilt it.",
      "expect": "lines get thicker as you press harder and the tilt numbers change"
    },
    {
      "id": "keys",
      "title": "keyboard",
      "kind": "keys",
      "instructions": "press every key, including the function row and the arrows. try the volume and brightness keys too.",
      "expect": "every key shows up; volume and brightness change on screen"
    },
    {
      "id": "touchpad",
      "title": "touchpad",
      "kind": "manual",
      "instructions": "move the pointer, tap to click, scroll with two fingers and right-click.",
      "expect": "all of it works smoothly"
    },
    {
      "id": "tablet-mode",
      "title": "tablet mode",
      "kind": "tablet-mode",
      "instructions": "fold the screen all the way back into tablet mode, then open it back to a laptop.",
      "expect": "tablet mode turns on when folded and off when opened; the keyboard stops typing while folded"
    },
    {
      "id": "rotation",
      "title": "screen rotation",
      "kind": "rotation",
      "instructions": "in tablet mode, turn the machine onto each side and upside down.",
      "expect": "the orientation follows the machine and the screen turns with it"
    },
    {
      "id": "power",
      "title": "charger",
      "kind": "power",
      "instructions": "unplug the charger, wait a few seconds, then plug it back in.",
      "expect": "it goes on battery when unplugged and back to mains, charging, when plugged in"
    },
    {
      "id": "lid",
      "title": "lid",
      "kind": "lid",
      "instructions": "close the lid for a few seconds, then open it. if the machine sleeps, wake it.",
      "expect": "the lid is seen closed then open, and the screen comes back"
    },
    {
      "id": "external-display",
      "title": "external display",
      "kind": "external-display",
      "instructions": "plug in an external display (usb-c or hdmi).",
      "expect": "the display is seen as connected and shows a picture"
    },
    {
      "id": "bluetooth",
      "title": "bluetooth",
      "kind": "bluetooth",
      "instructions": "connect a bluetooth device (headphones or a mouse) from the system's bluetooth settings.",
      "expect": "the device shows as connected and works"
    },
    {
      "id": "wifi",
      "title": "wifi",
      "kind": "manual",
      "instructions": "look at the network icon in the panel.",
      "expect": "wifi is connected with a good signal"
    },
    {
      "id": "hibernate",
      "title": "hibernate",
      "kind": "hibernate",
      "instructions": "this runs last. the machine hibernates and powers off; turn it back on with the power button.",
      "expect": "it comes back where it left off, with this page still open"
    }
  ]
}
```

</details>

## its results

[examples/results-2in1.json](examples/results-2in1.json) is that plan run end to
end against the recorded tool outputs the tests use (a ThinkPad X1 Yoga's), at a
person's pace: the charger pulled and replugged, the lid closed and opened, the
screen folded and turned, the machine hibernated and resumed.

<details>
<summary>examples/results-2in1.json</summary>

```json
{
  "hwcheck": "0.1.0",
  "machine": "testbook",
  "model": "LENOVO 21CD0019US ThinkPad X1 Yoga Gen 7",
  "boot_id": "6f1d2c3b-4a59-4e8f-9d7c-1b2a3c4d5e6f",
  "plan": "examples/plan-2in1.json",
  "started": "2026-10-27T14:02:11-07:00",
  "updated": "2026-10-27T14:15:35-07:00",
  "finished": "2026-10-27T14:15:35-07:00",
  "checks": [
    {
      "id": "speakers",
      "title": "speakers",
      "kind": "speakers",
      "device": null,
      "answer": "works",
      "note": "both sides clear",
      "at": "2026-10-27T14:02:51-07:00",
      "live": {
        "source": "page",
        "tones_played": [
          "left",
          "right",
          "both"
        ]
      }
    },
    {
      "id": "microphone",
      "title": "microphone",
      "kind": "microphone",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:03:48-07:00",
      "live": {
        "source": "page",
        "device": "Built-in Audio Analog Stereo",
        "peak_level": 0.71,
        "recorded": true,
        "played_back": true
      }
    },
    {
      "id": "headphones",
      "title": "headphone jack",
      "kind": "headphones",
      "device": null,
      "answer": "skip",
      "note": "no wired headphones to hand",
      "at": "2026-10-27T14:04:10-07:00",
      "live": {
        "source": "page",
        "tones_played": []
      }
    },
    {
      "id": "camera",
      "title": "camera",
      "kind": "camera",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:04:47-07:00",
      "live": {
        "source": "page",
        "cameras_seen": [
          "Integrated Camera (1280×720)"
        ],
        "cameras_found": 2
      }
    },
    {
      "id": "touch",
      "title": "touchscreen",
      "kind": "touch",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:05:34-07:00",
      "live": {
        "source": "page",
        "max_touches_seen": 10,
        "reported_max_touch_points": 10,
        "reached_target": true,
        "dragged_with": "touch"
      }
    },
    {
      "id": "pen",
      "title": "pen",
      "kind": "pen",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:06:46-07:00",
      "live": {
        "source": "page",
        "pointer_types": [
          "pen"
        ],
        "max_pressure": 0.98,
        "pressure_levels_seen": 87,
        "max_tilt_degrees": 52,
        "eraser_seen": false,
        "side_button_seen": true,
        "hover_seen": true
      }
    },
    {
      "id": "keys",
      "title": "keyboard",
      "kind": "keys",
      "device": null,
      "answer": "doesnt-work",
      "note": "f12 does nothing",
      "at": "2026-10-27T14:09:18-07:00",
      "live": {
        "source": "page",
        "keys_seen": 84,
        "presses": 97,
        "codes": [
          "KeyA"
        ]
      }
    },
    {
      "id": "touchpad",
      "title": "touchpad",
      "kind": "manual",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:09:50-07:00",
      "live": null
    },
    {
      "id": "tablet-mode",
      "title": "tablet mode",
      "kind": "tablet-mode",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:10:17-07:00",
      "live": {
        "read": true,
        "source": "/proc/bus/input/devices",
        "summary": "tablet mode: on",
        "lines": [
          "Intel HID switches: tablet mode on"
        ],
        "state": {
          "switches": [
            {
              "name": "Intel HID switches",
              "event": "event14",
              "on": true
            }
          ],
          "switch_on": true,
          "plasma": null
        },
        "at": "2026-10-27T14:10:07-07:00",
        "seen": [
          {
            "at": "2026-10-27T14:09:50-07:00",
            "summary": "tablet mode: off"
          },
          {
            "at": "2026-10-27T14:09:58-07:00",
            "summary": "tablet mode: on"
          },
          {
            "at": "2026-10-27T14:10:03-07:00",
            "summary": "tablet mode: off"
          },
          {
            "at": "2026-10-27T14:10:07-07:00",
            "summary": "tablet mode: on"
          }
        ]
      }
    },
    {
      "id": "rotation",
      "title": "screen rotation",
      "kind": "rotation",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:10:45-07:00",
      "live": {
        "read": true,
        "source": "monitor-sensor",
        "summary": "orientation: normal",
        "lines": [
          "orientation: normal",
          "tilt: vertical"
        ],
        "state": {
          "accelerometer": true,
          "orientation": "normal",
          "tilt": "vertical"
        },
        "at": "2026-10-27T14:10:32-07:00",
        "seen": [
          {
            "at": "2026-10-27T14:10:17-07:00",
            "summary": "orientation: normal"
          },
          {
            "at": "2026-10-27T14:10:23-07:00",
            "summary": "orientation: left-up"
          },
          {
            "at": "2026-10-27T14:10:26-07:00",
            "summary": "orientation: bottom-up"
          },
          {
            "at": "2026-10-27T14:10:29-07:00",
            "summary": "orientation: right-up"
          },
          {
            "at": "2026-10-27T14:10:32-07:00",
            "summary": "orientation: normal"
          }
        ]
      }
    },
    {
      "id": "power",
      "title": "charger",
      "kind": "power",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:11:09-07:00",
      "live": {
        "read": true,
        "source": "upower -d",
        "summary": "on mains, BAT0 85% charging",
        "lines": [
          "running on: mains",
          "charger: connected",
          "BAT0: 85%, charging"
        ],
        "state": {
          "on_battery": false,
          "charger": true,
          "batteries": [
            {
              "name": "BAT0",
              "state": "charging",
              "percentage": 85
            }
          ]
        },
        "at": "2026-10-27T14:11:01-07:00",
        "seen": [
          {
            "at": "2026-10-27T14:10:47-07:00",
            "summary": "on mains, BAT0 85% charging"
          },
          {
            "at": "2026-10-27T14:10:52-07:00",
            "summary": "on battery, BAT0 84% discharging"
          },
          {
            "at": "2026-10-27T14:11:01-07:00",
            "summary": "on mains, BAT0 85% charging"
          }
        ]
      }
    },
    {
      "id": "lid",
      "title": "lid",
      "kind": "lid",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:11:32-07:00",
      "live": {
        "read": true,
        "source": "upower -d",
        "summary": "lid open",
        "lines": [
          "lid open"
        ],
        "state": {
          "present": true,
          "closed": false
        },
        "at": "2026-10-27T14:11:20-07:00",
        "seen": [
          {
            "at": "2026-10-27T14:11:09-07:00",
            "summary": "lid open"
          },
          {
            "at": "2026-10-27T14:11:14-07:00",
            "summary": "lid closed"
          },
          {
            "at": "2026-10-27T14:11:20-07:00",
            "summary": "lid open"
          }
        ]
      }
    },
    {
      "id": "external-display",
      "title": "external display",
      "kind": "external-display",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:11:59-07:00",
      "live": {
        "read": true,
        "source": "kscreen-doctor -o",
        "summary": "external display: HDMI-A-1 on, 1920x1080@60",
        "lines": [
          "eDP-1 (built in): connected, on, 2880x1800@120",
          "HDMI-A-1 (external): connected, on, 1920x1080@60",
          "DP-1 (external): not connected, off"
        ],
        "state": {
          "outputs": [
            {
              "id": "1",
              "name": "eDP-1",
              "type": "Panel",
              "enabled": true,
              "connected": true,
              "mode": "2880x1800@120",
              "geometry": "0,0 1440x900",
              "builtin": true
            },
            {
              "id": "2",
              "name": "HDMI-A-1",
              "type": "HDMI",
              "enabled": true,
              "connected": true,
              "mode": "1920x1080@60",
              "geometry": "1440,0 1920x1080",
              "builtin": false
            },
            {
              "id": "3",
              "name": "DP-1",
              "type": "DisplayPort",
              "enabled": false,
              "connected": false,
              "mode": null,
              "geometry": "0,0 0x0",
              "builtin": false
            }
          ],
          "external_connected": 1
        },
        "at": "2026-10-27T14:11:32-07:00",
        "seen": [
          {
            "at": "2026-10-27T14:11:32-07:00",
            "summary": "external display: HDMI-A-1 on, 1920x1080@60"
          }
        ]
      }
    },
    {
      "id": "bluetooth",
      "title": "bluetooth",
      "kind": "bluetooth",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:13:01-07:00",
      "live": {
        "read": true,
        "source": "bluetoothctl",
        "summary": "2 connected: WH-1000XM4, MX Master 3",
        "lines": [
          "controller: testbook (powered on)",
          "connected: WH-1000XM4 (38:18:4C:00:00:02)",
          "connected: MX Master 3 (D4:2F:00:00:00:03)"
        ],
        "state": {
          "controller": {
            "name": "testbook",
            "powered": true
          },
          "connected": [
            {
              "address": "38:18:4C:00:00:02",
              "name": "WH-1000XM4"
            },
            {
              "address": "D4:2F:00:00:00:03",
              "name": "MX Master 3"
            }
          ]
        },
        "at": "2026-10-27T14:11:59-07:00",
        "seen": [
          {
            "at": "2026-10-27T14:11:59-07:00",
            "summary": "2 connected: WH-1000XM4, MX Master 3"
          }
        ]
      }
    },
    {
      "id": "wifi",
      "title": "wifi",
      "kind": "manual",
      "device": null,
      "answer": "works",
      "note": "",
      "at": "2026-10-27T14:13:18-07:00",
      "live": null
    },
    {
      "id": "hibernate",
      "title": "hibernate",
      "kind": "hibernate",
      "device": null,
      "answer": "works",
      "note": "came back with wifi and sound",
      "at": "2026-10-27T14:15:35-07:00",
      "live": {
        "source": "hibernate",
        "phase": "resumed",
        "resumed": true,
        "boot_id_before": "6f1d2c3b-4a59-4e8f-9d7c-1b2a3c4d5e6f",
        "boot_id_after": "6f1d2c3b-4a59-4e8f-9d7c-1b2a3c4d5e6f",
        "hibernated_at": "2026-10-27T14:13:40-07:00",
        "slept_seconds": 47,
        "error": null,
        "kernel_log": {
          "this_boot": {
            "read": true,
            "source": "journalctl -k -b 0",
            "lines": [
              "[    0.412345] testbook kernel: PM: Registering ACPI NVS region [mem 0x7a5a1000-0x7a7fefff] (2482176 bytes)",
              "[ 1234.567890] testbook kernel: PM: hibernation: hibernation entry",
              "[ 1235.001234] testbook kernel: PM: hibernation: Preallocating image memory",
              "[ 1236.789012] testbook kernel: PM: hibernation: Allocated 812345 pages for snapshot",
              "[ 1236.900000] testbook kernel: PM: hibernation: Creating image:",
              "[ 1237.100000] testbook kernel: PM: Image restored successfully.",
              "[ 1237.200000] testbook kernel: PM: hibernation: Basic memory bitmaps freed",
              "[ 1237.500000] testbook kernel: PM: hibernation: hibernation exit"
            ]
          }
        }
      }
    }
  ]
}
```

</details>

## requirements

- python 3.11 or later; nothing outside the standard library.
- chromium on the machine.
- for the server-read checks, whichever of these the machine has; without one,
  its check stays manual: `iio-sensor-proxy` (monitor-sensor), `upower`,
  `kscreen-doctor` (plasma), `bluez-utils` (bluetoothctl), systemd
  (`systemctl`, `journalctl`, `busctl`).

## what it doesn't do

- it listens on 127.0.0.1 only, refuses requests without this run's secret or
  carrying another host name or another site's origin, and sends nothing off
  the machine.
- it starts nothing else: the only other process it runs for longer than a
  read is `monitor-sensor`, while the rotation check is on screen. (asking
  fprintd for its readers may start fprintd by d-bus activation; it stops
  itself when idle.)
- it needs no root. the tablet-mode switch's state is readable only with access
  to `/dev/input/event*` (root or the `input` group), so usually plasma's view
  is shown instead, or the switch is shown as present and the check is manual.
- it doesn't open the browser or start itself in the person's session; the
  provisioner does that.

## tests

```sh
python3 -m unittest      # or: python3 -m pytest
```

every system reader is faked with recorded outputs in `tests/fixtures/`; no
test touches a device or runs `systemctl hibernate`. detection runs on three
recorded machines (`tests/fixtures/machine-*.json`): the sample 2-in-1, a
desktop with no battery, lid, touch or camera, and the 2-in-1 with its pen
missing. `tests/test_server.py` drives the http endpoints through the whole
sample plan, a detected plan, and `--resume` after a same-boot and a
fresh-boot hibernate.

## license

MIT; see [LICENSE](LICENSE).
