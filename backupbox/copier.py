"""Verified copy engine.

Invariant: a file only ever appears under its final name in ``Footage_RAW`` after
its bytes were written to a temporary file on the same SSD, flushed to disk,
read back from the disk (page cache dropped) and found identical, by hash, to
what was read from the card. An interrupted copy can only leave a temporary
file in ``.backupbox/tmp``, which is cleaned on the next mount.

The card is never written to.
"""

from __future__ import annotations

import errno
import os
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterator, Protocol

from .manifest import Manifest

try:
    import xxhash

    HASH_ALGO = "xxh128"

    def new_hasher():
        return xxhash.xxh3_128()

except ImportError:  # pragma: no cover - xxhash is a hard dependency on the Pi
    import hashlib

    HASH_ALGO = "blake2b"

    def new_hasher():
        return hashlib.blake2b()


MiB = 1024 * 1024
DEFAULT_CHUNK = 8 * MiB
SPACE_RESERVE = 64 * MiB
PER_FILE_SLACK = 256 * 1024  # cluster rounding on exFAT

JUNK_NAMES = {
    ".DS_Store",
    "System Volume Information",
    "$RECYCLE.BIN",
    ".Trashes",
    ".Spotlight-V100",
    ".fseventsd",
    ".TemporaryItems",
    "Thumbs.db",
    ".backupbox",
}


