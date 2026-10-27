"""User settings and local state kept on the Pi (``/var/lib/backupbox``)."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .destination import write_json_atomic

MAX_HISTORY = 200


@dataclass
class Settings:
    auto_backup: bool = True
    countdown_s: int = 30
    auto_eject: bool = True
    verify: bool = True
    strict_recheck: bool = False
    led: bool = True

    def validate(self) -> None:
        if isinstance(self.countdown_s, bool) or not isinstance(self.countdown_s, (int, float)):
            raise ValueError("Le délai doit être un nombre de secondes.")
        if not 0 <= int(self.countdown_s) <= 600:
            raise ValueError("Le délai doit être entre 0 et 600 secondes.")
        self.countdown_s = int(self.countdown_s)


class StateStore:
    """settings.json, known destinations and job history, all atomic JSON writes."""

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.settings = self._load_settings()
        self._state = self._load_json("state.json", {})
        self._history: list[dict] = self._load_json("history.json", [])

    def _load_json(self, name: str, default):
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _load_settings(self) -> Settings:
        data = self._load_json("settings.json", {})
        known = {f.name for f in fields(Settings)}
        settings = Settings(**{k: v for k, v in data.items() if k in known})
        try:
            settings.validate()
        except (ValueError, TypeError):
            settings = Settings()
        return settings

    def update_settings(self, changes: dict) -> Settings:
        with self._lock:
            known = {f.name: f.type for f in fields(Settings)}
            merged = asdict(self.settings)
            for key, value in changes.items():
                if key not in known:
                    raise ValueError(f"Réglage inconnu : {key}")
                merged[key] = value
            candidate = Settings(**merged)
            for name in ("auto_backup", "auto_eject", "verify", "strict_recheck", "led"):
                if not isinstance(getattr(candidate, name), bool):
                    raise ValueError(f"« {name} » doit être vrai ou faux.")
            candidate.validate()
            self.settings = candidate
            write_json_atomic(self.dir / "settings.json", asdict(candidate))
            return candidate

    # Destinations the Pi has already seen, so they are mounted read-write directly.
    def known_destinations(self) -> set[str]:
        return set(self._state.get("destinations", []))

    def remember_destination(self, uuid: str | None) -> None:
        if not uuid:
            return
        with self._lock:
            dests = set(self._state.get("destinations", []))
            if uuid in dests:
                return
            dests.add(uuid)
            self._state["destinations"] = sorted(dests)
            write_json_atomic(self.dir / "state.json", self._state)

    def preferred_destination(self) -> str | None:
        return self._state.get("preferred_destination")

    def set_preferred_destination(self, uuid: str | None) -> None:
        with self._lock:
            self._state["preferred_destination"] = uuid
            write_json_atomic(self.dir / "state.json", self._state)

    def history(self) -> list[dict]:
        with self._lock:
            return list(self._history)

    def add_history(self, record: dict) -> None:
        with self._lock:
            self._history = [r for r in self._history if r.get("id") != record.get("id")]
            self._history.insert(0, record)
            del self._history[MAX_HISTORY:]
            write_json_atomic(self.dir / "history.json", self._history)
