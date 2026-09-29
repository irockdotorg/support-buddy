#!/usr/bin/env python3
"""
SupportBuddy — local tech support app for Linux + Mac.
Runs a local web UI that lets users run diagnostics, clean caches,
check printers, test network, get email setup help, check disk space,
diagnose slow PCs, fix browser issues, check for updates, export reports,
and run quick scans — all offline, all local.
All commands run locally. App binds to localhost only.
"""

import os
import sys
import platform
import subprocess
import shutil
import json
import datetime
import psutil
import re
import shlex
import glob
from functools import wraps
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string, Response

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1MB max request size

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def os_type():
    """Return 'linux', 'mac', or 'other'."""
    system = platform.system()
    if system == "Linux":
        return "linux"
    if system == "Darwin":
        return "mac"
    return "other"


def distro():
    """Best-effort Linux distro name."""
    if os_type() != "linux":
        return None
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return platform.linux_distribution()[0] if hasattr(platform, "linux_distribution") else "Linux"


def package_manager():
    """Detect the Linux package manager."""
    if os_type() != "linux":
        return None
    for pm in ["apt", "dnf", "yum", "pacman", "zypper", "zypper-"]:
        if shutil.which(pm):
            return pm
    return None


def run(cmd, **kwargs):
    """Run a shell command safely. Returns (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            timeout=kwargs.pop("timeout", 30),
            **kwargs,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


def sudo_required():
    """Check if we're root."""
    return os.geteuid() != 0


def human_size(nbytes):
    """Format bytes to human-readable."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(nbytes) < 1024.0:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024.0
    return f"{nbytes:.1f} PB"


def html_template(name):
    """Load an HTML template from the templates directory."""
    path = TEMPLATES_DIR / name
    if path.exists():
        with open(path) as f:
            return f.read()
    # Fallback: try to find it anywhere in BASE_DIR
    for p in BASE_DIR.rglob(name):
        with open(p) as f:
            return f.read()
    raise FileNotFoundError(f"Template {name} not found")


def _du_size(path):
    """Get total size of a directory in bytes."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return 0
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += _du_size(entry.path)
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        return 0
    return total


def _parse_size(value, unit):
    """Parse a size string like '2.3 GB' to bytes."""
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return float(value) * multipliers.get(unit.upper(), 1)


def _parse_size_str(s):
    """Parse something like '2.3G' or '500M'."""
    import re
    m = re.match(r'([\d.]+)\s*([kmgt]?)b?', str(s), re.I)
    if not m:
        return 0
    val, unit = m.group(1), m.group(2).upper()
    multipliers = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return float(val) * multipliers.get(unit, 1)


def shlex_quote(s):
    """Shell-quote a string."""
    import shlex
    return shlex.quote(s)


def _get_dns_working():
    """Check if DNS is resolving — return a simple status string."""
    try:
        import socket
        socket.getaddrinfo("example.com", 80, timeout=3)
        return "DNS is resolving"
    except socket.gaierror:
        return "DNS may not be resolving"
    except Exception:
        return "DNS check inconclusive"


def _largest_files(path, min_size_mb=50, max_count=15):
    """Find files larger than min_size_mb in path."""
    path = os.path.expanduser(path)
    results = []
    try:
        for root, dirs, files in os.walk(path):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    sz = os.path.getsize(fpath)
                    if sz > min_size_mb * 1024 * 1024:
                        results.append({"path": fpath, "size": human_size(sz), "size_bytes": sz})
                except (PermissionError, OSError):
                    continue
    except Exception:
        pass
    results.sort(key=lambda x: -x["size_bytes"] if x.get("size_bytes") else 0)
    return results[:max_count]


def _top_processes(n=8):
    """Return top CPU-consuming processes."""
    try:
        procs = []
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
            try:
                pinfo = proc.info
                procs.append({
                    "pid": pinfo['pid'],
                    "name": pinfo['name'] or "unknown",
                    "cpu": pinfo['cpu_percent'],
                    "mem": pinfo['memory_percent'],
                    "status": pinfo['status']
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda p: p['cpu'] if p['cpu'] is not None else 0, reverse=True)
        return procs[:n]
    except Exception:
        return []


def _startup_items():
    """Find programs that start at login."""
    items = []
    system = os_type()
    if system == "linux":
        autostart = os.path.expanduser("~/.config/autostart/")
        if os.path.exists(autostart):
            for f in glob.glob(os.path.join(autostart, "*.desktop")):
                try:
                    with open(f) as fh:
                        name = None
                        for line in fh:
                            if line.startswith("Name="):
                                name = line.split("=", 1)[1].strip()
                                break
                    items.append({"type": "autostart", "name": name or os.path.basename(f), "path": f})
                except Exception:
                    continue
    elif system == "mac":
        rc, out, _ = run("launchctl list 2>/dev/null | grep -v '^-' | awk '{print $3}' | head -20 || echo ''")
        if rc == 0 and out:
            for name in out.splitlines():
                if name.strip():
                    items.append({"type": "launchd", "name": name.strip()})
    return items


def _pending_updates():
    """Check for pending system updates."""
    system = os_type()
    if system == "linux":
        pm = package_manager()
        if pm == "apt":
            rc, out, _ = run("apt list --upgradable 2>/dev/null | tail -n +2 | wc -l")
            count = int(out) if rc == 0 and out.isdigit() else 0
            rc2, out2, _ = run("apt list --upgradable 2>/dev/null | tail -n +2 | head -5")
            return {"count": count, "details": out2.splitlines() if rc2 == 0 else [], "pm": "apt"}
        elif pm == "dnf":
            rc, out, _ = run("dnf check-update 2>/dev/null | tail -n +3 | wc -l")
            count = int(out) if rc == 0 and out.isdigit() else 0
            return {"count": count, "details": [], "pm": "dnf"}
        elif pm == "pacman":
            rc, out, _ = run("pacman -Qu 2>/dev/null | wc -l")
            count = int(out) if rc == 0 and out.isdigit() else 0
            return {"count": count, "details": [], "pm": "pacman"}
        return {"count": 0, "details": [], "pm": pm}
    elif system == "mac":
        rc, out, _ = run("softwareupdate -l 2>/dev/null | grep -c '^[[:space:]]*[A-Z]' || echo 0")
        count = int(out) if rc == 0 and out.isdigit() else 0
        rc2, out2, _ = run("softwareupdate -l 2>/dev/null | head -10")
        return {"count": count, "details": out2.splitlines() if rc2 == 0 else [], "pm": "softwareupdate"}
    return {"count": 0, "details": [], "pm": None}


def _browser_info():
    """Find installed browsers and which are running."""
    system = os_type()
    browsers = []
    paths_map = {
        "linux": [
            ("/usr/bin/google-chrome", "Google Chrome"),
            ("/usr/bin/chromium", "Chromium"),
            ("/usr/bin/firefox", "Firefox"),
            ("/usr/bin/brave-browser", "Brave"),
            ("/usr/bin/epiphany", "GNOME Web"),
            ("/usr/bin/opera", "Opera"),
        ],
        "mac": [
            ("/Applications/Google Chrome.app", "Google Chrome"),
            ("/Applications/Chromium.app", "Chromium"),
            ("/Applications/Firefox.app", "Firefox"),
            ("/Applications/Brave Browser.app", "Brave"),
            ("/Applications/Safari.app", "Safari"),
            ("/Applications/Opera.app", "Opera"),
            ("/Applications/Microsoft Edge.app", "Microsoft Edge"),
        ],
    }
    for path, name in paths_map.get(system, []):
        if os.path.exists(path):
            browsers.append({"name": name, "path": path, "installed": True})
    running = []
    for proc in psutil.process_iter(['name']):
        try:
            pname = proc.info['name'].lower()
            for b in browsers:
                if b['name'].lower() in pname or pname in b['name'].lower():
                    if b['name'] not in running:
                        running.append(b['name'])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"browsers": browsers, "running": running}


