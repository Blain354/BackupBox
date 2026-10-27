"""US-05, US-06, US-08, US-09, US-10, US-18: the verified copy engine."""

import os
import threading
from pathlib import Path

import pytest

from backupbox import copier as c
from backupbox.copier import CopyAborted, CopyEngine, scan_source
from backupbox.manifest import Manifest

MTIME = 1_767_268_800_000_000_000  # 2026-01-01 12:00 UTC, in ns


def write(path: Path, data: bytes, mtime_ns: int = MTIME) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


@pytest.fixture
def card(tmp_path):
    root = tmp_path / "card"
    write(root / "DCIM/100GOPRO/GX010001.MP4", os.urandom(3 * 1024 * 1024 + 7))
    write(root / "DCIM/100GOPRO/GX010002.MP4", os.urandom(1024))
    write(root / "MISC/version.txt", b"v")
    write(root / "._GX010001.MP4", b"apple junk")
    write(root / "System Volume Information/IndexerVolumeGuid", b"junk")
    return root


@pytest.fixture
def ssd(tmp_path):
    root = tmp_path / "ssd"
    (root / ".backupbox/tmp").mkdir(parents=True)
    return root


def engine(card, ssd, **kw):
    manifest = kw.pop("manifest", None) or Manifest(ssd / "P/.backupbox/manifests/GoPro.jsonl")
    return CopyEngine(
        src_root=card,
        dest_dir=ssd / "P/Footage_RAW/GoPro",
        tmp_dir=ssd / ".backupbox/tmp",
        manifest=manifest,
        job_id="job1",
        card_identity="gopro:1",
        chunk_size=kw.pop("chunk_size", 1024 * 1024),
        **kw,
    )


def snapshot(root: Path) -> dict:
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_scan_skips_junk_and_symlinks(card):
    os.symlink(card / "MISC/version.txt", card / "link.txt")
    rels = [f.rel for f in scan_source(card)]
    assert rels == ["DCIM/100GOPRO/GX010001.MP4", "DCIM/100GOPRO/GX010002.MP4", "MISC/version.txt"]


def test_copy_is_verified_preserves_layout_and_mtime_and_never_touches_card(card, ssd):
    before = snapshot(card)
    e = engine(card, ssd)
    plan = e.plan(scan_source(card))
    assert plan.files_to_copy == 3
    outcomes = e.run(plan)
    assert [o.status for o in outcomes] == ["copied"] * 3
    dest = ssd / "P/Footage_RAW/GoPro"
    for rel in ["DCIM/100GOPRO/GX010001.MP4", "DCIM/100GOPRO/GX010002.MP4", "MISC/version.txt"]:
        assert (dest / rel).read_bytes() == (card / rel).read_bytes()
        assert (dest / rel).stat().st_mtime_ns == (card / rel).stat().st_mtime_ns
    assert snapshot(card) == before
    assert list((ssd / ".backupbox/tmp").iterdir()) == []
    manifest = Manifest(ssd / "P/.backupbox/manifests/GoPro.jsonl")
    entry = manifest.lookup("DCIM/100GOPRO/GX010001.MP4", 3 * 1024 * 1024 + 7, MTIME)
    assert entry["verified"] is True and entry["algo"] == c.HASH_ALGO and len(entry["hash"]) == 32
    p = e.progress.to_dict()
    assert p["percent"] == 100.0 and p["files_done"] == 3
    assert p["work_done"] == p["work_total"] == 2 * plan.bytes_needed - 2 * 3 * c.PER_FILE_SLACK


def test_second_pass_copies_only_new_files(card, ssd):
    engine(card, ssd).run(engine(card, ssd).plan(scan_source(card)))
    write(card / "DCIM/100GOPRO/GX010003.MP4", b"new clip")
    e = engine(card, ssd)
    plan = e.plan(scan_source(card))
    assert [i.action for i in plan.items] == ["skip", "skip", "copy", "skip"]
    outcomes = e.run(plan)
    assert sorted(o.status for o in outcomes) == ["copied", "skipped", "skipped", "skipped"]


def test_identical_file_already_on_ssd_is_checked_not_duplicated(card, ssd):
    dest = ssd / "P/Footage_RAW/GoPro"
    write(dest / "DCIM/100GOPRO/GX010002.MP4", (card / "DCIM/100GOPRO/GX010002.MP4").read_bytes(), MTIME + 10**12)
    e = engine(card, ssd)
    plan = e.plan(scan_source(card))
    assert plan.items[1].action == "check"
    outcomes = e.run(plan)
    assert outcomes[1].status == "skipped"
    assert sorted(p.name for p in (dest / "DCIM/100GOPRO").iterdir()) == ["GX010001.MP4", "GX010002.MP4"]
    assert e.manifest.by_dest("DCIM/100GOPRO/GX010002.MP4")["hash"] == outcomes[1].hash


def test_name_conflict_keeps_both_versions(card, ssd):
    dest = ssd / "P/Footage_RAW/GoPro"
    old = write(dest / "DCIM/100GOPRO/GX010002.MP4", b"older clip from a reformatted card")
    outcomes = engine(card, ssd).run(engine(card, ssd).plan(scan_source(card)))
    conflict = outcomes[1]
    assert conflict.status == "conflict"
    assert conflict.dest.startswith("DCIM/100GOPRO/GX010002_conflit-") and conflict.dest.endswith(".MP4")
    assert old.read_bytes() == b"older clip from a reformatted card"
    assert (dest / conflict.dest).read_bytes() == (card / "DCIM/100GOPRO/GX010002.MP4").read_bytes()
    # Re-inserting the same card does not create a third copy.
    again = engine(card, ssd)
    plan = again.plan(scan_source(card))
    assert [i.action for i in plan.items] == ["skip", "skip", "skip"]


