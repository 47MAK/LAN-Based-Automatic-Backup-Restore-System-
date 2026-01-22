
import os
import sys
import json
import socket
import threading
import traceback
import time
import logging
from logging.handlers import RotatingFileHandler
import stat
import datetime

try:
    import tkinter as tk
    import customtkinter as ctk
except Exception:
    tk = None
    ctk = None
import psutil
import winsound
import tempfile
import zipfile
import shutil

# Try import for embedded FTP server (left intact if available; not modified here)
try:
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer
    from pyftpdlib.authorizers import DummyAuthorizer
    PYFTPD_AVAILABLE = True
except Exception:
    PYFTPD_AVAILABLE = False


# ----------------------------- Base dir / settings paths -----------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(__file__)

SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")                 # (optional legacy host-side view)
SLAVE_SETTINGS_FILE = os.path.join(BASE_DIR, "slave_settings.json")     # per-slave JSON (this file)
LOG_FILE = os.path.join(BASE_DIR, "slave_agent.log")


# ----------------------------- Logging setup -----------------------------
logger = logging.getLogger("slave_agent")
logger.setLevel(logging.DEBUG)
handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
formatter = logging.Formatter("%(asctime)s  %(levelname)-6s  %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)
logger.info("Slave_agent starting up")


# ----------------------------- Defaults for slave_settings.json -----------------------------
DEFAULTS = {
    "ftp_user": "ftpuser",
    "ftp_pass": "ftppass",
    "ftp_root": "C://Backup Source",
    "ftp_permissions": "rw",
    "ftp_port": 2121,
    "ftp_passive_ports": "50000-51000",
    "json_port": 7788,        # JSON Agent (control channel) default port
    "auto_create_root": False # If True, create ftp_root folder on startup if missing
}


# ----------------------------- Runtime state & locks -----------------------------
state_lock = threading.Lock()
state = {
    "ftp_user": DEFAULTS["ftp_user"],
    "ftp_pass": DEFAULTS["ftp_pass"],
    "ftp_root": DEFAULTS["ftp_root"],
    "ftp_permissions": DEFAULTS["ftp_permissions"],
    "ftp_port": DEFAULTS["ftp_port"],
    "ftp_passive_ports": DEFAULTS["ftp_passive_ports"],
    "json_port": DEFAULTS["json_port"],
    "auto_create_root": DEFAULTS["auto_create_root"],
    "last_loaded": 0.0,           # mtime snapshot of slave_settings.json
}

# For clean agent port changes without process restart
AGENT_RESTART_EVENT = threading.Event()

# FTP server runtime objects (kept intact for compatibility)
ftp_server = None
ftp_thread = None
ftp_server_stop_event = threading.Event()


# ===== START: JSON setup logic =====
# (Created and loaded BEFORE main functions. Ensures first-run defaults and immediate apply.)

def _ensure_first_run_json():
    """
    If slave_settings.json doesn't exist, create it with DEFAULTS.
    This JSON is placed next to Slave_agent.py (BASE_DIR).
    """
    if not os.path.exists(SLAVE_SETTINGS_FILE):
        try:
            with open(SLAVE_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULTS, f, indent=2)
            logger.info("Created first-run default %s", SLAVE_SETTINGS_FILE)
        except Exception as e:
            logger.exception("Failed to create default slave_settings.json: %s", e)

def _load_slave_settings_unchecked():
    """Read JSON (may be partial), overlay with defaults, and return dict."""
    try:
        if os.path.exists(SLAVE_SETTINGS_FILE):
            with open(SLAVE_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        else:
            data = {}
    except Exception as e:
        logger.exception("Failed reading slave_settings.json: %s", e)
        data = {}
    # Overlay defaults to guarantee all keys exist
    for k, v in DEFAULTS.items():
        data.setdefault(k, v)
    return data

def _apply_settings_to_state(data: dict):
    """Copy keys into runtime state under lock and perform side-effects (like creating ftp_root)."""
    with state_lock:
        for k in (
            "ftp_user", "ftp_pass", "ftp_root", "ftp_permissions",
            "ftp_port", "ftp_passive_ports", "json_port", "auto_create_root"
        ):
            if k in data:
                state[k] = data[k]
        # record mtime snapshot
        try:
            state["last_loaded"] = os.path.getmtime(SLAVE_SETTINGS_FILE)
        except Exception:
            state["last_loaded"] = time.time()

    # Side-effect: auto-create ftp_root if requested
    try:
        if data.get("auto_create_root"):
            os.makedirs(data.get("ftp_root", DEFAULTS["ftp_root"]), exist_ok=True)
    except Exception as e:
        logger.exception("Failed to ensure ftp_root directory: %s", e)

# First-run guarantee + immediate load/apply
_ensure_first_run_json()
_initial = _load_slave_settings_unchecked()
_apply_settings_to_state(_initial)
logger.info("Startup settings applied: ftp_root='%s', json_port=%s",
            state.get("ftp_root"), state.get("json_port"))

# ===== END: JSON setup logic =====


def kill_apps(apps):
    killed = []
    if not apps:
        return killed
    targets = set(a.lower() for a in apps if isinstance(a, str))
    for proc in psutil.process_iter(['name']):
        try:
            pname = (proc.info.get('name') or "").lower()
            if pname and pname in targets:
                try:
                    proc.kill()
                    killed.append(proc.info.get('name') or pname)
                    logger.info("Killed process: %s", proc.info.get('name'))
                except Exception:
                    logger.exception("Failed to kill process %s", proc.info.get('name'))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # de-dup
    unique = []
    for n in killed:
        if n not in unique:
            unique.append(n)
    return unique


def _play_sound_if_enabled(local_sound_enabled: bool, local_sound_file: str):
    try:
        if local_sound_enabled:
            if local_sound_file and os.path.exists(local_sound_file):
                winsound.PlaySound(local_sound_file, winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        logger.exception("play sound failed")


def _ctk_confirm_dialog(remind_minutes: int, sound_enabled_payload: bool = True, sound_file_payload: str = "") -> dict:
    if tk is None or ctk is None:
        logger.info("No GUI libs available; defaulting confirm dialog to OK")
        return {"status": "ok", "choice": "ok", "remind_minutes": int(remind_minutes or 5)}

    st_local = {}
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                st_local = json.load(f) or {}
    except Exception:
        st_local = {}
    effective_sound_enabled = sound_enabled_payload if sound_enabled_payload is not None else st_local.get("sound_enabled", True)
    effective_sound_file = sound_file_payload or st_local.get("sound_file", "")

    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    root.withdraw()
    top = ctk.CTkToplevel(root)
    top.title("Backup Confirmation")
    top.geometry("520x260")
    top.resizable(False, False)
    try:
        top.attributes("-topmost", True)
    except Exception:
        pass
    top.grab_set()
    top.configure(fg_color="#F5F5F7")

    _play_sound_if_enabled(effective_sound_enabled, effective_sound_file)

    title = ctk.CTkLabel(top, text="Backup Confirmation", font=("Segoe UI Semibold", 16), text_color="black")
    title.pack(pady=(14, 6))

    msg = "A scheduled backup is about to start.\nDo you want to proceed?"
    ctk.CTkLabel(top, text=msg, font=("Segoe UI", 13), text_color="black", wraplength=470, justify="center").pack(padx=16, pady=(0, 8))

    countdown_var = tk.StringVar(value="Auto-OK in 30s")
    ctk.CTkLabel(top, textvariable=countdown_var, font=("Segoe UI", 12), text_color="gray").pack(pady=(0, 10))

    btn_frame = ctk.CTkFrame(top, fg_color="#F5F5F7")
    btn_frame.pack(pady=(6, 12))

    result = {"status": "ok", "choice": "ok", "remind_minutes": int(remind_minutes or 5)}
    done = {"flag": False}

    def finish(choice):
        if done["flag"]:
            return
        done["flag"] = True
        result["choice"] = choice
        try:
            top.destroy()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    ok_btn = ctk.CTkButton(btn_frame, text="OK", width=110, corner_radius=18,
                           fg_color="#1E2A44", hover_color="#233155",
                           command=lambda: finish("ok"))
    ok_btn.pack(side="left", padx=8)

    cancel_btn = ctk.CTkButton(btn_frame, text="Cancel", width=110, corner_radius=18,
                               fg_color="#1E2A44", hover_color="#233155",
                               command=lambda: finish("cancel"))
    cancel_btn.pack(side="left", padx=8)

    remind_btn = ctk.CTkButton(btn_frame, text="Remind Later", width=140, corner_radius=18,
                               fg_color="#1E2A44", hover_color="#233155",
                               command=lambda: finish("remind"))
    remind_btn.pack(side="left", padx=8)

    seconds = 30
    def tick():
        nonlocal seconds
        if done["flag"]:
            return
        seconds -= 1
        if seconds <= 0:
            finish("ok")
            return
        countdown_var.set(f"Auto-OK in {seconds}s")
        top.after(1000, tick)

    top.after(1000, tick)
    try:
        root.mainloop()
    except Exception:
        result["choice"] = "ok"
    return result


# ===== START: Timeout adjustment (raised to 3600s) =====
def _read_initial_recv(conn):
    """
    Read first chunk, split header/body on b"\\n\\n".
    Timeout extended to 3600s to avoid premature cutoff on slow/large jobs.
    """
    conn.settimeout(3600)  
    try:
        data = conn.recv(4096)
    except Exception:
        raise
    if not data:
        return None, b""
    if b"\n\n" in data:
        head, rest = data.split(b"\n\n", 1)
        return head.decode("utf-8", errors="replace"), rest
    else:
        return data.decode("utf-8", errors="replace"), b""
# ===== END: Timeout adjustment (raised to 3600s) =====


def handle_client(conn, addr):
    """
    JSON control handler:
    - Parse small header JSON
    - Commands: kill_apps, show_backup_prompt, send_zip, restore_stream
    - Keep socket alive during binary transfers
    """
    try:
        head_txt, remainder = _read_initial_recv(conn)
        if not head_txt:
            resp = {"status":"error", "error":"no data received"}
            try:
                conn.sendall((json.dumps(resp) + "\n\n").encode("utf-8"))
            except Exception:
                pass
            return

        try:
            msg = json.loads(head_txt)
        except Exception:
            resp = {"status":"error", "error":"invalid json payload"}
            try:
                conn.sendall((json.dumps(resp) + "\n\n").encode("utf-8"))
            except Exception:
                pass
            return

        cmd = msg.get("command")
        logger.info("JSON command received from %s: %s", addr, cmd)

        if cmd == "kill_apps":
            apps = msg.get("apps", [])
            killed = kill_apps(apps)
            resp = {"status":"ok", "killed": killed}
            conn.sendall((json.dumps(resp) + "\n\n").encode("utf-8"))
            return

        elif cmd == "show_backup_prompt":
            remind_minutes = int(msg.get("remind_minutes", 5))
            snd_enabled = bool(msg.get("sound_enabled", True))
            snd_file = msg.get("sound_file", "")
            result = _ctk_confirm_dialog(remind_minutes, snd_enabled, snd_file)
            conn.sendall((json.dumps(result) + "\n\n").encode("utf-8"))
            return

        elif cmd == "send_zip":
            # Host requests Slave to zip a (relative) path and stream ZIP back.
            remote_rel = msg.get("remote_rel", "").lstrip("/")
            with state_lock:
                ftp_root = state.get("ftp_root", DEFAULTS["ftp_root"])
            # Resolve target folder on Slave
            if remote_rel:
                target_path = os.path.normpath(os.path.join(ftp_root, remote_rel))
            else:
                target_path = ftp_root

            # Ensure target exists
            if not os.path.exists(target_path):
                resp = {"status":"error", "error": f"Source path not found on Slave: {target_path}"}
                conn.sendall((json.dumps(resp) + "\n\n").encode("utf-8"))
                return

            # Create temp ZIP
            try:
                tmp_dir = tempfile.mkdtemp(prefix="slave_zip_")
                zip_basename = f"slave_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
                tmp_zip_path = os.path.join(tmp_dir, zip_basename)
                base_name = os.path.splitext(tmp_zip_path)[0]
                shutil.make_archive(base_name, 'zip', root_dir=target_path)
                tmp_zip_path = base_name + ".zip"
                zip_size = os.path.getsize(tmp_zip_path)
            except Exception as e:
                logger.exception("Failed to create temp zip: %s", e)
                resp = {"status":"error", "error": f"zip failed: {e}"}
                conn.sendall((json.dumps(resp) + "\n\n").encode("utf-8"))
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass
                return

            # Send header with zip metadata
            header = {"status":"ok", "zip_name": os.path.basename(tmp_zip_path), "zip_size": zip_size}
            try:
                conn.settimeout(3600)  # <-- ensure long transfer window
                conn.sendall((json.dumps(header) + "\n\n").encode("utf-8"))
            except Exception as e:
                logger.exception("Failed to send zip header: %s", e)
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass
                return

            # Stream zip bytes
            try:
                with open(tmp_zip_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        conn.sendall(chunk)
                # Wait for Host ack header (single recv)
                try:
                    conn.settimeout(3600)  # <-- allow slow host ack
                    ack_raw = conn.recv(4096)
                    if ack_raw and b"\n\n" in ack_raw:
                        ack_txt = ack_raw.split(b"\n\n",1)[0].decode("utf-8", errors="replace")
                    elif ack_raw:
                        ack_txt = ack_raw.decode("utf-8", errors="replace")
                    else:
                        ack_txt = ""
                    try:
                        ack = json.loads(ack_txt) if ack_txt else {}
                    except Exception:
                        ack = {}
                    if ack.get("status") == "received":
                        logger.info("Host acknowledged receipt of zip %s", tmp_zip_path)
                    else:
                        logger.warning("No proper ack from host, ack=%s", ack)
                except Exception:
                    logger.exception("Error waiting for host ack")
            except Exception as e:
                logger.exception("Error streaming zip to Host: %s", e)
            finally:
                # Remove temp zip and directory
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass
                # Send final JSON result if possible
                try:
                    conn.settimeout(5)
                    final = {"status": "ok", "info": "zip_sent"}
                    conn.sendall((json.dumps(final) + "\n\n").encode("utf-8"))
                except Exception:
                    pass
            return

        elif cmd == "restore_stream":
            # Host streams ZIP bytes after initial header; unpack into target dir.
            target_rel = msg.get("target_rel", "").lstrip("/")
            zip_name = msg.get("zip_name", "restore.zip")
            zip_size = int(msg.get("zip_size", 0))
            with state_lock:
                ftp_root = state.get("ftp_root", DEFAULTS["ftp_root"])
            if target_rel:
                target_dir = os.path.normpath(os.path.join(ftp_root, target_rel))
            else:
                target_dir = ftp_root

            # Ensure target dir exists (or create)
            try:
                if not os.path.exists(target_dir):
                    os.makedirs(target_dir, exist_ok=True)
            except Exception as e:
                logger.exception("Failed ensuring target dir: %s", e)
                conn.sendall((json.dumps({"status":"error", "error": str(e)}) + "\n\n").encode("utf-8"))
                return

            # Reply ready header
            try:
                conn.settimeout(3600)  # <-- allow host to begin streaming
                conn.sendall((json.dumps({"status":"ready"}) + "\n\n").encode("utf-8"))
            except Exception as e:
                logger.exception("Failed to send ready header: %s", e)
                return

            # Receive bytes until EOF (Host will close connection)
            tmp_dir = tempfile.mkdtemp(prefix="slave_restore_")
            tmp_zip_path = os.path.join(tmp_dir, zip_name)
            try:
                first_body = remainder if remainder else b""
                received = 0
                with open(tmp_zip_path, "wb") as f:
                    if first_body:
                        f.write(first_body)
                        received += len(first_body)
                    while True:
                        conn.settimeout(3600)  # <-- long receive window
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        received += len(chunk)
                logger.info("Received %d bytes for restore (expected ~%d)", received, zip_size)
                # Attempt to unpack
                try:
                    shutil.unpack_archive(tmp_zip_path, target_dir)
                    conn.settimeout(30)
                    conn.sendall((json.dumps({"status":"ok", "detail":"restore_complete", "target": target_dir}) + "\n\n").encode("utf-8"))
                except Exception as e:
                    logger.exception("Restore unpack failed: %s", e)
                    conn.settimeout(30)
                    conn.sendall((json.dumps({"status":"error", "error": str(e)}) + "\n\n").encode("utf-8"))
            except Exception as e:
                logger.exception("Error receiving restore stream: %s", e)
                try:
                    conn.settimeout(30)
                    conn.sendall((json.dumps({"status":"error", "error": str(e)}) + "\n\n").encode("utf-8"))
                except Exception:
                    pass
            finally:
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass
            return

        else:
            resp = {"status":"error", "error": f"unknown command: {cmd}"}
            conn.sendall((json.dumps(resp) + "\n\n").encode("utf-8"))
            return

    except Exception as e:
        logger.exception("Error handling client %s: %s", addr, e)
        try:
            conn.settimeout(5)
            conn.sendall((json.dumps({"status":"error", "error": str(e)}) + "\n\n").encode("utf-8"))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def json_agent_loop(host="0.0.0.0"):
    """
    JSON Agent main listener.
    - Binds to state['json_port'].
    - Accept loop checks for port changes and restarts gracefully if settings changed.
    """
    while True:
        with state_lock:
            port = int(state.get("json_port", DEFAULTS["json_port"]))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.listen(5)
            s.settimeout(2.0)  # short accept timeout to allow checking for port change / restart
            logger.info("JSON agent listening on %s:%d", host, port)
            while True:
                # If settings changed to a new port, restart listener
                with state_lock:
                    current_port = int(state.get("json_port", DEFAULTS["json_port"]))
                if current_port != port or AGENT_RESTART_EVENT.is_set():
                    logger.info("Detected json_port change (%d -> %d). Restarting JSON agent.", port, current_port)
                    AGENT_RESTART_EVENT.clear()
                    break
                try:
                    conn, addr = s.accept()
                except socket.timeout:
                    continue
                except OSError:
                    # Socket closed due to restart
                    break
                threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
        except Exception:
            logger.exception("JSON agent error; restarting loop")
            try:
                s.close()
            except Exception:
                pass
            time.sleep(1)
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
        # Loop restarts and re-reads state for new port


# ===== START: Reload loop  =====
def settings_reload_loop():
    """
    Watches slave_settings.json and hot-applies changes.
    Sleep interval is 10 seconds (fast reload).
    If json_port changes, signal the JSON agent loop to restart cleanly.
    """
    last_mtime = None
    while True:
        try:
            time.sleep(10)  
            if not os.path.exists(SLAVE_SETTINGS_FILE):
                # If file was removed, recreate defaults
                _ensure_first_run_json()
            mtime = os.path.getmtime(SLAVE_SETTINGS_FILE)
            if last_mtime is None:
                last_mtime = mtime

            if mtime != last_mtime:
                logger.info("Detected change in %s; reloading settings.", SLAVE_SETTINGS_FILE)
                new_data = _load_slave_settings_unchecked()

                with state_lock:
                    old_port = int(state.get("json_port", DEFAULTS["json_port"]))
                _apply_settings_to_state(new_data)
                with state_lock:
                    new_port = int(state.get("json_port", DEFAULTS["json_port"]))

                # If port changed, trigger agent restart
                if new_port != old_port:
                    AGENT_RESTART_EVENT.set()

                last_mtime = mtime
        except Exception as e:
            logger.exception("settings_reload_loop error: %s", e)
            # Keep watching even after errors



# ===== END: =====


# ----------------------------- (Optional) FTP server helpers - unchanged -----------------------------
# NOTE: If you relied on the embedded FTP server previously, this section remains unchanged.
# No behavior changes are introduced here to preserve existing features.

def _start_ftp_server_if_configured():
    if not PYFTPD_AVAILABLE:
        return
    try:
        with state_lock:
            ftp_root = state.get("ftp_root", DEFAULTS["ftp_root"])
            ftp_user = state.get("ftp_user", DEFAULTS["ftp_user"])
            ftp_pass = state.get("ftp_pass", DEFAULTS["ftp_pass"])
            ftp_port = int(state.get("ftp_port", DEFAULTS["ftp_port"]))
            ftp_perms = state.get("ftp_permissions", DEFAULTS["ftp_permissions"])
            passive_ports = state.get("ftp_passive_ports", DEFAULTS["ftp_passive_ports"])

        ports = None
        if isinstance(passive_ports, str) and "-" in passive_ports:
            a, b = passive_ports.split("-", 1)
            ports = range(int(a.strip()), int(b.strip()) + 1)

        authorizer = DummyAuthorizer()
        authorizer.add_user(ftp_user, ftp_pass, ftp_root, perm=ftp_perms)

        class MyHandler(FTPHandler):
            pass

        MyHandler.authorizer = authorizer
        if ports:
            MyHandler.passive_ports = ports

        address = ("0.0.0.0", ftp_port)
        server = FTPServer(address, MyHandler)
        logger.info("FTP server listening on %s:%d, root=%s", "0.0.0.0", ftp_port, ftp_root)

        def _serve():
            try:
                server.serve_forever(timeout=1, blocking=True, handle_exit=True)
            except Exception:
                logger.exception("FTP server stopped")

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        return server, t
    except Exception:
        logger.exception("Failed to start FTP server")
        return None, None


# ----------------------------- Main entry -----------------------------
if __name__ == "__main__":
    # Optional: start FTP if applicable (unchanged behavior)
    if PYFTPD_AVAILABLE:
        ftp_server, ftp_thread = _start_ftp_server_if_configured()

    # Start JSON agent listener thread
    t_json = threading.Thread(target=json_agent_loop, daemon=True)
    t_json.start()

    # Start settings reload watcher (hourly)
    t_reload = threading.Thread(target=settings_reload_loop, daemon=True)
    t_reload.start()

    # Keep main process alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