# ---------------------------------------------------------------------------
# Templates (embedded so the app is self-contained)
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SupportBuddy</title>
<style>
  :root {
    --bg: #1a1a2e;
    --surface: #16213e;
    --card: #0f3460;
    --accent: #e94560;
    --text: #eaeaea;
    --muted: #a0a0b0;
    --good: #4ecca3;
    --warn: #f0a500;
    --bad: #e94560;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.6;
  }
  .container { max-width: 960px; margin: 0 auto; padding: 20px; }
  header {
    text-align: center;
    padding: 30px 0 20px;
    border-bottom: 1px solid #2a2a4a;
    margin-bottom: 30px;
  }
  header h1 {
    font-size: 2.2rem;
    color: var(--accent);
    margin-bottom: 6px;
  }
  header p { color: var(--muted); font-size: 0.95rem; }
  .status-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--surface);
    padding: 10px 16px;
    border-radius: 8px;
    margin-bottom: 24px;
    font-size: 0.85rem;
    flex-wrap: wrap;
    gap: 8px;
  }
  .status-bar .os { color: var(--good); font-weight: 600; }
  .status-bar .warn { color: var(--warn); }
  .status-bar .root-needed { color: var(--bad); }
  .section {
    background: var(--surface);
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 24px;
  }
  .section h2 {
    font-size: 1.2rem;
    margin-bottom: 16px;
    color: #fff;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 14px;
  }
  .card {
    background: var(--card);
    border: 1px solid #2a4a7a;
    border-radius: 10px;
    padding: 18px;
    cursor: pointer;
    transition: all 0.15s;
    text-align: center;
  }
  .card:hover {
    border-color: var(--accent);
    transform: translateY(-2px);
    box-shadow: 0 4px 16px rgba(233,69,96,0.15);
  }
  .card .icon { font-size: 2rem; margin-bottom: 8px; }
  .card .label { font-weight: 600; font-size: 0.95rem; }
  .card .desc { font-size: 0.8rem; color: var(--muted); margin-top: 4px; }
  .card.selected {
    border-color: var(--accent);
    background: rgba(233,69,96,0.1);
  }
  .card.in-progress {
    border-color: var(--warn);
    background: rgba(240,165,0,0.1);
  }
  .card.done {
    border-color: var(--good);
    background: rgba(78,204,163,0.1);
  }
  .card.error {
    border-color: var(--bad);
    background: rgba(233,69,96,0.1);
  }
  .wizard {
    background: var(--surface);
    border-radius: 12px;
    padding: 24px;
    margin-top: 20px;
  }
  .wizard h3 {
    font-size: 1.1rem;
    margin-bottom: 14px;
    color: #fff;
  }
  .step {
    background: var(--card);
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 12px;
    border-left: 3px solid var(--accent);
  }
  .step .q {
    font-weight: 600;
    margin-bottom: 10px;
    font-size: 0.95rem;
  }
  .step .options { display: flex; flex-direction: column; gap: 6px; }
  .step label.option {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 8px 12px;
    background: rgba(255,255,255,0.04);
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.9rem;
  }
  .step label.option:hover { background: rgba(255,255,255,0.08); }
  .step input[type="radio"] { margin-top: 3px; accent-color: var(--accent); }
  .btn-row {
    display: flex;
    gap: 10px;
    margin-top: 16px;
    flex-wrap: wrap;
  }
  .btn {
    padding: 10px 20px;
    border: none;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  .btn:hover { opacity: 0.85; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-secondary { background: var(--card); color: var(--text); border: 1px solid #2a4a7a; }
  .btn-success { background: var(--good); color: #1a1a2e; }
  .btn-danger { background: var(--bad); color: #fff; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .result {
    background: var(--card);
    border-radius: 8px;
    padding: 16px;
    margin-top: 14px;
    white-space: pre-wrap;
    font-family: 'Courier New', monospace;
    font-size: 0.85rem;
    color: var(--muted);
    max-height: 300px;
    overflow-y: auto;
  }
  .result.good { color: var(--good); }
  .result.bad { color: var(--bad); }
  .result.warn { color: var(--warn); }
  .summary-list {
    list-style: none;
    padding: 0;
  }
  .summary-list li {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid #2a2a4a;
    font-size: 0.9rem;
  }
  .summary-list li:last-child { border-bottom: none; }
  .summary-list .item-name { color: var(--text); }
  .summary-list .item-size { color: var(--muted); }
  .toast {
    position: fixed;
    bottom: 20px;
    right: 20px;
    background: var(--card);
    color: var(--text);
    padding: 12px 20px;
    border-radius: 8px;
    border-left: 3px solid var(--good);
    box-shadow: 0 4px 20px rgba(0,0,0,0.4);
    font-size: 0.9rem;
    opacity: 0;
    transform: translateY(20px);
    transition: all 0.3s;
    pointer-events: none;
    max-width: 360px;
    z-index: 100;
  }
  .toast.show { opacity: 1; transform: translateY(0); }
  .toast.error { border-left-color: var(--bad); }
  .toast.warn { border-left-color: var(--warn); }
  .hidden { display: none; }
  .tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    margin-right: 4px;
  }
  .tag-good { background: rgba(78,204,163,0.2); color: var(--good); }
  .tag-warn { background: rgba(240,165,0,0.2); color: var(--warn); }
  .tag-bad { background: rgba(233,69,96,0.2); color: var(--bad); }
  .disk-bar { height: 8px; background: #2a2a4a; border-radius: 4px; overflow: hidden; margin-top: 4px; }
  .disk-bar-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }
  .action-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    background: var(--card);
    color: var(--text);
    border: 1px solid #2a4a7a;
    border-radius: 6px;
    font-size: 0.85rem;
    cursor: pointer;
    margin: 2px;
    transition: all 0.15s;
  }
  .action-btn:hover { border-color: var(--accent); background: rgba(233,69,96,0.1); }
  .action-btn.danger:hover { border-color: var(--bad); background: rgba(233,69,96,0.2); }
  .report-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-top: 8px; }
  .report-table td, .report-table th { padding: 6px 8px; border-bottom: 1px solid #2a2a4a; vertical-align: top; }
  .report-table th { text-align: left; color: var(--muted); font-weight: 600; }
  footer {
    text-align: center;
    padding: 20px;
    color: var(--muted);
    font-size: 0.8rem;
    border-top: 1px solid #2a2a4a;
    margin-top: 30px;
  }
  @media (max-width: 600px) {
    .card-grid { grid-template-columns: 1fr; }
    header h1 { font-size: 1.6rem; }
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>🛠️ SupportBuddy</h1>
    <p>Your computer's friendly neighborhood support tool</p>
  </header>

  <div class="status-bar" id="statusBar">
    <span class="os" id="osInfo">Loading...</span>
    <span id="rootStatus"></span>
    <span id="clock"></span>
  </div>

  <!-- Main dashboard -->
  <div class="section" id="dashboard">
    <h2>What's going on?</h2>
    <div class="card-grid" id="cardGrid">
      <div class="card" data-module="system" onclick="selectModule('system')">
        <div class="icon">💻</div>
        <div class="label">My Computer's Info</div>
        <div class="desc">See what's under the hood — specs, storage, memory</div>
      </div>
      <div class="card" data-module="disk" onclick="selectModule('disk')">
        <div class="icon">💾</div>
        <div class="label">My Disk Is Full</div>
        <div class="desc">Find what's taking up space and clear it out</div>
      </div>
      <div class="card" data-module="cleanup" onclick="selectModule('cleanup')">
        <div class="icon">🧹</div>
        <div class="label">Clean Up My Computer</div>
        <div class="desc">Free up space — safe cache and junk cleaning</div>
      </div>
      <div class="card" data-module="slow" onclick="selectModule('slow')">
        <div class="icon">🐢</div>
        <div class="label">My Computer Is Slow</div>
        <div class="desc">Find out what's bogging it down and fix it</div>
      </div>
      <div class="card" data-module="printers" onclick="selectModule('printers')">
        <div class="icon">🖨️</div>
        <div class="label">My Printer Isn't Working</div>
        <div class="desc">Step-by-step printer troubleshooting</div>
      </div>
      <div class="card" data-module="browser" onclick="selectModule('browser')">
        <div class="icon">🌍</div>
        <div class="label">My Web Browser Is Acting Up</div>
        <div class="desc">Can't open it, slow, or something else</div>
      </div>
      <div class="card" data-module="network" onclick="selectModule('network')">
        <div class="icon">🌐</div>
        <div class="label">My Internet Isn't Working</div>
        <div class="desc">Check connection, test speed, basic fixes</div>
      </div>
      <div class="card" data-module="email" onclick="selectModule('email')">
        <div class="icon">📧</div>
        <div class="label">Set Up My Email</div>
        <div class="desc">Get Gmail, Outlook, or other email working</div>
      </div>
      <div class="card" data-module="updates" onclick="selectModule('updates')">
        <div class="icon">🔄</div>
        <div class="label">Check for Updates</div>
        <div class="desc">See if your system has updates waiting</div>
      </div>
      <div class="card" data-module="quickscan" onclick="selectModule('quickscan')">
        <div class="icon">⚡</div>
        <div class="label">Quick Diagnostic Scan</div>
        <div class="desc">Run a full check of everything in one click</div>
      </div>
      <div class="card" data-module="report" onclick="selectModule('report')">
        <div class="icon">📋</div>
        <div class="label">Export / Import Report</div>
        <div class="desc">Save or load a full diagnostic report</div>
      </div>
    </div>
    <div class="btn-row" style="margin-top:20px;">
      <button class="btn btn-secondary" onclick="resetAll()">Start Over</button>
    </div>
  </div>

  <!-- Module results area -->
  <div id="moduleResults" class="hidden">
    <div class="section" id="moduleSection"></div>
  </div>

  <footer>
    SupportBuddy runs entirely on your computer. Nothing is sent anywhere.<br>
    Made for folks who just want their tech to work.
  </footer>
</div>

<div class="toast" id="toast"></div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let currentModule = null;
let wizardState = {};   // { module, step, answers }

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  updateStatusBar();
  setInterval(updateClock, 10000);
});

function updateClock() {
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}

function updateStatusBar() {
  fetch('/api/system')
    .then(r => r.json())
    .then(data => {
      const osSpan = document.getElementById('osInfo');
      osSpan.textContent = data.os + (data.distro ? ' (' + data.distro + ')' : '');
      const rootSpan = document.getElementById('rootStatus');
      if (data.root) {
        rootSpan.className = 'root-needed';
        rootSpan.textContent = '⚠️  Running as administrator';
      } else {
        rootSpan.className = 'warn';
        rootSpan.textContent = 'ⓘ  Some cleanups need administrator access';
      }
    });
}

// ── Module selection ───────────────────────────────────────────────────────
function selectModule(mod) {
  currentModule = mod;
  wizardState = { module: mod, step: 0, answers: {} };
  document.querySelectorAll('.card').forEach(c => c.classList.remove('selected'));
  document.querySelector(`.card[data-module="${mod}"]`).classList.add('selected');
  hideModuleResults();
  loadModule(mod);
}

function resetAll() {
  currentModule = null;
  wizardState = {};
  document.querySelectorAll('.card').forEach(c => c.classList.remove('selected', 'in-progress', 'done', 'error'));
  hideModuleResults();
  updateStatusBar();
}

// ── Module loading ─────────────────────────────────────────────────────────
function hideModuleResults() {
  document.getElementById('moduleResults').classList.add('hidden');
}

function showModuleResults(html) {
  document.getElementById('moduleResults').classList.remove('hidden');
  document.getElementById('moduleSection').innerHTML = html;
}

function setCardState(mod, state) {
  const card = document.querySelector(`.card[data-module="${mod}"]`);
  if (!card) return;
  card.classList.remove('in-progress', 'done', 'error');
  if (state) card.classList.add(state);
}

function showToast(msg, type='good') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (type === 'error' ? 'error' : type === 'warn' ? 'warn' : '');
  setTimeout(() => { t.classList.remove('show'); }, 4000);
}

// ── Dispatch ──────────────────────────────────────────────────────────────
function loadModule(mod) {
  switch(mod) {
    case 'system': loadSystem(); break;
    case 'disk': loadDisk(); break;
    case 'cleanup': loadCleanup(); break;
    case 'slow': loadSlow(); break;
    case 'printers': loadPrinters(); break;
    case 'browser': loadBrowser(); break;
    case 'network': loadNetwork(); break;
    case 'email': loadEmail(); break;
    case 'updates': loadUpdates(); break;
    case 'quickscan': loadQuickScan(); break;
    case 'report': loadReport(); break;
  }
}

// ── System ─────────────────────────────────────────────────────────────────
function loadSystem() {
  setCardState('system', 'in-progress');
  fetch('/api/system')
    .then(r => r.json())
    .then(data => {
      setCardState('system', 'done');
      showModuleResults(`
        <div class="wizard">
          <h3>💻 Your Computer's Info</h3>
          <div class="step">
            <div><strong>Operating System:</strong> ${data.os} ${data.distro ? '· ' + data.distro : ''}</div>
            <div style="margin-top:6px;color:var(--muted)">${data.hostname}</div>
          </div>
          <div class="step">
            <div><strong>CPU:</strong> ${data.cpu}</div>
            <div><strong>Memory:</strong> ${data.memory_total} total · ${data.memory_used} used · ${data.memory_free} free</div>
          </div>
          <div class="step">
            <div><strong>Storage:</strong></div>
            ${data.disks ? data.disks.map(d => `
              <div style="margin-top:4px">${d.mountpoint} — ${d.fstype || 'filesystem'}</div>
              <div style="margin-left:12px;color:var(--muted);font-size:0.85rem">
                ${d.total} total · ${d.used} used · ${d.free} free · ${d.percent}% full
              </div>
              <div class="disk-bar"><div class="disk-bar-fill" style="width:${d.percent}%;background:${d.percent > 85 ? 'var(--bad)' : d.percent > 70 ? 'var(--warn)' : 'var(--good)'}"></div></div>
            `).join('') : '<div>No disk info available</div>'}
          </div>
          <div class="step" style="border-left-color:var(--good)">
            <div><strong>Uptime:</strong> ${data.uptime}</div>
            <div><strong>Logged in users:</strong> ${data.users || '—'}</div>
          </div>
        </div>
      `);
    })
    .catch(() => {
      setCardState('system', 'error');
      showModuleResults(`<div class="wizard"><div class="result bad">Failed to get system info.</div></div>`);
    });
}

// ── Disk Full ───────────────────────────────────────────────────────────────
function loadDisk() {
  setCardState('disk', 'in-progress');
  Promise.all([
    fetch('/api/system').then(r => r.json()),
    fetch('/api/disk/large-files').then(r => r.json()),
    fetch('/api/disk/thresholds').then(r => r.json()),
  ])
  .then(([sys, large, thresh]) => {
    setCardState('disk', 'done');
    let html = `<div class="wizard"><h3>💾 Disk Space — What's Taking Up Room?</h3>
      <p style="color:var(--muted);font-size:0.9rem;margin-bottom:14px">Here's what we found on your drives:</p>
      <div class="step"><strong>Your Drives:</strong>`;
    if (sys.disks && sys.disks.length > 0) {
      html += `<table class="report-table">
        <tr><th>Drive</th><th style="text-align:right">Used</th><th style="text-align:right">Free</th><th style="text-align:right">Full</th></tr>`;
      sys.disks.forEach(d => {
        const color = d.percent > 85 ? 'var(--bad)' : d.percent > 70 ? 'var(--warn)' : 'var(--good)';
        html += `<tr><td>${d.mountpoint}</td>
          <td style="text-align:right">${d.used}</td>
          <td style="text-align:right">${d.free}</td>
          <td style="text-align:right;color:${color}">${d.percent}%</td></tr>`;
      });
      html += `</table>`;
    } else {
      html += `<div style="color:var(--muted)">No disk info available</div>`;
    }
    html += `</div>`;
    if (large.files && large.files.length > 0) {
      html += `<div class="step" style="border-left-color:var(--warn)">
        <strong>Large files (${large.files.length} found over ${large.threshold_mb}MB):</strong>
        <table class="report-table">
          <tr><th>File</th><th style="text-align:right">Size</th><th>Action</th></tr>`;
      large.files.forEach(f => {
        const short = f.path.length > 55 ? '…' + f.path.slice(-52) : f.path;
        html += `<tr>
          <td style="font-family:monospace;font-size:0.8rem;word-break:break-all">${short}</td>
          <td style="text-align:right">${f.size}</td>
          <td><button class="action-btn danger" onclick="deleteFile('${f.path.replace(/'/g,"\\'")}')">🗑️ Delete</button></td>
        </tr>`;
      });
      html += `</table></div>`;
    } else {
      html += `<div class="step">
        <strong>No large files found.</strong>
        <div style="color:var(--muted);font-size:0.85rem;margin-top:6px">
          No files over ${large.threshold_mb}MB found in common locations.
        </div>
      </div>`;
    }
    if (thresh.warnings && thresh.warnings.length > 0) {
      html += `<div class="step" style="border-left-color:var(--bad)">
        <strong>⚠️  Space warnings:</strong>
        <ul style="margin-top:8px;padding-left:20px;font-size:0.9rem">`;
      thresh.warnings.forEach(w => { html += `<li style="margin-bottom:4px">${w}</li>`; });
      html += `</ul>
        <div class="btn-row">
          <button class="btn btn-primary" onclick="resetAll();setTimeout(()=>selectModule('cleanup'),300)">
            Go to Cleanup →
          </button>
        </div>
      </div>`;
    }
    html += `<div class="btn-row" style="margin-top:16px">
      <button class="btn btn-secondary" onclick="resetAll()">Start Over</button>
    </div></div>`;
    showModuleResults(html);
  })
  .catch(() => {
    setCardState('disk', 'error');
    showModuleResults(`<div class="wizard"><div class="result bad">Could not check disk.</div></div>`);
  });
}

function human_size(nbytes) {
  for (let u of ['B','KB','MB','GB','TB']) {
    if (Math.abs(nbytes) < 1024) return nbytes.toFixed(1) + ' ' + u;
    nbytes /= 1024;
  }
  return nbytes.toFixed(1) + ' PB';
}

function deleteFile(path) {
  if (!confirm('Delete this file?\n\n' + path + '\n\nThis cannot be undone.')) return;
  setCardState('disk', 'in-progress');
  fetch('/api/disk/delete-file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: path })
  })
  .then(r => r.json())
  .then(data => {
    setCardState('disk', data.error ? 'error' : 'done');
    showToast(data.error ? 'Delete failed: ' + data.error : 'File deleted!', data.error ? 'error' : 'good');
    loadDisk();
  })
  .catch(() => {
    setCardState('disk', 'error');
    showToast('Delete failed.', 'error');
  });
}

