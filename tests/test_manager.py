"""User stories end to end on simulated hardware (see docs/DESIGN.md §2)."""

import json
import os

import pytest

from backupbox.copier import CopyEngine
from backupbox.manager import ActionError


# US-01 -----------------------------------------------------------------------

def test_us01_new_ssd_is_unknown_until_initialised(box):
    media = box.ssd()
    box.poll()
    rec = box.device("sda1")
    assert rec.role == "unknown" and rec.read_only
    box.manager.init_destination(rec.dev.key, "Islande_2026")
    assert rec.role == "destination" and not rec.read_only
    assert (media / ".backupbox/config.json").is_file()
    assert (media / "Islande_2026/Footage_RAW").is_dir()
    assert box.manager.active_destination() is rec


def test_us01_known_ssd_is_mounted_read_write_directly(box):
    box.ssd("sda1", ssd_uuid="SSD-1")
    box.poll()
    box.manager.init_destination(box.key("sda1"), "Islande_2026")
    os.rename(box.root / "sda1", box.root / ".parked")  # unplugged, data kept
    box.poll()
    calls = []
    real_mount = box.platform.mount
    box.platform.mount = lambda dev, read_only: calls.append(read_only) or real_mount(dev, read_only)
    os.rename(box.root / ".parked", box.root / "sdc1")  # plugged into another port
    box.poll()
    rec = box.device("sdc1")
    assert rec.role == "destination" and rec.read_only is False
    assert calls == [False]


def test_us01_marker_found_on_a_disk_mounted_read_only_triggers_rw_remount(box, tmp_path):
    media = box.ssd()
    (media / ".backupbox").mkdir()
    (media / ".backupbox/config.json").write_text(json.dumps({"id": "x", "project": "P"}))
    box.poll()
    rec = box.device("sda1")
    assert rec.role == "destination"
    assert rec.read_only is False
    assert box.platform.is_read_only("sda1") is False
    assert rec.dev.uuid in box.store.known_destinations()


def test_us01_camera_card_cannot_become_destination(box):
    box.card(clips=1)
    box.poll()
    with pytest.raises(ActionError):
        box.manager.init_destination(box.key("sdb1"), "P")


# US-02 -----------------------------------------------------------------------

def test_us02_change_project_and_list_existing(box):
    media = box.ready_destination("Islande_2026")
    (media / "Voyages/Japon/Footage_RAW/DJI").mkdir(parents=True)
    box.manager.set_project("Voyages/Japon")
    info = box.manager.projects()
    assert info["current"] == "Voyages/Japon"
    assert info["projects"] == ["Islande_2026", "Voyages/Japon"]
    assert info["sources"] == ["DJI"]
    with pytest.raises(ActionError) as exc:
        box.manager.set_project("../etc")
    assert exc.value.status == 400


def test_us02_no_project_keeps_cards_waiting(box):
    media = box.ssd()
    (media / ".backupbox").mkdir()
    (media / ".backupbox/config.json").write_text(json.dumps({"id": "x", "project": None}))
    box.poll()
    box.card(clips=1)
    box.poll()
    job = box.job_for("sdb1")
    assert job.state == "waiting" and "projet" in job.wait_reason
    box.manager.set_project("Trip")
    assert job.state == "countdown"


# US-03 -----------------------------------------------------------------------

def test_us03_unknown_card_starts_after_countdown_read_only(box):
    media = box.ready_destination()
    box.card(clips=2, clip_bytes=2048)
    box.poll()
    rec = box.device("sdb1")
    assert rec.role == "source" and rec.read_only and box.platform.is_read_only("sdb1")
    job = box.job_for("sdb1")
    assert job.state == "countdown" and job.source_name == "GoPro"
    assert 0 < job.to_dict()["countdown_s"] <= 30
    assert box.drain() == []
    box.expire_countdowns()
    assert job.state == "queued"
    box.drain()
    assert job.state == "done"
    assert (box.raw(media) / "GoPro/DCIM/100GOPRO/GX010001.MP4").is_file()


def test_us03_card_waits_for_ssd(box):
    box.card(clips=1)
    box.poll()
    job = box.job_for("sdb1")
    assert job.state == "waiting" and "SSD" in job.wait_reason
    box.ready_destination()
    assert job.state == "countdown"


