"""US-19 to US-26: the travel day — drones on their cable, Pi buffer, SSD at night."""

import json
import time

import pytest

from backupbox.copier import CopyEngine
from backupbox.manager import BUFFER_PLACEHOLDER_PROJECT, ActionError


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def avata(box, kname="sdc1", clips=3, start=1, **kw):
    return box.card(
        kname, brand="dji", clips=clips, start=start, clip_bytes=40_000,
        usb_vendor="DJI", usb_model="Avata 2", usb_serial="1581F6AVATA", **kw,
    )


# US-19: a drone on its USB cable is recognised as itself -------------------------

def test_us19_direct_drone_is_named_and_identified_by_usb_serial(box):
    box.ready_destination()
    avata(box)
    box.poll()
    rec = box.device("sdc1")
    assert rec.card.connection == "direct"
    assert rec.card.brand == "DJI" and rec.card.model == "Avata 2"
    assert rec.card.identity == "usb:dji:1581F6AVATA"
    job = box.job_for("sdc1")
    assert job.source_name == "DJI_Avata_2" and job.device_name == "DJI Avata 2"
    box.expire_countdowns()
    box.drain()
    # Card reformatted inside the drone: same drone, same folder, no countdown.
    box.unplug("sdc1")
    box.poll()
    avata(box, card_uuid="NEWFS", start=10)
    box.poll()
    again = box.job_for("sdc1")
    assert again.known_card and again.state == "queued" and again.source_name == "DJI_Avata_2"


def test_us19_two_drones_get_two_folders(box):
    media = box.ready_destination()
    avata(box)
    box.card("sdd1", brand="dji", clips=1, clip_bytes=1000, usb_vendor="DJI", usb_model="Mini 5 Pro", usb_serial="MINI5")
    box.poll()
    box.expire_countdowns()
    box.drain()
    sources = sorted(p.name for p in (media / "Islande_2026/Footage_RAW").iterdir())
    assert sources == ["DJI_Avata_2", "DJI_Mini_5_Pro"]


def test_us19_card_reader_from_a_camera_maker_is_still_a_reader(box):
    box.card("sdb1", brand="generic", clips=1, usb_vendor="Sony", usb_model="MRW-G2", usb_serial="READER1")
    box.poll()
    assert box.device("sdb1").card.connection == "reader"


def test_us19_gopro_over_mtp(box):
    media = box.ready_destination()
    box.card("mtp1", brand="gopro", clips=2, clip_bytes=30_000, mtp=True, usb_model="GoPro HERO12 Black", usb_serial="C3471")
    box.poll()
    rec = next(r for r in box.manager.devices.values() if r.kind == "mtp")
    assert rec.role == "source" and rec.card.connection == "mtp"
    assert rec.card.identity == "mtp:C3471" and rec.card.brand == "GoPro"
    job = box.manager.jobs[rec.job_id]
    assert job.source_name == "GoPro_HERO12_Black"
    box.expire_countdowns()
    box.drain()
    assert job.state == "done"
    assert (media / "Islande_2026/Footage_RAW/GoPro_HERO12_Black/DCIM/100GOPRO/GX010001.MP4").is_file()
    # « Éjecter » closes the camera session.
    assert rec.status == "ejected" and rec.session is None


# US-20: no SSD in the field -> the Pi buffer ---------------------------------------

def test_us20_without_ssd_the_drone_goes_to_the_buffer(bbox):
    avata(bbox)
    bbox.poll()
    job = bbox.job_for("sdc1")
    assert job.target == "buffer" and job.destination_name == "Tampon du Pi"
    assert job.project == BUFFER_PLACEHOLDER_PROJECT
    bbox.expire_countdowns()
    bbox.drain()
    assert job.state == "done"
    stored = bbox.buffer_dir / BUFFER_PLACEHOLDER_PROJECT / "Footage_RAW/DJI_Avata_2/DCIM/DJI_001"
    assert len(list(stored.iterdir())) == 3
    snap = bbox.manager.snapshot()["buffer"]
    assert snap["pending_files"] == 3 and snap["pending"][0]["source"] == "DJI_Avata_2"