// ── Cleanup ─────────────────────────────────────────────────────────────────
let cleanupItems = [];

function loadCleanup() {
  setCardState('cleanup', 'in-progress');
  fetch('/api/cleanup/summary')
    .then(r => r.json())
    .then(data => {
      setCardState('cleanup', 'done');
      cleanupItems = data.items || [];
      let html = `
        <div class="wizard">
          <h3>🧹 What Can Be Cleaned</h3>
          <p style="color:var(--muted);font-size:0.9rem;margin-bottom:16px">
            These are safe to remove. Nothing important will be deleted.
          </p>
          <ul class="summary-list" id="cleanupList">
            ${cleanupItems.map((item, i) => `
              <li>
                <span>
                  <span class="item-name">${item.label}</span>
                  <span style="color:var(--muted);font-size:0.8rem;margin-left:8px">${item.desc}</span>
                </span>
                <span>
                  <span class="item-size">${item.size || '—'}</span>
                  <input type="checkbox" data-i="${i}" ${item.auto ? 'checked' : ''}
                         onchange="toggleCleanupItem(${i})"
                         style="margin-left:10px;accent-color:var(--accent);width:16px;height:16px">
                </span>
              </li>
            `).join('')}
          </ul>
          <div style="margin-top:8px;font-size:0.85rem;color:var(--muted)">
            ${cleanupItems.filter(i => i.warn).length > 0
              ? '⚠️  Some items are marked — read before checking.'
              : ''}
          </div>
          <div class="btn-row">
            <button class="btn btn-primary" onclick="runCleanup()">Clean Selected Items</button>
            <button class="btn btn-secondary" onclick="resetAll()">Back</button>
          </div>
        </div>
      `;
      showModuleResults(html);
    })
    .catch(() => {
      setCardState('cleanup', 'error');
      showModuleResults(`<div class="wizard"><div class="result bad">Could not scan for cleanable items.</div></div>`);
    });
}

function toggleCleanupItem(i) {
  // just visual — the checkbox is the source of truth
}

function runCleanup() {
  const checked = [];
  document.querySelectorAll('#cleanupList input[type="checkbox"]:checked').forEach(cb => {
    checked.push(parseInt(cb.dataset.i));
  });
  if (checked.length === 0) {
    showToast('Select at least one item to clean.', 'warn');
    return;
  }
  setCardState('cleanup', 'in-progress');
  const btn = document.querySelector('#moduleSection .btn-primary');
  if (btn) btn.disabled = true;

  fetch('/api/cleanup/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ indices: checked })
  })
  .then(r => r.json())
  .then(data => {
    setCardState('cleanup', data.error ? 'error' : 'done');
    let resultHtml = `
      <div class="result ${data.error ? 'bad' : 'good'}">
        ${data.error ? '❌ ' + data.error : '✅ Done! Cleaned ' + (data.cleaned || 0) + ' items'}
      </div>
      ${data.log ? `<div class="result" style="margin-top:8px">${data.log}</div>` : ''}
    `;
    showModuleResults(
      document.getElementById('moduleSection').innerHTML.replace(
        /<div class="btn-row">[\s\S]*<\/div>$/,
        ''
      ) + resultHtml + `
      <div class="btn-row" style="margin-top:16px">
        <button class="btn btn-secondary" onclick="resetAll()">Done</button>
      </div>
    `);
    showToast(data.error ? 'Cleanup had an issue' : 'Cleanup finished!', data.error ? 'error' : 'good');
  })
  .catch(() => {
    setCardState('cleanup', 'error');
    showToast('Cleanup failed.', 'error');
  });
}

// ── Slow Computer ───────────────────────────────────────────────────────────
function loadSlow() {
  setCardState('slow', 'in-progress');
  Promise.all([
    fetch('/api/slow/processes').then(r => r.json()),
    fetch('/api/slow/startup').then(r => r.json()),
    fetch('/api/system').then(r => r.json()),
  ])
  .then(([procs, startup, sys]) => {
    setCardState('slow', 'done');
    const memTotal = parseFloat(sys.memory_total);
    const memUsed = parseFloat(sys.memory_used);
    const memPct = memTotal && memUsed ? (memUsed / memTotal * 100) : 0;
    let html = `<div class="wizard"><h3>🐢 What's Making Your Computer Slow?</h3>
      <p style="color:var(--muted);font-size:0.9rem;margin-bottom:14px">Here's what we found:</p>
      <div class="step" style="border-left-color:${memPct > 80 ? 'var(--bad)' : memPct > 60 ? 'var(--warn)' : 'var(--good)'}">
        <strong>Memory usage:</strong> ${sys.memory_used} used of ${sys.memory_total} (${memPct.toFixed(0)}%)
        <div style="margin-top:6px;color:var(--muted);font-size:0.85rem">
          ${memPct > 80
            ? '⚠️  Very high — this is likely slowing things down. Try closing some apps.'
            : memPct > 60
              ? 'Moderate usage — keep an eye on it.'
              : 'Memory usage looks fine.'}
        </div>
      </div>
      <div class="step" style="border-left-color:var(--accent)">
        <strong>Processes using the most CPU:</strong>`;
    if (procs && procs.length > 0) {
      html += `<table class="report-table">
        <tr><th>Process</th><th style="text-align:right">PID</th><th style="text-align:right">CPU</th><th style="text-align:right">Mem</th></tr>`;
      procs.forEach(p => {
        html += `<tr>
          <td>${p.name}</td>
          <td style="text-align:right;color:var(--muted)">${p.pid}</td>
          <td style="text-align:right;color:${p.cpu > 50 ? 'var(--bad)' : p.cpu > 10 ? 'var(--warn)' : 'var(--muted)'}">
            ${p.cpu != null ? p.cpu.toFixed(1) : '—'}%
          </td>
          <td style="text-align:right;color:${p.mem > 20 ? 'var(--warn)' : 'var(--muted)'}">
            ${p.mem != null ? p.mem.toFixed(1) : '—'}%
          </td>
        </tr>`;
      });
      html += `</table>`;
    } else {
      html += `<div style="color:var(--muted);margin-top:8px">Could not get process info.</div>`;
    }
    html += `</div>`;
    if (startup && startup.items && startup.items.length > 0) {
      html += `<div class="step" style="border-left-color:var(--warn)">
        <strong>Programs that start at login (${startup.items.length} found):</strong>
        <p style="color:var(--muted);font-size:0.85rem;margin-top:6px">
          These run automatically when you sign in. Too many can slow down startup.
        </p>
        <table class="report-table">
          <tr><th>Program</th><th>Type</th><th>Path</th></tr>`;
      startup.items.forEach(item => {
        html += `<tr>
          <td>${item.name || 'Unknown'}</td>
          <td style="color:var(--muted)">${item.type}</td>
          <td style="font-size:0.75rem;color:var(--muted);word-break:break-all">${item.path || '—'}</td>
        </tr>`;
      });
      html += `</table></div>`;
    } else {
      html += `<div class="step"><strong>No startup items found.</strong>
        <div style="color:var(--muted);font-size:0.85rem;margin-top:6px">
          Your computer isn't loading extra programs at login.
        </div>
      </div>`;
    }
    html += `<div class="step" style="border-left-color:var(--good)">
      <strong>Quick things to try:</strong>
      <ol style="margin-top:8px;padding-left:20px;font-size:0.9rem">
        <li>Restart your computer — clears memory and stops stuck processes.</li>
        <li>Close apps you're not using — especially browsers with lots of tabs.</li>
        <li>Check your disk space — a full disk slows everything down.</li>
        <li>If the computer has been on a long time, a restart helps a lot.</li>
      </ol>
    </div>
    <div class="btn-row" style="margin-top:16px">
      <button class="btn btn-secondary" onclick="resetAll()">Start Over</button>
    </div></div>`;
    showModuleResults(html);
  })
  .catch(() => {
    setCardState('slow', 'error');
    showModuleResults(`<div class="wizard"><div class="result bad">Could not check slow computer.</div></div>`);
  });
}

// ── Printers ────────────────────────────────────────────────────────────────
let printerStep = 0;
let printerAnswers = {};

function loadPrinters() {
  setCardState('printers', 'in-progress');
  printerStep = 0;
  printerAnswers = {};
  setCardState('printers', 'done');
  showPrintersWizard();
}

function showPrintersWizard() {
  const steps = [
    {
      q: 'Let\'s figure out your printer issue. First — is your printer turned on and connected?',
      options: [
        { value: 'on_connected', label: 'Yes, it\'s on and connected (USB, Wi-Fi, or network)' },
        { value: 'on_not_connected', label: 'It\'s on but I\'m not sure if it\'s connected properly' },
        { value: 'off', label: 'It\'s off, or I\'m not sure if it\'s plugged in' },
      ]
    },
    {
      q: 'On your computer, can you see the printer in the printer settings?',
      condition: (a) => a.step1 === 'on_connected' || a.step1 === 'on_not_connected',
      options: [
        { value: 'yes', label: 'Yes, I see it listed' },
        { value: 'no', label: 'No, I don\'t see it anywhere' },
        { value: 'not_sure', label: 'I\'m not sure how to check' },
      ]
    },
    {
      q: 'When you try to print, what happens?',
      options: [
        { value: 'nothing', label: 'Nothing happens at all' },
        { value: 'stuck', label: 'It says "printing" but nothing comes out / it gets stuck' },
        { value: 'error', label: 'I get an error message' },
        { value: 'bad_quality', label: 'It prints but the quality is bad / wrong colors' },
      ]
    },
    {
      q: 'What kind of connection is your printer set up with?',
      options: [
        { value: 'usb', label: 'USB cable directly to the computer' },
        { value: 'wifi', label: 'Wi-Fi (wireless)' },
        { value: 'network', label: 'Network cable (Ethernet) to a router' },
        { value: 'not_sure', label: 'I\'m not sure' },
      ]
    }
  ];

  let html = `<div class="wizard"><h3>🖨️ Printer Helper</h3>`;
  let currentStep = steps[printerStep];
  let canContinue = true;

  // Check condition
  if (currentStep.condition && !currentStep.condition(printerAnswers)) {
    printerStep++;
    if (printerStep >= steps.length) {
      showPrintersResults();
      return;
    }
    showPrintersWizard();
    return;
  }

  html += `
    <div class="step">
      <div class="q">${currentStep.q}</div>
      <div class="options">
        ${currentStep.options.map((opt, i) => `
          <label class="option">
            <input type="radio" name="printer_step" value="${opt.value}"
                   ${printerAnswers['step' + printerStep] === opt.value ? 'checked' : ''}
                   onchange="printerAnswers['step${printerStep}']=this.value;updatePrinterButtons()">
            ${opt.label}
          </label>
        `).join('')}
      </div>
    </div>
  `;

  // Show previous answers
  for (let s = 0; s < printerStep; s++) {
    const prev = steps[s];
    const ans = printerAnswers['step' + s];
    if (prev && ans) {
      const label = prev.options.find(o => o.value === ans)?.label || ans;
      html += `<div style="font-size:0.85rem;color:var(--muted);margin-bottom:4px">
        ✓ ${prev.q.substring(0, 60)}… — "${label}"
      </div>`;
    }
  }

  html += `
    <div class="btn-row">
      ${printerStep > 0 ? `<button class="btn btn-secondary" onclick="printerStep--;showPrintersWizard()">← Back</button>` : ''}
      <button class="btn btn-primary" id="printerNext" onclick="goPrinterNext()" disabled>Continue →</button>
      <button class="btn btn-secondary" onclick="resetAll()">Cancel</button>
    </div>
  `;

  html += `</div>`;
  showModuleResults(html);
}

function updatePrinterButtons() {
  const btn = document.getElementById('printerNext');
  if (!btn) return;
  const checked = document.querySelector('input[name="printer_step"]:checked');
  btn.disabled = !checked;
}

function goPrinterNext() {
  const checked = document.querySelector('input[name="printer_step"]:checked');
  if (!checked) return;
  printerStep++;
  if (printerStep >= 4) {
    showPrintersResults();
  } else {
    showPrintersWizard();
  }
}

