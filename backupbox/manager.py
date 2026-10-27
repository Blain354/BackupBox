"""Orchestrator: watches devices, mounts them, starts backups, runs the queue.

Two threads: the poller (device changes, countdowns, buffer drain, indicators)
and the worker (one job at a time, in arrival order). The API only reads
snapshots and calls the action methods, which all take the same lock.

Where a source goes:

* an external SSD is connected (and has a project) -> straight to the SSD;
* otherwise, if the Pi has a local buffer -> to the buffer;
* otherwise the job waits for an SSD.

Plugging an SSD while a job counts down retargets it to the SSD. When an SSD is
connected and the buffer holds files, « drain » jobs move them to the SSD
(verified copy, then deletion from the buffer only).
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
from .copier import (
    DEFAULT_PRESET,
    PRESETS,
    CopyAborted,
    CopyEngine,
    FsSource,
    Progress,
    apply_preset,
    human_bytes,
    inventory,
    scan_source,
)
from .destination import (
    DestinationConfig,
    clean_tmp,
    find_raw_dir,
    init_destination,
    is_destination,
    list_projects,
    list_sources,
    load_config,
    now_iso,
    project_meta_dir,
    project_root,
    raw_dir,
    save_config,
    tmp_dir,
    validate_project,
    write_json_atomic,
)
from .detect import CardInfo, camera_info, detect_card, validate_source_name
from .devices import BlockDevice
from .indicators import Indicators
from .manifest import Manifest
from .notify import Notifier
from .platform import Platform, PlatformError
from .settings import StateStore
from .system import clock_synchronized, power_action, power_status

log = logging.getLogger(__name__)

ACTIVE_STATES = {"waiting", "held", "countdown", "queued", "running"}
FINAL_STATES = {"done", "partial", "failed", "cancelled"}
GB = 1000**3
BUFFER_PLACEHOLDER_PROJECT = "Sans_projet"
BUFFER_STATS_TTL = 20.0


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
    session: object | None = None  # open MTP/PTP camera
    inventory: dict | None = None

    @property
    def kind(self) -> str:
        return self.dev.kind

    def source_tree(self):
        if self.kind == "mtp":
            return self.session
        return FsSource(Path(self.mountpoint)) if self.mountpoint else None

    def to_dict(self, platform: Platform) -> dict:
        data = {
            "key": self.dev.key,
            "name": self.dev.display_name,
            "label": self.dev.label,
            "kname": self.dev.kname,
            "kind": self.kind,
            "fstype": self.dev.fstype,
            "size": self.dev.size,
            "model": self.dev.model,
            "vendor": self.dev.vendor,
            "role": self.role,
            "status": self.status,
            "read_only": self.read_only,
            "error": self.error,
            "job_id": self.job_id,
            "card": asdict(self.card) if self.card else None,
            "inventory": self.inventory,
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
    kind: str = "card"  # card | drain
    state: str = "waiting"
    known_card: bool = False
    name_set_by_user: bool = False
    preset: str = DEFAULT_PRESET
    preset_set_by_user: bool = False
    project: str | None = None
    buffer_project: str | None = None  # drain jobs: folder in the buffer
    target: str | None = None  # ssd | buffer
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
            "kind": self.kind,
            "device_key": self.device_key,
            "device_name": self.device_name,
            "card_identity": self.card_identity,
            "brand": self.brand,
            "source_name": self.source_name,
            "auto": self.auto,
            "state": self.state,
            "known_card": self.known_card,
            "preset": self.preset,
            "project": self.project,
            "target": self.target,
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
        led=None,
        buffer_dir: Path | None = None,
        buffer_reserve: int = 10 * GB,
        notifier: Notifier | None = None,
    ) -> None:
        self.platform = platform
        self.store = store
        self.simulate = simulate
        self.poll_interval = poll_interval
        self.led = led if led is not None else Indicators()
        self.notifier = notifier
        self.buffer_reserve = buffer_reserve
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
        self._ports: dict = {}
        self._buffer_stats: list[dict] | None = None
        self._buffer_stats_at = 0.0
        self._drain_blocked_for: str | None = None  # SSD whose last drain did not finish
        self.buffer: DeviceRecord | None = None
        if buffer_dir is not None:
            self._init_buffer(Path(buffer_dir))
        if hasattr(self.led, "on_short_press"):
            self.led.on_short_press = self.button_short
            self.led.on_long_press = self.button_long

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
        for rec in list(self.devices.values()):
            self._close_session(rec)
        if hasattr(self.led, "close"):
            self.led.close()
        else:
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

    # ---------------------------------------------------------------- buffer

    def _init_buffer(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            cfg = init_destination(path, None)
            if cfg.project is None:
                cfg.project = BUFFER_PLACEHOLDER_PROJECT
                save_config(path, cfg)
            clean_tmp(path)
        except OSError as exc:
            log.error("Tampon %s inutilisable : %s", path, exc)
            return
        dev = BlockDevice(
            kname="buffer",
            path=str(path),
            disk_kname="buffer",
            disk_path=str(path),
            fstype="local",
            label="Tampon du Pi",
            uuid="buffer",
            size=0,
            mountpoint=str(path),
            removable=False,
            transport=None,
            model=None,
            kind="buffer",
        )
        self.buffer = DeviceRecord(
            dev=dev, role="destination", status="ready", mountpoint=str(path), read_only=False, dest=cfg
        )

    def _is_buffer(self, rec: DeviceRecord | None) -> bool:
        return rec is not None and rec is self.buffer

    def buffer_pending(self, force: bool = False) -> list[dict]:
        """Sources waiting in the buffer: [{project, source, files, bytes}]."""
        if self.buffer is None:
            return []
        now = time.monotonic()
        if not force and self._buffer_stats is not None and now - self._buffer_stats_at < BUFFER_STATS_TTL:
            return self._buffer_stats
        root = Path(self.buffer.mountpoint)
        pending = []
        for project in list_projects(root):
            raw = find_raw_dir(project_root(root, project))
            for source in list_sources(root, project):
                try:
                    files = scan_source(raw / source)
                except OSError:
                    continue
                if files:
                    pending.append(
                        {"project": project, "source": source, "files": len(files), "bytes": sum(f.size for f in files)}
                    )
        self._buffer_stats, self._buffer_stats_at = pending, now
        return pending

    def _invalidate_buffer_stats(self) -> None:
        self._buffer_stats = None

    def _sync_buffer_with(self, ssd: DeviceRecord) -> None:
        """Share card names and presets between the buffer and an SSD; follow its project."""
        if self.buffer is None or ssd.dest is None:
            return
        buf = self.buffer.dest
        changed_buf = changed_ssd = False
        for mine, theirs in ((buf.cards, ssd.dest.cards), (buf.presets, ssd.dest.presets)):
            for key, value in theirs.items():
                if key not in mine:
                    mine[key] = value
                    changed_buf = True
            for key, value in mine.items():
                if key not in theirs:
                    theirs[key] = value
                    changed_ssd = True
        if ssd.dest.project is not None and buf.project != ssd.dest.project:
            buf.project = ssd.dest.project
            changed_buf = True
        try:
            if changed_buf:
                save_config(Path(self.buffer.mountpoint), buf)
            if changed_ssd:
                save_config(Path(ssd.mountpoint), ssd.dest)
        except OSError as exc:
            log.warning("Synchronisation tampon/SSD impossible : %s", exc)

    # --------------------------------------------------------------- polling

    def _current_devices(self) -> dict[str, BlockDevice]:
        current = {d.key: d for d in self.platform.list_devices()}
        try:
            cameras = self.platform.list_cameras()
        except Exception as exc:
            log.debug("Liste des appareils MTP impossible : %s", exc)
            cameras = []
        for port in cameras:
            dev = BlockDevice(
                kname=port.kname,
                path=port.port,
                disk_kname=port.kname,
                disk_path=port.port,
                fstype="mtp",
                label=port.model,
                uuid=None,
                size=0,
                mountpoint=None,
                removable=True,
                transport="usb",
                model=port.model,
                kind="mtp",
            )
            current[dev.key] = dev
            self._ports[dev.key] = port
        return current

    def poll_once(self) -> None:
        try:
            current = self._current_devices()
        except Exception as exc:
            log.warning("Liste des périphériques impossible : %s", exc)
            return
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
                    self._sync_buffer_with(rec)
                    self._notice("info", f"SSD « {rec.dev.display_name} » prêt.")
                    self._rearm_pending()
                elif rec.role == "source":
                    self._start_inventory(rec)
                    self._maybe_autostart(rec)
            self._refresh_wait_reasons()
            self._tick()
            self._maybe_drain()
            self._update_led()

    def _setup(self, rec: DeviceRecord) -> None:
        """Mount or open a new device and decide what it is. Runs without the lock."""
        dev = rec.dev
        if dev.kind == "mtp":
            self._setup_camera(rec)
            return
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
                rec.card = detect_card(
                    Path(mountpoint), dev.label, dev.uuid, dev.size, dev.vendor, dev.model, dev.serial
                )
                rec.role = "source" if rec.card.is_camera else "unknown"
            rec.mountpoint = mountpoint
            rec.status = "ready"
            rec.error = None
        except (PlatformError, OSError) as exc:
            rec.role, rec.status = "error", "error"
            rec.error = f"Montage impossible : {exc}"
            log.warning("Montage de %s impossible : %s", dev.path, exc)

    def _setup_camera(self, rec: DeviceRecord) -> None:
        port = self._ports.get(rec.dev.key)
        try:
            session = self.platform.open_camera(port)
        except Exception as exc:
            rec.role, rec.status = "error", "error"
            rec.error = f"Connexion impossible : {exc}"
            log.warning("Appareil %s inaccessible : %s", rec.dev.label, exc)
            return
        rec.session = session
        rec.card = camera_info(port.model, getattr(session, "serial", None))
        rec.role, rec.status, rec.read_only, rec.error = "source", "ready", True, None

    def _close_session(self, rec: DeviceRecord) -> None:
        if rec.session is not None:
            try:
                rec.session.close()
            except Exception:
                pass
            rec.session = None

    def _start_inventory(self, rec: DeviceRecord) -> None:
        """Count what is on a new source in the background, to show before copying."""
        tree = rec.source_tree()
        if tree is None:
            return

        def run() -> None:
            try:
                files = tree.scan()
            except Exception as exc:
                log.info("Inventaire de %s impossible : %s", rec.dev.display_name, exc)
                return
            rec.inventory = inventory(files)

        threading.Thread(target=run, name="bb-inventory", daemon=True).start()

    def _on_removed(self, key: str) -> None:
        rec = self.devices.pop(key)
        self._close_session(rec)
        for job in self.jobs.values():
            if job.state not in ACTIVE_STATES:
                continue
            if job.device_key == key:
                if job.state == "running":
                    job.cancel_reason = "source_gone"
                    job.cancel.set()
                else:
                    self._finish_unstarted(job, "cancelled", "Appareil débranché avant la sauvegarde.")
            elif job.state == "running" and job.dest_key == key:
                job.cancel_reason = "dest_gone"
                job.cancel.set()
        if rec.role == "destination":
            if rec.status != "ejected":
                self._notice("warning", f"SSD « {rec.dev.display_name} » débranché.")
            if self._drain_blocked_for == key:
                self._drain_blocked_for = None
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

    def external_destination(self) -> DeviceRecord | None:
        dests = self._destinations()
        preferred = self.store.preferred_destination()
        for rec in dests:
            if preferred and rec.dev.uuid == preferred:
                return rec
        return dests[0] if dests else None

    def active_destination(self) -> DeviceRecord | None:
        """Where a card goes now: the external SSD first, else the Pi buffer."""
        return self.external_destination() or self.buffer

    def _no_destination_reason(self) -> str:
        if any(r.role == "unknown" and r.status == "ready" for r in self.devices.values()):
            return "Déclarez le SSD : bouton « Utiliser comme SSD de destination »."
        return "Branchez le SSD de destination."

    def _refresh_wait_reasons(self) -> None:
        if self.active_destination() is not None:
            return
        reason = self._no_destination_reason()
        for job in self.jobs.values():
            if job.state == "waiting" and job.kind == "card":
                job.wait_reason = reason

    # ------------------------------------------------------------------ jobs

    def _maybe_autostart(self, rec: DeviceRecord) -> None:
        if rec.job_id is None and rec.autostart and self.store.settings.auto_backup:
            job = self._create_job(rec, source_name=None, auto=True)
            target = job.destination_name or "SSD"
            when = f"dans {self.store.settings.countdown_s} s" if job.state == "countdown" else "maintenant"
            self._notify(
                f"{self._device_title(rec)} branché",
                f"Sauvegarde vers {target} {when}. Touchez pour ouvrir BackupBox.",
                priority="high",
                tags=("floppy_disk",),
            )

    def _device_title(self, rec: DeviceRecord) -> str:
        card = rec.card
        if card and card.model and card.brand and not card.model.lower().startswith(card.brand.lower()):
            return f"{card.brand} {card.model}"
        if card and card.model:
            return card.model
        return rec.dev.display_name

    def _create_job(self, rec: DeviceRecord, source_name: str | None, auto: bool) -> Job:
        card = rec.card
        if card is None:
            card = detect_card(Path(rec.mountpoint), rec.dev.label, rec.dev.uuid, rec.dev.size)
            rec.card = card
        job = Job(
            id=_new_job_id(),
            device_key=rec.dev.key,
            device_name=self._device_title(rec),
            card_identity=card.identity,
            brand=card.brand,
            source_name=source_name or card.default_source,
            auto=auto,
            created_at=now_iso(),
            name_set_by_user=source_name is not None,
            preset=self.store.settings.default_preset,
        )
        self.jobs[job.id] = job
        rec.job_id = job.id
        self._arm(job)
        return job

    def _arm(self, job: Job) -> None:
        """Place a not-yet-started job in the right state for the current hardware."""
        if job.kind == "drain":
            dest = self.external_destination()
            if dest is None or dest.dest.project is None:
                self._finish_unstarted(job, "cancelled", "SSD débranché : le tampon sera vidé au prochain branchement.")
                return
            job.destination_name, job.target = dest.dev.display_name, "ssd"
            if job.state != "held":
                self._enqueue(job)
            return
        dest = self.active_destination()
        if dest is None:
            job.state, job.deadline = "waiting", None
            job.wait_reason = self._no_destination_reason()
            return
        if dest.dest.project is None:
            job.state, job.deadline = "waiting", None
            job.wait_reason = "Choisissez le projet (voyage) sur le SSD."
            return
        job.wait_reason = None
        job.project = dest.dest.project
        job.destination_name = dest.dev.display_name
        job.target = "buffer" if self._is_buffer(dest) else "ssd"
        known = dest.dest.cards.get(job.card_identity)
        if not job.name_set_by_user:
            job.source_name = known or job.source_name
        if not job.preset_set_by_user:
            job.preset = dest.dest.presets.get(job.card_identity, self.store.settings.default_preset)
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
        for job in list(self.jobs.values()):
            if job.state in {"waiting", "countdown", "queued", "held"}:
                if job.id in self._queue:
                    self._queue.remove(job.id)
                if job.state != "held":
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

    # ----------------------------------------------------------------- drain

    def _maybe_drain(self) -> None:
        if self.buffer is None or not self.store.settings.auto_drain:
            return
        ssd = self.external_destination()
        if ssd is not None and ssd.dev.key == self._drain_blocked_for:
            return  # no retry loop after a failure: replug the SSD or tap « Vider »
        self._create_drain_jobs()

    def _create_drain_jobs(self) -> list[Job]:
        ssd = self.external_destination()
        if self.buffer is None or ssd is None or ssd.dest.project is None:
            return []
        if any(j.kind == "drain" and j.state in ACTIVE_STATES for j in self.jobs.values()):
            return []
        created = []
        for item in self.buffer_pending():
            target_project = ssd.dest.project if item["project"] == BUFFER_PLACEHOLDER_PROJECT else item["project"]
            job = Job(
                id=_new_job_id(),
                kind="drain",
                device_key=self.buffer.dev.key,
                device_name="Tampon du Pi",
                card_identity=f"buffer:{item['project']}/{item['source']}",
                brand=None,
                source_name=item["source"],
                auto=True,
                created_at=now_iso(),
                known_card=True,
                project=target_project,
                buffer_project=item["project"],
            )
            self.jobs[job.id] = job
            self._arm(job)
            created.append(job)
        if created:
            total = sum(i["bytes"] for i in self.buffer_pending())
            self._notice("info", f"Vidage du tampon vers « {ssd.dev.display_name} » : {human_bytes(total)}.")
        return created

    def drain_now(self) -> list[Job]:
        with self._lock:
            if self.buffer is None:
                raise ActionError("Aucun tampon local configuré sur ce Pi.", 404)
            if self.external_destination() is None:
                raise ActionError("Branchez le SSD pour vider le tampon.")
            self._invalidate_buffer_stats()
            jobs = self._create_drain_jobs()
            if not jobs and not any(j.kind == "drain" and j.state in ACTIVE_STATES for j in self.jobs.values()):
                raise ActionError("Le tampon est vide.")
            self._drain_blocked_for = None
            self._update_led()
            return jobs

    def _release_from_buffer(self, job: Job, outcomes) -> int:
        """Delete from the buffer what is now verified on the SSD; keep a ledger."""
        root = Path(self.buffer.mountpoint)
        raw = find_raw_dir(project_root(root, job.buffer_project))
        if raw is None:
            return 0
        src_dir = raw / job.source_name
        manifest = Manifest(project_meta_dir(root, job.buffer_project) / "manifests" / f"{job.source_name}.jsonl")
        ledger = self._drain_ledger(job.source_name)
        released = 0
        for o in outcomes:
            if o.status not in {"copied", "conflict", "skipped"}:
                continue
            path = src_dir / o.rel
            if not path.is_file():
                continue
            st = path.stat()
            origin = manifest.by_dest(o.rel) or {}
            ledger.append(
                {
                    "src": origin.get("src", o.rel),
                    "dest": o.rel,
                    "size": st.st_size,
                    "mtime_ns": origin.get("mtime_ns", st.st_mtime_ns),
                    "hash": o.hash or origin.get("hash"),
                    "algo": origin.get("algo"),
                    "drained": True,
                    "drained_to": job.destination_name,
                    "job": job.id,
                    "at": now_iso(),
                }
            )
            path.unlink()
            released += 1
        for folder in sorted((p for p in src_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                folder.rmdir()
            except OSError:
                pass
        try:
            src_dir.rmdir()
        except OSError:
            pass
        self._invalidate_buffer_stats()
        return released

    def _drain_ledger(self, source_name: str) -> Manifest:
        return Manifest(Path(self.buffer.mountpoint) / ".backupbox" / "drained" / f"{source_name}.jsonl")

    # --------------------------------------------------------------- running

    def _device_present(self, key: str) -> bool:
        if self.buffer is not None and key == self.buffer.dev.key:
            return Path(self.buffer.mountpoint).is_dir()
        try:
            return key in self._current_devices()
        except Exception:
            return False

    def run_job(self, job: Job) -> None:
        """Execute one job. Public so tests can run jobs synchronously."""
        with self._lock:
            if job.kind == "drain":
                src_rec = self.buffer
                dest = self.external_destination()
                source = None
                if src_rec is not None:
                    raw = find_raw_dir(project_root(Path(src_rec.mountpoint), job.buffer_project))
                    if raw is not None and (raw / job.source_name).is_dir():
                        source = FsSource(raw / job.source_name)
                if source is None:
                    self._finish_unstarted(job, "done", "Rien à vider pour cette source.")
                    self._update_led()
                    return
            else:
                src_rec = self.devices.get(job.device_key)
                dest = self.active_destination()
                source = src_rec.source_tree() if src_rec is not None and src_rec.status == "ready" else None
                if source is None:
                    self._finish_unstarted(job, "failed", "L'appareil n'est plus disponible.")
                    self._update_led()
                    return
            if dest is None or dest.dest.project is None:
                job.state = "waiting"
                self._arm(job)
                return
            job.state = "running"
            job.dest_key = dest.dev.key
            if job.kind == "card":
                job.project = dest.dest.project
            job.destination_name = dest.dev.display_name
            job.target = "buffer" if self._is_buffer(dest) else "ssd"
            job.started_at = now_iso()
            dest_mount = Path(dest.mountpoint)
            dest_rec = dest
            settings = self.store.settings
            reserve = self.buffer_reserve if self._is_buffer(dest) else 0
            self._update_led()

        t0 = time.monotonic()
        outcomes = []
        excluded = []
        try:
            raw = raw_dir(dest_mount, job.project, create=True)
            manifest = Manifest(project_meta_dir(dest_mount, job.project) / "manifests" / f"{job.source_name}.jsonl")
            engine = CopyEngine(
                source=source,
                dest_dir=raw / job.source_name,
                tmp_dir=tmp_dir(dest_mount),
                manifest=manifest,
                job_id=job.id,
                card_identity=job.card_identity,
                verify=settings.verify,
                strict=settings.strict_recheck,
                cancel=job.cancel,
                progress=job.progress,
                src_alive=lambda: self._device_present(job.device_key) and source.alive(),
                dest_alive=lambda: self._device_present(job.dest_key) and dest_mount.is_dir(),
                ledger=self._drain_ledger(job.source_name) if self._is_buffer(dest_rec) else None,
            )
            job.progress.phase = "analyse de l'appareil" if job.kind == "card" else "analyse du tampon"
            try:
                all_files = source.scan(job.cancel)
            except OSError as exc:
                if not self._device_present(job.device_key):
                    raise CopyAborted("source_gone", "L'appareil a été débranché pendant l'analyse.") from exc
                raise
            files = apply_preset(all_files, job.preset)
            kept = {f.rel for f in files}
            excluded = [f for f in all_files if f.rel not in kept]
            plan = engine.plan(files)
            engine.check_space(plan, max(self.platform.free_bytes(str(dest_mount)) - reserve, 0))
            outcomes = engine.run(plan)
            job.progress.phase = "écriture sur le disque"
            os.sync()
            self._summarise(job, outcomes, plan.bytes_card, settings.verify, excluded)
            if job.kind == "drain" and job.state in {"done", "partial"}:
                released = self._release_from_buffer(job, outcomes)
                job.message = f"Tampon → {job.destination_name} : {released} fichier(s) déplacés et vérifiés."
                if job.state == "partial":
                    job.message += " Les fichiers en échec restent dans le tampon."
        except CopyAborted as exc:
            reason = job.cancel_reason or exc.reason
            job.state = "cancelled" if reason in {"cancelled", "user", "shutdown"} else "failed"
            job.error = {
                "source_gone": "L'appareil a été débranché pendant la sauvegarde. Rebranchez-le : seuls les fichiers manquants seront copiés.",
                "dest_gone": "Le disque de destination a été débranché pendant la sauvegarde. Rebranchez-le puis relancez.",
                "shutdown": "BackupBox s'est arrêtée pendant la sauvegarde.",
            }.get(reason, str(exc))
            if job.state == "cancelled":
                job.message = job.error if reason == "shutdown" else "Sauvegarde annulée."
                job.error = None
            self._summarise_partial(job)
        except Exception as exc:
            log.exception("Sauvegarde %s en échec", job.id)
            job.state = "failed"
            job.error = f"Erreur inattendue : {exc}"
            self._summarise_partial(job)
        finally:
            job.finished_at = now_iso()
            job.duration_s = round(time.monotonic() - t0, 1)
            job.progress.current_file = None
            job.progress.current_step = None
            job.progress.phase = "terminé"

        if job.kind == "card" and job.state in {"done", "partial"}:
            self._remember_card(dest_rec, job)
        if self._is_buffer(dest_rec) or job.kind == "drain":
            self._invalidate_buffer_stats()
        self._write_report(job, dest_mount)
        self.store.add_history(job.to_dict(include_problems=True))
        with self._lock:
            self._after_job(job, settings)
            self._update_led()

    def _after_job(self, job: Job, settings) -> None:
        if job.kind == "drain":
            if job.state != "done":
                self._drain_blocked_for = job.dest_key
            others = [j for j in self.jobs.values() if j.kind == "drain" and j.state in ACTIVE_STATES]
            if job.state == "done" and not others:
                self._notify("Tampon vidé", f"Tout est vérifié sur « {job.destination_name} ».", level="success")
            elif job.state in {"partial", "failed"}:
                self._notify("Vidage du tampon incomplet", job.error or job.message or "", level="error")
            return
        src = self.devices.get(job.device_key)
        where = "le tampon du Pi" if job.target == "buffer" else f"« {job.destination_name} »"
        if job.state == "done":
            self._notify(
                f"{job.device_name} : sauvegarde vérifiée",
                f"{job.message} Destination : {where}. Vous pouvez débrancher l'appareil.",
                level="success",
            )
            if settings.auto_eject and src is not None and src.status == "ready":
                try:
                    self._eject_record(src, notify=False)
                except (PlatformError, ActionError) as exc:
                    log.warning("Éjection automatique impossible : %s", exc)
        elif job.state == "partial":
            self._notify(
                f"{job.device_name} : sauvegarde incomplète",
                f"{job.summary['failed']} fichier(s) en échec. Ne formatez pas l'appareil.",
                level="error",
            )
        elif job.state == "failed":
            self._notify(f"{job.device_name} : échec", job.error or "", level="error")

    def _summarise(self, job: Job, outcomes, bytes_card: int, verified: bool, excluded=()) -> None:
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
        excluded = list(excluded)
        job.summary = {
            "files": len(outcomes),
            "copied": counts["copied"],
            "skipped": counts["skipped"],
            "conflicts": counts["conflict"],
            "failed": counts["failed"],
            "bytes_card": bytes_card,
            "bytes_copied": bytes_copied,
            "verified": verified,
            "preset": job.preset,
            "excluded_files": len(excluded),
            "excluded_bytes": sum(f.size for f in excluded),
        }
        job.problems = problems
        if counts["failed"]:
            job.state = "partial"
            job.message = f"{counts['failed']} fichier(s) n'ont pas pu être sauvegardés. Ne formatez pas l'appareil."
            return
        job.state = "done"
        new = counts["copied"] + counts["conflict"]
        if not outcomes:
            job.message = "Rien à sauvegarder." if excluded else "Appareil vide : rien à sauvegarder."
        elif new == 0:
            job.message = f"Déjà sauvegardé : les {counts['skipped']} fichier(s) étaient déjà copiés."
        else:
            verb = "copiés et vérifiés" if verified else "copiés (sans relecture)"
            parts = [f"{new} fichier(s) {verb} ({human_bytes(bytes_copied)})"]
            if counts["skipped"]:
                parts.append(f"{counts['skipped']} déjà présent(s)")
            if counts["conflict"]:
                parts.append(f"{counts['conflict']} conflit(s) de nom : les deux versions sont gardées")
            job.message = ", ".join(parts) + "."
        if excluded:
            job.message += f" {len(excluded)} fichier(s) ignorés par le préréglage « {PRESETS[job.preset]} »."

    def _summarise_partial(self, job: Job) -> None:
        """Counters of an interrupted job: the engine raised, so read its live progress."""
        p = job.progress
        if p.files_total:
            job.summary = {
                "files": p.files_total,
                "copied": p.files_copied,
                "skipped": p.files_skipped,
                "conflicts": p.files_conflict,
                "failed": p.files_failed,
                "bytes_card": p.bytes_card,
                "interrupted": True,
            }

    def _remember_card(self, dest_rec: DeviceRecord, job: Job) -> None:
        cfg = dest_rec.dest
        if cfg is None:
            return
        if cfg.cards.get(job.card_identity) == job.source_name and cfg.presets.get(job.card_identity) == job.preset:
            return
        cfg.cards[job.card_identity] = job.source_name
        cfg.presets[job.card_identity] = job.preset
        try:
            save_config(Path(dest_rec.mountpoint), cfg)
        except OSError as exc:
            log.warning("Mémorisation de l'appareil impossible : %s", exc)

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

    def start_backup(self, key: str, source_name: str | None = None, preset: str | None = None) -> Job:
        with self._lock:
            rec = self._record(key)
            if rec.status != "ready" or rec.role not in {"source", "unknown"}:
                raise ActionError("Ce périphérique ne peut pas être sauvegardé.")
            if rec.job_id and self.jobs[rec.job_id].state in ACTIVE_STATES:
                raise ActionError("Une sauvegarde de cet appareil est déjà prévue.")
            if source_name is not None:
                source_name = self._valid_source(source_name)
            job = self._create_job(rec, source_name=source_name, auto=False)
            if preset is not None:
                self._apply_preset(job, preset)
            self._update_led()
            return job

    def _valid_source(self, name: str) -> str:
        try:
            return validate_source_name(name)
        except ValueError as exc:
            raise ActionError(str(exc), 400) from exc

    def _apply_preset(self, job: Job, preset: str) -> None:
        if preset not in PRESETS:
            raise ActionError("Préréglage inconnu.", 400)
        job.preset, job.preset_set_by_user = preset, True

    def set_job_source(self, job_id: str, name: str) -> Job:
        with self._lock:
            job = self._job(job_id)
            if job.kind != "card" or job.state not in {"waiting", "held", "countdown", "queued"}:
                raise ActionError("La source ne peut plus être changée : la sauvegarde a commencé.")
            job.source_name = self._valid_source(name)
            job.name_set_by_user = True
            dest = self.active_destination()
            known = dest.dest.cards.get(job.card_identity) if dest and dest.dest else None
            job.known_card = known == job.source_name
            return job

    def set_job_preset(self, job_id: str, preset: str) -> Job:
        with self._lock:
            job = self._job(job_id)
            if job.kind != "card" or job.state not in {"waiting", "held", "countdown", "queued"}:
                raise ActionError("Le préréglage ne peut plus être changé : la sauvegarde a commencé.")
            self._apply_preset(job, preset)
            return job

    def hold_job(self, job_id: str) -> Job:
        with self._lock:
            job = self._job(job_id)
            if job.state in {"countdown", "queued", "waiting"}:
                if job.id in self._queue:
                    self._queue.remove(job.id)
                job.state, job.deadline = "held", None
                job.wait_reason = "En attente de votre confirmation."
            self._update_led()
            return job

    def start_job_now(self, job_id: str) -> Job:
        with self._lock:
            job = self._job(job_id)
            if job.state not in {"held", "countdown", "waiting", "queued"}:
                raise ActionError("Cette sauvegarde n'est pas en attente.")
            dest = self.active_destination() if job.kind == "card" else self.external_destination()
            if dest is None or dest.dest.project is None:
                job.state = "waiting"
                self._arm(job)
                raise ActionError(job.wait_reason or "Destination indisponible.")
            job.wait_reason = None
            if job.kind == "card":
                job.project = dest.dest.project
                job.target = "buffer" if self._is_buffer(dest) else "ssd"
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

    def button_short(self) -> None:
        """Physical button, short press: start everything that waits for a confirmation."""
        with self._lock:
            waiting = [j for j in self.jobs.values() if j.state in {"countdown", "held"}]
            started = 0
            for job in sorted(waiting, key=lambda j: j.created_at):
                try:
                    self.start_job_now(job.id)
                    started += 1
                except ActionError:
                    pass
            if started:
                self._chime("start")
            elif any(j.state in {"failed", "partial"} for j in self._recent_final_jobs()):
                self._chime("error")
            else:
                self._chime("ack")

    def button_long(self) -> None:
        """Physical button held 3 s: safe shutdown, refused while writing."""
        try:
            self.power("poweroff")
            self._chime("shutdown")
        except ActionError:
            self._chime("refused")

    def _chime(self, name: str) -> None:
        chime = getattr(self.led, "chime", None)
        if chime is not None:
            chime(name)

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
        if rec.kind == "mtp":
            self._close_session(rec)
        elif rec.status == "ready" and (rec.mounted_by_us or rec.dev.mountpoint):
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
                self._finish_unstarted(job, "cancelled", "Appareil éjecté avant la sauvegarde.")
        if rec.role == "destination":
            self._notice("info", f"SSD « {rec.dev.display_name} » éjecté : vous pouvez le débrancher.")
            self._rearm_pending()
        elif notify:
            self._notice("info", f"« {rec.dev.display_name} » éjecté : vous pouvez le débrancher.")

    def remount(self, key: str) -> DeviceRecord:
        with self._lock:
            rec = self._record(key)
            if rec.status not in {"ejected", "error"}:
                raise ActionError("Ce périphérique est déjà prêt.")
            rec.status, rec.error, rec.autostart = "mounting", None, False
        self._setup(rec)
        with self._lock:
            if rec.role == "destination":
                self._sync_buffer_with(rec)
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
            rec.role, rec.card, rec.inventory = "destination", None, None
            self.store.remember_destination(rec.dev.uuid)
            self.store.set_preferred_destination(rec.dev.uuid)
            self._sync_buffer_with(rec)
            self._notice("success", f"« {rec.dev.display_name} » est maintenant le SSD de sauvegarde.")
            self._rearm_pending()
            self._maybe_drain()
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
                raise ActionError(f"Impossible d'écrire sur le disque : {exc}") from exc
            if not self._is_buffer(rec):
                self._sync_buffer_with(rec)
            self._rearm_pending()
            self._maybe_drain()
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
            is_buffer = self._is_buffer(rec)
        projects = list_projects(mount)
        if current is not None and current not in projects:
            projects.append(current)
        return {
            "current": current,
            "projects": sorted(projects, key=str.lower),
            "sources": list_sources(mount, current) if current is not None else [],
            "is_buffer": is_buffer,
        }

    def update_settings(self, changes: dict):
        try:
            settings = self.store.update_settings(changes)
        except (ValueError, TypeError) as exc:
            raise ActionError(str(exc), 400) from exc
        with self._lock:
            if "auto_drain" in changes and settings.auto_drain:
                self._maybe_drain()
            self._update_led(force=True)
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

    def _notify(self, title: str, message: str, level: str = "info", priority: str = "default", tags=()) -> None:
        """A notice in the web UI, and a push notification when the Pi is online."""
        self._notice(level, f"{title} — {message}" if message else title)
        if self.notifier is not None:
            if level == "error":
                priority, tags = "high", tuple(tags) or ("warning",)
            elif level == "success" and not tags:
                tags = ("white_check_mark",)
            self.notifier.send(title, message, priority=priority, tags=tuple(tags))

    def health(self) -> dict:
        now = time.monotonic()
        if now - self._health_at > 30 or not self._health:
            self._health = {"power": power_status(), "clock_synchronized": clock_synchronized()}
            self._health_at = now
        return self._health

    def _buffer_snapshot(self) -> dict | None:
        if self.buffer is None:
            return None
        path = self.buffer.mountpoint
        try:
            free = self.platform.free_bytes(path)
            total = self.platform.total_bytes(path)
        except OSError:
            free = total = None
        pending = self.buffer_pending()
        return {
            "key": self.buffer.dev.key,
            "project": self.buffer.dest.project,
            "free_bytes": free,
            "usable_bytes": max((free or 0) - self.buffer_reserve, 0) if free is not None else None,
            "total_bytes": total,
            "reserve_bytes": self.buffer_reserve,
            "pending": pending,
            "pending_bytes": sum(p["bytes"] for p in pending),
            "pending_files": sum(p["files"] for p in pending),
        }

    def snapshot(self) -> dict:
        health = self.health()
        with self._lock:
            active = self.active_destination()
            external = self.external_destination()
            devices = [r.to_dict(self.platform) for r in sorted(self.devices.values(), key=lambda r: r.dev.key)]
            jobs = sorted(self.jobs.values(), key=lambda j: j.created_at)
            visible = [
                j
                for j in jobs
                if j.state in ACTIVE_STATES
                or j.device_key in self.devices
                or (j.kind == "drain" and j.dest_key in self.devices)
            ]
            return {
                "version": __version__,
                "simulate": self.simulate,
                "settings": asdict(self.store.settings),
                "presets": PRESETS,
                "active_destination": active.dev.key if active else None,
                "external_destination": external.dev.key if external else None,
                "devices": devices,
                "buffer": self._buffer_snapshot(),
                "jobs": [j.to_dict() for j in visible[-30:]],
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

    def _recent_final_jobs(self) -> list[Job]:
        """Latest finished job per device still connected (card) or per SSD (drain)."""
        latest: dict[str, Job] = {}
        for job in sorted(self.jobs.values(), key=lambda j: j.created_at):
            if job.state not in FINAL_STATES or job.state == "cancelled":
                continue
            key = job.dest_key if job.kind == "drain" else job.device_key
            if key in self.devices:
                latest[key] = job
        return list(latest.values())

    def _update_led(self, force: bool = False) -> None:
        jobs = list(self.jobs.values())
        if any(j.state in {"running", "queued"} for j in jobs):
            state = "busy"
        elif any(j.state in {"countdown", "held", "waiting"} for j in jobs):
            state = "attention"
        else:
            finals = self._recent_final_jobs()
            if any(j.state in {"failed", "partial"} for j in finals):
                state = "error"
            elif any(j.state == "done" for j in finals):
                state = "success"
            else:
                state = "idle"
        if force and getattr(self.led, "state", None) == state:
            self.led.state = None
        self.led.set(state)


def buffer_reserve_from_env(value: str | None, default_gb: float = 10) -> int:
    try:
        return int(float(value) * GB) if value else int(default_gb * GB)
    except ValueError:
        return int(default_gb * GB)


