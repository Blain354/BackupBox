"""Recognise camera cards and devices; derive a default source name and an identity.

Three ways a source reaches the Pi:

* ``reader``: a card in a USB card reader. Identity comes from the card itself
  (GoPro serial in ``MISC/version.txt``, else the filesystem UUID).
* ``direct``: the camera or drone plugged in with its USB cable and exposing its
  storage as a USB disk (DJI drones, Insta360). The USB vendor/model tell the
  brand and the USB serial number identifies the device for good, even after
  the card inside is reformatted.
* ``mtp``: the camera talks MTP/PTP over USB (GoPro, most photo cameras); see
  ``mtp.py``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

GENERIC_LABELS = {"", "NO NAME", "NONAME", "UNTITLED", "SD", "SDCARD", "SD_CARD", "DISK", "USB", "EOS_DIGITAL"}

# Directory name patterns under DCIM/ -> brand.
DCIM_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^\d{3}GOPRO$", re.I), "GoPro"),
    (re.compile(r"^DJI_\d+$", re.I), "DJI"),
    (re.compile(r"^\d{3}MEDIA$", re.I), "DJI"),
    (re.compile(r"^Camera\d+$", re.I), "Insta360"),
    (re.compile(r"^\d{3}CANON$", re.I), "Canon"),
    (re.compile(r"^\d{3}(NIKON|NCD\w*|NZ\w*)$", re.I), "Nikon"),
    (re.compile(r"^\d{3}_FUJI$", re.I), "Fujifilm"),
    (re.compile(r"^\d{3}_PANA$", re.I), "Panasonic"),
    (re.compile(r"^\d{3}(OLYMP|OMSYS)$", re.I), "Olympus"),
    (re.compile(r"^\d{3}MSDCF$", re.I), "Sony"),
]

FILE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(G[XHL]\d{6}|GOPR\d{4}|GP\d{6})\.", re.I), "GoPro"),
    (re.compile(r"^DJI_", re.I), "DJI"),
    (re.compile(r"\.ins[vp]$", re.I), "Insta360"),
]

SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")

# Substrings of USB vendor/model strings -> brand. A match means the device itself
# is plugged in (card readers report their own maker: Realtek, Generic, SanDisk…).
USB_BRANDS: list[tuple[str, str]] = [
    ("dji", "DJI"),
    ("gopro", "GoPro"),
    ("insta360", "Insta360"),
    ("arashi", "Insta360"),
    ("hasselblad", "Hasselblad"),
    ("sony", "Sony"),
    ("canon", "Canon"),
    ("nikon", "Nikon"),
    ("fujifilm", "Fujifilm"),
    ("panasonic", "Panasonic"),
    ("olympus", "Olympus"),
    ("om digital", "OM System"),
    ("ricoh", "Ricoh"),
    ("akaso", "Akaso"),
]

GENERIC_MODELS = {
    "", "mass storage", "mass-storage", "storage", "disk", "usb", "usb disk", "card reader",
    "sd card", "sd/mmc", "flash disk", "ums disk", "msc", "removable disk",
}


@dataclass(frozen=True)
class CardInfo:
    is_camera: bool
    brand: str | None
    model: str | None
    default_source: str
    identity: str
    connection: str = "reader"  # reader | direct | mtp


def _child(parent: Path, name: str) -> Path | None:
    """Case-insensitive lookup of a direct child."""
    try:
        for entry in parent.iterdir():
            if entry.name.lower() == name.lower():
                return entry
    except OSError:
        return None
    return None


def slugify_source(text: str) -> str:
    """Make a folder-safe source name: ASCII letters, digits, '_' and '-'."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", ascii_text).strip("_-")
    return slug[:48]


def validate_source_name(name: str) -> str:
    name = (name or "").strip()
    if not SOURCE_NAME_RE.match(name):
        raise ValueError(
            "Nom de source invalide : lettres sans accent, chiffres, « _ » ou « - », 48 caractères max."
        )
    return name


# Card readers sold by camera makers (Sony MRW-…) must not pass for the camera.
READER_HINTS = ("reader", "mrw-", "card")


def brand_from_usb(vendor: str | None, model: str | None) -> str | None:
    text = f"{vendor or ''} {model or ''}".lower()
    if any(hint in text for hint in READER_HINTS):
        return None
    for needle, brand in USB_BRANDS:
        if needle in text:
            return brand
    return None


