# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).
Each version's heading says whether it was a major, minor or patch change.

## [Unreleased]

### Fixed

- Saving is all or nothing: `state.json` and `results.json` are each written
  in full, fsynced, to a temporary file before either is renamed into place,
  and if the second rename fails the old `state.json` is put back. Previously
  a full disk while writing `results.json` left a new `state.json` beside the
  old results, while memory had rolled back.
- If the mark saved just before `systemctl hibernate` can't be written (a full
  disk), hibernate is now recorded as failed with the reason, and pressing it
  again works. Previously the thread gave up silently, the page still said
  pending, and a second press was refused as already pending.
- The time asleep is now measured from that mark, not from the press, so a
  suspend in the second before `systemctl` runs no longer counts as time
  hibernated; and a `systemctl` that refuses after a sleep was seen leaves
  hibernate failed, not resumed, so "works" can't be recorded for it.
- SIGTERM and SIGHUP handlers now only record the signal; the serve loop
  stops the run. Previously the handler called stop(), which takes locks
  (the stopping event's own among them), so a signal arriving while the main
  thread held one could hang hwcheck.

## [0.1.0] - 2026-10-27 (minor)

### Added

- `hwcheck.py --plan P [--resume | --new] [--port N] [--timeout S]`: a
  standard-library server on 127.0.0.1 with one local page that walks a person
  through a plan of hardware checks on a freshly provisioned laptop and records
  works, doesn't work or skip, with a note, for each.
- Checks run on the page: speakers (left, right, both tones), headphone jack
  (the same tones), microphone (live level, record 3 s and play back), camera
  (live preview, switching between cameras), touch (drag a box to a target;
  touches counted), pen (a drawing pad showing pressure, tilt, twist and
  buttons) and keys (each press shown live). The camera and microphone stop
  when their check ends.
- Checks read by the server and pushed to the page as live state, with every
  change seen during the check kept: rotation (`monitor-sensor`, kept running
  under a pty only while its check is on screen), tablet mode (the switch in
  `/proc/bus/input/devices`, its state over evdev where readable, else
  Plasma's own view), lid and power source (`upower -d`), external display
  (`kscreen-doctor -o`) and Bluetooth (`bluetoothctl show`, `bluetoothctl
  devices Connected`). A missing or failing tool shows "can't read" and the
  check stays manual.
- Hibernate, always the last check: pressing it saves "hibernate pending", the
  boot id and the time slept so far, then runs `systemctl hibernate` a second
  later unless the person has answered meanwhile, saving an `action_started`
  mark just before it does; a pending hibernate without that mark never ran,
  and is never taken for a resume. "Works" is accepted only once a sleep has
  been seen. A server that survives the hibernate notices the
  sleep and asks whether everything came back; `--resume` after a restart
  compares boot ids (a fresh boot is recorded as doesn't work, with the kernel
  log's hibernation lines from both boots) and, in the same boot, the time
  slept (none means it never hibernated, and it can be tried again).
- A secret made for each run, in the printed address (`?t=`), required on
  every api request, beside the host and origin checks. The page moves it
  into session storage and out of the address bar, and says so plainly when
  the run behind it was restarted. A private hand-off page (`open.html`, 0600)
  lets a browser be started without the secret on its command line; it is
  removed when the run stops, on SIGTERM (exit 143) and SIGHUP (129) too, and
  a stale one is removed by the next run that takes the state directory.
- One run per state directory: a lock held for the whole run (released by the
  kernel however the process ends). A second hwcheck on the same directory
  refuses at once and touches nothing of the running one.
- Detection, the default without `--plan`: the plan is built read-only, as
  the person, from the hardware the machine has (input devices, sound cards,
  cameras, fprintd, iio accelerometers, lid, batteries, display ports,
  bluetooth, wifi, and swap against memory for hibernate). Each check carries
  the device it was made for, on the page and in the results. `--expect FILE`
  lists what the model should have; each expected device not detected becomes
  a failing check. On a laptop (a battery or a lid), speakers, microphone and
  camera checks are always made, and start as doesn't work with the evidence
  when none is detected. `--detect --print-plan` prints the plan and exits.
  Plans may carry `device` and `preset` (an answer recorded at the start; a
  preset check can be answered again only as doesn't work or skip, and keeps
  its evidence).
- State and results are written atomically to `~/.local/state/hwcheck/` after
  every answer, and never once the run has finished or begun to stop (the
  hibernate thread included), so nothing lands after the directory's lock is
  given up; an unfinished run must be resumed or explicitly replaced
  (`--new`), and earlier runs are kept, renamed.
- The plan and results formats, a sample plan for a 2-in-1 and its results,
  in the README and `examples/`.
- Tests on recorded tool outputs, including a browser-free run of a whole plan
  through the http endpoints and same-boot and fresh-boot `--resume` runs.

[Unreleased]: https://github.com/pizzimenti/hwcheck/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pizzimenti/hwcheck/releases/tag/v0.1.0
