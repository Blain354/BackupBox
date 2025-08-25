from flask import Flask, jsonify, request, send_from_directory, render_template
import os, json, subprocess, re, shlex, time
from pathlib import Path
import psutil
from transfer import status, start_transfer, cancel_transfer
import config

app = Flask(__name__, static_url_path='/static', template_folder='templates')

def list_mounts():
    # Try lsblk JSON for robust device info
    devices = []
    try:
        out = subprocess.check_output(["lsblk", "-J", "-o", "NAME,MOUNTPOINT,LABEL,RM,TYPE,PATH"], text=True)
        data = json.loads(out)
        def walk(node):
            if isinstance(node, dict):
                name = node.get("name")
                mnt = node.get("mountpoint")
                label = node.get("label")
                rm = node.get("rm")
                typ = node.get("type")
                path = node.get("path")
                if mnt and typ in ("part","crypt","loop","lvm"):
                    devices.append({
                        "name": name,
                        "mountpoint": mnt,
                        "label": label or name,
                        "removable": bool(rm),
                        "path": path
                    })
                for c in node.get("children",[]):
                    walk(c)
            elif isinstance(node, list):
                for n in node:
                    walk(n)
        walk(data.get("blockdevices",[]))
    except Exception:
        # Fallback: list common mount roots
        seen = set()
        for root in config.MOUNT_ROOTS:
            if os.path.isdir(root):
                for base, dirs, files in os.walk(root):
                    # only take top-level mount dirs
                    for d in dirs:
                        mnt = os.path.join(base,d)
                        if os.path.ismount(mnt) and mnt not in seen:
                            seen.add(mnt)
                            devices.append({"name": d, "mountpoint": mnt, "label": d, "removable": True, "path": None})
                    break
    # Also include internal storage home folder convenience
    devices.append({"name":"home", "mountpoint":str(Path.home()), "label":"Home", "removable": False, "path": None})
    return devices

def list_dir(path):
    path = Path(path)
    if not path.exists():
        return []
    entries = []
    for p in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            entries.append({
                "name": p.name,
                "path": str(p),
                "is_dir": p.is_dir()
            })
        except Exception:
            pass
    return entries

@app.route("/")
def index():
    return render_template("index.html")

@app.get("/api/devices")
def api_devices():
    return jsonify(list_mounts())

@app.get("/api/list")
def api_list():
    path = request.args.get("path","")
    return jsonify(list_dir(path))

@app.post("/api/transfer")
def api_transfer():
    data = request.get_json(force=True)
    mode = data.get("mode")
    src = data.get("src")
    dst = data.get("dst")
    if mode not in ("copy","move"):
        return jsonify({"ok": False, "error":"invalid mode"}), 400
    if not src or not dst:
        return jsonify({"ok": False, "error":"src/dst required"}), 400
    try:
        start_transfer(src, dst, "move" if mode=="move" else "copy")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.get("/api/status")
def api_status():
    # try to estimate speed and ETA if rsync not present
    elapsed = (time.time() - status.start_time) if status.start_time else None
    resp = {
        "active": status.active,
        "mode": status.mode,
        "src": status.src,
        "dst": status.dst,
        "percent": status.percent,
        "bytes_total": status.bytes_total,
        "bytes_done": status.bytes_done,
        "speed": status.speed_human,
        "eta": status.eta,
        "message": status.message,
        "error": status.error,
        "elapsed_s": elapsed
    }
    return jsonify(resp)

@app.post("/api/cancel")
def api_cancel():
    cancel_transfer()
    return jsonify({"ok": True})

@app.post("/api/eject")
def api_eject():
    data = request.get_json(force=True)
    mountpoint = data.get("mountpoint")
    if not mountpoint:
        return jsonify({"ok": False, "error":"mountpoint required"}), 400
    try:
        subprocess.run(["sync"], check=False)
        # Try umount by mountpoint
        r = subprocess.run(["umount", mountpoint], check=False, capture_output=True, text=True)
        if r.returncode != 0:
            return jsonify({"ok": False, "error": r.stderr.strip() or "umount failed"}), 400
        subprocess.run(["sync"], check=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

def create_app():
    return app

if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, debug=False)
