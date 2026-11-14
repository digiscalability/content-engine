"""One GPU job at a time (SRD §13, NFR-6): renders, Whisper, indexing and later ComfyUI.

An OS byte-range lock on a small file (`msvcrt` on Windows, `fcntl` elsewhere), so the
operating system drops it when the holding process exits or crashes: a dead job never
leaves the GPU locked. A busy lock is retried until a timeout; the holder's job name and
pid sit in a sidecar file so the error says who has it. ADR 0016.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

DEFAULT_TIMEOUT_S = 3600.0  # a render may wait an hour for an indexing pass


class GpuBusy(RuntimeError):
    """Another job held the GPU lock for longer than the timeout."""


def _try_lock(handle: IO[bytes]) -> bool:
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - this machine is Windows
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[bytes]) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class GpuLock:
    path: Path | str
    job: str = "engine"
    timeout_s: float = DEFAULT_TIMEOUT_S
    poll_s: float = 0.5
    _handle: IO[bytes] | None = field(default=None, repr=False)

    @property
    def held(self) -> bool:
        return self._handle is not None

    @property
    def _owner(self) -> Path:
        return Path(f"{self.path}.owner")

    def acquire(self) -> GpuLock:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        deadline = time.monotonic() + self.timeout_s
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                handle.close()
                owner = self._owner.read_text(encoding="utf-8") if self._owner.is_file() else "?"
                raise GpuBusy(f"the GPU is busy ({owner.strip()}); waited {self.timeout_s:g} s")
            time.sleep(self.poll_s)
        self._handle = handle
        stamp = datetime.now().isoformat(timespec="seconds")
        self._owner.write_text(f"{self.job} pid {os.getpid()} since {stamp}\n", encoding="utf-8")
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> GpuLock:
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()
