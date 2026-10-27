from __future__ import annotations

import time
from pathlib import Path

import pytest

from backupbox import simctl
from backupbox.manager import Manager
from backupbox.platform import SimPlatform
from backupbox.settings import StateStore


class FakeLed:
    def __init__(self) -> None:
        self.state = "idle"
        self.history: list[str] = []

    def set(self, state: str) -> None:
        if state != self.state:
            self.history.append(state)
        self.state = state


class Box:
    """A Manager on simulated hardware, driven synchronously by the tests."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "dev"
        self.state_dir = tmp_path / "state"
        self.platform = SimPlatform(self.root)
        self.store = StateStore(self.state_dir)
        self.led = FakeLed()
        self.manager = Manager(self.platform, self.store, simulate=True, led=self.led)

    # hardware
    def ssd(self, kname="sda1", **kw) -> Path:
        return simctl.make_ssd(self.root, kname, **kw)

    def card(self, kname="sdb1", **kw) -> Path:
        return simctl.make_card(self.root, kname, **kw)

    def unplug(self, kname: str) -> None:
        simctl.unplug(self.root, kname)

    # manager helpers
    def poll(self) -> None:
        self.manager.poll_once()

    def device(self, kname: str):
        for rec in self.manager.devices.values():
            if rec.dev.kname == kname:
                return rec
        raise KeyError(kname)

    def key(self, kname: str) -> str:
        return self.device(kname).dev.key

    def ready_destination(self, project="Islande_2026", kname="sda1") -> Path:
        media = self.ssd(kname)
        self.poll()
        self.manager.init_destination(self.key(kname), project)
        return media

    def expire_countdowns(self) -> None:
        for job in self.manager.jobs.values():
            if job.state == "countdown":
                job.deadline = time.monotonic() - 1
        self.manager.poll_once()

    def drain(self) -> list:
        ran = []
        while True:
            job = self.manager._next_job()
            if job is None:
                return ran
            self.manager.run_job(job)
            ran.append(job)

    def job_for(self, kname: str):
        rec = self.device(kname)
        return self.manager.jobs[rec.job_id] if rec.job_id else None

    def raw(self, media: Path, project="Islande_2026") -> Path:
        return media / project / "Footage_RAW"


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)
