import os
import threading
import subprocess
import time
import re
import shutil
import queue
from pathlib import Path

class TransferStatus:
    def __init__(self):
        self.reset()
    def reset(self):
        self.active = False
        self.mode = None
        self.src = None
        self.dst = None
        self.start_time = None
        self.bytes_total = 0
        self.bytes_done = 0
        self.speed_human = None
        self.eta = None
        self.percent = 0.0
        self.message = "idle"
        self.error = None

status = TransferStatus()
_cancel_flag = threading.Event()

def human_readable_size(num):
    for unit in ['B','KB','MB','GB','TB']:
        if num < 1024.0:
            return f"{num:3.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}PB"

def compute_total_bytes(path):
    total = 0
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root)/f).stat().st_size
            except Exception:
                pass
    return total

def _run_rsync(src, dst, move=False):
    # Ensure dst exists
    os.makedirs(dst, exist_ok=True)
    # if moving, use --remove-source-files then cleanup empty dirs
    args = ["rsync", "-a", "--info=progress2", "--human-readable"]
    if move:
        args += ["--remove-source-files"]
    # trailing slash copies contents of directory
    if os.path.isdir(src) and not src.endswith(os.sep):
        src += os.sep
    args += [src, dst]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True)
    prog_re = re.compile(r"(\d+)%\s+([0-9.,]+[KMG]B/s)\s+(\d{2}:\d{2}:\d{2})")
    for line in proc.stdout:
        line = line.strip()
        if _cancel_flag.is_set():
            proc.terminate()
            raise RuntimeError("Transfer cancelled")
        # Update from rsync progress
        m = prog_re.search(line)
        if m:
            status.percent = float(m.group(1))
            status.speed_human = m.group(2)
            status.eta = m.group(3)
            status.message = line
        if "to-chk=" in line and "%" in line:
            status.message = line
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"rsync exited with code {rc}")
    if move:
        # remove emptied directories at source
        try:
            subprocess.run(["find", src, "-type", "d", "-empty", "-delete"], check=False)
        except Exception:
            pass

def _fallback_copy(src, dst, move=False):
    # naive copy with progress via precomputed size
    os.makedirs(dst, exist_ok=True)
    def copy_file(s, d):
        with open(s, 'rb') as fsrc, open(d, 'wb') as fdst:
            while True:
                buf = fsrc.read(1024*1024)
                if not buf:
                    break
                fdst.write(buf)
                status.bytes_done += len(buf)
                status.percent = (status.bytes_done / max(status.bytes_total,1)) * 100.0
                status.speed_human = None
                status.eta = None
                if _cancel_flag.is_set():
                    raise RuntimeError("Transfer cancelled")

    if os.path.isdir(src):
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target_root = os.path.join(dst, rel if rel != "." else "")
            os.makedirs(target_root, exist_ok=True)
            for f in files:
                s = os.path.join(root, f)
                d = os.path.join(target_root, f)
                copy_file(s, d)
        if move:
            shutil.rmtree(src, ignore_errors=True)
    else:
        target = os.path.join(dst, os.path.basename(src))
        copy_file(src, target)
        if move:
            os.remove(src)

def start_transfer(src, dst, mode):
    if status.active:
        raise RuntimeError("A transfer is already running")
    _cancel_flag.clear()
    status.reset()
    status.active = True
    status.mode = mode
    status.src = src
    status.dst = dst
    status.start_time = time.time()
    status.message = "scanning..."
    try:
        status.bytes_total = compute_total_bytes(src)
    except Exception:
        status.bytes_total = 0
    def worker():
        try:
            # Prefer rsync if available
            try:
                subprocess.run(["rsync", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                _run_rsync(src, dst, move=(mode=="move"))
                status.percent = 100.0
            except Exception as e:
                status.message = f"rsync unavailable, using fallback: {e}"
                _fallback_copy(src, dst, move=(mode=="move"))
                status.percent = 100.0
            status.message = "completed"
        except Exception as e:
            status.error = str(e)
            status.message = "error"
        finally:
            status.active = False
    threading.Thread(target=worker, daemon=True).start()
    return True

def cancel_transfer():
    _cancel_flag.set()
    return True