def test_us20_buffer_keeps_its_reserve(tmp_path):
    from tests.conftest import Box

    box = Box(tmp_path, buffer=True, buffer_reserve=10**15)  # more than the disk
    avata(box)
    box.poll()
    box.expire_countdowns()
    (job,) = box.drain()
    assert job.state == "failed" and "Espace insuffisant" in job.error


# US-21: plugging the SSD takes priority ---------------------------------------------

def test_us21_ssd_plugged_during_countdown_retargets_the_job(bbox):
    avata(bbox)
    bbox.poll()
    job = bbox.job_for("sdc1")
    assert job.target == "buffer" and job.state == "countdown"
    bbox.ready_destination()
    assert job.target == "ssd" and job.destination_name == "T7"
    bbox.expire_countdowns()
    bbox.drain()
    assert not any((bbox.buffer_dir).rglob("*.MP4"))


# US-22: SSD at the Airbnb -> the buffer empties itself into it -----------------------

def test_us22_buffer_drains_to_ssd_then_frees_the_pi(bbox):
    avata(bbox, clips=2)
    bbox.poll()
    bbox.expire_countdowns()
    bbox.drain()
    bbox.unplug("sdc1")
    bbox.poll()

    media = bbox.ready_destination("Islande_2026")
    bbox.poll()
    drains = [j for j in bbox.manager.jobs.values() if j.kind == "drain"]
    assert len(drains) == 1 and drains[0].state == "queued"
    assert bbox.led.state == "busy"
    bbox.drain()
    drain = drains[0]
    assert drain.state == "done", drain.error
    assert drain.project == "Islande_2026"  # placeholder project follows the SSD
    on_ssd = media / "Islande_2026/Footage_RAW/DJI_Avata_2/DCIM/DJI_001"
    assert len(list(on_ssd.iterdir())) == 2
    assert not list(bbox.buffer_dir.rglob("*.MP4"))
    assert bbox.manager.snapshot()["buffer"]["pending_files"] == 0
    assert bbox.led.state == "success"
    assert any(n["title"] == "Tampon vidé" for n in bbox.notifier.sent)
    # The SSD now knows the drone, learnt in the field.
    cfg = json.loads((media / ".backupbox/config.json").read_text())
    assert cfg["cards"]["usb:dji:1581F6AVATA"] == "DJI_Avata_2"


def test_us22_drained_clips_are_not_copied_again_to_the_buffer(bbox):
    avata(bbox, clips=2)
    bbox.poll()
    bbox.expire_countdowns()
    bbox.drain()
    bbox.unplug("sdc1")
    bbox.poll()
    bbox.ready_destination()
    bbox.poll()
    bbox.drain()
    bbox.manager.eject(bbox.key("sda1"))
    bbox.poll()
    # Next morning, the drone was not formatted: nothing to copy again.
    avata(bbox, clips=2)
    bbox.poll()
    (job,) = bbox.drain()
    assert job.target == "buffer"
    assert job.summary["copied"] == 0 and job.summary["skipped"] == 2


def test_us22_failed_drain_keeps_buffer_and_does_not_loop(bbox):
    avata(bbox, clips=1)
    bbox.poll()
    bbox.expire_countdowns()
    bbox.drain()
    bbox.ssd(free_bytes=10)
    bbox.poll()
    bbox.manager.init_destination(bbox.key("sda1"), "P")
    (drain,) = bbox.drain()
    assert drain.state == "failed" and "Espace insuffisant" in drain.error
    assert list(bbox.buffer_dir.rglob("*.MP4"))
    bbox.poll()
    assert bbox.drain() == []  # no automatic retry loop
    retry = bbox.manager.drain_now()  # a tap on « Vider » retries
    assert len(retry) == 1
    (again,) = bbox.drain()
    assert again.state == "failed"


def test_us22_ssd_unplugged_mid_drain(bbox, monkeypatch):
    avata(bbox, clips=3)
    bbox.poll()
    bbox.expire_countdowns()
    bbox.drain()
    bbox.ready_destination()
    bbox.poll()
    real = CopyEngine._copy_to_tmp
    calls = {"n": 0}

    def pull(self, f, tmp):
        calls["n"] += 1
        if calls["n"] == 2:
            bbox.unplug("sda1")
        return real(self, f, tmp)

    monkeypatch.setattr(CopyEngine, "_copy_to_tmp", pull)
    (drain,) = bbox.drain()
    assert drain.state == "failed"
    assert len(list(bbox.buffer_dir.rglob("*.MP4"))) == 3  # nothing released


