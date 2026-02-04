                 Backup Application (Direct TCP Socket via LAN)

This is a Backup \& Restore application for LAN (Local Network).

It lets you backup data from client machines (Slaves) to a central Host machine, and later restore it back — all using direct TCP socket transfer with ZIP files.

It also works locally on the Host machine itself:

You can back up folders from the Host to a local destination.

You can restore backups directly into local folders without involving any Slave.

 ######   Project Files

| File                  | What it does                                                                                      |

|  | |

| Backend_backup.py   | Host backend engine (scheduling, backup, restore)                                                 |

| Backup_Monitor.py   | Host-side graphical interface (buttons for Backup, Restore, Add/Edit schedules,apps to be closed) |

| Slave_agent.py      | Slave-side service, zips folders for backup and unpacks zips for restore                          |

| schedule.json       | Host config file for source/destination and scheduled backups                                     |

| settings.json       | Host config file for apps to close, sound alerts, slave agent IP/port                             |

| slave_settings.json | Slave config file for its root folder and port                                                    |

⚙️ Prerequisites

Python 3.9+ (on both Host and Slave)

 Libraries:

 psutil (for process control on Slave)

 Standard: zipfile, shutil, socket, tkinter

 Extra (for UI): customtkinter, tkcalendar, psutil (for process management on Slave)

Firewall Rule (Slave machine)

On every Slave computer, you must open the TCP port used by the JSON Agent.

Default port: 7788 (used if no port is written in both settings.json of Host and slave_settings.json of Slave).

Custom port: You can choose any available TCP port (e.g., 9000, 10001, etc.) — but the Host and Slave must both use the same value.

On Slave: set json_port in slave_settings.json

On Host: set slave_agent_port in settings.json

Example (default 7788): CMD Commdand

:: Allow JSON Agent (control channel,you can choose any TCP port other than 7788)

netsh advfirewall firewall add rule name="Slave Agent 7788" dir=in action=allow protocol=TCP localport=7788

:: Verify the rule

netsh advfirewall firewall show rule name="Slave Agent 7788"

🌐 Ports \& Networking

Control channel (JSON agent):

 Default TCP port 7788.
 This is the important port — open it in the Slave firewall.

Port matching:

 If slave_settings.json (on Slave) and settings.json (on Host) specify different JSON ports, connections will fail.

Both sides must agree on the same port:

 Host checks source_path and settings.json → slave_agent_port.

 Slave listens on slave_settings.json → json_port.

If they don’t match:

 Adjust slave_settings.json → json_port, or

 Adjust settings.json → slave_agent_port, or

 Use the ftp:// source but override Host settings with slave_agent_host and slave_agent_port.

IP addresses:

 Run ipconfig on the Slave to see its IP (e.g., 192.168.1.45).

In the Host UI Source field, use:

 ftp://192.168.1.45/Backup_Source

 The ftp:// prefix is just a marker for “remote,” not real FTP.

 The Source input in the Host UI must always contain the correct Slave IP in ftp://<SlaveIP>/Sub directory

Host IP:

 Usually doesn’t matter for firewall rules.

 Just ensure Host can reach Slave on the LAN (same network, or routable subnet).

Ping check:

 From Host, run:

   ping 192.168.1.45

 If ping works → Host can reach Slave’s IP.

If ping fails → check:

 Network cable/Wi-Fi
 Firewall blocking ICMP
 Subnet mismatch
 

🖥 Host Computer (Control Machine)

Place Backend_backup.py and Backup_Monitor.py together in one folder.

Run Backup_Monitor.py to open the Backup Monitor UI.

Run Backend_backup.py to so that it works in background

 ###### JSON Files (Configs)
 
###### 📌 Host side

1. schedule.json (auto-created by UI if missing)
1. 
Example:

{

  "source_path": "ftp://192.168.2.200/Backup_Source",

  "destination_path": "C:\\\\Backup Drive",

  "schedules": \[

    { "day": "2025-09-05", "time": "14:00", "remind_later_minutes": 10 }

  ]

}

2. settings.json (auto-created by backend if missing)

Example:

{

  "apps_to_close_slave": \["sqlservr.exe", "excel.exe"],

  "slave_agent_host": "192.168.2.200",

  "slave_agent_port": 7788,

  "sound_enabled": true

}

 apps_to_close_slave: process names (exact names from Task Manager → Details tab, e.g., sqlservr.exe).

 slave_agent_host: Slave IP (optional to write or keep it blank).

 slave_agent_port: JSON Agent port (must match Slave).

 sound_enabled: alert sounds before scheduled backups.

Configure in the UI:

 Source:

Local example: C:\\Users\\Admin\\Documents

Remote example: ftp://192.168.2.200/Backup_Source

(This IP must be the Slave computer’s IP address from ipconfig.)

 Destination: pick a folder on Host where backups are saved.

 Add schedules (date/time) for automatic backups.

 Apps to be closed:

In settings.json → apps_to_close_slave, list processes by their exact name from Task Manager → Details tab.

Example: "sqlservr.exe", "excel.exe".

The Slave will close these apps before backup from slave

 Trigger Backup Now or Restore manually at any time
 

💻 Slave Computer (Client Machine)

Place Slave_agent.py in a folder on the client machine.

📑 JSON Files (Configs)

📌 Slave side

1. slave_settings.json (auto-created on first run of Slave agent)
1. 
Example:

{

  "ftp_root": "C://Backup Source",

  "json_port": 7788,

  "auto_create_root": true

}

 ftp_root: base folder exposed to Host.

 json_port: port Slave listens on.

 auto_create_root: if true, creates missing root folder automatically.

By default, Slave exposes C:\\Backup Source as the root folder.

Edit slave_settings.json if you want a different root folder or port.

Keep Slave_agent.py running — it listens for commands from Host.

📦 How Backup \& Restore Works

🔹 Scheduled Backup

 Host checks schedule.json for jobs.

 At scheduled time, Host asks Slave for confirmation.

 If confirmed, Slave zips the folder and streams it to Host.

 Host saves it as Backup_YYYY-MM-DD_HH-MM-SS.zip.

🔹 Backup Now

 Immediate backup request from Host UI.

 Slave zips and streams to Host.

🔹 Restore (Host → Slave)

 Host streams a .zip backup file to Slave.

 Slave unpacks it into the target folder.

🔹 Restore Locally (Host only)

 Host extracts .zip to a chosen local folder.

🚫 What’s Not Needed

 FTP logic (_ftp_connect, _create_ftp) → not used anymore.

 No real FTP server required.

 Only ftp:// as a marker in paths (UI input) — not an actual FTP connection.

