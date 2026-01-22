import os
import json
import time
import zipfile
import datetime
import threading
import tkinter as tk
from tkinter import messagebox, filedialog
import schedule
import winsound
import sys
from urllib.parse import urlparse, unquote_plus
# ftplib left present for backward compatibility in other features but FTP restore path removed
from ftplib import FTP
import io
import posixpath
import socket
import shutil
import tempfile

# Optional CTk for themed popups if available (Host-only convenience)
try:
    import customtkinter as ctk
except Exception:
    ctk = None

# ------------------ Base dir handling ------------------
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(__file__)

SCHEDULE_FILE = os.path.join(BASE_DIR, "schedule.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

# ------------------ Themed input helper (Host) ------------------
def themed_input_popup(title, prompt, default_value=""):
    if ctk is None:
        root = tk.Tk(); root.withdraw()
        try:
            from tkinter.simpledialog import askstring
            return askstring(title, prompt, initialvalue=default_value)
        finally:
            root.destroy()

    popup = ctk.CTkToplevel()
    popup.title(title)
    popup.geometry("520x220")
    popup.resizable(False, False)
    popup.grab_set()
    popup.configure(fg_color="#F5F5F7")

    ctk.CTkLabel(
        popup, text=prompt, font=("Segoe UI", 13), text_color="black",
        wraplength=470, justify="left"
    ).pack(pady=(20, 10), padx=16, anchor="w")

    var = tk.StringVar(value=default_value)
    entry = ctk.CTkEntry(popup, textvariable=var, width=470)
    entry.pack(pady=(0, 18))
    entry.focus_set()

    def submit():
        popup.result = var.get().strip()
        popup.destroy()

    ctk.CTkButton(
        popup, text="OK", command=submit,
        fg_color="#1E2A44", hover_color="#233155", corner_radius=18
    ).pack(pady=(0, 12))
    popup.wait_window()
    return getattr(popup, "result", None)

# ------------------ Config helpers ------------------
def load_config():
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("source_path", "")
                data.setdefault("destination_path", os.path.join("C:\\", "Backup Drive"))
                data.setdefault("schedules", [])
                return data
        except Exception as e:
            print("[✘] load_config error:", e)
    return {"source_path": "", "destination_path": os.path.join("C:\\", "Backup Drive"), "schedules": []}

def save_config(data):
    allowed_keys = {"source_path", "destination_path", "schedules", "remote_path"}
    cleaned = {k: data[k] for k in data if k in allowed_keys}
    cleaned.setdefault("source_path", "")
    cleaned.setdefault("destination_path", os.path.join("C:\\", "Backup Drive"))
    cleaned.setdefault("schedules", [])
    try:
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=4)
    except Exception as e:
        print("[✘] save_config error:", e)

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print("[✘] load_settings error:", e)
            data = {}
    else:
        data = {}

    legacy = data.get("apps_to_close")
    if legacy and not data.get("apps_to_close_slave"):
        if isinstance(legacy, list):
            data["apps_to_close_slave"] = legacy[:]
        else:
            data["apps_to_close_slave"] = []

    data.setdefault("apps_to_close_slave", [])
    data.setdefault("slave_agent_host", "")
    data.setdefault("slave_agent_port", 7788)
    data.setdefault("sound_enabled", True)
    data.setdefault("sound_file", "")
    return data

def save_settings(st: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=4)
    except Exception as e:
        print("[✘] save_settings error:", e)

# ------------------ Sound helper (Host) ------------------
def play_alert_sound():
    try:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass

# ------------------ Slave agent comms ------------------
def _resolve_slave_host_from_source(source_url: str, explicit_host: str) -> str:
    host = (explicit_host or "").strip()
    if not host and source_url and source_url.lower().startswith("ftp://"):
        try:
            parsed = urlparse(source_url)
            if parsed.hostname:
                host = parsed.hostname
        except Exception:
            host = ""
    return host

# Small helper: read header terminated by b"\n\n"
def _recv_header(sock, timeout=20):
    sock.settimeout(timeout)
    data = b""
    try:
        data = sock.recv(4096)
    except Exception as e:
        raise
    if not data:
        return None, b""
    if b"\n\n" in data:
        head, rest = data.split(b"\n\n", 1)
        return head.decode("utf-8", errors="replace"), rest
    else:
        return data.decode("utf-8", errors="replace"), b""

