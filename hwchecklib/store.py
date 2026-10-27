"""state and results on disk, written atomically under ~/.local/state/hwcheck/."""

from __future__ import annotations

import fcntl
import html
import json
import os
import secrets
import tempfile
from pathlib import Path


class StoreError(RuntimeError):
    pass


class StoreBusy(StoreError):
    """another hwcheck holds this state directory."""


def default_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or ""
    root = Path(base) if base.startswith("/") else Path.home() / ".local" / "state"
    return root / "hwcheck"


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_temp(path: Path, data: bytes) -> str:
    """data in full, fsynced, in a temporary file beside path (0600); its name is returned."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        _unlink(tmp)
        raise
    return tmp


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def atomic_write_json(path: Path, obj) -> None:
    """write obj as json to path so a reader sees the old file or the new one, never half."""
    atomic_write_bytes(path, _json_bytes(obj))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """the same for any bytes; the file is the owner's alone (0600)."""
    path = Path(path)
    tmp = _write_temp(path, data)
    try:
        os.replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class Store:
    """state.json is hwcheck's own; results.json is what the provisioner reads."""

    def __init__(self, directory=None):
        self.dir = Path(directory) if directory else default_dir()
        self.state_path = self.dir / "state.json"
        self.results_path = self.dir / "results.json"
        self.handoff_path = self.dir / "open.html"
        self.lock_path = self.dir / "lock"
        self._lock_fd: int | None = None

    def lock(self) -> None:
        """hold this directory for this run. the kernel drops the lock when the process ends,
        however it ends, so a crash never leaves the directory stuck."""
        self.ensure()
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                holder = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
            except OSError:
                holder = ""
            os.close(fd)
            who = f"another hwcheck (pid {holder})" if holder.isdigit() else "another hwcheck"
            raise StoreBusy(f"{who} is running with {self.dir}; stop it, or pass another --state-dir") from None
        except BaseException:
            os.close(fd)
            raise
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{os.getpid()}\n".encode("ascii"), 0)
        except BaseException:  # a full disk, say: give the lock back rather than leak it
            os.close(fd)
            raise
        self._lock_fd = fd

    def unlock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing it releases the lock; the file stays, harmlessly
            self._lock_fd = None

    def write_handoff(self, url: str) -> Path:
        """a page only the owner can read that forwards to the address with its secret, so a
        browser can be started with a file path on its command line instead of the secret."""
        self.ensure()
        safe = html.escape(url, quote=True)
        page = ("<!doctype html>\n<meta charset=\"utf-8\">\n<meta name=\"referrer\" content=\"no-referrer\">\n"
                f"<meta http-equiv=\"refresh\" content=\"0;url={safe}\">\n<title>hwcheck</title>\n"
                f"<p>opening hwcheck… if nothing happens, <a href=\"{safe}\">open it here</a>.</p>\n")
        atomic_write_bytes(self.handoff_path, page.encode("utf-8"))
        return self.handoff_path

    def remove_handoff(self) -> None:
        try:
            self.handoff_path.unlink()
        except FileNotFoundError:
            pass

    def ensure(self) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def load_state(self) -> dict | None:
        try:
            text = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(f"can't read {self.state_path}: {exc.strerror or exc}") from None
        try:
            state = json.loads(text)
        except json.JSONDecodeError:
            raise StoreError(f"{self.state_path} is not valid json; move it aside to start over") from None
        if not isinstance(state, dict) or not isinstance(state.get("plan"), dict):
            raise StoreError(f"{self.state_path} is not an hwcheck state file; move it aside to start over")
        return state

    def save(self, state: dict, results: dict) -> None:
        """both files or neither. each is written in full (fsynced) to a temporary file before
        either is renamed into place, so a full disk changes nothing; if the second rename
        fails, the old state.json is put back from a hard link taken before the first."""
        self.ensure()
        temps: list[str | None] = []
        backup = None
        try:
            temps.append(_write_temp(self.state_path, _json_bytes(state)))
            temps.append(_write_temp(self.results_path, _json_bytes(results)))
            had_state = self.state_path.exists()
            if had_state:
                backup = self.dir / f".{self.state_path.name}.{secrets.token_hex(6)}.old"
                try:
                    os.link(self.state_path, backup)
                except OSError:  # no hard links here: then the state can't be put back
                    backup = None
            os.replace(temps[0], self.state_path)
            temps[0] = None
            try:
                os.replace(temps[1], self.results_path)
                temps[1] = None
            except BaseException:
                if backup is not None:  # the state is new and the results aren't: undo the state
                    os.replace(backup, self.state_path)
                    backup = None
                elif not had_state:
                    _unlink(self.state_path)
                raise
        finally:
            for tmp in temps:
                if tmp:
                    _unlink(tmp)
            if backup is not None:
                _unlink(backup)
        _fsync_dir(self.dir)

    def archive(self, stamp: str) -> list[Path]:
        """move an earlier run's files aside (state-<stamp>.json, results-<stamp>.json)."""
        moved = []
        safe = "".join(ch for ch in stamp if ch.isalnum() or ch in "-_") or "old"
        for path in (self.state_path, self.results_path):
            if not path.exists():
                continue
            target = path.with_name(f"{path.stem}-{safe}{path.suffix}")
            n = 1
            while target.exists():
                n += 1
                target = path.with_name(f"{path.stem}-{safe}-{n}{path.suffix}")
            os.replace(path, target)
            moved.append(target)
        if moved:
            _fsync_dir(self.dir)
        return moved
