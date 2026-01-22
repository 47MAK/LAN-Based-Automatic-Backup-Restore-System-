# Backup_Monitor.py
import json
import os
import sys
import threading
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from tkcalendar import Calendar
import importlib.util

# ------------------- Paths & Config -------------------
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(__file__)

SCHEDULE_FILE = os.path.join(BASE_DIR, "schedule.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

def _ensure_defaults(cfg: dict) -> dict:
    cfg.setdefault("source_path", "")
    cfg.setdefault("destination_path", os.path.join("C:\\", "Backup Drive"))
    cfg.setdefault("schedules", [])
    return cfg

def load_config() -> dict:
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                return _ensure_defaults(json.load(f))
        except Exception:
            pass
    return _ensure_defaults({})

def save_config(cfg: dict):
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(_ensure_defaults(cfg), f, indent=4)

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
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
    data.setdefault("source_history", [])
    data.setdefault("slave_agent_host", "")
    data.setdefault("slave_agent_port", 7788)
    data.setdefault("sound_enabled", True)
    data.setdefault("sound_file", "")
    return data

def save_settings(st: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=4)


# ------------------- Backend Import -------------------
import Backend_backup as backend

# ensure backend reads same files
backend.SCHEDULE_FILE = SCHEDULE_FILE
backend.SETTINGS_FILE = SETTINGS_FILE

# ------------------- UI Setup -------------------
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

app = ctk.CTk()
app.title("Backup Monitor")
app.geometry("1120x680")
app.minsize(980, 620)

# Colors/theme constants (kept identical)
BTN_FG = "#1E2A44"
BTN_FG_HOVER = "#233155"
BG_LIGHT = "#FFFFFF"
BG_FAINT = "#F5F5F7"
TEXT_PRIMARY = "black"

# ------------------- Top Bar -------------------
topbar = ctk.CTkFrame(app, fg_color=BG_FAINT)
topbar.pack(fill="x", side="top")

title_lbl = ctk.CTkLabel(topbar, text="Backup Monitor", font=("Segoe UI Semibold", 18))
title_lbl.pack(side="left", padx=12, pady=8)

# Safe background-run wrapper that checks backend return value
def _run_and_notify(func, success_title="Done", success_message="Operation completed successfully."):
    """
    Runs func() in a background thread.
    On success (truthy return), shows success popup. On false/None or exception -> error popup.

    Change: if the function returns a string path (ZIP path), show it in the message.
    """
    def worker():
        try:
            res = func()
            if res:
                def ok_cb():
                    try:
                        # If it's a path-like string, show the exact path as requested
                        if isinstance(res, str):
                            messagebox.showinfo("Backup Completed", f"Backup created at:\n{res}")
                        else:
                            messagebox.showinfo(success_title, success_message)
                    except Exception:
                        print(success_message if not isinstance(res, str) else f"Backup created at:\n{res}")
                app.after(0, ok_cb)
            else:
                def fail_cb():
                    try:
                        messagebox.showerror("Failed", "Operation did not complete successfully.")
                    except Exception:
                        print("Operation did not complete successfully.")
                app.after(0, fail_cb)
        except Exception as exc:
            err_msg = str(exc)
            def err_cb():
                try:
                    messagebox.showerror("Error", err_msg)
                except Exception:
                    print("Error:", err_msg)
            app.after(0, err_cb)
    threading.Thread(target=worker, daemon=True).start()

def trigger_backup_now():
    persist_fields()
    _run_and_notify(lambda: backend.backup_drive(), success_title="Backup Completed", success_message="Backup completed successfully.")

def trigger_restore_now():
    persist_fields()
    _run_and_notify(lambda: backend.restore_backup(), success_title="Restore Completed", success_message="Restore completed successfully.")

btn_grp = ctk.CTkFrame(topbar, fg_color="transparent")
btn_grp.pack(side="right", padx=12, pady=6)

backup_btn = ctk.CTkButton(
    btn_grp, text="Backup Now", command=trigger_backup_now,
    fg_color=BTN_FG, hover_color=BTN_FG_HOVER, corner_radius=18, width=110
)
backup_btn.pack(side="left", padx=(0, 8))

restore_btn = ctk.CTkButton(
    btn_grp, text="Restore", command=trigger_restore_now,
    fg_color=BTN_FG, hover_color=BTN_FG_HOVER, corner_radius=18, width=110
)
restore_btn.pack(side="left")


# ------------------- Main Scrollable Content -------------------
main = ctk.CTkScrollableFrame(app, fg_color=BG_LIGHT)
main.pack(fill="both", expand=True, padx=10, pady=(0, 10))

def make_card(parent, title, compact=False):
    card = ctk.CTkFrame(parent)
    card.pack(fill="x", padx=8, pady=8)
    header = ctk.CTkLabel(card, text=title, font=("Segoe UI Semibold", 13))
    header.pack(anchor="w", padx=12, pady=(10, 4))

    body = ctk.CTkFrame(card, fg_color=BG_LIGHT)
    body.pack(fill="x", padx=8, pady=(0, 10))
    if compact:
        body.configure(height=56)
        body.pack_propagate(False)
    return card, body

# Load config & settings (used to seed UI)
cfg = load_config()
settings = load_settings()


# ------------------- Source (compact) -------------------
src_card, src_body = make_card(main, "Source", compact=True)

src_row = ctk.CTkFrame(src_body, fg_color=BG_LIGHT)
src_row.pack(fill="x", padx=8, pady=6)

src_var = tk.StringVar(value=cfg.get("source_path", ""))

source_history = settings.get("source_history", [])
if not source_history:
    source_history = ["ftp://192.168.2.200"]


src_combo = ctk.CTkComboBox(src_row, values=source_history, variable=src_var, width=620)
src_combo.pack(side="left", padx=(0, 8))

# --- Right-click menus for managing source history (unchanged UX) ---
context_menu = tk.Menu(app, tearoff=0)
context_menu.add_command(label="Delete", command=lambda: None)

def show_context_menu(event):
    values = list(src_combo.cget("values"))
    if not values:
        return
    clicked_value = src_combo.get().strip()
    if not clicked_value or clicked_value not in values:
        return
    def delete_selected():
        nonlocal clicked_value
        if clicked_value in values:
            values.remove(clicked_value)
            src_combo.configure(values=values)
            settings["source_history"] = values
            save_settings(settings)
            if src_var.get() == clicked_value:
                src_var.set("")
                cfg["source_path"] = ""
                save_config(cfg)
    context_menu.entryconfig("Delete", command=delete_selected)
    context_menu.tk_popup(event.x_root, event.y_root)
src_combo.bind("<Button-3>", show_context_menu)

src_menu = tk.Menu(app, tearoff=0)
def show_src_menu(event):
    src_menu.delete(0, "end")
    hist = settings.get("source_history", [])
    if not hist:
        src_menu.add_command(label="(No sources)", state="disabled")
    else:
        for val in hist:
            src_menu.add_command(label=f"Delete: {val}", command=lambda v=val: delete_source(v))
    src_menu.tk_popup(event.x_root, event.y_root)

def delete_source(value):
    hist = settings.get("source_history", [])
    if value in hist:
        hist.remove(value)
        settings["source_history"] = hist
        save_settings(settings)
        src_combo.configure(values=hist)
        if src_var.get() == value:
            src_var.set("")
        messagebox.showinfo("Deleted", f"Source removed:\n{value}")

src_combo.bind("<Button-3>", show_src_menu)

def show_source_menu(event):
    val = src_combo.get().strip()
    if not val:
        return
    menu = tk.Menu(app, tearoff=0)
    menu.add_command(label="Delete", command=lambda: delete_source(val))
    menu.tk_popup(event.x_root, event.y_root)
def delete_source(val):
    hist = settings.get("source_history", [])
    if val in hist:
        hist.remove(val)
        settings["source_history"] = hist
        save_settings(settings)
        src_combo.configure(values=hist)
        if hist:
            src_var.set(hist[0])
        else:
            src_var.set("")
src_combo.bind("<Button-3>", show_source_menu)

def change_source_popup():
    val = themed_input_popup(
        "Change Source",
        "Enter FTP URL (Slave) or local folder path:\nExample: ftp://192.168.2.200",
        src_var.get()
    )
    if val:
        src_var.set(val)
        cfg["source_path"] = val
        save_config(cfg)
        hist = settings.get("source_history", [])
        if val not in hist:
            hist = [val] + hist
            settings["source_history"] = hist[:25]
            save_settings(settings)
            src_combo.configure(values=hist)

src_btn = ctk.CTkButton(
    src_row, text="Change Source", command=change_source_popup,
    fg_color=BTN_FG, hover_color=BTN_FG_HOVER, corner_radius=18, width=150
)
src_btn.pack(side="left")


# ------------------- Destination (compact) -------------------
dst_card, dst_body = make_card(main, "Destination", compact=True)

dst_row = ctk.CTkFrame(dst_body, fg_color=BG_LIGHT)
dst_row.pack(fill="x", padx=8, pady=6)

dst_var = tk.StringVar(value=cfg.get("destination_path", os.path.join("C:\\", "Backup Drive")))
dst_entry = ctk.CTkEntry(dst_row, textvariable=dst_var, width=620)
dst_entry.pack(side="left", padx=(0, 8))

def change_destination():
    folder = filedialog.askdirectory(title="Choose Host Destination Folder")
    if folder:
        dst_var.set(folder)
        cfg["destination_path"] = folder
        save_config(cfg)

dst_btn = ctk.CTkButton(
    dst_row, text="Change Destination", command=change_destination,
    fg_color=BTN_FG, hover_color=BTN_FG_HOVER, corner_radius=18, width=150
)
dst_btn.pack(side="left")


# ------------------- Schedules -------------------
sch_card, sch_body = make_card(main, "Schedules", compact=False)

sch_container = ctk.CTkFrame(sch_body, fg_color=BG_LIGHT)
sch_container.pack(fill="both", expand=True, padx=8, pady=(6, 2))
sch_container.grid_columnconfigure(0, weight=1)
sch_container.grid_rowconfigure(0, weight=1)

sch_list = tk.Listbox(sch_container, activestyle="dotbox", font=("Segoe UI", 12))
sch_list.grid(row=0, column=0, sticky="nsew")
sch_scroll = tk.Scrollbar(sch_container, orient="vertical", command=sch_list.yview)
sch_scroll.grid(row=0, column=1, sticky="ns")
sch_list.configure(yscrollcommand=sch_scroll.set)

def reload_schedule_list():
    cfg2 = load_config()
    sch_list.delete(0, tk.END)
    for s in cfg2.get("schedules", []):
        day = s.get("day")
        tm = s.get("time")
        remind = s.get("remind_later_minutes", 5)
        sch_list.insert(tk.END, f"{day}   {tm}   (Remind: {remind} min)")

reload_schedule_list()

def add_schedule():
    top = ctk.CTkToplevel(app)
    top.title("Add Schedule")
    top.geometry("380x380")
    top.resizable(False, False)
    top.grab_set()

    cal = Calendar(top, selectmode="day", date_pattern="yyyy-mm-dd")
    cal.pack(pady=(10, 6))

    time_var = tk.StringVar(value=datetime.now().strftime("            %H:%M"))
    ctk.CTkLabel(top, text="Time (HH:MM)", font=("Segoe UI", 12)).pack(pady=(6, 4))
    t_entry = ctk.CTkEntry(top, textvariable=time_var, width=140)
    t_entry.pack()

    remind_var = tk.IntVar(value=5)
    ctk.CTkLabel(top, text="Remind Later (minutes)", font=("Segoe UI", 12)).pack(pady=(10, 4))
    remind_spin = ctk.CTkComboBox(
        top, values=[str(i) for i in (1, 5, 10, 15, 30, 60)],
        variable=tk.StringVar(value="5"), width=120
    )
    remind_spin.pack()

    def done():
        day = cal.get_date()
        tm = time_var.get().strip()
        remind = int(remind_spin.get())
        cfg2 = load_config()
        cfg2["schedules"].append({"day": day, "time": tm, "remind_later_minutes": remind})
        save_config(cfg2)
        reload_schedule_list()
        top.destroy()

    ctk.CTkButton(top, text="Add", command=done,
                  fg_color=BTN_FG, hover_color=BTN_FG_HOVER,
                  corner_radius=18, width=120, height=40).pack(pady=12)

def edit_schedule():
    sel = sch_list.curselection()
    if not sel:
        messagebox.showinfo("Edit Schedule", "Please select a schedule to edit.")
        return
    idx = sel[0]
    cfg2 = load_config()
    items = cfg2.get("schedules", [])
    if not (0 <= idx < len(items)):
        return
    current = items[idx]

    top = ctk.CTkToplevel(app)
    top.title("Edit Schedule")
    top.geometry("380x380")
    top.resizable(False, False)
    top.grab_set()

    cal = Calendar(top, selectmode="day", date_pattern="yyyy-mm-dd")
    try:
        y, m, d = map(int, current.get("day", datetime.now().strftime("%Y-%m-%d")).split("-"))
        cal.selection_set(datetime(y, m, d))
    except Exception:
        pass
    cal.pack(pady=(10, 6))

    time_var = tk.StringVar(value=current.get("time", "00:00"))
    ctk.CTkLabel(top, text="Time (HH:MM)", font=("Segoe UI", 12)).pack(pady=(6, 4))
    t_entry = ctk.CTkEntry(top, textvariable=time_var, width=140)
    t_entry.pack()

    remind_var = tk.StringVar(value=str(current.get("remind_later_minutes", 5)))
    ctk.CTkLabel(top, text="Remind Later (minutes)", font=("Segoe UI", 12)).pack(pady=(10, 4))
    remind_spin = ctk.CTkComboBox(
        top, values=[str(i) for i in (1, 5, 10, 15, 30, 60)],
        variable=remind_var, width=120
    )
    remind_spin.pack()

    def save_edit():
        current["day"] = cal.get_date()
        current["time"] = time_var.get().strip()
        current["remind_later_minutes"] = int(remind_spin.get())
        save_config(cfg2)
        reload_schedule_list()
        top.destroy()

    ctk.CTkButton(top, text="Save", command=save_edit,
                  fg_color=BTN_FG, hover_color=BTN_FG_HOVER,
                  corner_radius=18, width=120, height=40).pack(pady=12)

def remove_schedule():
    sel = sch_list.curselection()
    if not sel:
        return
    idx = sel[0]
    cfg2 = load_config()
    items = cfg2.get("schedules", [])
    if 0 <= idx < len(items):
        del items[idx]
        cfg2["schedules"] = items
        save_config(cfg2)
        reload_schedule_list()

btn_row = ctk.CTkFrame(sch_body, fg_color=BG_LIGHT)
btn_row.pack(pady=(2, 10))
ctk.CTkButton(btn_row, text="Add", command=add_schedule,
              fg_color=BTN_FG, hover_color=BTN_FG_HOVER, corner_radius=18, width=110).pack(side="left", padx=10)
ctk.CTkButton(btn_row, text="Edit", command=edit_schedule,
              fg_color=BTN_FG, hover_color=BTN_FG_HOVER, corner_radius=18, width=110).pack(side="left", padx=10)
ctk.CTkButton(btn_row, text="Remove", command=remove_schedule,
              fg_color=BTN_FG, hover_color=BTN_FG_HOVER, corner_radius=18, width=110).pack(side="left", padx=10)

# ------------------- Applications to Close (Slave only) -------------------
apps_card, apps_body = make_card(main, "Applications to Close Before Backup", compact=False)

apps_top = ctk.CTkFrame(apps_body, fg_color=BG_LIGHT)
apps_top.pack(fill="x", padx=8, pady=(6, 4))

apps_search_var = tk.StringVar()
apps_entry = ctk.CTkEntry(
    apps_top, textvariable=apps_search_var, width=600,
    placeholder_text="Type process name that should be killed on Slave (e.g., sqlservr.exe)."
)
apps_entry.pack(side="left", padx=(0, 8), pady=(0, 4))

def add_app_from_entry():
    name = apps_search_var.get().strip()
    if not name:
        return
    existing = list(slave_list.get(0, tk.END))
    if name not in existing:
        slave_list.insert(tk.END, name)
        persist_apps_list(silent=True)
        apps_search_var.set("")

add_btn = ctk.CTkButton(
    apps_top, text="Add", command=add_app_from_entry,
    fg_color=BTN_FG, hover_color=BTN_FG_HOVER, corner_radius=18, width=90
)
add_btn.pack(side="left", padx=(0, 8))

remove_btn = ctk.CTkButton(
    apps_top, text="Remove", command=lambda: remove_selected_app(),
    fg_color=BTN_FG, hover_color=BTN_FG_HOVER, corner_radius=18, width=90
)
remove_btn.pack(side="left", padx=(0, 8))

apps_container = ctk.CTkFrame(apps_body, fg_color=BG_LIGHT)
apps_container.pack(fill="both", expand=True, padx=8, pady=(6, 4))
apps_container.grid_columnconfigure(0, weight=1)
apps_container.grid_rowconfigure(0, weight=1)

slave_list = tk.Listbox(apps_container, height=6, font=("Segoe UI", 12), activestyle="dotbox")
slave_list.grid(row=0, column=0, sticky="nsew")
apps_scroll = tk.Scrollbar(apps_container, command=slave_list.yview)
apps_scroll.grid(row=0, column=1, sticky="ns")
slave_list.configure(yscrollcommand=apps_scroll.set)

# populate from settings
for nm in settings.get("apps_to_close_slave", []):
    slave_list.insert(tk.END, nm)

def persist_apps_list(silent=False):
    arr = list(slave_list.get(0, tk.END))
    st = load_settings()
    st["apps_to_close_slave"] = arr
    save_settings(st)
    if not silent:
        messagebox.showinfo("Saved", "Slave applications list updated.")

def themed_input_popup(title, prompt, default_value=""):
    top = ctk.CTkToplevel(app)
    top.title(title)
    top.geometry("520x220")
    top.resizable(False, False)
    top.grab_set()
    top.configure(fg_color=BG_FAINT)

    ctk.CTkLabel(
        top, text=prompt, font=("Segoe UI", 13),
        text_color=TEXT_PRIMARY, wraplength=470, justify="left"
    ).pack(padx=18, pady=(20, 10), anchor="w")

    sv = tk.StringVar(value=default_value)
    entry = ctk.CTkEntry(top, textvariable=sv, width=470)
    entry.pack(padx=18, pady=(0, 16))
    entry.focus_set()

    def submit():
        top.result = sv.get().strip()
        top.destroy()

    ctk.CTkButton(
        top, text="Save", command=submit,
        fg_color=BTN_FG, hover_color=BTN_FG_HOVER,
        corner_radius=18, height=40, width=140
    ).pack(pady=(0, 12))

    top.wait_window()
    return getattr(top, "result", None)

def remove_selected_app():
    sel = slave_list.curselection()
    if not sel:
        return
    for i in reversed(sel):
        slave_list.delete(i)
    persist_apps_list(silent=True)


# ------------------- Persist helpers -------------------
def persist_fields():
    cfg["source_path"] = src_combo.get().strip()
    cfg["destination_path"] = dst_var.get().strip()
    save_config(cfg)
    val = cfg["source_path"]
    if val:
        hist = settings.get("source_history", [])
        if val not in hist:
            hist = [val] + hist
            settings["source_history"] = hist[:25]
            save_settings(settings)
            src_combo.configure(values=hist)


# ------------------- Smooth scrolling tweaks -------------------
def bind_smooth_mousewheel(widget):
    def _on_mousewheel(event):
        try:
            widget.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"
        except Exception:
            return None
    widget.bind("<MouseWheel>", _on_mousewheel)
    widget.bind("<Button-4>", lambda e: widget.yview_scroll(-1, "units"))
    widget.bind("<Button-5>", lambda e: widget.yview_scroll(1, "units"))

try:
    bind_smooth_mousewheel(sch_list)
    bind_smooth_mousewheel(slave_list)
except Exception:
    pass

# ------------------- Mainloop -------------------
app.mainloop()