class CopyAborted(Exception):
    """The whole job must stop (cancel, device gone, disk full)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def is_junk(name: str) -> bool:
    return name in JUNK_NAMES or name.startswith("._")


@dataclass(frozen=True)
class SourceFile:
    rel: str
    size: int
    mtime_ns: int


@dataclass
class PlanItem:
    file: SourceFile
    action: str  # "copy" | "check" | "skip"


@dataclass
class Plan:
    items: list[PlanItem]
    files_total: int
    bytes_card: int
    bytes_needed: int  # space the copy will take on the SSD
    work_bytes: int  # bytes to read/write, for progress and ETA

    @property
    def files_to_copy(self) -> int:
        return sum(1 for i in self.items if i.action != "skip")


@dataclass
class FileOutcome:
    rel: str
    status: str  # copied | skipped | conflict | failed
    size: int = 0
    dest: str | None = None
    hash: str | None = None
    error: str | None = None


class Progress:
    """Live counters read by the API while the engine runs."""

    def __init__(self) -> None:
        self.phase = "en attente"
        self.files_total = 0
        self.files_done = 0
        self.files_copied = 0
        self.files_skipped = 0
        self.files_conflict = 0
        self.files_failed = 0
        self.bytes_card = 0
        self.work_total = 0
        self.work_done = 0
        self.current_file: str | None = None
        self.current_step: str | None = None
        self._samples: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def add_work(self, n: int) -> None:
        with self._lock:
            self.work_done += n
            now = time.monotonic()
            self._samples.append((now, self.work_done))
            while len(self._samples) > 2 and now - self._samples[0][0] > 8.0:
                self._samples.popleft()

    def speed(self) -> float:
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        if time.monotonic() - t1 > 10.0:
            return 0.0
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def to_dict(self) -> dict:
        speed = self.speed()
        remaining = max(self.work_total - self.work_done, 0)
        percent = 100.0 * self.work_done / self.work_total if self.work_total else (100.0 if self.files_total and self.files_done >= self.files_total else 0.0)
        return {
            "phase": self.phase,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "files_copied": self.files_copied,
            "files_skipped": self.files_skipped,
            "files_conflict": self.files_conflict,
            "files_failed": self.files_failed,
            "bytes_card": self.bytes_card,
            "work_total": self.work_total,
            "work_done": self.work_done,
            "percent": round(min(percent, 100.0), 1),
            "speed_bps": round(speed),
            "eta_s": round(remaining / speed) if speed > 0 and remaining else None,
            "current_file": self.current_file,
            "current_step": self.current_step,
        }


# ------------------------------------------------------------------ presets

VIDEO_EXT = {
    ".mp4", ".mov", ".insv", ".360", ".mts", ".m2ts", ".avi", ".mxf", ".mkv", ".braw", ".crm", ".r3d", ".hevc",
}
PHOTO_EXT = {
    ".jpg", ".jpeg", ".heic", ".heif", ".dng", ".gpr", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".rw2", ".orf",
    ".insp", ".png", ".tif", ".tiff",
}
# Low-resolution proxies and thumbnails the camera regenerates anyway.
PREVIEW_EXT = {".lrv", ".lrf", ".thm"}

PRESETS = {
    "all": "Tout (recommandé)",
    "no_previews": "Sans aperçus (.LRV .LRF .THM)",
    "videos_only": "Vidéos seulement",
}
DEFAULT_PRESET = "all"


def file_category(rel: str) -> str:
    name = PurePosixPath(rel).name
    ext = PurePosixPath(name).suffix.lower()
    if ext in PREVIEW_EXT or name.upper().startswith("LRV_"):
        return "preview"
    if ext in VIDEO_EXT:
        return "video"
    if ext in PHOTO_EXT:
        return "photo"
    return "other"


def apply_preset(files: list[SourceFile], preset: str) -> list[SourceFile]:
    if preset == "no_previews":
        return [f for f in files if file_category(f.rel) != "preview"]
    if preset == "videos_only":
        return [f for f in files if file_category(f.rel) == "video"]
    return list(files)


def inventory(files: list[SourceFile]) -> dict:
    """What is on a source, by category, to show before transferring."""
    out = {cat: {"files": 0, "bytes": 0} for cat in ("video", "photo", "preview", "other")}
    for f in files:
        bucket = out[file_category(f.rel)]
        bucket["files"] += 1
        bucket["bytes"] += f.size
    out["total"] = {"files": len(files), "bytes": sum(f.size for f in files)}
    return out


# ------------------------------------------------------------------ sources


class SourceTree(Protocol):
    """Where the files come from: a mounted folder, or a camera over MTP/PTP."""

    def scan(self, cancel: threading.Event | None = None) -> list[SourceFile]: ...

    def open(self, rel: str) -> BinaryIO: ...

    def alive(self) -> bool: ...


class FsSource:
    def __init__(self, root: Path):
        self.root = Path(root)

    def scan(self, cancel: threading.Event | None = None) -> list[SourceFile]:
        return scan_source(self.root, cancel)

    def open(self, rel: str) -> BinaryIO:
        return open(self.root / rel, "rb")

    def alive(self) -> bool:
        return self.root.is_dir()


def scan_source(root: Path, cancel: threading.Event | None = None) -> list[SourceFile]:
    """All regular files on the card, junk excluded, symlinks ignored, sorted by path."""
    root = Path(root)
    files: list[SourceFile] = []
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        if cancel is not None and cancel.is_set():
            raise CopyAborted("cancelled", "Sauvegarde annulée.")
        with os.scandir(root / rel_dir if rel_dir else root) as it:
            for entry in it:
                if is_junk(entry.name):
                    continue
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(rel)
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    files.append(SourceFile(rel=rel, size=st.st_size, mtime_ns=st.st_mtime_ns))
    files.sort(key=lambda f: f.rel)
    return files


def _drop_cache(fd: int) -> None:
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass


def _advise(fh, advice_name: str) -> None:
    """posix_fadvise on a stream when it is backed by a real file descriptor."""
    try:
        os.posix_fadvise(fh.fileno(), 0, 0, getattr(os, advice_name))
    except (AttributeError, OSError, ValueError):
        pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def conflict_name(rel: str, digest: str, n: int = 1) -> str:
    p = PurePosixPath(rel)
    tag = f"_conflit-{digest[:8]}" + (f"-{n}" if n > 1 else "")
    return str(p.with_name(p.stem + tag + p.suffix))


class CopyEngine:
    def __init__(
        self,
        *,
        dest_dir: Path,
        src_root: Path | None = None,
        source: SourceTree | None = None,
        tmp_dir: Path,
        manifest: Manifest,
        job_id: str,
        card_identity: str,
        verify: bool = True,
        strict: bool = False,
        chunk_size: int = DEFAULT_CHUNK,
        cancel: threading.Event | None = None,
        progress: Progress | None = None,
        src_alive: Callable[[], bool] | None = None,
        dest_alive: Callable[[], bool] | None = None,
        ledger: Manifest | None = None,
    ) -> None:
        if source is None:
            if src_root is None:
                raise ValueError("source or src_root is required")
            source = FsSource(Path(src_root))
        self.source = source
        self.dest_dir = Path(dest_dir)
        self.tmp_dir = Path(tmp_dir)
        self.manifest = manifest
        self.job_id = job_id
        self.card_identity = card_identity
        self.verify = verify
        self.strict = strict
        self.chunk_size = chunk_size
        self.cancel = cancel or threading.Event()
        self.progress = progress or Progress()
        self.src_alive = src_alive or source.alive
        self.dest_alive = dest_alive or (lambda: self.tmp_dir.is_dir())
        # Pi buffer only: files already moved on to an SSD, whatever the project.
        self.ledger = ledger

    # ---------------------------------------------------------------- planning

    def plan(self, files: list[SourceFile]) -> Plan:
        items: list[PlanItem] = []
        needed = 0
        work = 0
        for f in files:
            action = self._decide(f)
            items.append(PlanItem(f, action))
            if action == "copy":
                needed += f.size + PER_FILE_SLACK
                work += f.size * (2 if self.verify else 1)
            elif action == "check":
                work += f.size * 2
        return Plan(
            items=items,
            files_total=len(files),
            bytes_card=sum(f.size for f in files),
            bytes_needed=needed,
            work_bytes=work,
        )

    def _decide(self, f: SourceFile) -> str:
        if self.ledger is not None and self.ledger.lookup(f.rel, f.size, f.mtime_ns) is not None:
            return "skip"  # already moved from the Pi buffer to an SSD, verified
        entry = self.manifest.lookup(f.rel, f.size, f.mtime_ns)
        if entry is not None:
            dest = self.dest_dir / entry["dest"]
            try:
                if dest.stat().st_size == f.size:
                    return "check" if self.strict else "skip"
            except OSError:
                pass
        dest = self.dest_dir / f.rel
        try:
            st = dest.stat()
        except OSError:
            return "copy"
        # Same size under the same name: compare bytes, never duplicate identical files.
        return "check" if st.st_size == f.size else "copy"

    @staticmethod
    def check_space(plan: Plan, free_bytes: int) -> None:
        if plan.bytes_needed + SPACE_RESERVE > free_bytes:
            raise CopyAborted(
                "no_space",
                f"Espace insuffisant sur le SSD : {human_bytes(plan.bytes_needed)} nécessaires, "
                f"{human_bytes(free_bytes)} disponibles. Rien n'a été copié.",
            )

    # --------------------------------------------------------------- execution

    def run(self, plan: Plan) -> list[FileOutcome]:
        p = self.progress
        p.files_total = plan.files_total
        p.bytes_card = plan.bytes_card
        p.work_total = plan.work_bytes
        p.phase = "copie"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        outcomes: list[FileOutcome] = []
        for item in plan.items:
            self._check_cancel()
            if item.action == "skip":
                outcomes.append(FileOutcome(item.file.rel, "skipped", item.file.size, dest=item.file.rel))
                p.files_skipped += 1
                p.files_done += 1
                continue
            p.current_file = item.file.rel
            try:
                outcome = self._process(item)
            except CopyAborted:
                raise
            except OSError as exc:
                self._raise_if_device_gone(exc)
                outcome = FileOutcome(item.file.rel, "failed", item.file.size, error=_describe_oserror(exc))
            except _FileFailed as exc:
                outcome = FileOutcome(item.file.rel, "failed", item.file.size, error=str(exc))
            outcomes.append(outcome)
            if outcome.status == "copied":
                p.files_copied += 1
            elif outcome.status == "conflict":
                p.files_conflict += 1
            elif outcome.status == "skipped":
                p.files_skipped += 1
            else:
                p.files_failed += 1
            p.files_done += 1
        p.current_file = None
        p.current_step = None
        p.phase = "terminé"
        return outcomes

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise CopyAborted("cancelled", "Sauvegarde annulée.")

    def _raise_if_device_gone(self, exc: OSError) -> None:
        if not self.src_alive():
            raise CopyAborted("source_gone", "La carte a été retirée pendant la sauvegarde.") from exc
        if not self.dest_alive():
            raise CopyAborted("dest_gone", "Le SSD a été débranché pendant la sauvegarde.") from exc
        if exc.errno == errno.ENOSPC:
            raise CopyAborted("no_space", "Le SSD est plein.") from exc

    def _process(self, item: PlanItem) -> FileOutcome:
        f = item.file
        if item.action == "check":
            src_hash = self._hash_source(f.rel)
            entry = self.manifest.lookup(f.rel, f.size, f.mtime_ns)
            dest_rel = entry["dest"] if entry else f.rel
            dest_hash = self._hash_file(self.dest_dir / dest_rel, "vérification")
            if src_hash == dest_hash:
                if entry is None or entry.get("hash") != src_hash:
                    self._record(f, dest_rel, src_hash)
                return FileOutcome(f.rel, "skipped", f.size, dest=dest_rel, hash=src_hash)
            # Same name, size and date but different bytes: keep both.
            self.progress.work_total += f.size * (2 if self.verify else 1)
        return self._copy(f)

    def _copy(self, f: SourceFile) -> FileOutcome:
        last_error: Exception | None = None
        for attempt in (1, 2):
            tmp = self.tmp_dir / f"{uuid.uuid4().hex}.bbpart"
            try:
                src_hash = self._copy_to_tmp(f, tmp)
                if self.verify:
                    dest_hash = self._hash_file(tmp, "vérification")
                    if dest_hash != src_hash:
                        last_error = _FileFailed(
                            "La relecture sur le SSD ne correspond pas à la carte (vérification échouée)."
                        )
                        tmp.unlink(missing_ok=True)
                        if attempt == 1:
                            self.progress.work_total += f.size * 2
                            continue
                        raise last_error
                return self._publish(f, tmp, src_hash)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        raise last_error or _FileFailed("Échec inconnu")  # pragma: no cover

    def _copy_to_tmp(self, f: SourceFile, tmp: Path) -> str:
        p = self.progress
        p.current_step = "copie"
        hasher = new_hasher()
        read_total = 0
        with self.source.open(f.rel) as fsrc, open(tmp, "wb") as fdst:
            _advise(fsrc, "POSIX_FADV_SEQUENTIAL")
            for buf in self._pipelined_read(fsrc, hasher):
                fdst.write(buf)
                read_total += len(buf)
                p.add_work(len(buf))
            fdst.flush()
            os.fsync(fdst.fileno())
            _drop_cache(fdst.fileno())
            _advise(fsrc, "POSIX_FADV_DONTNEED")
        if read_total != f.size:
            raise _FileFailed("Le fichier a changé sur la carte pendant la copie.")
        try:
            os.utime(tmp, ns=(f.mtime_ns, f.mtime_ns))
        except OSError:
            pass
        return hasher.hexdigest()

    def _pipelined_read(self, fsrc, hasher) -> Iterator[bytes]:
        """Read and hash the card in a thread while the caller writes to the SSD."""
        q: queue.Queue = queue.Queue(maxsize=4)
        stop = threading.Event()
        done = object()

        def put(item) -> None:
            while not stop.is_set():
                try:
                    q.put(item, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def reader() -> None:
            try:
                while not stop.is_set():
                    buf = fsrc.read(self.chunk_size)
                    if not buf:
                        put(done)
                        return
                    hasher.update(buf)
                    put(buf)
            except BaseException as exc:  # handed to the consumer
                put(exc)

        thread = threading.Thread(target=reader, name="bb-reader", daemon=True)
        thread.start()
        try:
            while True:
                if self.cancel.is_set():
                    raise CopyAborted("cancelled", "Sauvegarde annulée.")
                try:
                    item = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is done:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stop.set()
            thread.join(timeout=10)

    def _hash_source(self, rel: str) -> str:
        with self.source.open(rel) as fh:
            return self._hash_stream(fh, "lecture carte")

    def _hash_file(self, path: Path, step: str) -> str:
        """Hash a file on the SSD, from the disk itself (page cache dropped first)."""
        with open(path, "rb") as fh:
            _drop_cache(fh.fileno())
            return self._hash_stream(fh, step)

    def _hash_stream(self, fh, step: str) -> str:
        p = self.progress
        p.current_step = step
        hasher = new_hasher()
        while True:
            self._check_cancel()
            buf = fh.read(self.chunk_size)
            if not buf:
                break
            hasher.update(buf)
            p.add_work(len(buf))
        return hasher.hexdigest()

    def _publish(self, f: SourceFile, tmp: Path, digest: str) -> FileOutcome:
        dest_rel = f.rel
        status = "copied"
        if (self.dest_dir / dest_rel).exists():
            status = "conflict"
            n = 1
            while True:
                dest_rel = conflict_name(f.rel, digest, n)
                existing = self.dest_dir / dest_rel
                if not existing.exists():
                    break
                known = self.manifest.by_dest(dest_rel)
                if known and known.get("hash") == digest and existing.stat().st_size == f.size:
                    tmp.unlink(missing_ok=True)
                    return FileOutcome(f.rel, "skipped", f.size, dest=dest_rel, hash=digest)
                n += 1
        final = self.dest_dir / dest_rel
        final.parent.mkdir(parents=True, exist_ok=True)
        os.rename(tmp, final)
        _fsync_dir(final.parent)
        self._record(f, dest_rel, digest)
        return FileOutcome(f.rel, status, f.size, dest=dest_rel, hash=digest)

    def _record(self, f: SourceFile, dest_rel: str, digest: str) -> None:
        self.manifest.append(
            {
                "src": f.rel,
                "dest": dest_rel,
                "size": f.size,
                "mtime_ns": f.mtime_ns,
                "hash": digest,
                "algo": HASH_ALGO,
                "verified": self.verify,
                "job": self.job_id,
                "card": self.card_identity,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )


class _FileFailed(Exception):
    pass


def _describe_oserror(exc: OSError) -> str:
    if exc.errno == errno.EIO:
        return "Erreur de lecture/écriture (EIO) : carte ou câble défectueux ?"
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return f"Accès refusé : {exc.strerror}"
    if exc.errno == errno.ENAMETOOLONG:
        return "Nom de fichier trop long pour le SSD."
    return f"{exc.strerror or exc}"


def human_bytes(n: float) -> str:
    units = ["o", "Ko", "Mo", "Go", "To"]
    value = float(n)
    for unit in units:
        if abs(value) < 1000 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}".replace(".", ",")
        value /= 1000
    return f"{value:.1f} To"  # pragma: no cover