def test_us22_auto_drain_off_needs_a_tap(bbox):
    bbox.store.update_settings({"auto_drain": False})
    avata(bbox, clips=1)
    bbox.poll()
    bbox.expire_countdowns()
    bbox.drain()
    bbox.ready_destination()
    bbox.poll()
    assert bbox.drain() == []
    jobs = bbox.manager.drain_now()
    assert len(jobs) == 1
    bbox.drain()
    assert jobs[0].state == "done"


# US-23: presets -------------------------------------------------------------------

def test_us23_videos_only_preset_is_applied_and_remembered(box):
    media = box.ready_destination()
    avata(box, clips=2, sidecars=True)
    box.poll()
    rec = box.device("sdc1")
    assert wait_for(lambda: rec.inventory is not None)
    inv = rec.inventory
    assert inv["video"]["files"] == 2 and inv["preview"]["files"] == 2 and inv["other"]["files"] == 2
    job = box.job_for("sdc1")
    box.manager.set_job_preset(job.id, "videos_only")
    box.manager.start_job_now(job.id)
    box.drain()
    folder = media / "Islande_2026/Footage_RAW/DJI_Avata_2/DCIM/DJI_001"
    assert sorted(p.suffix for p in folder.iterdir()) == [".MP4", ".MP4"]
    assert job.summary["excluded_files"] == 4 and "ignorés" in job.message
    # Remembered for this drone next time.
    box.unplug("sdc1")
    box.poll()
    avata(box, clips=1, start=5, sidecars=True)
    box.poll()
    assert box.job_for("sdc1").preset == "videos_only"


def test_us23_no_previews_keeps_telemetry(box):
    media = box.ready_destination()
    box.store.update_settings({"default_preset": "no_previews"})
    avata(box, clips=1, sidecars=True)
    box.poll()
    box.expire_countdowns()
    box.drain()
    folder = media / "Islande_2026/Footage_RAW/DJI_Avata_2/DCIM/DJI_001"
    assert sorted(p.suffix for p in folder.iterdir()) == [".MP4", ".SRT"]


def test_us23_unknown_preset_refused(box):
    box.ready_destination()
    avata(box)
    box.poll()
    with pytest.raises(ActionError) as exc:
        box.manager.set_job_preset(box.job_for("sdc1").id, "tout")
    assert exc.value.status == 400


# US-24: notifications ----------------------------------------------------------------

def test_us24_push_notifications_on_detection_and_result(box):
    box.ready_destination()
    avata(box, clips=1)
    box.poll()
    first = box.notifier.sent[-1]
    assert first["title"] == "DJI Avata 2 branché" and "dans 30 s" in first["message"]
    box.expire_countdowns()
    box.drain()
    last = box.notifier.sent[-1]
    assert last["title"] == "DJI Avata 2 : sauvegarde vérifiée"


# US-25: the physical button ------------------------------------------------------------

def test_us25_short_press_starts_what_waits(box):
    box.ready_destination()
    avata(box)
    box.poll()
    job = box.job_for("sdc1")
    assert job.state == "countdown"
    box.manager.button_short()
    assert job.state == "queued" and box.led.chimes[-1] == "start"
    box.manager.button_short()
    assert box.led.chimes[-1] == "ack"


def test_us25_long_press_shuts_down_unless_writing(box):
    box.ready_destination()
    avata(box)
    box.poll()
    box.job_for("sdc1").state = "running"
    box.manager.button_long()
    assert box.led.chimes[-1] == "refused"
    box.job_for("sdc1").state = "done"
    box.manager.button_long()
    assert box.led.chimes[-1] == "shutdown"


# US-26: safety signal ----------------------------------------------------------------

def test_us26_indicator_states_follow_the_buffer_day(bbox):
    avata(bbox, clips=1)
    bbox.poll()
    assert bbox.led.state == "attention"
    bbox.expire_countdowns()
    assert bbox.led.state == "busy"
    bbox.drain()
    assert bbox.led.state == "success"
