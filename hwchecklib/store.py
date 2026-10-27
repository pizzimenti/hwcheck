"""state and results on disk, written atomically under ~/.local/state/hwcheck/."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class StoreError(RuntimeError):
    pass


def default_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or ""
    root = Path(base) if base.startswith("/") else Path.home() / ".local" / "state"
    return root / "hwcheck"


def atomic_write_json(path: Path, obj) -> None:
    """write obj as json to path so a reader sees the old file or the new one, never half."""
    path = Path(path)
    data = (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
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
        self.ensure()
        atomic_write_json(self.state_path, state)
        atomic_write_json(self.results_path, results)

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