def test_not_enough_space_refuses_before_copying(card, ssd):
    e = engine(card, ssd)
    plan = e.plan(scan_source(card))
    with pytest.raises(CopyAborted) as exc:
        CopyEngine.check_space(plan, free_bytes=plan.bytes_needed)
    assert exc.value.reason == "no_space"
    assert "Espace insuffisant" in str(exc.value)
    CopyEngine.check_space(plan, free_bytes=plan.bytes_needed + c.SPACE_RESERVE)
    assert not (ssd / "P/Footage_RAW").exists()


def test_verification_mismatch_is_retried_then_reported(card, ssd, monkeypatch):
    real = CopyEngine._hash_file
    calls = {"n": 0}

    def corrupt(self, path, step):
        digest = real(self, path, step)
        if step == "vérification" and path.name.endswith(".bbpart"):
            calls["n"] += 1
            return "0" * 32
        return digest

    monkeypatch.setattr(CopyEngine, "_hash_file", corrupt)
    e = engine(card, ssd)
    outcomes = e.run(e.plan(scan_source(card)))
    assert all(o.status == "failed" for o in outcomes)
    assert "vérification échouée" in outcomes[0].error
    assert calls["n"] == 6  # three files, two attempts each
    assert not (ssd / "P/Footage_RAW/GoPro/DCIM/100GOPRO/GX010001.MP4").exists()
    assert list((ssd / ".backupbox/tmp").iterdir()) == []
    assert len(e.manifest) == 0


def test_transient_mismatch_succeeds_on_retry(card, ssd, monkeypatch):
    real = CopyEngine._hash_file
    seen = set()

    def flaky(self, path, step):
        digest = real(self, path, step)
        if step == "vérification" and len(seen) == 0:
            seen.add(path)
            return "f" * 32
        return digest

    monkeypatch.setattr(CopyEngine, "_hash_file", flaky)
    e = engine(card, ssd)
    outcomes = e.run(e.plan(scan_source(card)))
    assert [o.status for o in outcomes] == ["copied"] * 3


def test_cancel_stops_and_leaves_no_partial_file(card, ssd):
    cancel = threading.Event()
    e = engine(card, ssd, cancel=cancel, chunk_size=64 * 1024)
    plan = e.plan(scan_source(card))
    original = e.progress.add_work

    def add_work(n):
        original(n)
        cancel.set()

    e.progress.add_work = add_work
    with pytest.raises(CopyAborted) as exc:
        e.run(plan)
    assert exc.value.reason == "cancelled"
    assert list((ssd / ".backupbox/tmp").iterdir()) == []
    assert not (ssd / "P/Footage_RAW/GoPro/DCIM/100GOPRO/GX010001.MP4").exists()


def test_card_removed_mid_copy_aborts_the_job(card, ssd):
    alive = {"card": True}
    e = engine(card, ssd, src_alive=lambda: alive["card"])
    plan = e.plan(scan_source(card))
    os.remove(card / "DCIM/100GOPRO/GX010001.MP4")
    alive["card"] = False
    with pytest.raises(CopyAborted) as exc:
        e.run(plan)
    assert exc.value.reason == "source_gone"


def test_unreadable_file_fails_alone(card, ssd):
    e = engine(card, ssd)
    plan = e.plan(scan_source(card))
    os.remove(card / "DCIM/100GOPRO/GX010001.MP4")  # card still present
    outcomes = e.run(plan)
    assert [o.status for o in outcomes] == ["failed", "copied", "copied"]


def test_file_changing_during_copy_is_reported(card, ssd):
    e = engine(card, ssd)
    plan = e.plan(scan_source(card))
    write(card / "MISC/version.txt", b"changed size")
    outcomes = e.run(plan)
    assert outcomes[2].status == "failed"
    assert "a changé" in outcomes[2].error


def test_ssd_full_aborts(card, ssd, monkeypatch):
    import errno

    def full(self, f, tmp):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(CopyEngine, "_copy_to_tmp", full)
    e = engine(card, ssd)
    with pytest.raises(CopyAborted) as exc:
        e.run(e.plan(scan_source(card)))
    assert exc.value.reason == "no_space"


def test_manifest_ignores_torn_last_line(tmp_path):
    path = tmp_path / "m.jsonl"
    m = Manifest(path)
    m.append({"src": "a", "dest": "a", "size": 1, "mtime_ns": MTIME, "hash": "h"})
    with open(path, "a") as fh:
        fh.write('{"src": "b", "dest": "b", "si')  # power cut while appending
    m2 = Manifest(path)
    assert len(m2) == 1
    assert m2.lookup("a", 1, MTIME + 1_000_000_000) is not None  # FAT 2 s tolerance
    assert m2.lookup("a", 1, MTIME + 3_000_000_000) is None


def test_human_bytes():
    assert c.human_bytes(999) == "999 o"
    assert c.human_bytes(1_500_000) == "1,5 Mo"
    assert c.human_bytes(2_000_000_000_000) == "2,0 To"


def test_verification_reads_from_disk_not_page_cache(card, ssd, monkeypatch):
    """Without DONTNEED the re-read would come from RAM and prove nothing."""
    calls = []
    real = os.posix_fadvise

    def spy(fd, offset, length, advice):
        calls.append((os.readlink(f"/proc/self/fd/{fd}"), advice))
        return real(fd, offset, length, advice)

    monkeypatch.setattr(os, "posix_fadvise", spy)
    e = engine(card, ssd)
    e.run(e.plan(scan_source(card)))
    dropped_tmp = [p for p, a in calls if p.endswith(".bbpart") and a == os.POSIX_FADV_DONTNEED]
    # once after the write (fsync'd pages) and once before the verification read, per file
    assert len(dropped_tmp) == 2 * 3
