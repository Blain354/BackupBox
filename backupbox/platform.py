"""Hardware access: listing, mounting, unmounting and powering off devices.

``LinuxPlatform`` drives the real Pi through ``lsblk`` and ``udisksctl`` (udisks2
with a polkit rule, so the service never needs root) and libgphoto2 for cameras
that speak MTP/PTP. ``SimPlatform`` maps each sub-directory of a folder to a fake
device, which lets the whole application run and be tested on any computer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Protocol

from .devices import BlockDevice, list_block_devices
from .mtp import CameraPort, GphotoBackend, SimCameraSession

log = logging.getLogger(__name__)


class PlatformError(RuntimeError):
    """A device operation failed; the message is shown to the user."""


class Platform(Protocol):
    def list_devices(self) -> list[BlockDevice]: ...

    def mount(self, dev: BlockDevice, read_only: bool) -> str: ...

    def unmount(self, dev: BlockDevice) -> None: ...

    def power_off(self, dev: BlockDevice) -> None: ...

    def free_bytes(self, path: str) -> int: ...

    def total_bytes(self, path: str) -> int: ...

    def list_cameras(self) -> list[CameraPort]: ...

    def open_camera(self, port: CameraPort): ...


def _statvfs_free(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _statvfs_total(path: str) -> int:
    st = os.statvfs(path)
    return st.f_blocks * st.f_frsize


class LinuxPlatform:
    """Real devices through lsblk and udisks2; MTP/PTP cameras through libgphoto2."""

    def __init__(self) -> None:
        self._gphoto = GphotoBackend()

    def list_cameras(self) -> list[CameraPort]:
        return self._gphoto.list_cameras()

    def open_camera(self, port: CameraPort):
        return self._gphoto.open(port, alive=lambda: any(c.port == port.port for c in self.list_cameras()))

    def list_devices(self) -> list[BlockDevice]:
        return list_block_devices()

    def _udisksctl(self, *args: str) -> str:
        if shutil.which("udisksctl") is None:
            raise PlatformError("udisksctl introuvable : installer le paquet udisks2.")
        proc = subprocess.run(
            ["udisksctl", *args, "--no-user-interaction"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            raise PlatformError(detail[-1] if detail else f"udisksctl {args[0]} a échoué")
        return proc.stdout

    def mount(self, dev: BlockDevice, read_only: bool) -> str:
        args = ["mount", "-b", dev.path]
        if read_only:
            args += ["-o", "ro"]
        out = self._udisksctl(*args)
        match = re.search(r" at (.+?)\.?\s*$", out.strip())
        if match:
            return match.group(1)
        for fresh in self.list_devices():
            if fresh.kname == dev.kname and fresh.mountpoint:
                return fresh.mountpoint
        raise PlatformError(f"Montage de {dev.path} introuvable après udisksctl")

    def unmount(self, dev: BlockDevice) -> None:
        os.sync()
        self._udisksctl("unmount", "-b", dev.path)

    def power_off(self, dev: BlockDevice) -> None:
        self._udisksctl("power-off", "-b", dev.disk_path)

    def free_bytes(self, path: str) -> int:
        return _statvfs_free(path)

    def total_bytes(self, path: str) -> int:
        return _statvfs_total(path)


class SimPlatform:
    """Fake devices for development and tests.

    Layout: ``<root>/<kname>/device.json`` describes the device (label, uuid,
    fstype, size, vendor, model, serial, optional free_bytes; ``"kind": "mtp"``
    for a camera talking MTP) and ``<root>/<kname>/media/`` holds its files.
    Creating the folder plugs the device in; deleting it unplugs it.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._mounted: dict[str, bool] = {}  # kname -> read_only
        self._powered_off: set[str] = set()

    def _meta(self, kname: str) -> dict:
        try:
            return json.loads((self.root / kname / "device.json").read_text())
        except (OSError, ValueError):
            return {}

    def list_devices(self) -> list[BlockDevice]:
        devices = []
        with self._lock:
            present = {p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")}
            self._powered_off &= present
            for kname in list(self._mounted):
                if kname not in present:
                    del self._mounted[kname]
            for kname in sorted(present - self._powered_off):
                if not (self.root / kname / "media").is_dir():
                    continue
                meta = self._meta(kname)
                if meta.get("kind") == "mtp":
                    continue
                devices.append(
                    BlockDevice(
                        kname=kname,
                        path=f"/dev/{kname}",
                        disk_kname=meta.get("disk", kname),
                        disk_path=f"/dev/{meta.get('disk', kname)}",
                        fstype=meta.get("fstype", "exfat"),
                        label=meta.get("label"),
                        uuid=meta.get("uuid"),
                        size=int(meta.get("size", 64 * 1024**3)),
                        mountpoint=str(self.root / kname / "media") if kname in self._mounted else None,
                        removable=True,
                        transport="usb",
                        model=meta.get("model"),
                        vendor=meta.get("vendor"),
                        serial=meta.get("serial"),
                    )
                )
        return devices

    def list_cameras(self) -> list[CameraPort]:
        cameras = []
        for device in sorted(self.root.iterdir()):
            if device.name.startswith(".") or not (device / "media").is_dir():
                continue
            meta = self._meta(device.name)
            if meta.get("kind") == "mtp":
                cameras.append(CameraPort(model=meta.get("model", "Camera"), port=f"sim:{device.name}"))
        return cameras

    def open_camera(self, port: CameraPort) -> SimCameraSession:
        kname = port.port.split(":", 1)[1]
        meta = self._meta(kname)
        if meta.get("fail_mount"):
            raise PlatformError(meta["fail_mount"])
        return SimCameraSession(self.root / kname / "media", meta.get("serial"))

    def mount(self, dev: BlockDevice, read_only: bool) -> str:
        meta = self._meta(dev.kname)
        if meta.get("fail_mount"):
            raise PlatformError(meta["fail_mount"])
        with self._lock:
            self._mounted[dev.kname] = read_only
        return str(self.root / dev.kname / "media")

    def is_read_only(self, kname: str) -> bool | None:
        return self._mounted.get(kname)

    def unmount(self, dev: BlockDevice) -> None:
        with self._lock:
            self._mounted.pop(dev.kname, None)

    def power_off(self, dev: BlockDevice) -> None:
        with self._lock:
            self._mounted.pop(dev.kname, None)
            self._powered_off.add(dev.kname)

    def free_bytes(self, path: str) -> int:
        kname = self._kname_for(path)
        if kname:
            meta = self._meta(kname)
            if "free_bytes" in meta:
                return int(meta["free_bytes"])
        return _statvfs_free(path)

    def total_bytes(self, path: str) -> int:
        kname = self._kname_for(path)
        if kname:
            meta = self._meta(kname)
            if "free_bytes" in meta and "size" in meta:
                return int(meta["size"])
        return _statvfs_total(path)

    def _kname_for(self, path: str) -> str | None:
        try:
            rel = Path(path).resolve().relative_to(self.root.resolve())
        except ValueError:
            return None
        return rel.parts[0] if rel.parts else None