def test_us03_auto_backup_off_needs_a_tap(box):
    box.store.update_settings({"auto_backup": False})
    box.ready_destination()
    box.card(clips=1)
    box.poll()
    assert box.device("sdb1").job_id is None
    job = box.manager.start_backup(box.key("sdb1"))
    assert job.state == "queued"
    box.drain()
    assert job.state == "done"


def test_us03_disk_without_dcim_is_never_backed_up_automatically(box):
    box.ready_destination()
    other = box.ssd("sdc1", label="Archives")
    (other / "docs").mkdir()
    (other / "docs/a.txt").write_text("x")
    box.poll()
    rec = box.device("sdc1")
    assert rec.role == "unknown" and rec.job_id is None
    job = box.manager.start_backup(rec.dev.key, "Archives")
    box.drain()
    assert job.state == "done"


def test_us03_mount_failure_is_reported(box):
    media = box.card(clips=1)
    meta = json.loads((media.parent / "device.json").read_text())
    meta["fail_mount"] = "wrong fs type, bad superblock"
    (media.parent / "device.json").write_text(json.dumps(meta))
    box.poll()
    rec = box.device("sdb1")
    assert rec.status == "error" and "bad superblock" in rec.error


# US-04 -----------------------------------------------------------------------

def test_us04_rename_source_then_card_is_remembered(box):
    media = box.ready_destination()
    box.card(clips=1, serial="C1")
    box.poll()
    job = box.job_for("sdb1")
    box.manager.hold_job(job.id)
    assert job.state == "held"
    box.expire_countdowns()
    assert job.state == "held"
    with pytest.raises(ActionError):
        box.manager.set_job_source(job.id, "GoPro Keven")
    box.manager.set_job_source(job.id, "GoPro_Keven")
    box.manager.start_job_now(job.id)
    box.drain()
    assert job.state == "done"
    assert (box.raw(media) / "GoPro_Keven").is_dir()
    cfg = json.loads((media / ".backupbox/config.json").read_text())
    assert cfg["cards"] == {"gopro:C1": "GoPro_Keven"}

    # Same camera, freshly formatted card (new uuid): no countdown, same folder.
    box.unplug("sdb1")
    box.card("sdb1", clips=1, serial="C1", start=7)
    box.poll()
    again = box.job_for("sdb1")
    assert again.known_card and again.source_name == "GoPro_Keven" and again.state == "queued"


# US-05 -----------------------------------------------------------------------

def test_us05_report_history_and_card_untouched(box):
    media = box.ready_destination()
    card = box.card(clips=3, clip_bytes=100_000)
    before = {p: p.read_bytes() for p in card.rglob("*") if p.is_file()}
    box.poll()
    box.expire_countdowns()
    (job,) = box.drain()
    assert job.state == "done"
    assert job.summary["copied"] == 4 and job.summary["verified"] is True
    assert "copiés et vérifiés" in job.message
    assert {p: p.read_bytes() for p in card.rglob("*") if p.is_file()} == before
    report = json.loads((media / "Islande_2026/.backupbox/reports" / f"{job.id}.json").read_text())
    assert report["state"] == "done" and report["source_name"] == "GoPro"
    assert box.store.history()[0]["id"] == job.id
    assert box.manager.job_detail(job.id)["summary"]["files"] == 4


# US-06 -----------------------------------------------------------------------

def test_us06_reinserted_card_copies_only_new_clips(box):
    media = box.ready_destination()
    card = box.card(clips=2, serial="S")
    box.poll()
    box.expire_countdowns()
    box.drain()
    box.unplug("sdb1")
    box.poll()
    # same card, two more clips shot since
    card2 = box.card("sdb1", clips=0, serial="S")
    for rel in ["DCIM/100GOPRO/GX010001.MP4", "DCIM/100GOPRO/GX010002.MP4", "MISC/version.txt"]:
        src = media / "Islande_2026/Footage_RAW/GoPro" / rel
        dst = card2 / rel
        dst.write_bytes(src.read_bytes())
        st = src.stat()
        os.utime(dst, ns=(st.st_mtime_ns, st.st_mtime_ns))
    (card2 / "DCIM/100GOPRO/GX010003.MP4").write_bytes(b"new")
    box.poll()
    (job,) = box.drain()
    assert job.summary["copied"] == 1 and job.summary["skipped"] == 3
    assert job.known_card


# US-07 -----------------------------------------------------------------------

