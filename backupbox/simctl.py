"""Plug and unplug fake devices for ``--simulate`` mode.

Examples::

    python -m backupbox.simctl ./sim ssd sda1 --label T7
    python -m backupbox.simctl ./sim card sdb1 --brand gopro --clips 5 --clip-mb 50
    python -m backupbox.simctl ./sim unplug sdb1
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

CARD_LAYOUTS = {
    "gopro": ("DCIM/100GOPRO", "GX01{n:04d}.MP4", "GOPRO"),
    "dji": ("DCIM/DJI_001", "DJI_20260101120000_{n:04d}_D.MP4", "SD_Card"),
    "insta360": ("DCIM/Camera01", "VID_20260101_120000_00_{n:03d}.insv", "Insta360"),
    "sony": ("PRIVATE/M4ROOT/CLIP", "C{n:04d}.MP4", "Untitled"),
    "generic": ("DCIM/100CAMERA", "IMG_{n:04d}.JPG", "NO NAME"),
}


def _staging(root: Path, kname: str, meta: dict) -> Path:
    """Build a device in a hidden folder; ``_publish`` plugs it in atomically."""
    device = root / f".staging-{kname}"
    shutil.rmtree(device, ignore_errors=True)
    (device / "media").mkdir(parents=True)
    (device / "device.json").write_text(json.dumps(meta, indent=2))
    return device / "media"


def _publish(root: Path, kname: str) -> Path:
    target = root / kname
    if target.exists():
        raise FileExistsError(f"{kname} est déjà branché")
    os.rename(root / f".staging-{kname}", target)
    return target / "media"


def make_card(
    root: Path,
    kname: str,
    brand: str = "gopro",
    clips: int = 3,
    clip_bytes: int = 1024 * 1024,
    label: str | None = None,
    card_uuid: str | None = None,
    serial: str | None = None,
    start: int = 1,
) -> Path:
    folder, pattern, default_label = CARD_LAYOUTS[brand]
    media = _staging(
        root,
        kname,
        {
            "label": label if label is not None else default_label,
            "uuid": card_uuid or uuid.uuid4().hex[:8].upper(),
            "fstype": "exfat",
            "size": 128 * 1000**3,
            "disk": kname.rstrip("0123456789") or kname,
        },
    )
    target = media / folder
    target.mkdir(parents=True, exist_ok=True)
    for n in range(start, start + clips):
        path = target / pattern.format(n=n)
        with open(path, "wb") as fh:
            fh.write(os.urandom(min(clip_bytes, 1024 * 1024)))
            remaining = clip_bytes - min(clip_bytes, 1024 * 1024)
            while remaining > 0:
                chunk = min(remaining, 1024 * 1024)
                fh.write(os.urandom(chunk))
                remaining -= chunk
        os.utime(path, (1767268800 + n * 60, 1767268800 + n * 60))
    if brand == "gopro":
        misc = media / "MISC"
        misc.mkdir(exist_ok=True)
        (misc / "version.txt").write_text(
            '{\n"info version":"2.0",\n"camera type":"HERO12 Black",\n'
            + (f'"camera serial number":"{serial}",\n' if serial else "")
            + "}\n"
        )
    return _publish(root, kname)


def make_ssd(root: Path, kname: str, label: str = "T7", free_bytes: int | None = None, ssd_uuid: str | None = None) -> Path:
    meta = {
        "label": label,
        "uuid": ssd_uuid or uuid.uuid4().hex[:8].upper(),
        "fstype": "exfat",
        "size": 2 * 1000**4,
        "disk": kname.rstrip("0123456789") or kname,
    }
    if free_bytes is not None:
        meta["free_bytes"] = free_bytes
    _staging(root, kname, meta)
    return _publish(root, kname)


def unplug(root: Path, kname: str) -> None:
    shutil.rmtree(root / kname, ignore_errors=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="backupbox.simctl")
    parser.add_argument("root", type=Path)
    sub = parser.add_subparsers(dest="cmd", required=True)
    ssd = sub.add_parser("ssd", help="plug an empty SSD")
    ssd.add_argument("kname")
    ssd.add_argument("--label", default="T7")
    ssd.add_argument("--free-bytes", type=int)
    card = sub.add_parser("card", help="plug a camera card")
    card.add_argument("kname")
    card.add_argument("--brand", choices=sorted(CARD_LAYOUTS), default="gopro")
    card.add_argument("--clips", type=int, default=3)
    card.add_argument("--clip-mb", type=float, default=5)
    card.add_argument("--label")
    card.add_argument("--uuid")
    card.add_argument("--serial")
    out = sub.add_parser("unplug", help="remove a device")
    out.add_argument("kname")
    args = parser.parse_args(argv)
    args.root.mkdir(parents=True, exist_ok=True)
    if args.cmd == "ssd":
        print(make_ssd(args.root, args.kname, args.label, args.free_bytes))
    elif args.cmd == "card":
        print(
            make_card(
                args.root,
                args.kname,
                args.brand,
                args.clips,
                int(args.clip_mb * 1024 * 1024),
                args.label,
                args.uuid,
                args.serial,
            )
        )
    else:
        unplug(args.root, args.kname)


if __name__ == "__main__":
    main()