def _send_header(sock, obj: dict):
    raw = (json.dumps(obj) + "\n\n").encode("utf-8")
    sock.sendall(raw)

def _agent_roundtrip(payload: dict, host: str, port: int, timeout: int = 40) -> dict:
    data_out = json.dumps(payload).encode("utf-8") + b"\n\n"
    resp = None
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(data_out)
        try:
            hdr_raw = sock.recv(4096)
        except Exception as e:
            raise
        if not hdr_raw:
            raise TimeoutError("No response from Slave agent")
        try:
            txt = hdr_raw.decode("utf-8", errors="replace")
            if "\n\n" in txt:
                txt = txt.split("\n\n", 1)[0]
            resp = json.loads(txt)
            return resp
        except Exception as e:
            raise ValueError(f"Invalid JSON from Slave agent: {e}")

def send_apps_to_slave_and_wait(apps, source_url=None, timeout=20):
    if not apps:
        return {"status": "ok", "killed": []}

    settings = load_settings()
    host = _resolve_slave_host_from_source(source_url, settings.get("slave_agent_host", ""))
    port = int(settings.get("slave_agent_port", 7788))
    if not host:
        raise Exception("Slave agent host unknown. Set 'slave_agent_host' in settings.json or use ftp:// source_path with hostname.")

    payload = {"command": "kill_apps", "apps": apps}
    try:
        return _agent_roundtrip(payload, host, port, timeout=timeout)
    except Exception as e:
        raise Exception(f"Failed to reach Slave agent at {host}:{port} — {e}")

def show_slave_backup_prompt(remind_minutes: int, source_url: str, timeout: int = 45) -> dict:
    try:
        settings = load_settings()
        host = _resolve_slave_host_from_source(source_url, settings.get("slave_agent_host", ""))
        port = int(settings.get("slave_agent_port", 7788))
        if not host:
            raise Exception("Slave agent host unknown.")

        payload = {
            "command": "show_backup_prompt",
            "remind_minutes": int(remind_minutes or 5),
            "sound_enabled": bool(settings.get("sound_enabled", True)),
            "sound_file": settings.get("sound_file", ""),
        }
        resp = _agent_roundtrip(payload, host, port, timeout=timeout)
        if resp.get("status") == "ok" and resp.get("choice") in ("ok", "cancel", "remind"):
            return resp
        return {"status": "ok", "choice": "ok", "remind_minutes": int(remind_minutes or 5)}
    except Exception as e:
        print(f"[⚠] Slave prompt unavailable, proceeding with backup (fail-open): {e}")
        return {"status": "ok", "choice": "ok", "remind_minutes": int(remind_minutes or 5)}

# ------------------ Direct socket ZIP helpers (Option B) ------------------
def _request_zip_from_slave(host: str, port: int, remote_rel_path: str, dest_zip_path: str, timeout: int = 120):
    """
    Ask Slave to zip remote_rel_path (relative to Slave's configured root) and stream zip back.
    Protocol:
      - Host sends header JSON: {"command":"send_zip", "remote_rel": "..."} + \n\n
      - Slave responds header JSON: {"status":"ok","zip_name":"...","zip_size":N} + \n\n
      - Slave then streams exactly zip_size bytes (raw)
      - Host writes bytes to dest_zip_path
      - Host sends final ack JSON {"status":"received"} + \n\n
      - Slave deletes temp zip and will send final JSON result (optional). We will attempt to read that final JSON header (single recv).
    """
    with socket.create_connection((host, port), timeout=30) as sock:
        sock.settimeout(timeout)
        # send request header
        req = {"command": "send_zip", "remote_rel": remote_rel_path}
        _send_header(sock, req)

        # read ack header (single recv that may contain header only or header+part-of-zip - we handle both)
        hdr_raw = sock.recv(4096)
        if not hdr_raw:
            raise TimeoutError("No response header from Slave when requesting ZIP")
        if b"\n\n" in hdr_raw:
            hdr_txt, remainder = hdr_raw.split(b"\n\n", 1)
        else:
            hdr_txt = hdr_raw
            remainder = b""

        try:
            hdr = json.loads(hdr_txt.decode("utf-8", errors="replace"))
        except Exception as e:
            raise ValueError(f"Invalid header JSON from Slave: {e}")

        if hdr.get("status") != "ok":
            raise Exception(f"Slave error: {hdr}")

        zip_size = int(hdr.get("zip_size", 0))
        zip_name = hdr.get("zip_name", "backup.zip")

        # open destination and write remainder first, then read rest
        downloaded = 0
        with open(dest_zip_path, "wb") as f:
            if remainder:
                f.write(remainder)
                downloaded += len(remainder)
            while downloaded < zip_size:
                chunk = sock.recv(min(65536, zip_size - downloaded))
                if not chunk:
                    raise ConnectionError("Connection closed before zip transfer completed")
                f.write(chunk)
                downloaded += len(chunk)

        # send final ack
        _send_header(sock, {"status": "received"})
        # attempt to read final JSON response (single recv) - not required but helpful
        try:
            final_raw = sock.recv(4096)
            if final_raw:
                # trim delimiting \n\n if present
                if b"\n\n" in final_raw:
                    txt = final_raw.split(b"\n\n", 1)[0].decode("utf-8", errors="replace")
                else:
                    txt = final_raw.decode("utf-8", errors="replace")
                try:
                    final = json.loads(txt)
                    return {"status": "ok", "zip_name": zip_name, "zip_size": zip_size, "info": final}
                except Exception:
                    # ignore parse errors; return success
                    return {"status": "ok", "zip_name": zip_name, "zip_size": zip_size}
        except Exception:
            pass

        return {"status": "ok", "zip_name": zip_name, "zip_size": zip_size}

