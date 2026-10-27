"""Orchestrator: watches devices, mounts them, starts backups, runs the queue.

Two threads: the poller (device changes, countdowns, LED) and the worker (one
backup at a time, in arrival order). The API only reads snapshots and calls the
action methods, which all take the same lock.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from .copier import CopyAborted, CopyEngine, Progress, human_bytes, scan_source
from .destination import (
    DestinationConfig,
    clean_tmp,
    init_destination,
    is_destination,
    list_projects,
    list_sources,
    load_config,
    now_iso,
    project_meta_dir,
    raw_dir,
    save_config,
    tmp_dir,
    validate_project,
    write_json_atomic,
)
from .detect import CardInfo, detect_card, validate_source_name
from .devices import BlockDevice
from .manifest import Manifest
from .platform import Platform, PlatformError
from .settings import StateStore
from .system import Led, clock_synchronized, power_action, power_status

log = logging.getLogger(__name__)

ACTIVE_STATES = {"waiting", "held", "countdown", "queued", "running"}
FINAL_STATES = {"done", "partial", "failed", "cancelled"}


class ActionError(Exception):
    """A user action was refused; ``status`` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass
class DeviceRecord:
    dev: BlockDevice
    role: str = "pending"  # pending | source | destination | unknown | unsupported | error
    status: str = "mounting"  # mounting | ready | ejected | error
    mountpoint: str | None = None
    read_only: bool = True
    mounted_by_us: bool = False
    card: CardInfo | None = None
    dest: DestinationConfig | None = None
    error: str | None = None
    job_id: str | None = None
    autostart: bool = True

    def to_dict(self, platform: Platform) -> dict:
        data = {
            "key": self.dev.key,
            "name": self.dev.display_name,
            "label": self.dev.label,
            "kname": self.dev.kname,
            "fstype": self.dev.fstype,
            "size": self.dev.size,
            "model": self.dev.model,
            "role": self.role,
            "status": self.status,
            "read_only": self.read_only,
            "error": self.error,
            "job_id": self.job_id,
            "card": asdict(self.card) if self.card else None,
        }
        if self.role == "destination" and self.dest is not None:
            data["project"] = self.dest.project
            data["destination_id"] = self.dest.id
        if self.mountpoint and self.status == "ready":
            try:
                data["free_bytes"] = platform.free_bytes(self.mountpoint)
                data["total_bytes"] = platform.total_bytes(self.mountpoint)
            except OSError:
                pass
        return data


@dataclass
class Job:
    id: str
    device_key: str
    device_name: str
    card_identity: str
    brand: str | None
    source_name: str
    auto: bool
    created_at: str
    state: str = "waiting"
    known_card: bool = False
    name_set_by_user: bool = False
    project: str | None = None
    dest_key: str | None = None
    destination_name: str | None = None
    deadline: float | None = None
    wait_reason: str | None = None
    message: str | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    summary: dict | None = None
    problems: list[dict] = field(default_factory=list)
    progress: Progress = field(default_factory=Progress)
    cancel: threading.Event = field(default_factory=threading.Event)
    cancel_reason: str | None = None

    def to_dict(self, include_problems: bool = False) -> dict:
        data = {
            "id": self.id,
            "device_key": self.device_key,
            "device_name": self.device_name,
            "card_identity": self.card_identity,
            "brand": self.brand,
            "source_name": self.source_name,
            "auto": self.auto,
            "state": self.state,
            "known_card": self.known_card,
            "project": self.project,
            "destination_name": self.destination_name,
            "countdown_s": max(0, round(self.deadline - time.monotonic())) if self.state == "countdown" and self.deadline else None,
            "wait_reason": self.wait_reason,
            "message": self.message,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": self.duration_s,
            "summary": self.summary,
            "problem_count": len(self.problems),
            "progress": self.progress.to_dict(),
        }
        if include_problems:
            data["problems"] = self.problems
        return data


def _new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