function showPrintersResults() {
  setCardState('printers', 'in-progress');
  // Run actual printer diagnostics
  Promise.all([
    fetch('/api/printers').then(r => r.json()),
    fetch('/api/printers/details').then(r => r.json()),
    fetch('/api/printers/actions').then(r => r.json()),
  ])
  .then(([list, details, actions]) => {
    setCardState('printers', 'done');
    let html = `
      <div class="wizard">
        <h3>🖨️ Printer Diagnostics & Actions</h3>
        <p style="color:var(--muted);font-size:0.9rem;margin-bottom:12px">
          Based on what you told me and what we found:
        </p>
    `;

    // Summary of their answers
    html += `<div class="step" style="border-left-color:var(--accent);font-size:0.9rem">`;
    html += `<strong>Your situation:</strong><br>`;
    if (printerAnswers.step0) html += `• Printer status: ${printerAnswers.step0.replace('_',' ')}<br>`;
    if (printerAnswers.step1) html += `• Visible in settings: ${printerAnswers.step1}<br>`;
    if (printerAnswers.step2) html += `• Problem: ${printerAnswers.step2}<br>`;
    if (printerAnswers.step3) html += `• Connection: ${printerAnswers.step3}<br>`;
    html += `</div>`;

    // System printer info
    if (list.printers && list.printers.length > 0) {
      html += `<div class="step" style="border-left-color:var(--good)">
        <strong>Printers found on your system:</strong>
        <ul style="margin-top:8px;padding-left:20px">
          ${list.printers.map(p => `<li style="font-size:0.9rem">${p}</li>`).join('')}
        </ul>
      </div>`;
    } else {
      html += `<div class="step" style="border-left-color:var(--warn)">
        <strong>No printers found on your system.</strong>
        <div style="margin-top:6px;color:var(--muted);font-size:0.85rem">
          This usually means the printer isn't connected or configured yet.
        </div>
      </div>`;
    }

    // Actions
    if (actions.available && actions.available.length > 0) {
      html += `<div class="step" style="border-left-color:var(--accent)">
        <strong>Actions you can try:</strong>
        <div style="margin-top:10px">`;
      actions.available.forEach(a => {
        html += `<button class="action-btn ${a.danger ? 'danger' : ''}"
          onclick="runPrinterAction('${a.cmd.replace(/'/g,"\\'")}')"
          ${a.warn ? 'onclick="if(!confirm(\''+a.warn.replace(/'/g,"\\'")+'\'))return"' : ''}>
          ${a.icon} ${a.label}
        </button>`;
      });
      html += `</div>
        <div style="margin-top:8px;color:var(--muted);font-size:0.85rem">
          ${actions.note || ''}
        </div>
      </div>`;
    }

    // Recommendations based on their answers
    html += `<div class="step" style="border-left-color:var(--accent)">
      <strong>What to try:</strong>
      <ol style="margin-top:8px;padding-left:20px;font-size:0.9rem">`;

    const recs = [];
    if (printerAnswers.step0 === 'off') {
      recs.push('Turn the printer on and make sure it\'s plugged in or connected to Wi-Fi.');
    }
    if (printerAnswers.step0 === 'on_not_connected') {
      recs.push('Check the printer\'s display (if it has one) for any error messages or connection status.');
      recs.push('If it\'s a Wi-Fi printer, make sure it\'s on the same Wi-Fi network as your computer.');
    }
    if (printerAnswers.step1 === 'no') {
      recs.push('Add the printer: go to your system\'s printer settings and click "Add Printer" or the + button.');
      recs.push('If it\'s a USB printer, try a different USB port or cable.');
    }
    if (printerAnswers.step1 === 'not_sure') {
      recs.push('Here\'s how to check:');
      if (osType === 'mac') {
        recs.push('→ Click the Apple menu → System Settings → Printers & Scanners. Your printers should be listed there.');
      } else {
        recs.push('→ Open Settings → Printers (or search for "Printers" in your settings). Your printers should be listed there.');
      }
    }
    if (printerAnswers.step2 === 'stuck') {
      recs.push('Clear the print queue: in printer settings, find your printer, look for "Print Queue" or "Jobs", and cancel all pending jobs.');
      recs.push('Restart the printer (turn it off, wait 10 seconds, turn it back on).');
    }
    if (printerAnswers.step2 === 'error') {
      recs.push('Take a picture of the error message if you can — that helps narrow it down.');
      recs.push('Try removing the printer and adding it again.');
    }
    if (printerAnswers.step2 === 'bad_quality') {
      recs.push('Check if the printer needs ink/toner. Most printers show this on their display or through a status app.');
      recs.push('Run a head cleaning cycle — your printer\'s software or display should have an option for this.');
    }
    if (printerAnswers.step3 === 'usb') {
      recs.push('Try a different USB port on your computer.');
      recs.push('If it\'s a USB hub, plug directly into the computer instead.');
    }
    if (printerAnswers.step3 === 'wifi') {
      recs.push('Make sure the printer and computer are on the same Wi-Fi network.');
      recs.push('If the printer has a display, check its network settings to confirm it\'s connected.');
    }

    if (recs.length === 0) {
      recs.push('Try restarting your computer and the printer, then try printing again.');
      recs.push('If it still doesn\'t work, you may need to reinstall the printer driver/software from the manufacturer\'s website.');
    }

    recs.forEach(r => { html += `<li style="margin-bottom:4px">${r}</li>`; });
    html += `</ol></div>`;

    // Test print button with the round smiley
    html += `
      <div class="step" style="border-left-color:var(--accent);margin-top:12px">
        <strong>🧪 Test Your Printer:</strong>
        <div style="margin-top:6px;color:var(--muted);font-size:0.85rem">
          Send a test page to your default printer. If it prints, you're good to go.
        </div>
        <div class="btn-row" style="margin-top:8px">
          <button class="btn btn-primary" onclick="runPrinterTest()">🖨️ Print Test Page</button>
        </div>
        <div id="printerTestResult" style="margin-top:10px"></div>
      </div>`;

    // Offer to copy summary
    html += `
      <div class="step" style="border-left-color:var(--good);margin-top:12px">
        <strong>Still stuck?</strong>
        <div style="margin-top:6px;color:var(--muted);font-size:0.85rem">
          You can share what you see here with a support person, or we can connect remotely.
          Click below to copy a summary of your printer situation.
        </div>
        <div class="btn-row" style="margin-top:10px">
          <button class="btn btn-secondary" onclick="copyPrinterSummary()">Copy Summary</button>
          <button class="btn btn-primary" onclick="resetAll()">Start Over</button>
        </div>
      </div>
    `;

    html += `</div>`;
    showModuleResults(html);
  })
  .catch(() => {
    setCardState('printers', 'error');
    showModuleResults(`<div class="wizard"><div class="result bad">Could not run printer diagnostics.</div></div>`);
  });
}

function copyPrinterSummary() {
  const text = document.querySelector('#moduleSection .step strong').parentElement.innerText;
  navigator.clipboard.writeText(text).then(() => showToast('Summary copied!')).catch(() => showToast('Could not copy', 'error'));
}

function runPrinterAction(cmd) {
  setCardState('printers', 'in-progress');
  fetch('/api/printers/run-action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cmd: cmd })
  })
  .then(r => r.json())
  .then(data => {
    setCardState('printers', 'done');
    showToast(data.error ? 'Action failed: ' + data.error : 'Done!', data.error ? 'error' : 'good');
    showPrintersResults();
  })
  .catch(() => {
    setCardState('printers', 'error');
    showToast('Action failed.', 'error');
  });
}

// Round traditional smiley — "have a nice day" style
function runPrinterTest() {
  setCardState('printers', 'in-progress');
  const resultDiv = document.getElementById('printerTestResult');
  resultDiv.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Sending test page...</div>';
  fetch('/api/printers/test', { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      setCardState('printers', 'done');
      if (data.smiley) {
        resultDiv.innerHTML = `
          <div style="text-align:center;margin-top:10px">
            <div style="font-family:monospace;font-size:1.4rem;line-height:1.2;color:var(--good);white-space:pre">◕‿◕</div>
            <div style="color:var(--good);font-weight:600;margin-top:8px">✅ Test page sent! Your printer is working.</div>
            <div style="color:var(--muted);font-size:0.8rem;margin-top:4px">Printer: ${data.printer}</div>
          </div>`;
        showToast('Test page sent! 🎉', 'good');
      } else {
        resultDiv.innerHTML = `
          <div class="result bad" style="margin-top:8px">
            ❌ ${data.error || 'Test print failed'}
          </div>`;
        showToast('Test print failed', 'error');
      }
    })
    .catch(() => {
      setCardState('printers', 'error');
      resultDiv.innerHTML = '<div class="result bad">Something went wrong.</div>';
      showToast('Test failed.', 'error');
    });
}

// ── Network ─────────────────────────────────────────────────────────────────
function loadNetwork() {
  setCardState('network', 'in-progress');
  fetch('/api/network')
    .then(r => r.json())
    .then(data => {
      setCardState('network', data.error ? 'error' : 'done');
      let html = `
        <div class="wizard">
          <h3>🌐 Network Check</h3>
          <p style="color:var(--muted);font-size:0.9rem;margin-bottom:14px">
            Quick check of your internet connection.
          </p>
      `;

      if (data.error) {
        html += `<div class="result bad">${data.error}</div>`;
      } else {
        html += `
          <div class="step" style="border-left-color:${data.connected ? 'var(--good)' : 'var(--bad)'}">
            <strong>Internet connection:</strong> ${data.connected ? '✅ Connected' : '❌ Not connected'}
            <div style="margin-top:6px;color:var(--muted);font-size:0.85rem">
              ${data.connected
                ? 'Your computer can reach the internet.'
                : 'Your computer cannot reach the internet right now.'}
            </div>
          </div>
        `;

        if (data.connected) {
          html += `
            <div class="step">
              <strong>Connection type:</strong> ${data.connection_type || '—'}
            </div>
            <div class="step">
              <strong>Public IP:</strong> ${data.public_ip || '—'}
              <div style="margin-top:4px;color:var(--muted);font-size:0.85rem">
                DNS: ${data.dns || '—'}
              </div>
            </div>
            <div class="step">
              <strong>Network interfaces:</strong>
              <ul style="margin-top:6px;padding-left:20px;font-size:0.85rem">
                ${data.interfaces ? data.interfaces.map(i => `
                  <li>${i.name}: ${i.state === 'up' ? '🟢 Up' : '🔴 Down'} — ${i.ip || 'no IP'}</li>
                `).join('') : '<li>No interface info available</li>'}
              </ul>
            </div>
          `;
        } else {
          html += `
            <div class="step" style="border-left-color:var(--warn)">
              <strong>Try these steps:</strong>
              <ol style="margin-top:8px;padding-left:20px;font-size:0.9rem">
                <li>Check that Wi-Fi is turned on, or your Ethernet cable is plugged in.</li>
                <li>Restart your router/modem (unplug for 10 seconds, plug back in).</li>
                <li>Restart your computer.</li>
                <li>If you're on Wi-Fi, try forgetting the network and reconnecting.</li>
              </ol>
            </div>
          `;
        }

        html += `
          <div class="btn-row" style="margin-top:16px">
            <button class="btn btn-secondary" onclick="resetAll()">Start Over</button>
          </div>
        `;
      }

      html += `</div>`;
      showModuleResults(html);
    })
    .catch(() => {
      setCardState('network', 'error');
      showModuleResults(`<div class="wizard"><div class="result bad">Could not check network.</div></div>`);
    });
}

// ── Email ───────────────────────────────────────────────────────────────────
let emailStep = 0;
let emailAnswers = {};
const emailProviders = [
  { id: 'gmail', label: 'Gmail (Google)', icon: '📬' },
  { id: 'outlook', label: 'Outlook / Hotmail (Microsoft)', icon: '🌐' },
  { id: 'yahoo', label: 'Yahoo Mail', icon: '📧' },
  { id: 'icloud', label: 'iCloud Mail (Apple)', icon: '🍎' },
  { id: 'other', label: 'Other / I don\'t know', icon: '❓' },
];

function loadEmail() {
  setCardState('email', 'in-progress');
  emailStep = 0;
  emailAnswers = {};
  setCardState('email', 'done');
  showEmailWizard();
}

function showEmailWizard() {
  const steps = [
    {
      q: 'Are you trying to set up email in a web browser (like Chrome or Safari), or in an email app (like Thunderbird, Apple Mail, or the Windows Mail app)?',
      options: [
        { value: 'web', label: 'In a web browser — I go to a website to check email' },
        { value: 'app', label: 'In an email app on my computer' },
        { value: 'not_sure', label: 'I\'m not sure what the difference is' },
      ]
    },
    {
      q: 'Which email service do you use?',
      options: emailProviders.map(p => ({ value: p.id, label: p.label }))
    },
    {
      q: 'What\'s the main problem?',
      options: [
        { value: 'never_set_up', label: 'I\'ve never set it up before — I need to start from scratch' },
        { value: 'password', label: 'I forgot my password' },
        { value: 'app_failing', label: 'I set it up but the app keeps saying something\'s wrong' },
        { value: 'can_t_send', label: 'I can receive email but can\'t send' },
        { value: 'other', label: 'Something else' },
      ]
    }
  ];

  let html = `<div class="wizard"><h3>📧 Email Setup Helper</h3>`;
  let currentStep = steps[emailStep];

  if (emailStep >= steps.length) {
    showEmailResults();
    return;
  }

  html += `
    <div class="step">
      <div class="q">${currentStep.q}</div>
      <div class="options">
        ${currentStep.options.map((opt, i) => `
          <label class="option">
            <input type="radio" name="email_step" value="${opt.value}"
                   ${emailAnswers['step' + emailStep] === opt.value ? 'checked' : ''}
                   onchange="emailAnswers['step${emailStep}']=this.value;updateEmailButtons()">
            ${opt.label}
          </label>
        `).join('')}
      </div>
    </div>
  `;

  for (let s = 0; s < emailStep; s++) {
    const prev = steps[s];
    const ans = emailAnswers['step' + s];
    if (prev && ans) {
      const label = prev.options.find(o => o.value === ans)?.label || ans;
      html += `<div style="font-size:0.85rem;color:var(--muted);margin-bottom:4px">
        ✓ ${prev.q.substring(0, 60)}… — "${label}"
      </div>`;
    }
  }

  html += `
    <div class="btn-row">
      ${emailStep > 0 ? `<button class="btn btn-secondary" onclick="emailStep--;showEmailWizard()">← Back</button>` : ''}
      <button class="btn btn-primary" id="emailNext" onclick="goEmailNext()" disabled>Continue →</button>
      <button class="btn btn-secondary" onclick="resetAll()">Cancel</button>
    </div>
  `;
  html += `</div>`;
  showModuleResults(html);
}

