"""The destination SSD: its marker, its projects and the Footage_RAW layout.

An SSD becomes a destination once ``.backupbox/config.json`` exists at its root.
The file travels with the drive: it holds the active project and which card goes
to which source folder, so a reinstalled Pi still files cards the same way.

Layout written on the SSD::

    <SSD>/.backupbox/config.json        marker + active project + card->source map
    <SSD>/.backupbox/tmp/                in-flight copies (never a final name)
    <SSD>/<project>/Footage_RAW/<source>/<path on card>
    <SSD>/<project>/.backupbox/manifests/<source>.jsonl
    <SSD>/<project>/.backupbox/reports/<job>.json
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MARKER_DIR = ".backupbox"
CONFIG_NAME = "config.json"
TMP_NAME = "tmp"
RAW_NAME = "Footage_RAW"
CONFIG_VERSION = 1
MAX_PROJECT_DEPTH = 3

_SEGMENT_RE = re.compile(r"^[\w][\w .-]{0,79}$", re.UNICODE)
_FORBIDDEN_CHARS = set('\\:*?"<>|')


@dataclass
class DestinationConfig:
    id: str
    project: str | None
    cards: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    version: int = CONFIG_VERSION

    @classmethod
    def from_dict(cls, data: dict) -> "DestinationConfig":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            project=data.get("project"),
            cards={str(k): str(v) for k, v in (data.get("cards") or {}).items()},
            created_at=str(data.get("created_at") or ""),
            version=int(data.get("version") or CONFIG_VERSION),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def validate_project(project: str) -> str:
    """Return the normalised project path ('' = SSD root) or raise ValueError."""
    project = (project or "").strip().strip("/")
    if project == "":
        return ""
    parts = project.split("/")
    if len(parts) > MAX_PROJECT_DEPTH:
        raise ValueError(f"Projet trop profond : {MAX_PROJECT_DEPTH} niveaux de dossiers au maximum.")
    for part in parts:
        if (
            part in {".", ".."}
            or part.startswith(".")
            or part != part.strip()
            or part.endswith(".")
            or any(c in _FORBIDDEN_CHARS for c in part)
            or not _SEGMENT_RE.match(part)
        ):
            raise ValueError(
                f"Nom de projet invalide : « {part} ». Lettres, chiffres, espaces, « _ », « - » ou « . »."
            )
        if part.lower() == RAW_NAME.lower() or part.lower() == MARKER_DIR:
            raise ValueError(f"« {part} » est un nom réservé.")
    return "/".join(parts)


def marker_dir(mount: Path) -> Path:
    return Path(mount) / MARKER_DIR


def is_destination(mount: Path) -> bool:
    return (marker_dir(mount) / CONFIG_NAME).is_file()


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_config(mount: Path) -> DestinationConfig:
    path = marker_dir(mount) / CONFIG_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return DestinationConfig.from_dict(data)


def save_config(mount: Path, cfg: DestinationConfig) -> None:
    write_json_atomic(marker_dir(mount) / CONFIG_NAME, asdict(cfg))


def init_destination(mount: Path, project: str | None) -> DestinationConfig:
    if is_destination(mount):
        cfg = load_config(mount)
    else:
        cfg = DestinationConfig(id=uuid.uuid4().hex, project=None, created_at=now_iso())
    if project is not None:
        cfg.project = validate_project(project)
    save_config(mount, cfg)
    tmp_dir(mount).mkdir(parents=True, exist_ok=True)
    return cfg


def tmp_dir(mount: Path) -> Path:
    return marker_dir(mount) / TMP_NAME


def clean_tmp(mount: Path) -> int:
    """Delete leftovers of interrupted copies. Returns how many were removed."""
    folder = tmp_dir(mount)
    removed = 0
    if not folder.is_dir():
        return 0
    for entry in folder.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def find_raw_dir(project_root: Path) -> Path | None:
    """Existing Footage_RAW folder, any casing (Footage Organizer accepts all)."""
    exact = project_root / RAW_NAME
    if exact.is_dir():
        return exact
    try:
        for entry in project_root.iterdir():
            if entry.is_dir() and entry.name.lower() == RAW_NAME.lower():
                return entry
    except OSError:
        return None
    return None


def project_root(mount: Path, project: str) -> Path:
    return Path(mount) / project if project else Path(mount)


def raw_dir(mount: Path, project: str, create: bool = False) -> Path:
    root = project_root(mount, project)
    found = find_raw_dir(root)
    if found is not None:
        return found
    path = root / RAW_NAME
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def project_meta_dir(mount: Path, project: str) -> Path:
    return project_root(mount, project) / MARKER_DIR


def list_projects(mount: Path) -> list[str]:
    """Folders (up to 3 levels deep) that contain a Footage_RAW directory."""
    mount = Path(mount)
    found: list[str] = []

    def visit(folder: Path, rel: str, depth: int) -> None:
        if find_raw_dir(folder) is not None:
            found.append(rel)
        if depth >= MAX_PROJECT_DEPTH:
            return
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.is_symlink() or child.name.startswith((".", "$")):
                continue
            if child.name.lower() in {RAW_NAME.lower(), "system volume information"}:
                continue
            visit(child, f"{rel}/{child.name}" if rel else child.name, depth + 1)

    visit(mount, "", 0)
    return found


def list_sources(mount: Path, project: str) -> list[str]:
    raw = find_raw_dir(project_root(mount, project))
    if raw is None:
        return []
    try:
        return sorted(p.name for p in raw.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
