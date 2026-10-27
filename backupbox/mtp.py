"""Cameras that talk MTP/PTP over USB instead of showing up as a disk.

GoPro cameras and most photo cameras (Canon, Nikon, Sony, Fujifilm…) do this
when plugged in with their cable. libgphoto2 (python ``gphoto2`` package) reads
them; each file is streamed in 8 MiB chunks so the copy engine hashes and writes
it exactly like a file from a card.

Detection is cheap: ``/sys/bus/usb/devices`` is fingerprinted on every poll and
``gphoto2`` auto-detection only runs for a few seconds after something changed
(a GoPro needs a moment after power-on before it answers MTP).
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from .copier import CopyAborted, SourceFile, is_junk

log = logging.getLogger(__name__)

USB_SYSFS = Path("/sys/bus/usb/devices")
REDETECT_WINDOW_S = 12.0


class MtpError(RuntimeError):
    pass


@dataclass(frozen=True)
class CameraPort:
    model: str
    port: str  # libgphoto2 port, e.g. "usb:001,007"

    @property
    def kname(self) -> str:
        return "mtp-" + re.sub(r"[^0-9A-Za-z]+", "-", self.port).strip("-")


def usb_signature(root: Path = USB_SYSFS) -> tuple:
    """Fingerprint of the USB devices currently attached."""
    entries = []
    try:
        for dev in sorted(root.iterdir()):
            vendor = dev / "idVendor"
            if not vendor.is_file():
                continue
            try:
                entries.append((dev.name, vendor.read_text().strip(), (dev / "devnum").read_text().strip()))
            except OSError:
                continue
    except OSError:
        return ()
    return tuple(entries)


SERIAL_RE = re.compile(r"Serial\s*Number\s*:\s*(\S+)", re.I)


def serial_from_summary(text: str) -> str | None:
    match = SERIAL_RE.search(text or "")
    if not match:
        return None
    serial = match.group(1).strip()
    return serial if serial and set(serial) != {"0"} else None


class CameraReader(io.RawIOBase):
    """Sequential file-like view of one file on the camera."""

    def __init__(self, read_at: Callable[[int, memoryview], int], size: int):
        self._read_at = read_at
        self._size = size
        self._offset = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        remaining = self._size - self._offset
        if remaining <= 0:
            return 0
        view = memoryview(buffer)[: min(len(buffer), remaining)]
        n = self._read_at(self._offset, view)
        if n <= 0:
            raise MtpError("L'appareil a cessé de répondre pendant la lecture.")
        self._offset += n
        return n


class GphotoSession:
    """An open camera. Implements the copier's SourceTree protocol."""

    def __init__(self, gp, camera, alive: Callable[[], bool]):
        self.gp = gp
        self.camera = camera
        self._alive = alive
        self._lock = threading.Lock()
        self._index: dict[str, tuple[str, str, int]] = {}
        try:
            self.serial = serial_from_summary(str(camera.get_summary()))
        except Exception:  # summary is optional
            self.serial = None

    def alive(self) -> bool:
        return self._alive()

    def _folders(self, path: str) -> list[str]:
        return [name for name, _ in self.camera.folder_list_folders(path)]

    def _files(self, path: str) -> list[str]:
        return [name for name, _ in self.camera.folder_list_files(path)]

    def scan(self, cancel: threading.Event | None = None) -> list[SourceFile]:
        with self._lock:
            roots = self._folders("/")
            stores = [r for r in roots if r.lower().startswith("store_")] or roots
            strip = len(stores) == 1
            files: list[SourceFile] = []
            index: dict[str, tuple[str, str, int]] = {}
            stack = [f"/{s}" for s in stores]
            while stack:
                if cancel is not None and cancel.is_set():
                    raise CopyAborted("cancelled", "Sauvegarde annulée.")
                folder = stack.pop()
                for sub in self._folders(folder):
                    if not is_junk(sub):
                        stack.append(f"{folder}/{sub}")
                for name in self._files(folder):
                    if is_junk(name):
                        continue
                    info = self.camera.file_get_info(folder, name).file
                    parts = PurePosixPath(folder).parts[1:]  # drop "/"
                    if strip:
                        parts = parts[1:]
                    rel = str(PurePosixPath(*parts, name))
                    mtime = int(getattr(info, "mtime", 0) or 0)
                    files.append(SourceFile(rel=rel, size=int(info.size), mtime_ns=mtime * 1_000_000_000))
                    index[rel] = (folder, name, int(info.size))
            self._index = index
            files.sort(key=lambda f: f.rel)
            return files

    def open(self, rel: str):
        try:
            folder, name, size = self._index[rel]
        except KeyError:
            raise FileNotFoundError(rel) from None

        def read_at(offset: int, view: memoryview) -> int:
            with self._lock:
                return self.camera.file_read(folder, name, self.gp.GP_FILE_TYPE_NORMAL, offset, view)

        return CameraReader(read_at, size)

    def close(self) -> None:
        with self._lock:
            try:
                self.camera.exit()
            except Exception:
                pass


class GphotoBackend:
    """libgphoto2 access, if the ``gphoto2`` package is installed."""

    def __init__(self, sysfs: Path = USB_SYSFS):
        try:
            import gphoto2 as gp  # noqa: PLC0415 - optional dependency
        except ImportError:
            gp = None
        self.gp = gp
        self.sysfs = sysfs
        self._signature: tuple | None = None
        self._window_until = 0.0
        self._cached: list[CameraPort] = []

    @property
    def available(self) -> bool:
        return self.gp is not None

    def list_cameras(self) -> list[CameraPort]:
        if not self.available:
            return []
        signature = usb_signature(self.sysfs)
        now = time.monotonic()
        if signature != self._signature:
            self._signature = signature
            self._window_until = now + REDETECT_WINDOW_S
        if now <= self._window_until:
            try:
                found = self.gp.Camera.autodetect()
                self._cached = [CameraPort(model=name, port=addr) for name, addr in found if addr.startswith("usb:")]
            except Exception as exc:
                log.debug("Détection gphoto2 impossible : %s", exc)
        return list(self._cached)

    def open(self, port: CameraPort, alive: Callable[[], bool]) -> GphotoSession:
        if not self.available:
            raise MtpError("Paquet python « gphoto2 » absent.")
        gp = self.gp
        try:
            camera = gp.Camera()
            ports = gp.PortInfoList()
            ports.load()
            camera.set_port_info(ports[ports.lookup_path(port.port)])
            abilities = gp.CameraAbilitiesList()
            abilities.load()
            camera.set_abilities(abilities[abilities.lookup_model(port.model)])
            camera.init()
        except Exception as exc:
            raise MtpError(f"Connexion à « {port.model} » impossible : {exc}") from exc
        return GphotoSession(gp, camera, alive)


class SimCameraSession:
    """Simulation of an MTP camera: a folder whose files are read through ``open``."""

    def __init__(self, root: Path, serial: str | None):
        self.root = Path(root)
        self.serial = serial

    def alive(self) -> bool:
        return self.root.is_dir()

    def scan(self, cancel: threading.Event | None = None) -> list[SourceFile]:
        from .copier import scan_source

        return scan_source(self.root, cancel)

    def open(self, rel: str):
        return open(self.root / rel, "rb")

    def close(self) -> None:
        pass