function updateEmailButtons() {
  const btn = document.getElementById('emailNext');
  if (!btn) return;
  const checked = document.querySelector('input[name="email_step"]:checked');
  btn.disabled = !checked;
}

function goEmailNext() {
  const checked = document.querySelector('input[name="email_step"]:checked');
  if (!checked) return;
  emailStep++;
  if (emailStep >= 3) {
    showEmailResults();
  } else {
    showEmailWizard();
  }
}

function showEmailResults() {
  setCardState('email', 'in-progress');
  fetch('/api/email/config')
    .then(r => r.json())
    .then(config => {
      setCardState('email', 'done');
      let html = `
        <div class="wizard">
          <h3>📧 Email Setup Guide</h3>
          <p style="color:var(--muted);font-size:0.9rem;margin-bottom:14px">
            Here's what to do based on what you told me:
          </p>
      `;

      // Their situation summary
      html += `<div class="step" style="border-left-color:var(--accent);font-size:0.9rem"><strong>Your situation:</strong><br>`;
      if (emailAnswers.step0) html += `• Setup type: ${emailAnswers.step0.replace('_',' ')}<br>`;
      if (emailAnswers.step1) html += `• Provider: ${emailAnswers.step1}<br>`;
      if (emailAnswers.step2) html += `• Issue: ${emailAnswers.step2.replace('_',' ')}<br>`;
      html += `</div>`;

      // Provider-specific config
      const prov = config.providers?.find(p => p.id === emailAnswers.step1) || config.providers?.find(p => p.id === 'other');
      if (prov) {
        html += `
          <div class="step" style="border-left-color:var(--good)">
            <strong>${prov.display_name || prov.id} — Server Settings:</strong>
            <div class="result" style="margin-top:8px;font-size:0.8rem">
              Incoming (IMAP): ${prov.imap_host || '—'} : ${prov.imap_port || '—'}
              ${prov.imap_secure ? '· Security: ' + prov.imap_secure : ''}
              <br>
              Outgoing (SMTP): ${prov.smtp_host || '—'} : ${prov.smtp_port || '—'}
              ${prov.smtp_secure ? '· Security: ' + prov.smtp_secure : ''}
              <br>
              Username: your full email address
              ${prov.note ? '<br><span style="color:var(--warn)">Note: ' + prov.note + '</span>' : ''}
            </div>
          </div>
        `;
      }

      // Steps based on their issue
      html += `<div class="step" style="border-left-color:var(--accent)"><strong>Steps to try:</strong><ol style="margin-top:8px;padding-left:20px;font-size:0.9rem">`;

      const steps_list = [];
      if (emailAnswers.step0 === 'web') {
        steps_list.push('Open your web browser and go to your email provider\'s website (like gmail.com, outlook.com, or yahoo.com).');
        steps_list.push('Log in with your email address and password. If you forgot your password, look for a "Forgot password?" link on the login page.');
        steps_list.push('Once logged in, you can send and receive email right there in the browser. No app setup needed.');
      } else if (emailAnswers.step0 === 'app') {
        steps_list.push('Open your email app (Thunderbird, Apple Mail, Windows Mail, etc.).');
        steps_list.push('Look for an option to "Add Account" or "New Account" — usually in the File, Edit, or gear menu, or right when you open the app.');
        steps_list.push('If the app supports automatic setup (many do for Gmail, Outlook, Yahoo), just enter your email and password and it should configure itself.');
        steps_list.push('If automatic setup doesn\'t work, you\'ll need to enter the server settings manually. Use the settings shown above.');
        if (emailAnswers.step2 === 'password') {
          steps_list.push('⚠️  First, reset your password: go to your email provider\'s website and use the "Forgot password?" link. You\'ll usually need access to a recovery phone number or alternate email.');
        }
        if (emailAnswers.step2 === 'app_failing') {
          steps_list.push('Remove the account from the app and add it again from scratch.');
          steps_list.push('Double-check the server settings match what\'s shown above — especially the port numbers and security type.');
          steps_list.push('Make sure your app has permission to access the network (some antivirus or firewall software can block it).');
        }
        if (emailAnswers.step2 === 'can_t_send') {
          steps_list.push('This is usually an SMTP (outgoing mail) issue. Check that the SMTP server, port, and security settings are correct.');
          steps_list.push('Some providers require you to enable "less secure apps" or generate an app-specific password. Check your provider\'s account security settings.');
        }
      } else {
        // not_sure
        steps_list.push('If you check your email by going to a website (like gmail.com), you don\'t need to set anything up — just log in to the website.');
        steps_list.push('If you want to use an app, open the app and look for "Add Account". Enter your email and password — most apps figure out the rest automatically for common providers.');
        steps_list.push('If you\'re not sure which email service you use, check any old email you\'ve received — the sender address will show your provider (gmail.com, outlook.com, etc.).');
      }

      if (steps_list.length === 0) {
        steps_list.push('Try restarting the email app and adding the account again.');
        steps_list.push('If it still doesn\'t work, you may need to check your email provider\'s help pages for specific setup instructions.');
      }

      steps_list.forEach(s => { html += `<li style="margin-bottom:4px">${s}</li>`; });
      html += `</ol></div>`;

      // Copy config button
      html += `
        <div class="btn-row" style="margin-top:16px">
          <button class="btn btn-secondary" onclick="copyEmailConfig()">Copy Server Settings</button>
          <button class="btn btn-primary" onclick="resetAll()">Start Over</button>
        </div>
      `;

      html += `</div>`;
      showModuleResults(html);
    })
    .catch(() => {
      setCardState('email', 'error');
      showModuleResults(`<div class="wizard"><div class="result bad">Could not generate email config.</div></div>`);
    });
}

function copyEmailConfig() {
  const prov = emailAnswers.step1;
  fetch('/api/email/config')
    .then(r => r.json())
    .then(config => {
      const p = config.providers?.find(x => x.id === prov);
      if (!p) { showToast('No config found for that provider', 'error'); return; }
      const text = `
${p.display_name || prov} Email Settings
━━━━━━━━━━━━━━━━━━━━━━━
Incoming (IMAP): ${p.imap_host}:${p.imap_port} (${p.imap_secure || '—'})
Outgoing (SMTP): ${p.smtp_host}:${p.smtp_port} (${p.smtp_secure || '—'})
Username: your full email address
${p.note ? 'Note: ' + p.note : ''}
      `.trim();
      navigator.clipboard.writeText(text).then(() => showToast('Settings copied!')).catch(() => showToast('Could not copy', 'error'));
    });
}

// ── Browser ─────────────────────────────────────────────────────────────────
function loadBrowser() {
  setCardState('browser', 'in-progress');
  fetch('/api/browser')
    .then(r => r.json())
    .then(data => {
      setCardState('browser', 'done');
      let html = `
        <div class="wizard">
          <h3>🌍 Web Browser Helper</h3>
          <p style="color:var(--muted);font-size:0.9rem;margin-bottom:14px">
            Here's what we found:
          </p>
      `;
      if (data.browsers && data.browsers.length > 0) {
        html += `<div class="step" style="border-left-color:var(--good)">
          <strong>Browsers installed:</strong>
          <ul>`;
        data.browsers.forEach(b => {
          html += `<li style="font-size:0.9rem">
            ${b.name} — ${data.running.includes(b.name) ? '🟢 Running' : '🔴 Not running'}
            <div style="margin-left:12px;color:var(--muted);font-size:0.8rem">${b.path}</div>
          </li>`;
        });
        html += `</ul></div>`;
      } else {
        html += `<div class="step" style="border-left-color:var(--warn)">
          <strong>No common browsers found.</strong>
          <div style="color:var(--muted);font-size:0.85rem;margin-top:6px">
            We looked for Chrome, Firefox, Safari, Edge, Brave, and Opera.
          </div>
        </div>`;
      }
      if (data.running && data.running.length > 0) {
        html += `<div class="step" style="border-left-color:var(--accent)">
          <strong>Browsers running:</strong>
          <div style="margin-top:8px">`;
        data.running.forEach(b => {
          html += `<button class="action-btn" onclick="closeBrowser('${b.replace(/'/g,"\\'")}')">
            🛑 Close ${b}
          </button>`;
        });
        html += `</div>
          <div style="margin-top:8px;color:var(--muted);font-size:0.85rem">
            Closing a browser closes all windows and tabs. Unsaved work may be lost.
          </div>
        </div>`;
      } else {
        html += `<div class="step">
          <strong>No browsers running.</strong>
          <div style="color:var(--muted);font-size:0.85rem;margin-top:6px">
            If your browser isn't opening:
          </div>
          <ol style="margin-top:8px;padding-left:20px;font-size:0.9rem">
            <li>Restart your computer.</li>
            <li>Check the browser is in your Applications/Programs folder.</li>
            <li>Try opening from terminal/command line to see error messages.</li>
            <li>Check if an update broke it.</li>
          </ol>
        </div>`;
      }
      html += `<div class="step" style="border-left-color:var(--good)">
        <strong>Common fixes:</strong>
        <ol style="margin-top:8px;padding-left:20px;font-size:0.9rem">
          <li>Restart the browser completely.</li>
          <li>Try a different browser for the problem website.</li>
          <li>Clear cache and cookies (in the browser's History or Privacy settings).</li>
          <li>Disable extensions one at a time to find the culprit.</li>
          <li>Make sure the browser is up to date.</li>
        </ol>
      </div>
      <div class="btn-row" style="margin-top:16px">
        <button class="btn btn-secondary" onclick="resetAll()">Start Over</button>
      </div></div>`;
      showModuleResults(html);
    })
    .catch(() => {
      setCardState('browser', 'error');
      showModuleResults(`<div class="wizard"><div class="result bad">Could not check browsers.</div></div>`);
    });
}

function closeBrowser(name) {
  if (!confirm(`Close ${name}? This will close all windows and tabs. Unsaved work may be lost.`)) return;
  fetch('/api/browser/close', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name })
  })
  .then(r => r.json())
  .then(data => {
    showToast(data.error ? 'Could not close: ' + data.error : `Closed ${name}!`, data.error ? 'error' : 'good');
    loadBrowser();
  })
  .catch(() => showToast('Could not close browser.', 'error'));
}

// ── Updates ─────────────────────────────────────────────────────────────────
function loadUpdates() {
  setCardState('updates', 'in-progress');
  fetch('/api/updates')
    .then(r => r.json())
    .then(data => {
      setCardState('updates', 'done');
      let html = `
        <div class="wizard">
          <h3>🔄 Software Updates</h3>
          <p style="color:var(--muted);font-size:0.9rem;margin-bottom:14px">
            Checking for updates on your system:
          </p>
      `;
      if (data.error) {
        html += `<div class="result bad">${data.error}</div>`;
      } else {
        const count = data.count || 0;
        html += `<div class="step" style="border-left-color:${count > 0 ? 'var(--warn)' : 'var(--good)'}">
          <strong>Updates available:</strong> ${count} ${count === 1 ? 'update' : 'updates'}
          <div style="color:var(--muted);font-size:0.85rem;margin-top:6px">
            Package manager: ${data.pm || '—'}
          </div>
        </div>`;
        if (count > 0 && data.details && data.details.length > 0) {
          html += `<div class="step">
            <strong>Update details:</strong>
            <ul style="margin-top:8px;padding-left:20px;font-size:0.85rem">`;
          data.details.forEach(d => { html += `<li style="margin-bottom:2px">${d}</li>`; });
          html += `</ul></div>`;
        }
        html += `<div class="step" style="border-left-color:var(--accent)">
          <strong>What to do:</strong>
          <ol style="margin-top:8px;padding-left:20px;font-size:0.9rem">
            <li>Run your system's update tool to install these updates.</li>
            ${data.pm === 'apt' ? '<li>On Linux: open your package manager or run "sudo apt update && sudo apt upgrade".</li>' : ''}
            ${data.pm === 'softwareupdate' ? '<li>On Mac: Apple menu → System Settings → General → Software Update.</li>' : ''}
            <li>Restart your computer after installing updates if prompted.</li>
          </ol>
        </div>`;
        if (count === 0) {
          html += `<div class="step" style="border-left-color:var(--good)">
            ✅ Your system is up to date! No pending updates.
          </div>`;
        }
      }
      html += `
        <div class="btn-row" style="margin-top:16px">
          <button class="btn btn-secondary" onclick="resetAll()">Start Over</button>
        </div>
      </div>`;
      showModuleResults(html);
    })
    .catch(() => {
      setCardState('updates', 'error');
      showModuleResults(`<div class="wizard"><div class="result bad">Could not check updates.</div></div>`);
    });
}

