import json
import os
import unittest
from pathlib import Path
from unittest import mock

from hwchecklib.store import Store, StoreError, atomic_write_json, default_dir

from .fakes import scratch_dir


class AtomicWrites(unittest.TestCase):
    def test_writes_and_replaces_leaving_no_temp_files(self):
        d = scratch_dir(self)
        path = d / "results.json"
        atomic_write_json(path, {"a": 1})
        atomic_write_json(path, {"a": 2, "note": "naïve café"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 2, "note": "naïve café"})
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["results.json"])

    def test_a_failed_write_keeps_the_old_file(self):
        d = scratch_dir(self)
        path = d / "results.json"
        atomic_write_json(path, {"a": 1})
        with mock.patch("hwchecklib.store.os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                atomic_write_json(path, {"a": 2})
        self.assertEqual(json.loads(path.read_text()), {"a": 1})
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["results.json"])

    def test_unserialisable_object_writes_nothing(self):
        d = scratch_dir(self)
        with self.assertRaises(TypeError):
            atomic_write_json(d / "x.json", {"a": object()})
        self.assertEqual(list(d.iterdir()), [])


class StoreFiles(unittest.TestCase):
    def test_default_dir(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "", "HOME": "/home/someone"}):
            self.assertEqual(default_dir(), Path("/home/someone/.local/state/hwcheck"))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/srv/state"}):
            self.assertEqual(default_dir(), Path("/srv/state/hwcheck"))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative", "HOME": "/home/someone"}):
            self.assertEqual(default_dir(), Path("/home/someone/.local/state/hwcheck"))

    def test_save_load_and_archive(self):
        d = scratch_dir(self) / "hwcheck"
        store = Store(d)
        self.assertIsNone(store.load_state())
        store.save({"plan": {"checks": []}, "started": "x"}, {"checks": []})
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
        self.assertEqual(store.load_state()["started"], "x")
        moved = store.archive("20261027T113000")
        self.assertEqual(sorted(p.name for p in moved),
                         ["results-20261027T113000.json", "state-20261027T113000.json"])
        self.assertIsNone(store.load_state())
        store.save({"plan": {"checks": []}}, {"checks": []})
        moved = store.archive("20261027T113000")
        self.assertIn("state-20261027T113000-2.json", [p.name for p in moved])

    def test_a_failed_pid_write_gives_the_lock_back(self):
        for failing in ("pwrite", "ftruncate"):
            with self.subTest(failing=failing):
                store = Store(scratch_dir(self))

                def lock_fds():
                    fds = []
                    for fd in os.listdir("/proc/self/fd"):
                        try:
                            if os.readlink(f"/proc/self/fd/{fd}") == str(store.lock_path):
                                fds.append(fd)
                        except OSError:
                            pass
                    return fds

                with mock.patch(f"hwchecklib.store.os.{failing}",
                                side_effect=OSError(28, "No space left on device")):
                    with self.assertRaises(OSError):
                        store.lock()
                self.assertEqual(lock_fds(), [])  # not leaked
                store.lock()  # and the lock was given back: it can be taken again
                self.assertEqual(len(lock_fds()), 1)
                store.unlock()
                self.assertEqual(lock_fds(), [])

    def test_corrupt_state(self):
        d = scratch_dir(self)
        store = Store(d)
        store.state_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StoreError):
            store.load_state()
        store.state_path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(StoreError):
            store.load_state()


if __name__ == "__main__":
    unittest.main()
