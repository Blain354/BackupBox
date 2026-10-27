"""US-03: camera cards are recognised and get a sensible default source."""

import pytest

from backupbox import simctl
from backupbox.detect import detect_card, slugify_source, validate_source_name


@pytest.mark.parametrize(
    "brand, expected",
    [("gopro", "GoPro"), ("dji", "DJI"), ("insta360", "Insta360"), ("sony", "Sony")],
)
def test_brands(tmp_path, brand, expected):
    media = simctl.make_card(tmp_path, "sdb1", brand=brand, clips=1, clip_bytes=10)
    info = detect_card(media, "LABEL", "UUID-1", 1)
    assert info.is_camera
    assert info.brand == expected
    assert info.default_source == expected


def test_gopro_serial_survives_reformatting(tmp_path):
    a = simctl.make_card(tmp_path, "a", clips=1, clip_bytes=10, serial="C3471325", card_uuid="1111")
    b = simctl.make_card(tmp_path, "b", clips=1, clip_bytes=10, serial="C3471325", card_uuid="2222")
    ia = detect_card(a, "GOPRO", "1111", 1)
    ib = detect_card(b, "GOPRO", "2222", 1)
    assert ia.identity == ib.identity == "gopro:C3471325"
    assert ia.model == "HERO12 Black"


def test_identity_falls_back_to_filesystem_uuid(tmp_path):
    media = simctl.make_card(tmp_path, "c", brand="dji", clips=1, clip_bytes=10)
    assert detect_card(media, "SD", "ABCD-1234", 1).identity == "fs:ABCD-1234"


def test_generic_dcim_uses_meaningful_label(tmp_path):
    media = simctl.make_card(tmp_path, "d", brand="generic", clips=1, clip_bytes=10)
    assert detect_card(media, "Caméra Rox", None, 5).default_source == "Camera_Rox"
    assert detect_card(media, "NO NAME", None, 5).default_source == "Camera"


def test_disk_without_dcim_is_not_a_camera(tmp_path):
    (tmp_path / "Documents").mkdir()
    info = detect_card(tmp_path, "Samsung T7", "X", 1)
    assert not info.is_camera
    assert info.default_source == "Samsung_T7"


def test_dcim_case_insensitive(tmp_path):
    (tmp_path / "dcim" / "100GOPRO").mkdir(parents=True)
    assert detect_card(tmp_path, None, None, 1).brand == "GoPro"


def test_dji_100media_needs_dji_files(tmp_path):
    folder = tmp_path / "DCIM" / "100MEDIA"
    folder.mkdir(parents=True)
    (folder / "IMG_0001.JPG").write_bytes(b"x")
    assert detect_card(tmp_path, None, None, 1).brand is None
    (folder / "DJI_0001.MP4").write_bytes(b"x")
    assert detect_card(tmp_path, None, None, 1).brand == "DJI"


def test_source_name_rules():
    assert validate_source_name(" GoPro_Keven ") == "GoPro_Keven"
    for bad in ["", "GoPro Keven", "../x", "_x", "Caméra", "a" * 49]:
        with pytest.raises(ValueError):
            validate_source_name(bad)
    assert slugify_source("Drône de Kévin!") == "Drone_de_Kevin"
