"""Append-only record of every verified file, one JSON line per file.

One manifest per source folder lives at
``<project>/.backupbox/manifests/<source>.jsonl``. It is what lets a re-inserted
card copy only its new clips, and it is the proof of what was verified, with
which hash. A torn last line (power cut while appending) is ignored on load.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

MTIME_TOLERANCE_NS = 2_000_000_000  # FAT stores mtimes with 2 s resolution


def same_mtime(a_ns: int, b_ns: int) -> bool:
    return abs(int(a_ns) - int(b_ns)) <= MTIME_TOLERANCE_NS


class Manifest:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._by_src: dict[str, list[dict]] = {}
        self._by_dest: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    self._index(entry)
        except FileNotFoundError:
            pass

    def _index(self, entry: dict) -> None:
        self._by_src.setdefault(entry["src"], []).append(entry)
        self._by_dest[entry["dest"]] = entry

    def lookup(self, src_rel: str, size: int, mtime_ns: int) -> dict | None:
        """Most recent verified entry for this card file (same path, size and mtime)."""
        for entry in reversed(self._by_src.get(src_rel, [])):
            if entry.get("size") == size and same_mtime(entry.get("mtime_ns", 0), mtime_ns):
                return entry
        return None

    def by_dest(self, dest_rel: str) -> dict | None:
        return self._by_dest.get(dest_rel)

    def append(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._index(entry)

    def __len__(self) -> int:
        return len(self._by_dest)
