SupportBuddy — Portable Technician Kit
=======================================

A single-kit tech tool for diagnosing and fixing common computer issues
on Linux, macOS, and Windows. No install needed — just copy the kit to a
USB stick and run it on any machine.

WHAT'S IN THIS KIT
------------------
  run.sh          — Linux/macOS launcher (pick the right binary automatically)
  run.bat         — Windows launcher
  run.command     — macOS double-click launcher
  support-buddy-linux    — Linux binary (64-bit)
  support-buddy-mac      — macOS binary (64-bit Intel + Apple Silicon)
  support-buddy-win.exe  — Windows binary (64-bit)

QUICK START
-----------
  Linux/macOS:
    ./run.sh

  Windows:
    Double-click run.bat

  The app opens in your default web browser. Everything runs locally —
  nothing is sent over the network.

ADMINISTRATIVE ACCESS
---------------------
Some cleanup actions need administrator/root access. The launcher will
ask if you want to run as admin. You can also run the binary directly:

  Linux:   sudo ./support-buddy-linux
  macOS:   sudo ./support-buddy-mac
  Windows: Right-click run.bat → "Run as administrator"

Without admin access, the app still works for diagnostics (system info,
printers, network, email config). Cleanup actions that need elevated
privileges will show a warning.

EXPORTING A REPORT
------------------
From the app's web interface, you can export a full diagnostic report
as a JSON file. This is useful for:
  - Keeping records of what you found/fixed
  - Sending findings to a remote colleague
  - Loading the report on another machine

To export: click the menu in the top-right corner → "Export Report"

OFFLINE USE
-----------
The app runs entirely offline. It binds to localhost (127.0.0.1) and
does not make any network connections except:
  - Public IP lookup (api.ipify.org) — only when you check network status
  - DNS check (example.com) — only when you check network status
  - Update check — only when you view the Updates module

All other features work with zero internet connection.

SYSTEM REQUIREMENTS
-------------------
  Linux:    Any 64-bit distribution with CUPS (for printer support)
  macOS:    macOS 10.13 (High Sierra) or later, 64-bit
  Windows:  Windows 10 or later, 64-bit

  Python is NOT required — the binaries are self-contained.

BUILDING FROM SOURCE
--------------------
If you want to rebuild the binaries:

  Linux:   pip install flask psutil pyinstaller
           python -m PyInstaller --onefile --name support-buddy-linux main.py

  macOS:   pip install flask psutil pyinstaller
           python -m PyInstaller --onefile --name support-buddy-mac main.py

  Windows: pip install flask psutil pyinstaller
           python -m PyInstaller --onefile --name support-buddy-win main.py

  The main.py file is all you need — no other source files.

TROUBLESHOOTING
---------------
  "Command not found" / "not executable":
    Linux/macOS: chmod +x support-buddy-linux  (or support-buddy-mac)

  "Permission denied" on cleanup actions:
    Run with sudo (Linux/macOS) or as administrator (Windows).

  App won't start / port already in use:
    The app uses port 58678 by default. If it's taken, use --port:
      ./support-buddy-linux --port 58679

  Browser doesn't open automatically:
    Open http://127.0.0.1:58678 in any web browser manually.

  Printer not detected:
    Make sure CUPS is running (Linux: systemctl status cups).
    On macOS, printers are managed by the system print framework.

VERSION
-------
Current version: 1.0.0
