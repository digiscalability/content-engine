"""Windows notifications (SRD §13, D11, C17): one toast when something waits on Abbas,
naming what ("3 drafts to approve, 1 avatar request"). Clicking it opens the day's digest
through a `file:///` launch action, so it works after the engine has exited. Behind
`Notifier`, so tests never pop a toast (NFR-8). ADR 0016.

windows-toasts runs on the winrt packages, which bundle an old C++ runtime
(msvcp140.dll 14.29). The first msvcp140.dll a process loads serves every later
extension, and opentimelineio needs 14.40 or newer, so the system's copy is loaded before
winrt, and winrt is imported only when a toast is actually made.
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from windows_toasts import Toast


def _system_cpp_runtime_first() -> None:
    if sys.platform != "win32":
        return
    system = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "msvcp140.dll"
    if system.is_file():
        ctypes.WinDLL(str(system))  # later loads of "msvcp140.dll" reuse this module


@runtime_checkable
class Notifier(Protocol):
    def notify(self, title: str, body: str, open_path: Path) -> None: ...


def toast_for(title: str, body: str, open_path: Path) -> Toast:
    _system_cpp_runtime_first()
    from windows_toasts import Toast

    return Toast(text_fields=[title, body], launch_action=open_path.resolve().as_uri())


@dataclass
class ToastNotifier:
    app: str = "content-engine"

    def notify(self, title: str, body: str, open_path: Path) -> None:
        toast = toast_for(title, body, open_path)
        from windows_toasts import WindowsToaster

        WindowsToaster(self.app).show_toast(toast)


@dataclass
class FakeNotifier:
    shown: list[tuple[str, str, Path]] = field(default_factory=list)

    def notify(self, title: str, body: str, open_path: Path) -> None:
        self.shown.append((title, body, open_path))