// ── Quick Scan ──────────────────────────────────────────────────────────────
function loadQuickScan() {
  setCardState('quickscan', 'in-progress');
  showModuleResults(`
    <div class="wizard">
      <h3>⚡ Quick Diagnostic Scan</h3>
      <p style="color:var(--muted);font-size:0.9rem">Running a full check of your system...</p>
      <div class="result" style="margin-top:12px;font-size:0.8rem">Please wait...</div>
    </div>
  `);
  fetch('/api/quick-scan')
    .then(r => r.json())
    .then(data => {
      setCardState('quickscan', 'done');
      let html = `
        <div class="wizard">
          <h3>⚡ Quick Diagnostic Scan — Complete</h3>
          <p style="color:var(--muted);font-size:0.9rem;margin-bottom:14px">
            Scan ran at ${data.scan_time || '—'}. Here's what we found:
          </p>
      `;
      if (data.system && !data.system.error) {
        const s = data.system;
        html += `
          <div class="step" style="border-left-color:var(--good)">
            <strong>💻 System</strong>
            <table class="report-table">
              <tr><td>OS</td><td>${s.os}${s.distro ? ' · ' + s.distro : ''}</td></tr>
              <tr><td>Hostname</td><td>${s.hostname}</td></tr>
              <tr><td>CPU</td><td>${s.cpu}</td></tr>
              <tr><td>Memory</td><td>${s.memory_used} used / ${s.memory_total} total</td></tr>
              <tr><td>Uptime</td><td>${s.uptime}</td></tr>
            </table>
          </div>
        `;
      }
      if (data.system && data.system.disks) {
        html += `
          <div class="step" style="border-left-color:var(--accent)">
            <strong>💾 Storage</strong>
            <table class="report-table">
              <tr><th>Drive</th><th style="text-align:right">Used</th><th style="text-align:right">Free</th><th style="text-align:right">%</th></tr>
        `;
        data.system.disks.forEach(d => {
          const c = d.percent > 85 ? 'var(--bad)' : d.percent > 70 ? 'var(--warn)' : 'var(--good)';
          html += `<tr>
            <td>${d.mountpoint}</td>
            <td style="text-align:right">${d.used}</td>
            <td style="text-align:right">${d.free}</td>
            <td style="text-align:right;color:${c}">${d.percent}%</td>
          </tr>`;
        });
        html += `</table></div>`;
      }
      if (data.cleanup && !data.cleanup.error) {
        html += `
          <div class="step" style="border-left-color:var(--warn)">
            <strong>🧹 Cleanup potential</strong>
            <div style="color:var(--muted);font-size:0.85rem;margin-top:4px">
              ${data.cleanup.total || '0 B'} can be cleaned
            </div>
            <div style="margin-top:6px">
              <button class="btn btn-primary"
                onclick="resetAll();setTimeout(()=>selectModule('cleanup'),300)">
                Go to Cleanup →
              </button>
            </div>
          </div>
        `;
      }
      if (data.printers && !data.printers.error) {
        html += `
          <div class="step" style="border-left-color:var(--good)">
            <strong>🖨️ Printers</strong>
            <div style="color:var(--muted);font-size:0.85rem;margin-top:4px">
              ${data.printers.printers && data.printers.printers.length > 0
                ? data.printers.printers.join(', ')
                : 'No printers found'}
              ${data.printers.cups_running ? ' · CUPS running' : ''}
            </div>
            <div style="margin-top:6px">
              <button class="btn btn-primary"
                onclick="resetAll();setTimeout(()=>selectModule('printers'),300)">
                Fix Printer →
              </button>
            </div>
          </div>
        `;
      }
      if (data.network && !data.network.error) {
        html += `
          <div class="step" style="border-left-color:${data.network.connected ? 'var(--good)' : 'var(--bad)'}">
            <strong>🌐 Network</strong>
            <div style="color:var(--muted);font-size:0.85rem;margin-top:4px">
              ${data.network.connected ? '✅ Connected' : '❌ Not connected'}
            </div>
            <div style="margin-top:6px">
              <button class="btn btn-primary"
                onclick="resetAll();setTimeout(()=>selectModule('network'),300)">
                Check Network →
              </button>
            </div>
          </div>
        `;
      }
      if (data.updates && !data.updates.error) {
        const uc = data.updates.count || 0;
        html += `
          <div class="step" style="border-left-color:${uc > 0 ? 'var(--warn)' : 'var(--good)'}">
            <strong>🔄 Updates</strong>
            <div style="color:var(--muted);font-size:0.85rem;margin-top:4px">
              ${uc} ${uc === 1 ? 'update' : 'updates'} available
            </div>
            <div style="margin-top:6px">
              <button class="btn btn-primary"
                onclick="resetAll();setTimeout(()=>selectModule('updates'),300)">
                Check Updates →
              </button>
            </div>
          </div>
        `;
      }
      html += `
        <div class="btn-row" style="margin-top:16px">
          <button class="btn btn-secondary" onclick="resetAll()">Start Over</button>
        </div>
      </div>
    `;
      showModuleResults(html);
    })
    .catch(() => {
      setCardState('quickscan', 'error');
      showModuleResults(`<div class="wizard"><div class="result bad">Scan failed.</div></div>`);
    });
}

// ── Report ──────────────────────────────────────────────────────────────────
function loadReport() {
  setCardState('report', 'in-progress');
  setCardState('report', 'done');
  showModuleResults(`
    <div class="wizard">
      <h3>📋 Export / Import Diagnostic Report</h3>
      <p style="color:var(--muted);font-size:0.9rem;margin-bottom:14px">
        Save a full diagnostic report to a file, or load a previously saved one.
      </p>
      <div class="step" style="border-left-color:var(--accent)">
        <strong>Export a report:</strong>
        <div style="color:var(--muted);font-size:0.85rem;margin-top:6px">
          Downloads a JSON file with everything we know about this computer —
          system info, disks, cleanup potential, printers, network, updates,
          browsers, processes, and startup items.
        </div>
        <div class="btn-row" style="margin-top:10px">
          <button class="btn btn-primary" onclick="exportReport()">
            📥 Download Report (JSON)
          </button>
        </div>
      </div>
      <div class="step" style="border-left-color:var(--good);margin-top:12px">
        <strong>Import a report:</strong>
        <div style="color:var(--muted);font-size:0.85rem;margin-top:6px">
          Load a report you saved earlier. Useful for viewing findings on another
          machine or sharing with a colleague.
        </div>
        <div class="btn-row" style="margin-top:10px">
          <button class="btn btn-secondary" onclick="document.getElementById('reportFileInput').click()">
            📤 Load Report File
          </button>
          <input type="file" id="reportFileInput" accept=".json" style="display:none"
                 onchange="importReport(this)">
        </div>
      </div>
      <div id="reportResult" style="margin-top:12px"></div>
      <div class="btn-row" style="margin-top:16px">
        <button class="btn btn-secondary" onclick="resetAll()">Start Over</button>
      </div>
    </div>
  `);
}

function exportReport() {
  setCardState('report', 'in-progress');
  const resultDiv = document.getElementById('reportResult');
  resultDiv.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Generating report...</div>';
  fetch('/api/report/export')
    .then(r => {
      if (!r.ok) throw new Error('Export failed');
      return r.blob();
    })
    .then(blob => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'supportbuddy-report.json';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setCardState('report', 'done');
      resultDiv.innerHTML = `
        <div style="color:var(--good);font-weight:600">✅ Report downloaded!</div>
        <div style="color:var(--muted);font-size:0.85rem;margin-top:4px">
          Save this file safely — it contains full system details.
        </div>
      `;
      showToast('Report exported!', 'good');
    })
    .catch(() => {
      setCardState('report', 'error');
      resultDiv.innerHTML = '<div class="result bad">❌ Export failed.</div>';
      showToast('Export failed.', 'error');
    });
}

function importReport(input) {
  const file = input.files[0];
  if (!file) return;
  setCardState('report', 'in-progress');
  const resultDiv = document.getElementById('reportResult');
  resultDiv.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Reading report...</div>';
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const data = JSON.parse(reader.result);
      if (!data.system && !data.os) throw new Error('Not a valid report');
      setCardState('report', 'done');
      let html = `
        <div class="wizard" style="margin-top:12px">
          <h3>📋 Loaded Report</h3>
          <p style="color:var(--muted);font-size:0.85rem">
            Scanned on: ${data.scan_time || 'unknown'} · ${data.os || 'unknown'}
            ${data.distro ? ' · ' + data.distro : ''}
          </p>
      `;
      if (data.system) {
        const s = data.system;
        html += `
          <div class="step" style="border-left-color:var(--good)">
            <strong>System</strong>
            <table class="report-table">
              <tr><td>OS</td><td>${s.os}${s.distro ? ' · ' + s.distro : ''}</td></tr>
              <tr><td>Hostname</td><td>${s.hostname || '—'}</td></tr>
              <tr><td>CPU</td><td>${s.cpu || '—'}</td></tr>
              <tr><td>Memory</td><td>${s.memory_used || '—'} used / ${s.memory_total || '—'} total</td></tr>
            </table>
          </div>
        `;
      }
      if (data.cleanup) {
        html += `
          <div class="step" style="border-left-color:var(--warn)">
            <strong>Cleanup</strong>
            <div style="color:var(--muted);font-size:0.85rem;margin-top:4px">
              ${data.cleanup.total || '0 B'} cleanable
            </div>
          </div>
        `;
      }
      if (data.printers) {
        html += `
          <div class="step" style="border-left-color:var(--good)">
            <strong>Printers</strong>
            <div style="color:var(--muted);font-size:0.85rem;margin-top:4px">
              ${data.printers.printers && data.printers.printers.length > 0
                ? data.printers.printers.join(', ')
                : 'None found'}
            </div>
          </div>
        `;
      }
      if (data.network) {
        html += `
          <div class="step" style="border-left-color:${data.network.connected ? 'var(--good)' : 'var(--bad)'}">
            <strong>Network</strong>
            <div style="color:var(--muted);font-size:0.85rem;margin-top:4px">
              ${data.network.connected ? '✅ Connected' : '❌ Not connected'}
            </div>
          </div>
        `;
      }
      if (data.updates) {
        html += `
          <div class="step" style="border-left-color:${data.updates.count > 0 ? 'var(--warn)' : 'var(--good)'}">
            <strong>Updates</strong>
            <div style="color:var(--muted);font-size:0.85rem;margin-top:4px">
              ${data.updates.count || 0} available
            </div>
          </div>
        `;
      }
      html += `
        <div class="btn-row" style="margin-top:12px">
          <button class="btn btn-secondary" onclick="resetAll()">Start Over</button>
        </div>
      </div>
    `;
      resultDiv.innerHTML = html;
      showToast('Report loaded!', 'good');
    } catch (e) {
      setCardState('report', 'error');
      resultDiv.innerHTML = '<div class="result bad">❌ Not a valid SupportBuddy report file.</div>';
      showToast('Invalid report file.', 'error');
    }
  };
  reader.readAsText(file);
  input.value = '';
}

// Store osType for printer summary
const osType = {{ os_type | tojson }};
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(INDEX_HTML, os_type=os_type())


