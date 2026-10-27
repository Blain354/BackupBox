"""HTTP API: the contract the web UI relies on."""

import pytest
from fastapi.testclient import TestClient

from backupbox.api import create_app


@pytest.fixture
def client(box):
    return TestClient(create_app(box.manager))


def test_index_and_static_are_served(client):
    res = client.get("/")
    assert res.status_code == 200 and "BackupBox" in res.text
    assert res.headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_full_flow_over_http(box, client):
    box.ssd()
    box.poll()
    ssd_key = box.key("sda1")
    assert client.get("/api/state").json()["devices"][0]["role"] == "unknown"

    res = client.post(f"/api/devices/{ssd_key}/init-destination", json={"project": "Islande_2026"})
    assert res.status_code == 200 and res.json()["role"] == "destination"

    box.card(clips=2)
    box.poll()
    job = client.get("/api/state").json()["jobs"][0]
    assert job["state"] == "countdown" and job["source_name"] == "GoPro"

    assert client.post(f"/api/jobs/{job['id']}/hold").json()["state"] == "held"
    bad = client.post(f"/api/jobs/{job['id']}/source", json={"name": "Go Pro"})
    assert bad.status_code == 400 and "Nom de source invalide" in bad.json()["detail"]
    assert client.post(f"/api/jobs/{job['id']}/source", json={"name": "GoPro_Rox"}).json()["source_name"] == "GoPro_Rox"
    assert client.post(f"/api/jobs/{job['id']}/start").json()["state"] == "queued"
    box.drain()

    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert detail["state"] == "done" and detail["problems"] == []
    assert client.get("/api/history").json()[0]["id"] == job["id"]
    projects = client.get("/api/destination/projects").json()
    assert projects == {"current": "Islande_2026", "projects": ["Islande_2026"], "sources": ["GoPro_Rox"]}

    assert client.post(f"/api/devices/{ssd_key}/eject").status_code == 200


def test_errors_are_french_with_status_codes(box, client):
    assert client.post("/api/jobs/nope/start").status_code == 404
    assert client.post("/api/devices/nope/eject").json()["detail"].startswith("Périphérique introuvable")
    assert client.get("/api/destination/projects").status_code == 404
    assert client.post("/api/destination/project", json={"project": "x"}).status_code == 409
    box.ssd()
    box.poll()
    res = client.post(f"/api/devices/{box.key('sda1')}/init-destination", json={"project": "../x"})
    assert res.status_code == 400
    assert client.post("/api/system/format").status_code == 404


def test_settings_endpoint(client):
    assert client.put("/api/settings", json={}).status_code == 400
    assert client.put("/api/settings", json={"countdown_s": 700}).status_code == 400
    res = client.put("/api/settings", json={"countdown_s": 10, "auto_eject": False})
    assert res.status_code == 200 and res.json()["countdown_s"] == 10
    assert client.get("/api/settings").json()["auto_eject"] is False


def test_manual_backup_and_poweroff(box, client):
    box.store.update_settings({"auto_backup": False})
    box.ready_destination()
    box.card(clips=1)
    box.poll()
    res = client.post(f"/api/devices/{box.key('sdb1')}/backup", json={"source_name": "Cam_A"})
    assert res.status_code == 200 and res.json()["state"] == "queued"
    again = client.post(f"/api/devices/{box.key('sdb1')}/backup", json={})
    assert again.status_code == 409
    box.drain()
    assert client.post("/api/system/poweroff").json() == {"ok": True}
    assert client.get("/api/health").json()["version"]