def _stream_zip_to_slave(host: str, port: int, zip_path: str, target_rel: str, timeout: int = 120):
    """
    Stream a local zip at zip_path to the Slave for restore.
    Protocol:
      - Host connects and sends header JSON: {"command":"restore_stream", "target_rel": "...", "zip_name": "...", "zip_size": N} + \n\n
      - Slave responds with {"status":"ready"} + \n\n
      - Host streams raw zip bytes and then closes socket.
      - Slave will process (unpack) and reply when done (may be via log).
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(zip_path)
    size = os.path.getsize(zip_path)
    zip_name = os.path.basename(zip_path)

    with socket.create_connection((host, port), timeout=30) as sock:
        sock.settimeout(timeout)
        req = {"command": "restore_stream", "target_rel": target_rel, "zip_name": zip_name, "zip_size": size}
        _send_header(sock, req)

        # read slave ready header (single recv)
        hdr_raw = sock.recv(4096)
        if not hdr_raw:
            raise TimeoutError("No response from Slave for restore_stream")
        # parse header
        if b"\n\n" in hdr_raw:
            hdr_txt = hdr_raw.split(b"\n\n", 1)[0]
        else:
            hdr_txt = hdr_raw
        try:
            hdr = json.loads(hdr_txt.decode("utf-8", errors="replace"))
        except Exception as e:
            raise ValueError(f"Invalid JSON from Slave during restore_stream: {e}")
        if hdr.get("status") != "ready":
            raise Exception(f"Slave not ready for restore: {hdr}")

        # stream zip
        with open(zip_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                sock.sendall(chunk)

        # close socket to indicate EOF to slave (socket context manager will close)
        # Attempt to read final JSON result (slave may send result before closing)
        try:
            sock.settimeout(10)
            final = sock.recv(4096)
            if final:
                if b"\n\n" in final:
                    txt = final.split(b"\n\n", 1)[0].decode("utf-8", errors="replace")
                else:
                    txt = final.decode("utf-8", errors="replace")
                try:
                    return json.loads(txt)
                except Exception:
                    return {"status": "ok"}
        except Exception:
            pass
    return {"status": "ok"}

# ------------------ FTP helpers (kept for other features) ------------------
def _create_ftp(host, user, pwd, port=2121, passive=True, timeout=20, debug=0):
    ftp = FTP()
    try:
        ftp.connect(host, int(port), timeout=timeout)
    except Exception:
        ftp.connect(host, 21, timeout=timeout)
    ftp.set_pasv(passive)
    ftp.set_debuglevel(debug)
    ftp.login(user or "", pwd or "")
    ftp._host = host
    ftp._user = user
    ftp._pass = pwd
    ftp._mode = 'passive' if passive else 'active'
    return ftp

def _ftp_connect(ftp_url: str):
    if not ftp_url or not ftp_url.lower().startswith("ftp://"):
        raise ValueError("ftp_url must be ftp://user:pass@host/path")

    url = urlparse(ftp_url)
    host = url.hostname
    user = url.username
    pwd = url.password
    port = url.port or int(2121)

    raw_path = unquote_plus(url.path or "")
    remote_root = posixpath.normpath(raw_path) if raw_path else "/"
    if not remote_root.startswith("/"):
        remote_root = "/" + remote_root

    # Passive/Active tries omitted here for brevity; keep compatibility if used elsewhere
    ftp = _create_ftp(host, user, pwd, port=port, passive=True)
    try:
        ftp.cwd(remote_root)
    except Exception:
        pass
    return ftp, remote_root

# ------------------ ZIP helper for local folder ------------------
def _zip_folder(folder_path, zip_path):
    """
    Create a compressed ZIP (DEFLATED) archive of folder_path.
    Returns the absolute path of the created archive.
    """
    try:
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    full_path = os.path.join(root, file)
                    arcname = os.path.relpath(full_path, folder_path)
                    zf.write(full_path, arcname)
        return zip_path
    except Exception as e:
        print(f"[✘] _zip_folder failed: {e}")
        raise

# ------------------ Backup / Restore ------------------
def ensure_backup_folder(cfg):
    dst = cfg.get("destination_path") or os.path.join("C:\\", "Backup Drive")
    try:
        os.makedirs(dst, exist_ok=True)
    except Exception as e:
        print("[✘] ensure_backup_folder:", e)

def backup_drive():
    """
    Manual backup entrypoint (UI will call this).
    - ftp:// source (Slave) -> request zip over socket and save locally
    - local source  -> copy to temp, zip
    Returns: absolute ZIP path (string) on success, None on failure.
    """
    cfg = load_config()
    ensure_backup_folder(cfg)

    if not cfg.get("source_path"):
        ftp_url = themed_input_popup(
            "Enter Slave Location",
            "Enter FTP URL for Slave (source)\nExample: ftp://user:pass@192.168.2.200/Backup_Source",
            ""
        )
        if not ftp_url:
            print("[✘] No source provided.")
            return None
        cfg["source_path"] = ftp_url
        save_config(cfg)

    source = cfg["source_path"].strip()
    dst = cfg.get("destination_path") or os.path.join("C:\\", "Backup Drive")
    date_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    zip_filename = os.path.join(dst, f"Backup_{date_str}.zip")

    settings = load_settings()
    apps_slave = settings.get("apps_to_close_slave", [])

    if source.lower().startswith("ftp://"):
        #  direct socket transfer from Slave
        try:
            # ask slave to kill apps first
            send_apps_to_slave_and_wait(apps_slave, source_url=source, timeout=25)
            print("[✔] Slave agent confirmed killed apps (if any).")
        except Exception as e:
            print(f"[✘] Slave agent step failed: {e}")
            return None

        # Resolve host and relative path
        parsed = urlparse(source)
        host = parsed.hostname
        if not host:
            print("[✘] Could not determine Slave host from source URL")
            return None
        # remote_rel is the path part on the slave (strip leading "/")
        remote_rel = (unquote_plus(parsed.path) or "").lstrip("/")

        # Use settings override if provided
        settings_general = load_settings()
        agent_host = settings_general.get("slave_agent_host") or host
        agent_port = int(settings_general.get("slave_agent_port", 7788))

        try:
            res = _request_zip_from_slave(agent_host, agent_port, remote_rel, zip_filename, timeout=3600)
            if res and res.get("status") == "ok":
                print(f"[✔] Direct zip transfer completed: {zip_filename}")
                return zip_filename
            else:
                print("[✘] Direct zip transfer failed:", res)
                # remove incomplete file
                try:
                    if os.path.exists(zip_filename):
                        os.remove(zip_filename)
                except Exception:
                    pass
                return None
        except Exception as e:
            print(f"[✘] Direct zip transfer error: {e}")
            try:
                if os.path.exists(zip_filename):
                    os.remove(zip_filename)
            except Exception:
                pass
            return None

    # Local source backup (unchanged)
    try:
        if not os.path.exists(source):
            print(f"[✘] Local source does not exist: {source}")
            return None

        temp_dir = tempfile.mkdtemp(prefix=f"backup_{date_str}_")
        if os.path.isfile(source):
            os.makedirs(temp_dir, exist_ok=True)
            shutil.copy2(source, os.path.join(temp_dir, os.path.basename(source)))
        else:
            base_name = os.path.basename(os.path.normpath(source))
            dest_sub = os.path.join(temp_dir, base_name)
            shutil.copytree(source, dest_sub)

        try:
            archive = _zip_folder(temp_dir, zip_filename)
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"[✔] Local backup completed: {archive}")
            return archive
        except Exception as e:
            print(f"[✘] Zipping local copy failed: {e}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None
    except Exception as e:
        print(f"[✘] Local backup error: {e}")
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass
        return None

def restore_backup():
    """
    Restore:
    - For ftp:// destination (Slave) -> stream local ZIP to Slave via socket (Option B).
    - Local destination -> unpack archive locally.
    Returns True on success, False on failure.
    """
    cfg = load_config()
    if not cfg.get("source_path"):
        ftp_url = themed_input_popup(
            "Enter Restore Destination",
            "Enter FTP URL (Slave) or local folder path to restore files into.\n"
            "Examples:\nftp://user:pass@192.168.2.200/Backup_Source\nC:\\\\SlaveSource\\\\Folder",
            cfg.get("source_path","")
        )
        if not ftp_url:
            print("[✘] No destination provided.")
            return False
        cfg["source_path"] = ftp_url
        save_config(cfg)

    # pick zip
    root = tk.Tk(); root.withdraw()
    zip_file_path = filedialog.askopenfilename(
        title="Select backup ZIP to restore",
        filetypes=[("ZIP files","*.zip")]
    )
    root.destroy()
    if not zip_file_path or not os.path.exists(zip_file_path):
        print("[✘] Backup file not found.")
        return False

    dest = cfg["source_path"].strip()
    settings = load_settings()
    apps_slave = settings.get("apps_to_close_slave", [])

    if dest.lower().startswith("ftp://"):
        # New Option B: instruct slave to receive ZIP stream and unpack it into its root + relative path
        try:
            # kill apps first
            send_apps_to_slave_and_wait(apps_slave, source_url=dest, timeout=25)
            print("[✔] Slave agent confirmed killed apps for restore (if any).")
        except Exception as e:
            print(f"[✘] Slave agent step failed before restore: {e}")
            return False

        parsed = urlparse(dest)
        host = parsed.hostname
        if not host:
            print("[✘] Could not determine Slave host from destination URL")
            return False
        remote_rel = (unquote_plus(parsed.path) or "").lstrip("/")

        agent_host = settings.get("slave_agent_host") or host
        agent_port = int(settings.get("slave_agent_port", 7788))

        try:
            res = _stream_zip_to_slave(agent_host, agent_port, zip_file_path, remote_rel, timeout=3600)
            if isinstance(res, dict) and res.get("status") in ("ok","success"):
                print("[✔] Restore streamed and completed on Slave.")
                return True
            else:
                print("[✘] Restore streaming response:", res)
                return False
        except Exception as e:
            print(f"[✘] Direct restore error: {e}")
            return False

    # Local restore (unchanged)
    target_path = dest
    if not target_path:
        target_path = themed_input_popup(
            "Enter local restore destination",
            "Enter local path to restore files into (e.g. C:\\SlaveSource\\Folder)",
            ""
        )
        if not target_path:
            print("[✘] Restore cancelled: no local target provided.")
            return False
        cfg["source_path"] = target_path
        save_config(cfg)

    try:
        os.makedirs(target_path, exist_ok=True)
    except Exception as e:
        print(f"[✘] Could not create target directory {target_path}: {e}")
        return False

    try:
        shutil.unpack_archive(zip_file_path, target_path)
        print(f"[✔] Local restore completed: {target_path}")
        return True
    except Exception as e:
        print(f"[✘] Local restore failed: {e}")
        return False

# ------------------ Scheduling (with Slave confirmation) ------------------
def _dt_now():
    return datetime.datetime.now()

def _host_schedule_prompt(remind_minutes: int):
    play_alert_sound()
    root = tk.Tk(); root.withdraw()
    try:
        ans = messagebox.askyesnocancel(
            "Backup",
            "Do you want to backup now?\nYes = Start Backup\nNo = Skip\nCancel = Remind Later"
        )
    finally:
        root.destroy()
    if ans is True:
        return "ok"
    if ans is False:
        return "cancel"
    return "remind"

def _remove_exact_schedule(entry):
    cfg2 = load_config()
    cfg2["schedules"] = [
        s for s in cfg2.get("schedules", [])
        if not (s.get("day") == entry.get("day") and s.get("time") == entry.get("time"))
    ]
    save_config(cfg2)

def apply_schedules():
    cfg = load_config()
    for sch in cfg.get("schedules", []):
        day = sch.get("day", "")
        time_str = sch.get("time", "00:00").strip()
        remind_minutes = int(sch.get("remind_later_minutes", 10))
        try:
            scheduled_dt = datetime.datetime.strptime(f"{day} {time_str}", "%Y-%m-%d %H:%M")
        except Exception:
            print(f"[⚠] Invalid schedule entry: {sch}")
            continue
        delay = (scheduled_dt - _dt_now()).total_seconds()
        if delay <= 0:
            print(f"[⚠] Skipping past schedule: {scheduled_dt}")
            continue

        def make_job(rem=remind_minutes, entry=sch):
            def job():
                try:
                    cfg_now = load_config()
                    source = (cfg_now.get("source_path") or "").strip()

                    if source.lower().startswith("ftp://"):
                        # Slave-side CTk popup
                        resp = show_slave_backup_prompt(remind_minutes=rem, source_url=source, timeout=45)
                        choice = resp.get("choice", "ok")
                        if choice == "remind":
                            t2 = threading.Timer(rem * 60, job)
                            t2.daemon = True
                            t2.start()
                            return
                        if choice == "cancel":
                            _remove_exact_schedule(entry)
                            return
                        zpath = backup_drive()
                        if zpath:
                            rt = tk.Tk(); rt.withdraw()
                            try:
                                messagebox.showinfo("Scheduled Backup Completed", f"Backup created at:\n{zpath}")
                            except Exception:
                                print("Scheduled Backup Completed:", zpath)
                            rt.destroy()
                            _remove_exact_schedule(entry)
                        else:
                            rt = tk.Tk(); rt.withdraw()
                            try:
                                messagebox.showwarning("Scheduled Backup", "Backup did not complete successfully or was aborted.")
                            except Exception:
                                print("Scheduled Backup did not complete or was aborted.")
                            rt.destroy()
                        return

                    # Local Host backup -> Host popup
                    choice = _host_schedule_prompt(rem)
                    if choice == "remind":
                        t2 = threading.Timer(rem * 60, job)
                        t2.daemon = True
                        t2.start()
                        return
                    if choice == "cancel":
                        _remove_exact_schedule(entry)
                        return
                    # ok
                    zpath = backup_drive()
                    if zpath:
                        rt = tk.Tk(); rt.withdraw()
                        try:
                            messagebox.showinfo("Scheduled Backup Completed", f"Backup created at:\n{zpath}")
                        except Exception:
                            print("Scheduled Backup Completed:", zpath)
                        rt.destroy()
                        _remove_exact_schedule(entry)
                    else:
                        rt = tk.Tk(); rt.withdraw()
                        try:
                            messagebox.showwarning("Scheduled Backup", "Backup did not complete successfully or was aborted.")
                        except Exception:
                            print("Scheduled Backup did not complete or was aborted.")
                        rt.destroy()
                except Exception as e:
                    print("[✘] Scheduled job error:", e)
            return job

        t = threading.Timer(delay, make_job())
        t.daemon = True
        t.start()
        print(f"[📅] Scheduled backup at {scheduled_dt} (in {int(delay)}s)")

def schedule_reloader():
    last_modified = None
    while True:
        try:
            if os.path.exists(SCHEDULE_FILE):
                current = os.path.getmtime(SCHEDULE_FILE)
                if current != last_modified:
                    last_modified = current
                    print("[🔄] Reloading schedule.json")
                    apply_schedules()
        except Exception as e:
            print("[⚠] schedule_reloader:", e)
        time.sleep(5)

def launch_background():
    apply_schedules()
    t = threading.Thread(target=schedule_reloader, daemon=True)
    t.start()
    print("[🟢] Backend scheduler started (launch_background)")

# ------------------ If run as script ------------------
if __name__ == "__main__":
    launch_background()
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("[ℹ] Backend stopped by user.")
