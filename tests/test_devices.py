"""US-12: only removable data filesystems are ever offered; the system disk never is."""

import json

from backupbox.devices import parse_lsblk


def node(name, type_="part", fstype=None, mount=None, children=None, rm=True, size=64_000_000_000, **kw):
    data = {
        "name": name,
        "kname": name,
        "path": f"/dev/{name}",
        "type": type_,
        "fstype": fstype,
        "label": kw.get("label"),
        "uuid": kw.get("uuid"),
        "size": size,
        "mountpoint": mount,
        "rm": rm,
        "hotplug": kw.get("hotplug", rm),
        "tran": kw.get("tran"),
        "model": kw.get("model"),
    }
    if children:
        data["children"] = children
    return data


def lsblk(*disks):
    return json.dumps({"blockdevices": list(disks)})


PI4_SD = node(
    "mmcblk0",
    "disk",
    rm=False,
    children=[
        node("mmcblk0p1", fstype="vfat", mount="/boot/firmware", rm=False),
        node("mmcblk0p2", fstype="ext4", mount="/", rm=False),
    ],
)


def test_pi4_system_card_is_excluded_and_usb_devices_are_listed():
    ssd = node("sda", "disk", rm=False, tran="usb", model="Portable SSD T7",
               children=[node("sda1", fstype="exfat", label="T7", uuid="AAAA-1111", rm=False)])
    reader = node("sdb", "disk", tran="usb", children=[node("sdb1", fstype="exfat", label="GOPRO", uuid="BBBB-2222")])
    devices = parse_lsblk(lsblk(PI4_SD, ssd, reader))
    assert [d.kname for d in devices] == ["sda1", "sdb1"]
    t7 = devices[0]
    assert t7.disk_path == "/dev/sda"
    assert t7.model == "Portable SSD T7"
    assert t7.transport == "usb"
    assert t7.key == "sda1-AAAA-1111"


def test_system_booted_from_usb_ssd_is_excluded():
    boot_ssd = node("sda", "disk", rm=False, tran="usb", children=[
        node("sda1", fstype="vfat", mount="/boot/firmware"),
        node("sda2", fstype="ext4", mount="/"),
        node("sda3", fstype="exfat", label="DATA"),
    ])
    card = node("sdb", "disk", children=[node("sdb1", fstype="vfat", label="CANON")])
    assert [d.kname for d in parse_lsblk(lsblk(boot_ssd, card))] == ["sdb1"]


def test_card_without_partition_table_is_listed():
    superfloppy = node("sdc", "disk", fstype="exfat", label="DJI", uuid="CCCC")
    devices = parse_lsblk(lsblk(PI4_SD, superfloppy))
    assert [d.kname for d in devices] == ["sdc"]
    assert devices[0].disk_kname == "sdc"


def test_empty_reader_swap_loop_zram_and_unformatted_are_ignored():
    empty_reader = node("sdd", "disk", size=0)
    loop = node("loop0", "loop", fstype="squashfs", mount="/snap/x")
    zram = node("zram0", "disk", size=1_000_000, mount="[SWAP]")
    raw = node("sde", "disk", children=[node("sde1", fstype=None)])
    swap = node("sdf", "disk", children=[node("sdf1", fstype="swap")])
    assert parse_lsblk(lsblk(PI4_SD, empty_reader, loop, zram, raw, swap)) == []


def test_old_lsblk_string_values_are_normalised():
    disk = node("sda", "disk", rm="0", size="1000", children=[node("sda1", fstype="exfat", size="1000", rm="0")])
    disk["hotplug"] = "1"
    devices = parse_lsblk(lsblk(disk))
    assert devices[0].size == 1000
    assert devices[0].removable is True


def test_unsupported_filesystem_is_flagged():
    disk = node("sda", "disk", children=[node("sda1", fstype="apfs")])
    (dev,) = parse_lsblk(lsblk(disk))
    assert dev.supported is False


def test_internal_buffer_disk_mounted_by_fstab_is_excluded():
    internal = node("sda", "disk", rm=False, tran="usb", children=[node("sda1", fstype="ext4", mount="/srv/backupbox/buffer")])
    drone = node("sdb", "disk", tran="usb", children=[node("sdb1", fstype="exfat", mount="/media/backupbox/DJI")])
    drone["vendor"], drone["model"], drone["serial"] = "DJI     ", "Avata2", "1581F6"
    devices = parse_lsblk(lsblk(PI4_SD, internal, drone))
    assert [d.kname for d in devices] == ["sdb1"]
    assert (devices[0].vendor, devices[0].model, devices[0].serial) == ("DJI", "Avata2", "1581F6")
