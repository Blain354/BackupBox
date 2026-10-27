"""US-01/US-02: the SSD marker, project names and the Footage_RAW layout."""

import pytest

from backupbox import destination as d


def test_validate_project():
    assert d.validate_project("Islande_2026") == "Islande_2026"
    assert d.validate_project(" /Voyages/Islande 2026/ ") == "Voyages/Islande 2026"
    assert d.validate_project("") == ""
    assert d.validate_project("Québec") == "Québec"
    for bad in ["..", "a/../b", ".cache", "a/b/c/d", "Footage_RAW", "x:y", "trailing.", "a//b", "a?b"]:
        with pytest.raises(ValueError):
            d.validate_project(bad)


def test_init_creates_marker_and_keeps_existing_data(tmp_path):
    (tmp_path / "old.txt").write_text("keep")
    cfg = d.init_destination(tmp_path, "Islande_2026")
    assert d.is_destination(tmp_path)
    assert (tmp_path / "old.txt").read_text() == "keep"
    assert d.load_config(tmp_path).project == "Islande_2026"
    again = d.init_destination(tmp_path, None)
    assert again.id == cfg.id and again.project == "Islande_2026"


def test_raw_dir_reuses_any_casing(tmp_path):
    (tmp_path / "Trip" / "Footage_raw").mkdir(parents=True)
    assert d.raw_dir(tmp_path, "Trip").name == "Footage_raw"
    created = d.raw_dir(tmp_path, "New", create=True)
    assert created == tmp_path / "New" / "Footage_RAW" and created.is_dir()
    assert d.raw_dir(tmp_path, "", create=True) == tmp_path / "Footage_RAW"


def test_list_projects_and_sources(tmp_path):
    (tmp_path / "Islande" / "Footage_RAW" / "GoPro").mkdir(parents=True)
    (tmp_path / "Voyages" / "Japon" / "footage_raw" / "DJI").mkdir(parents=True)
    (tmp_path / "Divers" / "photos").mkdir(parents=True)
    (tmp_path / ".backupbox").mkdir()
    assert d.list_projects(tmp_path) == ["Islande", "Voyages/Japon"]
    assert d.list_sources(tmp_path, "Islande") == ["GoPro"]
    assert d.list_sources(tmp_path, "Divers") == []


def test_clean_tmp_removes_interrupted_copies(tmp_path):
    folder = d.tmp_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "abc.bbpart").write_bytes(b"partial")
    assert d.clean_tmp(tmp_path) == 1
    assert list(folder.iterdir()) == []
