"""Block device discovery.

Turns ``lsblk`` JSON into a flat list of filesystems BackupBox may use. Any disk
with a partition mounted by the system (``/``, ``/boot/firmware``, swap, or any
fstab mount outside ``/media``, such as an internal buffer SSD) is excluded up
front, so the rest of the application never sees the Pi's own storage.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

LSBLK_COLUMNS = "NAME,KNAME,PATH,PKNAME,TYPE,FSTYPE,LABEL,UUID,SIZE,MOUNTPOINT,RM,HOTPLUG,TRAN,MODEL,VENDOR,SERIAL"

# Mount points that identify the disk the OS runs from.
SYSTEM_MOUNTS = {"/", "/boot", "/boot/firmware", "[SWAP]"}

# Where udisks2 (or a desktop) mounts removable media. Anything mounted elsewhere
# was mounted by the administrator (fstab) and is not ours to touch.
USER_MOUNT_PREFIXES = ("/media/", "/run/media/")

# Filesystem types that are never user data.
IGNORED_FSTYPES = {"swap", "crypto_LUKS", "LVM2_member", "linux_raid_member", "squashfs", "iso9660"}

# Filesystems BackupBox can read cards from and write to.
SUPPORTED_FSTYPES = {"vfat", "exfat", "ntfs", "ntfs3", "ext4", "ext3", "ext2", "btrfs", "xfs", "hfsplus"}

IGNORED_DISK_PREFIXES = ("loop", "zram", "ram", "sr", "mtdblock")


@dataclass(frozen=True)
class BlockDevice:
    """A filesystem on a removable disk, as reported by lsblk."""

    kname: str
    path: str
    disk_kname: str
    disk_path: str
    fstype: str | None
    label: str | None
    uuid: str | None
    size: int
    mountpoint: str | None
    removable: bool
    transport: str | None
    model: str | None
    vendor: str | None = None
    serial: str | None = None
    kind: str = "block"  # block | mtp | buffer

    @property
    def key(self) -> str:
        """Identity of one insertion: the same slot with another card gets a new key."""
        return f"{self.kname}-{self.uuid or 'nouuid'}"

    @property
    def supported(self) -> bool:
        return self.kind != "block" or (self.fstype or "") in SUPPORTED_FSTYPES

    @property
    def display_name(self) -> str:
        if self.label:
            return self.label
        if self.model:
            return self.model.strip()
        return self.kname


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip() in {"1", "true", "True"}


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clean(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _system_managed(mountpoint: str | None) -> bool:
    if not mountpoint:
        return False
    return mountpoint in SYSTEM_MOUNTS or not mountpoint.startswith(USER_MOUNT_PREFIXES)


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def parse_lsblk(text: str) -> list[BlockDevice]:
    """Parse ``lsblk -J`` output into candidate filesystems, system disk excluded."""
    data = json.loads(text)
    devices: list[BlockDevice] = []
    for disk in data.get("blockdevices") or []:
        disk_kname = disk.get("kname") or disk.get("name") or ""
        if disk_kname.startswith(IGNORED_DISK_PREFIXES) or (
            disk_kname.startswith("mmcblk") and "boot" in disk_kname
        ):
            continue
        nodes = list(_walk(disk))
        if any(_system_managed(n.get("mountpoint")) for n in nodes):
            continue
        for node in nodes:
            fstype = node.get("fstype")
            if not fstype or fstype in IGNORED_FSTYPES:
                continue
            if node.get("type") not in ("part", "disk"):
                continue
            size = _as_int(node.get("size"))
            if size <= 0:
                continue
            kname = node.get("kname") or node.get("name")
            devices.append(
                BlockDevice(
                    kname=kname,
                    path=node.get("path") or f"/dev/{kname}",
                    disk_kname=disk_kname,
                    disk_path=disk.get("path") or f"/dev/{disk_kname}",
                    fstype=fstype,
                    label=node.get("label"),
                    uuid=node.get("uuid"),
                    size=size,
                    mountpoint=node.get("mountpoint"),
                    removable=_as_bool(disk.get("rm")) or _as_bool(disk.get("hotplug")),
                    transport=disk.get("tran"),
                    model=_clean(disk.get("model")),
                    vendor=_clean(disk.get("vendor")),
                    serial=_clean(disk.get("serial")),
                )
            )
    return devices


def list_block_devices() -> list[BlockDevice]:
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    return parse_lsblk(out)
