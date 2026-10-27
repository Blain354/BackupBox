"""MTP/PTP cameras: the gphoto2 session against a fake camera object."""

import os
from types import SimpleNamespace

import pytest

from backupbox.copier import CopyEngine
from backupbox.manifest import Manifest
from backupbox.mtp import CameraPort, CameraReader, GphotoSession, MtpError, serial_from_summary, usb_signature


class FakeGp:
    GP_FILE_TYPE_NORMAL = 1


class FakeCamera:
    """Mimics the parts of gphoto2.Camera that GphotoSession uses."""

    def __init__(self, tree: dict, mtime: int = 1_767_268_800):
        self.tree = tree  # {"/store_00010001/DCIM/100GOPRO": {"GX010001.MP4": b"..."}}
        self.mtime = mtime
        self.closed = False
        self.reads = 0

    def get_summary(self):
        return "Manufacturer: GoPro\nModel: HERO12 Black\n  Serial Number: C3471325521234\n"

    def folder_list_folders(self, path):
        prefix = path.rstrip("/") + "/"
        children = set()
        for folder in self.tree:
            if folder.startswith(prefix):
                children.add(folder[len(prefix):].split("/")[0])
        return [(c, None) for c in sorted(children)]

    def folder_list_files(self, path):
        return [(name, None) for name in sorted(self.tree.get(path, {}))]

    def file_get_info(self, folder, name):
        return SimpleNamespace(file=SimpleNamespace(size=len(self.tree[folder][name]), mtime=self.mtime))

    def file_read(self, folder, name, file_type, offset, view):
        data = self.tree[folder][name][offset: offset + len(view)]
        view[: len(data)] = data
        self.reads += 1
        return len(data)

    def exit(self):
        self.closed = True


@pytest.fixture
def camera():
    return FakeCamera(
        {
            "/store_00010001/DCIM/100GOPRO": {
                "GX010001.MP4": os.urandom(3 * 1024 * 1024 + 11),
                "GX010001.LRV": os.urandom(5000),
            },
            "/store_00010001/MISC": {"version.txt": b"{}"},
        }
    )


def test_scan_strips_single_storage_and_keeps_camera_dates(camera):
    session = GphotoSession(FakeGp, camera, alive=lambda: True)
    files = session.scan()
    assert [f.rel for f in files] == ["DCIM/100GOPRO/GX010001.LRV", "DCIM/100GOPRO/GX010001.MP4", "MISC/version.txt"]
    assert files[1].size == 3 * 1024 * 1024 + 11
    assert files[1].mtime_ns == 1_767_268_800 * 10**9
    assert session.serial == "C3471325521234"


def test_two_storages_keep_their_prefix():
    cam = FakeCamera({"/store_00010001/DCIM": {"a.JPG": b"1"}, "/store_00020001/DCIM": {"a.JPG": b"2"}})
    rels = [f.rel for f in GphotoSession(FakeGp, cam, alive=lambda: True).scan()]
    assert rels == ["store_00010001/DCIM/a.JPG", "store_00020001/DCIM/a.JPG"]


def test_copy_engine_streams_from_the_camera(camera, tmp_path):
    session = GphotoSession(FakeGp, camera, alive=lambda: True)
    (tmp_path / "tmp").mkdir()
    engine = CopyEngine(
        source=session,
        dest_dir=tmp_path / "dest",
        tmp_dir=tmp_path / "tmp",
        manifest=Manifest(tmp_path / "m.jsonl"),
        job_id="j",
        card_identity="mtp:C3471",
        chunk_size=1024 * 1024,
    )
    outcomes = engine.run(engine.plan(session.scan()))
    assert [o.status for o in outcomes] == ["copied"] * 3
    clip = tmp_path / "dest/DCIM/100GOPRO/GX010001.MP4"
    assert clip.read_bytes() == camera.tree["/store_00010001/DCIM/100GOPRO"]["GX010001.MP4"]
    assert clip.stat().st_mtime_ns == 1_767_268_800 * 10**9
    assert camera.reads >= 4  # chunked reads, not one giant transfer
    session.close()
    assert camera.closed


def test_reader_fails_loudly_when_camera_stops_answering():
    reader = CameraReader(lambda offset, view: 0, size=10)
    with pytest.raises(MtpError):
        reader.read(4)


def test_serial_parsing():
    assert serial_from_summary("Serial Number: C34") == "C34"
    assert serial_from_summary("Serial Number: 00000000") is None
    assert serial_from_summary("nothing") is None


def test_usb_signature_changes_when_a_device_appears(tmp_path):
    def add(name, vendor, devnum):
        d = tmp_path / name
        d.mkdir()
        (d / "idVendor").write_text(vendor)
        (d / "devnum").write_text(devnum)

    add("1-1", "2109", "2")
    before = usb_signature(tmp_path)
    add("1-1.2", "2672", "7")  # a GoPro
    assert usb_signature(tmp_path) != before
    assert CameraPort("GoPro HERO12 Black", "usb:001,007").kname == "mtp-usb-001-007"