def test_us07_auto_eject_after_success_and_manual_eject(box):
    box.ready_destination()
    box.card(clips=1)
    box.poll()
    box.expire_countdowns()
    box.drain()
    rec = box.device("sdb1")
    assert rec.status == "ejected" and box.platform.is_read_only("sdb1") is None
    box.poll()  # still physically present: must not be remounted
    assert rec.status == "ejected"
    box.manager.remount(rec.dev.key)
    assert rec.status == "ready" and rec.job_id  # previous job kept, no new auto job
    box.manager.eject(rec.dev.key)
    assert rec.status == "ejected"


def test_us07_ssd_eject_powers_off_and_disappears(box):
    box.ready_destination()
    box.manager.eject(box.key("sda1"))
    box.poll()
    assert all(r.dev.kname != "sda1" for r in box.manager.devices.values())
    assert any("éjecté" in n["text"] for n in box.manager.notices)


def test_us07_cannot_eject_while_copying(box):
    box.ready_destination()
    box.card(clips=1)
    box.poll()
    job = box.job_for("sdb1")
    job.state = "running"
    job.dest_key = box.key("sda1")
    with pytest.raises(ActionError):
        box.manager.eject(box.key("sda1"))
    with pytest.raises(ActionError):
        box.manager.eject(box.key("sdb1"))


# US-08 -----------------------------------------------------------------------

def test_us08_not_enough_space(box):
    media = box.ssd(free_bytes=1000)
    box.poll()
    box.manager.init_destination(box.key("sda1"), "P")
    box.card(clips=2, clip_bytes=10_000)
    box.poll()
    box.expire_countdowns()
    (job,) = box.drain()
    assert job.state == "failed" and "Espace insuffisant" in job.error
    assert not any((media / "P/Footage_RAW").iterdir())


# US-09 -----------------------------------------------------------------------

def test_us09_card_pulled_mid_copy(box, monkeypatch):
    media = box.ready_destination()
    box.card(clips=3, clip_bytes=50_000)
    box.poll()
    box.expire_countdowns()
    real = CopyEngine._copy_to_tmp
    state = {"n": 0}

    def pull_after_first(self, f, tmp):
        state["n"] += 1
        if state["n"] == 2:
            box.unplug("sdb1")
        return real(self, f, tmp)

    monkeypatch.setattr(CopyEngine, "_copy_to_tmp", pull_after_first)
    (job,) = box.drain()
    assert job.state == "failed"
    assert "débranché" in job.error and "Rebranchez" in job.error
    assert list((media / ".backupbox/tmp").iterdir()) == []
    finals = list((box.raw(media) / "GoPro").rglob("*.MP4"))
    assert len(finals) == 1  # only the file verified before the card was pulled


def test_us09_poller_cancels_running_job_when_card_disappears(box):
    box.ready_destination()
    box.card(clips=1)
    box.poll()
    job = box.job_for("sdb1")
    job.state = "running"
    box.unplug("sdb1")
    box.poll()
    assert job.cancel.is_set() and job.cancel_reason == "source_gone"


def test_us09_unstarted_job_cancelled_when_card_removed(box):
    box.ready_destination()
    box.card(clips=1)
    box.poll()
    job = box.job_for("sdb1")
    box.unplug("sdb1")
    box.poll()
    assert job.state == "cancelled" and "débranché" in job.message


# US-10 -----------------------------------------------------------------------

def test_us10_leftovers_cleaned_when_ssd_is_mounted(box):
    media = box.ready_destination()
    (media / ".backupbox/tmp/dead.bbpart").write_bytes(b"x" * 10)
    box.manager.eject(box.key("sda1"))
    box.poll()
    # power-cycled SSD comes back
    box.platform._powered_off.clear()
    box.poll()
    assert box.device("sda1").role == "destination"
    assert list((media / ".backupbox/tmp").iterdir()) == []


# US-13 -----------------------------------------------------------------------

def test_us13_led_follows_state(box):
    box.ready_destination()
    box.card(clips=1)
    box.poll()
    assert box.led.state == "attention"  # countdown: waiting for the user, nothing written yet
    box.expire_countdowns()
    assert box.led.state == "busy"  # queued/writing: do not unplug
    box.drain()
    assert box.led.state == "success"
    box.unplug("sdb1")
    box.poll()
    assert box.led.state == "idle"