def _meaningful_model(model: str | None) -> str | None:
    if not model:
        return None
    cleaned = model.strip().replace("_", " ")
    return None if cleaned.lower() in GENERIC_MODELS else cleaned


def _default_for(brand: str, model: str | None) -> str:
    if model:
        name = model if model.lower().startswith(brand.lower()) else f"{brand} {model}"
        slug = slugify_source(name)
        if slug:
            return slug
    return brand


def camera_info(model: str, serial: str | None) -> CardInfo:
    """Identity of a camera seen over MTP/PTP (no filesystem to inspect)."""
    brand = brand_from_usb(None, model)
    display = _meaningful_model(model)
    return CardInfo(
        is_camera=True,
        brand=brand,
        model=display,
        default_source=_default_for(brand, display) if brand else (slugify_source(model) or "Camera"),
        identity=f"mtp:{serial}" if serial else f"mtp-model:{model}",
        connection="mtp",
    )


def read_gopro_version(root: Path) -> dict[str, str]:
    misc = _child(root, "MISC")
    if misc is None:
        return {}
    version = _child(misc, "version.txt")
    if version is None or not version.is_file():
        return {}
    try:
        text = version.read_text(errors="replace")
    except OSError:
        return {}
    # GoPro writes JSON with a trailing comma: parse key/value pairs directly.
    return dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]*)"', text))


def _brand_from_dcim(dcim: Path) -> str | None:
    try:
        entries = sorted(dcim.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for entry in entries:
        if entry.is_dir():
            for pattern, brand in DCIM_PATTERNS:
                if pattern.match(entry.name):
                    if brand == "DJI" and entry.name.upper().endswith("MEDIA"):
                        if not _has_matching_file(entry, FILE_PATTERNS[1][0]):
                            continue
                    return brand
    for entry in entries:
        if entry.is_dir():
            for pattern, brand in FILE_PATTERNS:
                if _has_matching_file(entry, pattern):
                    return brand
    return None


def _has_matching_file(folder: Path, pattern: re.Pattern, limit: int = 50) -> bool:
    try:
        for i, entry in enumerate(folder.iterdir()):
            if i >= limit:
                break
            if pattern.search(entry.name):
                return True
    except OSError:
        return False
    return False


def detect_card(
    root: Path,
    label: str | None,
    uuid: str | None,
    size: int,
    vendor: str | None = None,
    model: str | None = None,
    serial: str | None = None,
) -> CardInfo:
    """Inspect a mounted filesystem and say whether it is a camera card or device.

    ``vendor``, ``model`` and ``serial`` are the USB descriptors of the disk.
    """
    root = Path(root)
    dcim = _child(root, "DCIM")
    private = _child(root, "PRIVATE")
    brand: str | None = None
    is_camera = False

    if dcim is not None and dcim.is_dir():
        is_camera = True
        brand = _brand_from_dcim(dcim)
    if private is not None and private.is_dir():
        if _child(private, "M4ROOT") is not None:
            is_camera, brand = True, brand or "Sony"
        elif _child(private, "AVCHD") is not None:
            is_camera = True

    gopro = read_gopro_version(root)
    if gopro:
        brand = "GoPro"

    usb_brand = brand_from_usb(vendor, model)
    if usb_brand:
        # The camera/drone itself is on the cable: its USB identity is the best one.
        usb_model = _meaningful_model(model)
        identity = f"usb:{usb_brand.lower()}:{serial}" if serial else _card_identity(gopro, uuid, label, size)
        return CardInfo(
            is_camera=True,
            brand=usb_brand,
            model=usb_model or model_from_card(gopro),
            default_source=_default_for(usb_brand, usb_model),
            identity=identity,
            connection="direct",
        )

    identity = _card_identity(gopro, uuid, label, size)
    if brand:
        default = brand
    elif label and label.strip().upper() not in GENERIC_LABELS and slugify_source(label):
        default = slugify_source(label)
    else:
        default = "Camera" if is_camera else "Disque"
    return CardInfo(
        is_camera=is_camera, brand=brand, model=model_from_card(gopro), default_source=default, identity=identity
    )


def model_from_card(gopro: dict[str, str]) -> str | None:
    return gopro.get("camera type") or None


def _card_identity(gopro: dict[str, str], uuid: str | None, label: str | None, size: int) -> str:
    serial = gopro.get("camera serial number")
    if serial:
        return f"gopro:{serial}"
    if uuid:
        return f"fs:{uuid}"
    return f"label:{label or ''}:{size}"