class Manager:
    def __init__(
        self,
        platform: Platform,
        store: StateStore,
        *,
        simulate: bool = False,
        poll_interval: float = 2.0,
        led: Led | None = None,
    ) -> None:
        self.platform = platform
        self.store = store
        self.simulate = simulate
        self.poll_interval = poll_interval
        self.led = led or Led(None)
        self.devices: dict[str, DeviceRecord] = {}
        self.jobs: dict[str, Job] = {}
        self.notices: deque[dict] = deque(maxlen=20)
        self._queue: deque[str] = deque()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._health: dict = {}
        self._health_at = 0.0
        self._notice_seq = 0

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        for target, name in ((self._poll_loop, "bb-poller"), (self._work_loop, "bb-worker")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            for job in self.jobs.values():
                if job.state == "running":
                    job.cancel_reason = "shutdown"
                    job.cancel.set()
        for thread in self._threads:
            thread.join(timeout=15)
        self.led.set("idle")

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # keep watching whatever happens
                log.exception("Erreur pendant la surveillance des périphériques")
            self._stop.wait(self.poll_interval)

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            job = self._next_job()
            if job is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                self.run_job(job)
            except Exception:
                log.exception("Erreur inattendue dans la sauvegarde %s", job.id)

    # --------------------------------------------------------------- polling

    def poll_once(self) -> None:
        try:
            devices = self.platform.list_devices()
        except Exception as exc:
            log.warning("Liste des périphériques impossible : %s", exc)
            return
        current = {d.key: d for d in devices}
        new_records: list[DeviceRecord] = []
        with self._lock:
            for key in list(self.devices):
                if key not in current:
                    self._on_removed(key)
            for key, dev in current.items():
                rec = self.devices.get(key)
                if rec is None:
                    rec = DeviceRecord(dev=dev)
                    self.devices[key] = rec
                    new_records.append(rec)
                else:
                    rec.dev = dev
        for rec in new_records:
            self._setup(rec)
        with self._lock:
            for rec in new_records:
                if rec.role == "destination":
                    self._notice("info", f"SSD « {rec.dev.display_name} » prêt.")
                    self._rearm_pending()
                elif rec.role == "source":
                    self._maybe_autostart(rec)
            self._tick()
            self._update_led()

    def _setup(self, rec: DeviceRecord) -> None:
        """Mount a new device and decide what it is. Runs without the lock."""
        dev = rec.dev
        if not dev.supported:
            rec.role, rec.status = "unsupported", "error"
            rec.error = f"Système de fichiers « {dev.fstype} » non pris en charge."
            return
        try:
            if dev.mountpoint:
                mountpoint, rec.mounted_by_us = dev.mountpoint, False
                rec.read_only = not os.access(mountpoint, os.W_OK)
            else:
                writable = dev.uuid is not None and dev.uuid in self.store.known_destinations()
                mountpoint = self.platform.mount(dev, read_only=not writable)
                rec.mounted_by_us, rec.read_only = True, not writable
            root = Path(mountpoint)
            if is_destination(root):
                if rec.read_only and rec.mounted_by_us:
                    self.platform.unmount(dev)
                    mountpoint = self.platform.mount(dev, read_only=False)
                    rec.read_only = False
                rec.dest = load_config(Path(mountpoint))
                rec.role = "destination"
                self.store.remember_destination(dev.uuid)
                removed = clean_tmp(Path(mountpoint))
                if removed:
                    log.info("%d copie(s) interrompue(s) nettoyée(s) sur %s", removed, dev.path)
            else:
                if not rec.read_only and rec.mounted_by_us:
                    # A former destination lost its marker: cards stay read-only.
                    self.platform.unmount(dev)
                    mountpoint = self.platform.mount(dev, read_only=True)
                    rec.read_only = True
                rec.card = detect_card(Path(mountpoint), dev.label, dev.uuid, dev.size)
                rec.role = "source" if rec.card.is_camera else "unknown"
            rec.mountpoint = mountpoint
            rec.status = "ready"
            rec.error = None
        except (PlatformError, OSError) as exc:
            rec.role, rec.status = "error", "error"
            rec.error = f"Montage impossible : {exc}"
            log.warning("Montage de %s impossible : %s", dev.path, exc)

    def _on_removed(self, key: str) -> None:
        rec = self.devices.pop(key)
        for job in self.jobs.values():
            if job.state not in ACTIVE_STATES:
                continue
            if job.device_key == key:
                if job.state == "running":
                    job.cancel_reason = "source_gone"
                    job.cancel.set()
                else:
                    self._finish_unstarted(job, "cancelled", "Carte retirée avant la sauvegarde.")
            elif job.state == "running" and job.dest_key == key:
                job.cancel_reason = "dest_gone"
                job.cancel.set()
        if rec.role == "destination":
            if rec.status != "ejected":
                self._notice("warning", f"SSD « {rec.dev.display_name} » débranché.")
            self._rearm_pending()

    def _tick(self) -> None:
        now = time.monotonic()
        for job in self.jobs.values():
            if job.state == "countdown" and job.deadline is not None and job.deadline <= now:
                self._enqueue(job)

    # ---------------------------------------------------------- destinations

    def _destinations(self) -> list[DeviceRecord]:
        return sorted(
            (r for r in self.devices.values() if r.role == "destination" and r.status == "ready"),
            key=lambda r: r.dev.key,
        )

    def active_destination(self) -> DeviceRecord | None:
        dests = self._destinations()
        preferred = self.store.preferred_destination()
        for rec in dests:
            if preferred and rec.dev.uuid == preferred:
                return rec
        return dests[0] if dests else None

    # ------------------------------------------------------------------ jobs

    def _maybe_autostart(self, rec: DeviceRecord) -> None:
        if rec.job_id is None and rec.autostart and self.store.settings.auto_backup:
            self._create_job(rec, source_name=None, auto=True)

    def _create_job(self, rec: DeviceRecord, source_name: str | None, auto: bool) -> Job:
        card = rec.card or detect_card(Path(rec.mountpoint), rec.dev.label, rec.dev.uuid, rec.dev.size)
        rec.card = card
        job = Job(
            id=_new_job_id(),
            device_key=rec.dev.key,
            device_name=rec.dev.display_name,
            card_identity=card.identity,
            brand=card.brand,
            source_name=source_name or card.default_source,
            auto=auto,
            created_at=now_iso(),
            name_set_by_user=source_name is not None,
        )
        self.jobs[job.id] = job
        rec.job_id = job.id
        self._arm(job)
        return job

    def _arm(self, job: Job) -> None:
        """Place a not-yet-started job in the right state for the current hardware."""
        dest = self.active_destination()
        if dest is None:
            job.state, job.deadline = "waiting", None
            job.wait_reason = "Branchez le SSD de destination."
            return
        if dest.dest.project is None:
            job.state, job.deadline = "waiting", None
            job.wait_reason = "Choisissez le projet (voyage) sur le SSD."
            return
        job.wait_reason = None
        job.project = dest.dest.project
        job.destination_name = dest.dev.display_name
        known = dest.dest.cards.get(job.card_identity)
        if not job.name_set_by_user:
            job.source_name = known or job.source_name
        job.known_card = known is not None and known == job.source_name
        if job.state == "held":
            return
        delay = 0 if (not job.auto or job.known_card) else self.store.settings.countdown_s
        if delay > 0:
            job.state = "countdown"
            job.deadline = time.monotonic() + delay
        else:
            self._enqueue(job)

    def _rearm_pending(self) -> None:
        for job in self.jobs.values():
            if job.state in {"waiting", "countdown", "queued", "held"}:
                if job.state == "queued":
                    if job.id in self._queue:
                        self._queue.remove(job.id)
                previous = job.state
                if previous != "held":
                    job.state = "waiting"
                self._arm(job)

    def _enqueue(self, job: Job) -> None:
        job.state = "queued"
        job.deadline = None
        if job.id not in self._queue:
            self._queue.append(job.id)
        self._wake.set()

    def _next_job(self) -> Job | None:
        with self._lock:
            while self._queue:
                job = self.jobs.get(self._queue.popleft())
                if job is not None and job.state == "queued":
                    job.state = "running"
                    return job
        return None

    def _finish_unstarted(self, job: Job, state: str, message: str) -> None:
        job.state = state
        job.deadline = None
        job.message = message
        job.finished_at = now_iso()
        if job.id in self._queue:
            self._queue.remove(job.id)

    # --------------------------------------------------------------- running

    def _device_present(self, key: str) -> bool:
        try:
            return any(d.key == key for d in self.platform.list_devices())
        except Exception:
            return False

    def run_job(self, job: Job) -> None:
        """Execute one backup. Public so tests can run jobs synchronously."""
        with self._lock:
            src = self.devices.get(job.device_key)
            dest = self.active_destination()
            if src is None or src.status != "ready" or not src.mountpoint:
                self._finish_unstarted(job, "failed", "La carte n'est plus disponible.")
                self._update_led()
                return
            if dest is None or dest.dest.project is None:
                job.state = "waiting"
                self._arm(job)
                return
            job.state = "running"
            job.dest_key = dest.dev.key
            job.project = dest.dest.project
            job.destination_name = dest.dev.display_name
            job.started_at = now_iso()
            src_root = Path(src.mountpoint)
            dest_mount = Path(dest.mountpoint)
            dest_rec = dest
            settings = self.store.settings
            self._update_led()

        t0 = time.monotonic()
        outcomes = []
        try:
            raw = raw_dir(dest_mount, job.project, create=True)
            manifest = Manifest(project_meta_dir(dest_mount, job.project) / "manifests" / f"{job.source_name}.jsonl")
            engine = CopyEngine(
                src_root=src_root,
                dest_dir=raw / job.source_name,
                tmp_dir=tmp_dir(dest_mount),
                manifest=manifest,
                job_id=job.id,
                card_identity=job.card_identity,
                verify=settings.verify,
                strict=settings.strict_recheck,
                cancel=job.cancel,
                progress=job.progress,
                src_alive=lambda: self._device_present(job.device_key) and src_root.is_dir(),
                dest_alive=lambda: self._device_present(job.dest_key) and dest_mount.is_dir(),
            )
            job.progress.phase = "analyse de la carte"
            try:
                files = scan_source(src_root, job.cancel)
            except OSError as exc:
                if not self._device_present(job.device_key):
                    raise CopyAborted("source_gone", "La carte a été retirée pendant l'analyse.") from exc
                raise
            plan = engine.plan(files)
            engine.check_space(plan, self.platform.free_bytes(str(dest_mount)))
            outcomes = engine.run(plan)
            job.progress.phase = "écriture sur le disque"
            os.sync()
            self._summarise(job, outcomes, plan.bytes_card, settings.verify)
        except CopyAborted as exc:
            reason = job.cancel_reason or exc.reason
            job.state = "cancelled" if reason in {"cancelled", "user", "shutdown"} else "failed"
            job.error = {
                "source_gone": "La carte a été retirée pendant la sauvegarde. Réinsérez-la : seuls les fichiers manquants seront copiés.",
                "dest_gone": "Le SSD a été débranché pendant la sauvegarde. Rebranchez-le puis relancez.",
                "shutdown": "BackupBox s'est arrêtée pendant la sauvegarde.",
            }.get(reason, str(exc))
            if job.state == "cancelled":
                job.message = job.error if reason == "shutdown" else "Sauvegarde annulée."
                job.error = None
            self._summarise_partial(job, outcomes)
        except Exception as exc:
            log.exception("Sauvegarde %s en échec", job.id)
            job.state = "failed"
            job.error = f"Erreur inattendue : {exc}"
            self._summarise_partial(job, outcomes)
        finally:
            job.finished_at = now_iso()
            job.duration_s = round(time.monotonic() - t0, 1)
            job.progress.current_file = None
            job.progress.current_step = None
            job.progress.phase = "terminé"

        if job.state in {"done", "partial"}:
            self._remember_card(dest_rec, job)
        self._write_report(job, dest_mount)
        self.store.add_history(job.to_dict(include_problems=True))
        with self._lock:
            src = self.devices.get(job.device_key)
            if job.state == "done":
                self._notice("success", f"{job.device_name} : sauvegarde vérifiée. La carte peut être retirée.")
                if settings.auto_eject and src is not None and src.status == "ready":
                    try:
                        self._eject_record(src, notify=False)
                    except (PlatformError, ActionError) as exc:
                        log.warning("Éjection automatique impossible : %s", exc)
            elif job.state == "partial":
                self._notice("error", f"{job.device_name} : {job.summary['failed']} fichier(s) en échec. Ne formatez pas la carte.")
            elif job.state == "failed":
                self._notice("error", f"{job.device_name} : {job.error}")
            self._update_led()

    def _summarise(self, job: Job, outcomes, bytes_card: int, verified: bool) -> None:
        counts = {"copied": 0, "skipped": 0, "conflict": 0, "failed": 0}
        bytes_copied = 0
        problems = []
        for o in outcomes:
            counts[o.status] += 1
            if o.status in {"copied", "conflict"}:
                bytes_copied += o.size
            if o.status == "failed":
                problems.append({"file": o.rel, "type": "échec", "detail": o.error})
            elif o.status == "conflict":
                problems.append(
                    {
                        "file": o.rel,
                        "type": "conflit",
                        "detail": f"Un fichier différent portait déjà ce nom : copié sous {o.dest}",
                    }
                )
        job.summary = {
            "files": len(outcomes),
            "copied": counts["copied"],
            "skipped": counts["skipped"],
            "conflicts": counts["conflict"],
            "failed": counts["failed"],
            "bytes_card": bytes_card,
            "verified": verified,
        }
        job.problems = problems
        if counts["failed"]:
            job.state = "partial"
            job.message = f"{counts['failed']} fichier(s) n'ont pas pu être sauvegardés. Ne formatez pas la carte."
        else:
            job.state = "done"
            new = counts["copied"] + counts["conflict"]
            if new == 0:
                job.message = f"Déjà sauvegardée : les {counts['skipped']} fichier(s) étaient déjà sur le SSD."
            else:
                verb = "copiés et vérifiés" if verified else "copiés (sans relecture)"
                parts = [f"{new} fichier(s) {verb} ({human_bytes(bytes_copied)})"]
                if counts["skipped"]:
                    parts.append(f"{counts['skipped']} déjà présent(s)")
                if counts["conflict"]:
                    parts.append(f"{counts['conflict']} conflit(s) de nom : les deux versions sont gardées")
                job.message = ", ".join(parts) + "."

    def _summarise_partial(self, job: Job, outcomes) -> None:
        if outcomes:
            done = sum(1 for o in outcomes if o.status in {"copied", "conflict", "skipped"})
            job.summary = {"files": job.progress.files_total, "completed": done}

    def _remember_card(self, dest_rec: DeviceRecord, job: Job) -> None:
        cfg = dest_rec.dest
        if cfg is None or cfg.cards.get(job.card_identity) == job.source_name:
            return
        cfg.cards[job.card_identity] = job.source_name
        try:
            save_config(Path(dest_rec.mountpoint), cfg)
        except OSError as exc:
            log.warning("Mémorisation de la carte impossible : %s", exc)

    def _write_report(self, job: Job, dest_mount: Path) -> None:
        if job.project is None or not dest_mount.is_dir():
            return
        report = job.to_dict(include_problems=True)
        report["generated_by"] = f"BackupBox {__version__}"
        try:
            write_json_atomic(project_meta_dir(dest_mount, job.project) / "reports" / f"{job.id}.json", report)
        except OSError as exc:
            log.warning("Rapport %s non écrit : %s", job.id, exc)

    # --------------------------------------------------------------- actions

    def _record(self, key: str) -> DeviceRecord:
        rec = self.devices.get(key)
        if rec is None:
            raise ActionError("Périphérique introuvable (débranché ?).", 404)
        return rec

    def _job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise ActionError("Sauvegarde introuvable.", 404)
        return job

    def start_backup(self, key: str, source_name: str | None = None) -> Job:
        with self._lock:
            rec = self._record(key)
            if rec.status != "ready" or rec.role not in {"source", "unknown"}:
                raise ActionError("Ce périphérique ne peut pas être sauvegardé.")
            if rec.job_id and self.jobs[rec.job_id].state in ACTIVE_STATES:
                raise ActionError("Une sauvegarde de cette carte est déjà prévue.")
            if source_name is not None:
                source_name = self._valid_source(source_name)
            job = self._create_job(rec, source_name=source_name, auto=False)
            self._update_led()
            return job

    def _valid_source(self, name: str) -> str:
        try:
            return validate_source_name(name)
        except ValueError as exc:
            raise ActionError(str(exc), 400) from exc

    def set_job_source(self, job_id: str, name: str) -> Job:
        with self._lock:
            job = self._job(job_id)
            if job.state not in {"waiting", "held", "countdown", "queued"}:
                raise ActionError("La source ne peut plus être changée : la sauvegarde a commencé.")
            job.source_name = self._valid_source(name)
            job.name_set_by_user = True
            dest = self.active_destination()
            known = dest.dest.cards.get(job.card_identity) if dest and dest.dest else None
            job.known_card = known == job.source_name
            return job

    def hold_job(self, job_id: str) -> Job:
        with self._lock:
            job = self._job(job_id)
            if job.state in {"countdown", "queued", "waiting"}:
                if job.id in self._queue:
                    self._queue.remove(job.id)
                job.state, job.deadline = "held", None
                job.wait_reason = "En attente de votre confirmation."
            return job

    def start_job_now(self, job_id: str) -> Job:
        with self._lock:
            job = self._job(job_id)
            if job.state not in {"held", "countdown", "waiting", "queued"}:
                raise ActionError("Cette sauvegarde n'est pas en attente.")
            dest = self.active_destination()
            if dest is None or dest.dest.project is None:
                job.state = "waiting"
                self._arm(job)
                raise ActionError(job.wait_reason or "Destination indisponible.")
            job.wait_reason = None
            job.project = dest.dest.project
            job.destination_name = dest.dev.display_name
            self._enqueue(job)
            self._update_led()
            return job

    def cancel_job(self, job_id: str) -> Job:
        with self._lock:
            job = self._job(job_id)
            if job.state == "running":
                job.cancel_reason = "user"
                job.cancel.set()
            elif job.state in ACTIVE_STATES:
                self._finish_unstarted(job, "cancelled", "Sauvegarde annulée.")
                rec = self.devices.get(job.device_key)
                if rec is not None:
                    rec.autostart = False
            self._update_led()
            return job

    def _busy_with(self, key: str) -> bool:
        return any(
            j.state == "running" and key in {j.device_key, j.dest_key} for j in self.jobs.values()
        )

    def eject(self, key: str) -> DeviceRecord:
        with self._lock:
            rec = self._record(key)
            self._eject_record(rec)
            self._update_led()
            return rec

    def _eject_record(self, rec: DeviceRecord, notify: bool = True) -> None:
        if self._busy_with(rec.dev.key):
            raise ActionError("Une sauvegarde utilise ce périphérique : annulez-la ou attendez la fin.")
        os.sync()
        if rec.status == "ready" and (rec.mounted_by_us or rec.dev.mountpoint):
            self.platform.unmount(rec.dev)
        if rec.role == "destination":
            try:
                self.platform.power_off(rec.dev)
            except PlatformError as exc:
                log.info("Mise hors tension de %s impossible : %s", rec.dev.disk_path, exc)
        rec.status = "ejected"
        rec.mountpoint = None
        for job in self.jobs.values():
            if job.device_key == rec.dev.key and job.state in ACTIVE_STATES - {"running"}:
                self._finish_unstarted(job, "cancelled", "Carte éjectée avant la sauvegarde.")
        if rec.role == "destination":
            self._notice("info", f"SSD « {rec.dev.display_name} » éjecté : vous pouvez le débrancher.")
            self._rearm_pending()
        elif notify:
            self._notice("info", f"« {rec.dev.display_name} » éjectée : vous pouvez la retirer.")

    def remount(self, key: str) -> DeviceRecord:
        with self._lock:
            rec = self._record(key)
            if rec.status not in {"ejected", "error"}:
                raise ActionError("Ce périphérique est déjà prêt.")
            rec.status, rec.error, rec.autostart = "mounting", None, False
        self._setup(rec)
        with self._lock:
            if rec.role == "destination":
                self._rearm_pending()
            self._update_led()
        return rec

    def init_destination(self, key: str, project: str) -> DeviceRecord:
        try:
            project = validate_project(project)
        except ValueError as exc:
            raise ActionError(str(exc), 400) from exc
        with self._lock:
            rec = self._record(key)
            if rec.role != "unknown" or rec.status != "ready":
                raise ActionError("Seul un disque non reconnu (pas une carte d'appareil photo) peut devenir le SSD de destination.")
            if rec.job_id and self.jobs[rec.job_id].state in ACTIVE_STATES:
                raise ActionError("Une sauvegarde est prévue depuis ce disque.")
            try:
                if rec.read_only:
                    if rec.mounted_by_us:
                        self.platform.unmount(rec.dev)
                        rec.mountpoint = self.platform.mount(rec.dev, read_only=False)
                        rec.read_only = False
                    else:
                        raise ActionError("Ce disque est monté en lecture seule par le système.")
                rec.dest = init_destination(Path(rec.mountpoint), project)
                raw_dir(Path(rec.mountpoint), project, create=True)
            except (PlatformError, OSError) as exc:
                raise ActionError(f"Initialisation impossible : {exc}") from exc
            rec.role, rec.card = "destination", None
            self.store.remember_destination(rec.dev.uuid)
            self.store.set_preferred_destination(rec.dev.uuid)
            self._notice("success", f"« {rec.dev.display_name} » est maintenant le SSD de sauvegarde.")
            self._rearm_pending()
            self._update_led()
            return rec

    def set_project(self, project: str, key: str | None = None) -> DeviceRecord:
        try:
            project = validate_project(project)
        except ValueError as exc:
            raise ActionError(str(exc), 400) from exc
        with self._lock:
            rec = self._record(key) if key else self.active_destination()
            if rec is None or rec.role != "destination" or rec.status != "ready":
                raise ActionError("Aucun SSD de destination branché.")
            try:
                raw_dir(Path(rec.mountpoint), project, create=True)
                rec.dest.project = project
                save_config(Path(rec.mountpoint), rec.dest)
            except OSError as exc:
                raise ActionError(f"Impossible d'écrire sur le SSD : {exc}") from exc
            self._rearm_pending()
            return rec

    def select_destination(self, key: str) -> DeviceRecord:
        with self._lock:
            rec = self._record(key)
            if rec.role != "destination":
                raise ActionError("Ce disque n'est pas un SSD de destination.")
            self.store.set_preferred_destination(rec.dev.uuid)
            self._rearm_pending()
            return rec

    def projects(self, key: str | None = None) -> dict:
        with self._lock:
            rec = self._record(key) if key else self.active_destination()
            if rec is None or rec.role != "destination" or not rec.mountpoint:
                raise ActionError("Aucun SSD de destination branché.", 404)
            mount = Path(rec.mountpoint)
            current = rec.dest.project
        projects = list_projects(mount)
        if current is not None and current not in projects:
            projects.append(current)
        return {
            "current": current,
            "projects": sorted(projects, key=str.lower),
            "sources": list_sources(mount, current) if current is not None else [],
        }

    def update_settings(self, changes: dict):
        try:
            settings = self.store.update_settings(changes)
        except (ValueError, TypeError) as exc:
            raise ActionError(str(exc), 400) from exc
        with self._lock:
            if "led" in changes and not settings.led:
                self.led.set("idle")
            self._update_led()
        return settings

    def power(self, action: str) -> None:
        with self._lock:
            if any(j.state == "running" for j in self.jobs.values()):
                raise ActionError("Une sauvegarde est en cours.")
            os.sync()
        power_action(action, self.simulate)

    # -------------------------------------------------------------- snapshot

    def _notice(self, level: str, text: str) -> None:
        self._notice_seq += 1
        self.notices.append({"id": self._notice_seq, "level": level, "text": text, "at": now_iso()})
        log.info("[%s] %s", level, text)

    def health(self) -> dict:
        now = time.monotonic()
        if now - self._health_at > 30 or not self._health:
            self._health = {"power": power_status(), "clock_synchronized": clock_synchronized()}
            self._health_at = now
        return self._health

    def snapshot(self) -> dict:
        health = self.health()
        with self._lock:
            active = self.active_destination()
            devices = [r.to_dict(self.platform) for r in sorted(self.devices.values(), key=lambda r: r.dev.key)]
            jobs = sorted(self.jobs.values(), key=lambda j: j.created_at)
            visible = [j for j in jobs if j.state in ACTIVE_STATES or j.device_key in self.devices]
            return {
                "version": __version__,
                "simulate": self.simulate,
                "settings": asdict(self.store.settings),
                "active_destination": active.dev.key if active else None,
                "devices": devices,
                "jobs": [j.to_dict() for j in visible[-20:]],
                "notices": list(self.notices),
                "health": health,
            }

    def job_detail(self, job_id: str) -> dict:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is not None:
                return job.to_dict(include_problems=True)
        for record in self.store.history():
            if record.get("id") == job_id:
                return record
        raise ActionError("Sauvegarde introuvable.", 404)

    def _update_led(self) -> None:
        if not self.store.settings.led:
            return
        if any(j.state in {"countdown", "queued", "running"} for j in self.jobs.values()):
            self.led.set("busy")
            return
        states = [self.jobs[r.job_id].state for r in self.devices.values() if r.job_id in self.jobs]
        if any(s in {"failed", "partial"} for s in states):
            self.led.set("error")
        elif any(s == "done" for s in states):
            self.led.set("success")
        else:
            self.led.set("idle")