def test_us13_led_error_on_failure(box):
    box.ssd(free_bytes=10)
    box.poll()
    box.manager.init_destination(box.key("sda1"), "P")
    box.card(clips=1)
    box.poll()
    box.expire_countdowns()
    box.drain()
    assert box.led.state == "error"


# US-14 -----------------------------------------------------------------------

def test_us14_settings_persist_and_validate(box, tmp_path):
    box.manager.update_settings({"countdown_s": 5, "verify": False})
    from backupbox.settings import StateStore

    reloaded = StateStore(box.state_dir)
    assert reloaded.settings.countdown_s == 5 and reloaded.settings.verify is False
    for bad in ({"countdown_s": -1}, {"countdown_s": 9999}, {"verify": "oui"}, {"nope": 1}):
        with pytest.raises(ActionError):
            box.manager.update_settings(bad)


def test_us14_verify_off_is_reported(box):
    box.manager.update_settings({"verify": False, "countdown_s": 0})
    box.ready_destination()
    box.card(clips=1)
    box.poll()
    (job,) = box.drain()
    assert job.summary["verified"] is False and "sans relecture" in job.message


# US-15 -----------------------------------------------------------------------

def test_us15_cancel_waiting_and_running(box):
    box.ready_destination()
    box.card(clips=1)
    box.poll()
    job = box.job_for("sdb1")
    box.manager.cancel_job(job.id)
    assert job.state == "cancelled"
    box.expire_countdowns()
    assert box.drain() == []
    # a running job is cancelled through its event
    job2 = box.manager.start_backup(box.key("sdb1"))
    job2.cancel_reason = "user"
    job2.cancel.set()
    box.drain()
    assert job2.state == "cancelled" and job2.message == "Sauvegarde annulée."


# US-16 -----------------------------------------------------------------------

def test_us16_power_refused_while_copying(box):
    box.ready_destination()
    box.card(clips=1)
    box.poll()
    box.job_for("sdb1").state = "running"
    with pytest.raises(ActionError):
        box.manager.power("poweroff")


def test_us16_power_in_simulation_is_a_noop(box):
    box.manager.power("reboot")


# Multiple destinations ---------------------------------------------------------

def test_two_ssds_preferred_one_wins(box):
    box.ready_destination("A", kname="sda1")
    box.ready_destination("B", kname="sdc1")
    assert box.manager.active_destination().dev.kname == "sdc1"  # last initialised
    box.manager.select_destination(box.key("sda1"))
    assert box.manager.active_destination().dev.kname == "sda1"


def test_snapshot_shape(box):
    box.ready_destination()
    box.card(clips=1)
    box.poll()
    snap = box.manager.snapshot()
    assert snap["active_destination"] == box.key("sda1")
    roles = {d["kname"]: d["role"] for d in snap["devices"]}
    assert roles == {"sda1": "destination", "sdb1": "source"}
    dest = next(d for d in snap["devices"] if d["role"] == "destination")
    assert dest["project"] == "Islande_2026" and dest["free_bytes"] > 0
    assert snap["jobs"][0]["state"] == "countdown"


def test_wait_reason_points_to_uninitialised_ssd(box):
    box.card(clips=1)
    box.poll()
    job = box.job_for("sdb1")
    assert job.wait_reason == "Branchez le SSD de destination."
    box.ssd()
    box.poll()
    assert "Utiliser comme SSD de destination" in job.wait_reason


def test_empty_card_and_interrupted_summary(box, monkeypatch):
    box.ready_destination()
    media = box.card(clips=0)
    (media / "MISC/version.txt").unlink()
    box.poll()
    box.expire_countdowns()
    (job,) = box.drain()
    assert job.state == "done" and job.message == "Appareil vide : rien à sauvegarder."

    box.card("sdc1", clips=3, clip_bytes=10_000)
    box.poll()
    box.expire_countdowns()
    real = CopyEngine._copy_to_tmp
    calls = {"n": 0}

    def pull(self, f, tmp):
        calls["n"] += 1
        if calls["n"] == 3:
            box.unplug("sdc1")
        return real(self, f, tmp)

    monkeypatch.setattr(CopyEngine, "_copy_to_tmp", pull)
    (job2,) = box.drain()
    assert job2.state == "failed"
    assert job2.summary["copied"] == 2 and job2.summary["interrupted"] is True