@app.route("/api/system")
def api_system():
    try:
        mem = psutil.virtual_memory()
        disks = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype or "",
                    "total": human_size(usage.total),
                    "used": human_size(usage.used),
                    "free": human_size(usage.free),
                    "percent": usage.percent,
                })
            except Exception:
                continue

        # Uptime
        try:
            with open("/proc/uptime") as f:
                uptime_secs = float(f.read().split()[0])
                uptime_str = str(datetime.timedelta(seconds=int(uptime_secs)))
        except Exception:
            uptime_str = "—"

        # Users
        users = []
        try:
            import pwd
            users = [u.pw_name for u in psutil.users()]
        except Exception:
            pass

        return jsonify({
            "os": platform.system(),
            "distro": distro() or "",
            "hostname": platform.node() or "—",
            "cpu": platform.processor() or "—",
            "memory_total": human_size(mem.total),
            "memory_used": human_size(mem.used),
            "memory_free": human_size(mem.available),
            "disks": disks,
            "uptime": uptime_str,
            "users": ", ".join(users) if users else "—",
            "root": os.geteuid() == 0,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/disk/large-files")
def api_disk_large():
    try:
        system = os_type()
        search_paths = ["~/", "/var/log", "/var/cache", "/tmp"] if system == "linux" else ["~/", "/Library/Logs", "/var/log"]
        files = []
        for sp in search_paths:
            files.extend(_largest_files(sp))
        return jsonify({"files": files[:15], "threshold_mb": 50, "searched": search_paths})
    except Exception as e:
        return jsonify({"error": str(e), "files": [], "threshold_mb": 50}), 500


@app.route("/api/disk/thresholds")
def api_disk_thresholds():
    try:
        warnings = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                if usage.percent > 90:
                    warnings.append(
                        f"🚨 {part.mountpoint} is {usage.percent}% full — only {human_size(usage.free)} free. This can cause serious problems."
                    )
                elif usage.percent > 80:
                    warnings.append(
                        f"⚠️  {part.mountpoint} is {usage.percent}% full — {human_size(usage.free)} free remaining."
                    )
            except Exception:
                continue
        return jsonify({"warnings": warnings, "root_needed": sudo_required()})
    except Exception as e:
        return jsonify({"error": str(e), "warnings": []}), 500


@app.route("/api/disk/delete-file", methods=["POST"])
def api_disk_delete():
    try:
        data = request.get_json(safe=True) or {}
        path = data.get("path", "")
        if not path:
            return jsonify({"error": "No path provided"}), 400
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return jsonify({"error": "File not found"}), 404
        if not os.path.isfile(path):
            return jsonify({"error": "Not a file"}), 400
        if path.startswith(("/etc", "/var", "/usr", "/bin", "/sbin")):
            if sudo_required():
                return jsonify({"error": "System files require administrator access"}), 403
        os.remove(path)
        return jsonify({"success": True, "deleted": path})
    except PermissionError:
        return jsonify({"error": "Permission denied — administrator access required"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/slow/processes")
def api_slow_processes():
    try:
        return jsonify({"processes": _top_processes(10)})
    except Exception as e:
        return jsonify({"error": str(e), "processes": []}), 500


@app.route("/api/slow/startup")
def api_slow_startup():
    try:
        items = _startup_items()
        return jsonify({"items": items, "os": os_type(), "count": len(items)})
    except Exception as e:
        return jsonify({"error": str(e), "items": [], "count": 0}), 500


@app.route("/api/printers")
def api_printers():
    try:
        rc, out, err = run("lpstat -a 2>/dev/null || lpstat 2>/dev/null || echo ''")
        printers = []
        if rc == 0 and out:
            for line in out.splitlines():
                line = line.strip()
                if line and not line.startswith("lpstat"):
                    printers.append(line)

        # Also try listing via CUPS
        if not printers:
            rc2, out2, _ = run("curl -s http://localhost:631/printers 2>/dev/null || echo ''")
            if rc2 == 0 and out2:
                # Basic parse — look for printer names
                for line in out2.splitlines():
                    import re
                    m = re.search(r'<title>([^<]+)</title>', line)
                    if m:
                        printers.append(m.group(1))

        return jsonify({
            "printers": printers,
            "cups_running": rc == 0,
            "error": err if rc != 0 else "",
        })
    except Exception as e:
        return jsonify({"error": str(e), "printers": []}), 500


@app.route("/api/printers/details")
def api_printers_details():
    try:
        info = {}
        # Default printer
        rc, out, _ = run("lpstat -d 2>/dev/null")
        if rc == 0 and out:
            info["default"] = out.replace("device for ", "")

        # Print queue status
        rc, out, _ = run("lpq 2>/dev/null || echo ''")
        info["queue"] = out if rc == 0 and out else "No queue info available"

        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/printers/actions")
def api_printers_actions():
    try:
        cups_available = shutil.which("cupsenable") is not None or shutil.which("lpadmin") is not None
        actions = []
        if cups_available or os_type() == "linux":
            actions = [
                {
                    "name": "cupsenable",
                    "label": "Enable All Printers",
                    "icon": "🔓",
                    "cmd": "cupsenable -a",
                    "danger": False,
                    "warn": None,
                    "note": "Enables all disabled printers.",
                },
                {
                    "name": "cancel_all",
                    "label": "Clear All Print Jobs",
                    "icon": "🗑️",
                    "cmd": "cancel -a 2>/dev/null || echo 'No jobs to cancel'",
                    "danger": True,
                    "warn": "This will cancel ALL pending print jobs. Continue?",
                    "note": "Cancels everything in the print queue.",
                },
                {
                    "name": "restart_cups",
                    "label": "Restart CUPS (Print Service)",
                    "icon": "🔄",
                    "cmd": "sudo systemctl restart cups 2>/dev/null || sudo service cups restart 2>/dev/null || echo 'Could not restart CUPS — try manually'",
                    "danger": False,
                    "warn": None,
                    "note": "Restarts the print spooler service.",
                },
                {
                    "name": "lpstat_full",
                    "label": "Show Full Printer Status",
                    "icon": "📋",
                    "cmd": "lpstat -t 2>/dev/null || lpstat -p 2>/dev/null || echo 'lpstat not available'",
                    "danger": False,
                    "warn": None,
                    "note": "Shows detailed status of all printers and jobs.",
                },
            ]
        return jsonify({"available": actions, "cups_available": cups_available})
    except Exception as e:
        return jsonify({"error": str(e), "available": []}), 500


@app.route("/api/printers/run-action", methods=["POST"])
def api_printers_run_action():
    try:
        data = request.get_json(safe=True) or {}
        cmd = data.get("cmd", "")
        if not cmd:
            return jsonify({"error": "No command provided"}), 400
        rc, out, err = run(cmd, timeout=30)
        if rc == 0:
            return jsonify({"success": True, "output": out[:500] if out else "Command completed successfully"})
        else:
            return jsonify({"error": err[:200] if err else "Command failed", "output": out[:200] if out else ""})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/printers/test", methods=["POST"])
def api_printers_test():
    """Send a test print job to the default printer. Returns ASCII smiley on success."""
    try:
        # Find default printer
        rc, out, _ = run("lpstat -d 2>/dev/null")
        if rc != 0 or not out:
            return jsonify({"error": "No default printer found. Set a default printer first."}), 400

        default_printer = out.replace("device for ", "").strip()

        # Create a test print file
        import tempfile
        test_content = """SupportBuddy Test Print
========================
Date: {timestamp}
Printer: {printer}
Status: OK

If you can read this, your printer is working!
""".format(timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), printer=default_printer)

        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, prefix='supportbuddy_test_')
        tmp.write(test_content)
        tmp.close()

        # Send to printer
        rc, out, err = run(f"lp {shlex_quote(tmp.name)} 2>&1")
        os.unlink(tmp.name)

        if rc == 0:
            return jsonify({
                "success": True,
                "printer": default_printer,
                "message": "Test page sent!",
                "smiley": True,
                "ascii": "  ◕‿◕",
            })
        else:
            return jsonify({"error": err[:200] if err else "Print command failed", "smiley": False}), 500

    except Exception as e:
        return jsonify({"error": str(e), "smiley": False}), 500


@app.route("/api/browser")
def api_browser():
    try:
        return jsonify(_browser_info())
    except Exception as e:
        return jsonify({"error": str(e), "browsers": [], "running": []}), 500


@app.route("/api/browser/close", methods=["POST"])
def api_browser_close():
    try:
        data = request.get_json(safe=True) or {}
        name = data.get("name", "")
        if not name:
            return jsonify({"error": "No browser name provided"}), 400
        name_lower = name.lower()
        killed = []
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                pname = (proc.info['name'] or '').lower()
                if name_lower in pname or pname in name_lower:
                    proc.kill()
                    killed.append(proc.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        if killed:
            return jsonify({"success": True, "killed_pids": killed, "message": f"Killed {len(killed)} process(es)"})
        return jsonify({"error": f"No running processes found for '{name}'"}, custom_status=404)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/network")
def api_network():
    try:
        import socket

        # Basic connectivity test
        connected = False
        public_ip = ""
        dns = ""
        interfaces = []

        try:
            # Test via multiple targets
            for target in [("8.8.8.8", 53), ("1.1.1.1", 53), ("208.67.222.222", 53)]:
                try:
                    s = socket.create_connection(target, timeout=3)
                    s.close()
                    connected = True
                    break
                except (socket.timeout, socket.error, OSError):
                    continue
        except Exception:
            pass

        # Get public IP
        if connected:
            try:
                rc, out, _ = run("curl -s --max-time 5 https://api.ipify.org 2>/dev/null || echo ''")
                if rc == 0 and out:
                    public_ip = out.strip()
            except Exception:
                pass

        # DNS check (independent of connectivity — test resolution directly)
        try:
            dns = _get_dns_working()
        except Exception:
            dns = "DNS appears working"

        # Interfaces
        try:
            if os_type() == "linux":
                rc, out, _ = run("ip -brief addr 2>/dev/null || ifconfig 2>/dev/null | grep -E '^[a-z]' || echo ''")
            else:
                rc, out, _ = run("ifconfig 2>/dev/null | grep -E '^[a-zA-Z]' || networksetup -listallhardwareports 2>/dev/null || echo ''")
            if rc == 0 and out:
                for line in out.splitlines():
                    if line.strip():
                        interfaces.append({"name": line.split()[0] if line.split() else line.strip(), "raw": line.strip()})
        except Exception:
            pass

        # Connection type detection
        connection_type = "—"
        if connected:
            if interfaces:
                wifi_iface = next(
                    (i for i in interfaces if any(k in i.get("raw", "").lower() for k in ["wl", "wifi", "wireless", "en0"])),
                    None
                )
                if wifi_iface:
                    connection_type = "Wi-Fi"
                elif any("eth" in i.get("name", "").lower() or "en" == i.get("name", "")[:2].lower() for i in interfaces):
                    connection_type = "Wired (Ethernet)"
                else:
                    connection_type = "Network"

        return jsonify({
            "connected": connected,
            "public_ip": public_ip,
            "dns": dns,
            "connection_type": connection_type,
            "interfaces": interfaces,
            "error": "",
        })
    except Exception as e:
        return jsonify({"error": str(e), "connected": False}), 500


@app.route("/api/email/config")
def api_email_config():
    providers = [
        {
            "id": "gmail",
            "display_name": "Gmail",
            "imap_host": "imap.gmail.com",
            "imap_port": 993,
            "imap_secure": "SSL/TLS",
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 465,
            "smtp_secure": "SSL/TLS",
            "note": "Google may require an App Password instead of your regular password. Go to your Google Account → Security → App passwords to create one.",
        },
        {
            "id": "outlook",
            "display_name": "Outlook / Hotmail / Live",
            "imap_host": "outlook.office365.com",
            "imap_port": 993,
            "imap_secure": "SSL/TLS",
            "smtp_host": "smtp.office365.com",
            "smtp_port": 587,
            "smtp_secure": "STARTTLS",
            "note": "Microsoft accounts often need an App Password or modern auth. If setup fails with your regular password, try an App Password from your Microsoft account security page.",
        },
        {
            "id": "yahoo",
            "display_name": "Yahoo Mail",
            "imap_host": "imap.mail.yahoo.com",
            "imap_port": 993,
            "imap_secure": "SSL/TLS",
            "smtp_host": "smtp.mail.yahoo.com",
            "smtp_port": 465,
            "smtp_secure": "SSL/TLS",
            "note": "Yahoo requires an App Password for most email apps. Generate one from your Yahoo Account Security page.",
        },
        {
            "id": "icloud",
            "display_name": "iCloud Mail",
            "imap_host": "imap.mail.me.com",
            "imap_port": 993,
            "imap_secure": "SSL/TLS",
            "smtp_host": "smtp.mail.me.com",
            "smtp_port": 587,
            "smtp_secure": "STARTTLS",
            "note": "Two-factor authentication must be enabled. Use an App-Specific Password from appleid.apple.com.",
        },
        {
            "id": "other",
            "display_name": "Other Provider",
            "imap_host": "ask your provider",
            "imap_port": "usually 993 or 143",
            "imap_secure": "usually SSL/TLS or STARTTLS",
            "smtp_host": "ask your provider",
            "smtp_port": "usually 465 or 587",
            "smtp_secure": "usually SSL/TLS or STARTTLS",
            "note": "Check your email provider's help pages for IMAP/SMTP settings. Your full email address is always the username.",
        },
    ]
    return jsonify({"providers": providers})


@app.route("/api/updates")
def api_updates():
    try:
        data = _pending_updates()
        return jsonify({
            "count": data["count"],
            "details": data.get("details", []),
            "pm": data.get("pm", ""),
            "error": "",
            "latest": datetime.datetime.now().strftime("%H:%M"),
            "note": "Run your system's update tool to install these.",
        })
    except Exception as e:
        return jsonify({"error": str(e), "count": 0, "details": [], "pm": ""}), 500


@app.route("/api/quick-scan")
def api_quick_scan():
    try:
        results = {}
        try:
            results["system"] = _get_system_info()
        except Exception as e:
            results["system"] = {"error": str(e)}
        try:
            results["cleanup"] = _get_cleanup_summary()
        except Exception as e:
            results["cleanup"] = {"error": str(e)}
        try:
            results["printers"] = _get_printers_info()
        except Exception as e:
            results["printers"] = {"error": str(e)}
        try:
            results["network"] = _get_network_info()
        except Exception as e:
            results["network"] = {"error": str(e)}
        try:
            results["updates"] = _get_updates_info()
        except Exception as e:
            results["updates"] = {"error": str(e)}
        results["scan_time"] = datetime.datetime.now().isoformat()
        results["os"] = os_type()
        results["distro"] = distro() or ""
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _get_system_info():
    mem = psutil.virtual_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mountpoint": part.mountpoint,
                "fstype": part.fstype or "",
                "total": human_size(usage.total),
                "used": human_size(usage.used),
                "free": human_size(usage.free),
                "percent": usage.percent,
            })
        except Exception:
            continue
    try:
        with open("/proc/uptime") as f:
            uptime_str = str(datetime.timedelta(seconds=int(float(f.read().split()[0]))))
    except Exception:
        uptime_str = "—"
    users = []
    try:
        import pwd
        users = [u.pw_name for u in psutil.users()]
    except Exception:
        pass
    return {
        "os": platform.system(),
        "distro": distro() or "",
        "hostname": platform.node() or "—",
        "cpu": platform.processor() or "—",
        "memory_total": human_size(mem.total),
        "memory_used": human_size(mem.used),
        "memory_free": human_size(mem.available),
        "disks": disks,
        "uptime": uptime_str,
        "users": ", ".join(users) if users else "—",
    }


def _get_cleanup_summary():
    system = os_type()
    if system == "linux":
        items = _linux_cleanup_summary()
        return {"items": items, "total": human_size(sum(i.get("size_bytes", 0) for i in items))}
    elif system == "mac":
        items = _mac_cleanup_summary()
        return {"items": items, "total": human_size(sum(i.get("size_bytes", 0) for i in items))}
    return {"items": [], "total": "0 B"}


def _get_printers_info():
    rc, out, err = run("lpstat -a 2>/dev/null || lpstat 2>/dev/null || echo ''")
    printers = []
    if rc == 0 and out:
        for line in out.splitlines():
            line = line.strip()
            if line and not line.startswith("lpstat"):
                printers.append(line)
    return {"printers": printers, "cups_running": rc == 0}


def _get_network_info():
    import socket
    connected = False
    try:
        for target in [("8.8.8.8", 53), ("1.1.1.1", 53), ("208.67.222.222", 53)]:
            try:
                s = socket.create_connection(target, timeout=3)
                s.close()
                connected = True
                break
            except (socket.timeout, socket.error, OSError):
                continue
    except Exception:
        pass
    interfaces = []
    try:
        if os_type() == "linux":
            rc, out, _ = run("ip -brief addr 2>/dev/null || echo ''")
        else:
            rc, out, _ = run("ifconfig 2>/dev/null | grep -E '^[a-zA-Z]' || echo ''")
        if rc == 0 and out:
            for line in out.splitlines():
                if line.strip():
                    name = line.split()[0] if line.split() else line.strip()
                    ip = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                    interfaces.append({"name": name, "ip": ip.group(1) if ip else None})
    except Exception:
        pass
    return {"connected": connected, "interfaces": interfaces}


def _get_updates_info():
    data = _pending_updates()
    return {"count": data["count"], "details": data.get("details", []), "pm": data.get("pm", "")}


@app.route("/api/report/export")
def api_report_export():
    try:
        scan = {}
        try:
            scan["system"] = _get_system_info()
        except Exception as e:
            scan["system"] = {"error": str(e)}
        try:
            scan["cleanup"] = _get_cleanup_summary()
        except Exception as e:
            scan["cleanup"] = {"error": str(e)}
        try:
            scan["printers"] = _get_printers_info()
        except Exception as e:
            scan["printers"] = {"error": str(e)}
        try:
            scan["network"] = _get_network_info()
        except Exception as e:
            scan["network"] = {"error": str(e)}
        try:
            scan["updates"] = _get_updates_info()
        except Exception as e:
            scan["updates"] = {"error": str(e)}
        try:
            scan["browsers"] = _browser_info()
        except Exception as e:
            scan["browsers"] = {"error": str(e)}
        try:
            scan["slow_processes"] = _top_processes(10)
        except Exception as e:
            scan["slow_processes"] = {"error": str(e)}
        try:
            scan["startup_items"] = _startup_items()
        except Exception as e:
            scan["startup_items"] = {"error": str(e)}
        scan["scan_time"] = datetime.datetime.now().isoformat()
        scan["os"] = os_type()
        scan["distro"] = distro() or ""
        scan["hostname"] = platform.node() or "—"
        scan["exported_by"] = "SupportBuddy"
        json_str = json.dumps(scan, indent=2, default=str)
        return Response(
            json_str,
            mimetype="application/json",
            headers={
                "Content-Disposition": f"attachment; filename=supportbuddy-report-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            },
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/report/import", methods=["POST"])
def api_report_import():
    try:
        data = request.get_json(safe=True) or {}
        if not data:
            return jsonify({"error": "No report data provided"}), 400
        if "system" not in data and "os" not in data:
            return jsonify({"error": "Not a valid SupportBuddy report"}), 400
        return jsonify({"success": True, "report": data, "message": "Report loaded successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Linux cleanup logic
# ---------------------------------------------------------------------------

def _linux_cleanup_summary():
    items = []

    # Package manager cache
    pm = package_manager()
    if pm == "apt":
        rc, out, _ = run("dpkg -l | grep -c '^ii' 2>/dev/null || echo 0")
        items.append({
            "label": "APT package cache",
            "desc": "Cached .deb files from apt installs",
            "size_bytes": _du_size("/var/cache/apt/archives/"),
            "auto": True,
            "cmd": "apt-get clean",
            "warn": False,
        })
        items.append({
            "label": "Unneeded packages (apt autoremove)",
            "desc": "Old dependencies no longer required",
            "size_bytes": _du_size("/var/cache/apt/archives/") // 2,
            "auto": False,
            "cmd": "apt-get autoremove --dry-run",
            "warn": True,
            "dry_run": True,
        })
    elif pm == "dnf":
        rc, out, _ = run("dnf clean packages 2>/dev/null && du -sb /var/cache/dnf 2>/dev/null || echo 0")
        items.append({
            "label": "DNF package cache",
            "desc": "Cached RPM files",
            "size_bytes": _du_size("/var/cache/dnf/"),
            "auto": True,
            "cmd": "dnf clean packages",
            "warn": False,
        })
    elif pm == "pacman":
        items.append({
            "label": "Pacman package cache",
            "desc": "Cached package files",
            "size_bytes": _du_size("/var/cache/pacman/pkg/"),
            "auto": True,
            "cmd": "pacman -Sc",
            "warn": False,
        })

    # Systemd journal
    rc, out, _ = run("journalctl --disk-usage 2>/dev/null || echo ''")
    journal_size = 0
    if rc == 0 and out:
        import re
        m = re.search(r'([\d.]+)\s*(GB|MB|KB|B)', out)
        if m:
            journal_size = _parse_size(m.group(1), m.group(2))
    if journal_size > 0:
        items.append({
            "label": "System journal logs (old entries)",
            "desc": "System logs — keeps recent ones, removes old ones",
            "size_bytes": int(journal_size * 0.5),  # estimate half is removable
            "auto": False,
            "cmd": "journalctl --vacuum-time=7d",
            "warn": True,
            "dry_run": False,
        })

    # User thumbnail cache
    thumb_size = _du_size(os.path.expanduser("~/.cache/thumbnails/"))
    if thumb_size > 0:
        items.append({
            "label": "Thumbnail cache",
            "desc": "Cached image thumbnails — will rebuild as needed",
            "size_bytes": thumb_size,
            "auto": True,
            "cmd": f"rm -rf {shlex_quote(os.path.expanduser('~/.cache/thumbnails/'))}/*",
            "warn": False,
        })

    # General user cache (selective — skip large active caches)
    cache_dirs = [
        ("~/.cache/fontconfig", "Font config cache"),
        ("~/.cache/gnome-", "GNOME app caches"),
    ]
    for path, label in cache_dirs:
        p = os.path.expanduser(path)
        if os.path.exists(p):
            sz = _du_size(p)
            if sz > 0:
                items.append({
                    "label": label,
                    "desc": "App cache — safe to clear",
                    "size_bytes": sz,
                    "auto": True,
                    "cmd": f"rm -rf {shlex_quote(p)}/*",
                    "warn": False,
                })

    # Temporary files in user's /tmp-like dirs
    tmp_size = _du_size(os.path.expanduser("~/.local/share/Trash/"))
    if tmp_size > 0:
        items.append({
            "label": "Trash (your deleted files)",
            "desc": "Files you've deleted — permanently remove",
            "size_bytes": tmp_size,
            "auto": False,
            "cmd": "rm -rf ~/.local/share/Trash/*",
            "warn": True,
            "dry_run": False,
        })

    return items


def _linux_cleanup_run(indices):
    items = _linux_cleanup_summary()
    log_lines = []
    cleaned = 0
    errors = []

    for i in indices:
        if i < 0 or i >= len(items):
            continue
        item = items[i]

        if item.get("dry_run"):
            rc, out, err = run(item["cmd"])
            if rc == 0:
                log_lines.append(f"✅ {item['label']}: would clean (dry run OK)")
            else:
                log_lines.append(f"⚠️  {item['label']}: issue found — {err[:100]}")
            continue

        if sudo_required() and item["cmd"].startswith(("apt-get", "dnf", "pacman", "journalctl")):
            # Try with sudo
            cmd = f"sudo {item['cmd']}"
        else:
            cmd = item["cmd"]

        rc, out, err = run(cmd, timeout=60)
        if rc == 0:
            log_lines.append(f"✅ {item['label']}: cleaned")
            cleaned += 1
        else:
            log_lines.append(f"⚠️  {item['label']}: {err[:120] if err else 'failed'}")
            errors.append(item['label'])

    return {
        "cleaned": cleaned,
        "log": "\n".join(log_lines),
        "error": "; ".join(errors) if errors else "",
    }


# ---------------------------------------------------------------------------
# Mac cleanup logic
# ---------------------------------------------------------------------------

def _mac_cleanup_summary():
    items = []

    # User caches
    cache_dir = os.path.expanduser("~/Library/Caches/")
    if os.path.exists(cache_dir):
        total_cache = _du_size(cache_dir)
        if total_cache > 0:
            items.append({
                "label": "User cache files (~/Library/Caches)",
                "desc": "App cache — safe to clear, apps will rebuild",
                "size_bytes": int(total_cache * 0.7),  # estimate 70% is safe to remove
                "auto": False,
                "cmd": f"find {shlex_quote(cache_dir)} -type f -name '*.cache' -delete 2>/dev/null",
                "warn": True,
                "dry_run": False,
            })

    # Log files
    log_dir = os.path.expanduser("~/Library/Logs/")
    if os.path.exists(log_dir):
        log_size = _du_size(log_dir)
        if log_size > 0:
            items.append({
                "label": "User log files",
                "desc": "Application logs — older logs are safe to remove",
                "size_bytes": log_size,
                "auto": False,
                "cmd": f"find {shlex_quote(log_dir)} -type f -mtime +30 -delete 2>/dev/null",
                "warn": False,
                "dry_run": False,
            })

    # System logs (requires sudo)
    sys_log = _du_size("/Library/Logs/")
    if sys_log > 0:
        items.append({
            "label": "System log files (/Library/Logs)",
            "desc": "System-wide logs — requires administrator access",
            "size_bytes": sys_log,
            "auto": False,
            "cmd": "sudo rm -rf /Library/Logs/DiagnosticReports/* 2>/dev/null",
            "warn": True,
            "dry_run": False,
        })

    # Downloads clutter — just warn, don't auto-clean
    dl_size = _du_size(os.path.expanduser("~/Downloads/"))
    if dl_size > 100 * 1024 * 1024:  # > 100MB
        items.append({
            "label": "Downloads folder is large",
            "desc": f"~{human_size(dl_size)} in Downloads — review and delete what you don't need",
            "size_bytes": 0,
            "auto": False,
            "cmd": "",
            "warn": True,
            "dry_run": False,
            "is_info": True,
        })

    return items


def _mac_cleanup_run(indices):
    items = _mac_cleanup_summary()
    log_lines = []
    cleaned = 0
    errors = []

    for i in indices:
        if i < 0 or i >= len(items):
            continue
        item = items[i]
        if item.get("is_info"):
            log_lines.append(f"ℹ️  {item['label']}: {item['desc']}")
            continue
        if not item.get("cmd"):
            continue

        if sudo_required() and "sudo" not in item["cmd"] and item["cmd"].startswith("sudo") == False:
            cmd = f"sudo {item['cmd']}"
        else:
            cmd = item["cmd"]

        rc, out, err = run(cmd, timeout=60)
        if rc == 0:
            log_lines.append(f"✅ {item['label']}: cleaned")
            cleaned += 1
        else:
            log_lines.append(f"⚠️  {item['label']}: {err[:120] if err else 'failed'}")
            errors.append(item['label'])

    return {
        "cleaned": cleaned,
        "log": "\n".join(log_lines),
        "error": "; ".join(errors) if errors else "",
    }


# ---------------------------------------------------------------------------
# Cleanup API
# ---------------------------------------------------------------------------

@app.route("/api/cleanup/summary")
def api_cleanup_summary():
    try:
        items = []
        system = os_type()

        if system == "linux":
            items = _linux_cleanup_summary()
        elif system == "mac":
            items = _mac_cleanup_summary()
        else:
            items = [{"label": "Not supported on this OS", "size": "—", "desc": "", "auto": False}]

        # Calculate total
        total_bytes = 0
        for item in items:
            if item.get("size_bytes"):
                total_bytes += item["size_bytes"]

        return jsonify({
            "items": items,
            "total": human_size(total_bytes),
            "root_needed": sudo_required(),
        })
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500


@app.route("/api/cleanup/run", methods=["POST"])
def api_cleanup_run():
    try:
        data = request.get_json(safe=True) or {}
        indices = data.get("indices", [])

        if not isinstance(indices, list):
            return jsonify({"error": "Invalid request"}), 400

        system = os_type()
        if system == "linux":
            result = _linux_cleanup_run(indices)
        elif system == "mac":
            result = _mac_cleanup_run(indices)
        else:
            result = {"error": "Not supported on this OS", "log": "", "cleaned": 0}

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "log": "", "cleaned": 0}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SupportBuddy — local tech support app")
    parser.add_argument("--port", type=int, default=58678, help="Port to bind to (default: 58678)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1 — localhost only)")
    parser.add_argument("--open", action="store_true", help="Open the UI in the default browser on start")
    args = parser.parse_args()

    port = args.port
    host = args.host

    print(f"\n{'='*50}")
    print(f"  SupportBuddy starting...")
    print(f"  Open: http://{host}:{port}")
    print(f"  OS: {os_type()} {distro() or ''}")
    print(f"{'='*50}\n")

    if args.open:
        import webbrowser
        import time
        def _open():
            time.sleep(1.5)
            webbrowser.open(f"http://{host}:{port}")
        import threading
        threading.Thread(target=_open, daemon=True).start()

    app.run(host=host, port=port, debug=False)
