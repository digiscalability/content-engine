"""Mounted volumes by serial and label (SRD §8.2): drive letters change between plug-ins, so
a registered drive is matched on both, never on its letter. ADR 0020."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Volume:
    root: Path
    serial: str  # as Windows prints it, 1A2B-3C4D
    label: str


def normalise_serial(serial: str) -> str:
    """Eight upper-case hex digits: '1a2b-3c4d' and '1A2B3C4D' are the same volume."""
    digits = re.sub(r"[^0-9A-Fa-f]", "", str(serial)).upper()
    return digits.rjust(8, "0")


def find_volume(source, volumes: list[Volume]) -> Volume | None:
    """The mounted volume whose serial and label both match the source, or None."""
    serial = normalise_serial(source.volume_serial or "")
    label = (source.volume_label or "").strip().casefold()
    for volume in volumes:
        if normalise_serial(volume.serial) == serial and volume.label.strip().casefold() == label:
            return volume
    return None


def mounted_volumes() -> list[Volume]:
    """Every mounted drive letter with its serial and label (Windows; elsewhere none)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetErrorMode(1)  # SEM_FAILCRITICALERRORS: an empty card reader asks nothing
    volumes = []
    mask = kernel32.GetLogicalDrives()
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = f"{chr(65 + index)}:\\"
        label = ctypes.create_unicode_buffer(261)
        filesystem = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), label, 261, ctypes.byref(serial), None, None, filesystem, 261
        )
        if ok:
            value = serial.value
            volumes.append(
                Volume(Path(root), f"{value >> 16:04X}-{value & 0xFFFF:04X}", label.value)
            )
    return volumes
