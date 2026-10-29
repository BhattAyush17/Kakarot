#!/usr/bin/env python3
import os
import sys
import json
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

# ANSI Colors
C_RESET = '\033[0m'
C_PASS = '\033[1;32m[PASS]\033[0m'
C_WARN = '\033[1;33m[WARN]\033[0m'
C_FAIL = '\033[1;31m[FAIL]\033[0m'
C_INFO = '\033[1;36m[INFO]\033[0m'

# Paths
KAKAROT_DIR = Path.home() / ".local/share/kakarot"
DESKTOP_FILE = Path.home() / ".local/share/applications/kakarot.desktop"
SERVICE_NAME = "kakarot.service"
SOCKET_PATH = KAKAROT_DIR / "kakarot.sock"
DB_PATH = KAKAROT_DIR / "kakarot.db"
# Fallback db path based on ls
if not DB_PATH.exists() and (KAKAROT_DIR / "kakarot-reminders.sqlite3").exists():
    DB_PATH = KAKAROT_DIR / "kakarot-reminders.sqlite3"

def print_section(title):
    print(f"\n{C_INFO} {title}")
    print("=" * 60)

def run_cmd(cmd, check=False):
    try:
        return subprocess.run(cmd, shell=True, text=True, capture_output=True, check=check)
    except subprocess.CalledProcessError as e:
        return e

def check_process_identity():
    print_section("1. Desktop & OS Process Identity Verification")
    
    # 1. Check for generically named python processes vs renamed ones
    pgrep = run_cmd("pgrep -af kakarot")
    if pgrep.returncode == 0:
        kakarot_procs = [line for line in pgrep.stdout.strip().split('\n') if 'kakarot' in line.lower()]
        generic_found = False
        for proc in kakarot_procs:
            if proc.split()[1].startswith('python'):
                print(f"{C_WARN} Found generically named process: {proc}")
                generic_found = True
        
        if not generic_found and kakarot_procs:
            print(f"{C_PASS} No generic 'python' process identities found for Kakarot.")
        elif not kakarot_procs:
            print(f"{C_FAIL} No running Kakarot processes found.")
    else:
        print(f"{C_FAIL} No running Kakarot processes found.")

    # 2. Window Properties (X11 / Wayland)
    xprop = run_cmd("xprop -name \"Kakarot\" WM_CLASS")
    if xprop.returncode == 0 and "kakarot" in xprop.stdout.lower():
        print(f"{C_PASS} Window WM_CLASS matches 'Kakarot': {xprop.stdout.strip()}")
    else:
        print(f"{C_WARN} Could not verify WM_CLASS via xprop. If running Wayland, verify via GNOME Looking Glass. (Output: {xprop.stdout.strip()})")

    # 3. Desktop file validation
    if DESKTOP_FILE.exists():
        val_res = run_cmd(f"desktop-file-validate {DESKTOP_FILE}")
        if val_res.returncode == 0:
            print(f"{C_PASS} Desktop file validation passed.")
        else:
            print(f"{C_FAIL} Desktop file validation failed:\n{val_res.stderr.strip()}")
            
        # Icon check
        with open(DESKTOP_FILE, 'r') as f:
            for line in f:
                if line.startswith("Icon="):
                    icon_val = line.split("=")[1].strip()
                    if icon_val.startswith("/"):
                        if Path(icon_val).exists():
                            print(f"{C_PASS} Desktop Icon path resolves: {icon_val}")
                        else:
                            print(f"{C_FAIL} Desktop Icon path does not exist: {icon_val}")
                    else:
                        print(f"{C_INFO} Desktop Icon is a system theme icon: {icon_val}")
    else:
        print(f"{C_FAIL} Desktop file not found at {DESKTOP_FILE}")


def check_systemd_cgroups():
    print_section("2. Systemd Service & Cgroup Memory Confinement")
    
    show_res = run_cmd(f"systemctl --user show {SERVICE_NAME}")
    props = {}
    for line in show_res.stdout.split('\n'):
        if '=' in line:
            k, v = line.split('=', 1)
            props[k] = v
            
    active = props.get("ActiveState")
    sub = props.get("SubState")
    if active == "active" and sub == "running":
        print(f"{C_PASS} Service {SERVICE_NAME} is active and running.")
    else:
        print(f"{C_FAIL} Service state is {active}/{sub} (expected active/running).")
        
    mem_high = props.get("MemoryHigh")
    mem_max = props.get("MemoryMax")
    
    if mem_high == "134217728":
        print(f"{C_PASS} MemoryHigh is correctly set to 128 MiB (134217728).")
    elif mem_high == "infinity" or not mem_high:
        print(f"{C_FAIL} MemoryHigh is not configured or infinite.")
    else:
        print(f"{C_WARN} MemoryHigh is {mem_high} (expected 134217728).")
        
    if mem_max == "201326592":
        print(f"{C_PASS} MemoryMax is correctly set to 192 MiB (201326592).")
    elif mem_max == "infinity" or not mem_max:
        print(f"{C_FAIL} MemoryMax is not configured or infinite.")
    else:
        print(f"{C_WARN} MemoryMax is {mem_max} (expected 201326592).")

    mem_current = props.get("MemoryCurrent", "N/A")
    mem_peak = props.get("MemoryPeak", "N/A")
    if mem_current != "[not set]" and mem_current != "infinity":
        try:
            curr_mb = int(mem_current) / 1024 / 1024
            print(f"{C_INFO} MemoryCurrent: {curr_mb:.2f} MiB")
        except ValueError:
             print(f"{C_INFO} MemoryCurrent: {mem_current}")
    else:
        print(f"{C_WARN} MemoryCurrent metric unavailable.")
        
    if mem_peak != "[not set]" and mem_peak != "infinity":
        try:
            peak_mb = int(mem_peak) / 1024 / 1024
            print(f"{C_INFO} MemoryPeak: {peak_mb:.2f} MiB")
        except ValueError:
             print(f"{C_INFO} MemoryPeak: {mem_peak}")


def check_ipc_socket():
    print_section("3. IPC Socket & State Engine Handshake")
    
    if not SOCKET_PATH.exists():
        print(f"{C_FAIL} Unix socket not found at {SOCKET_PATH}")
        return
        
    print(f"{C_PASS} Unix socket exists at {SOCKET_PATH}")
    
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(str(SOCKET_PATH))
            print(f"{C_PASS} Successfully connected to IPC socket.")
            
            ping_msg = json.dumps({"command": "status"}) + "\n"
            client.sendall(ping_msg.encode('utf-8'))
            
            resp = client.recv(4096).decode('utf-8')
            if resp:
                try:
                    data = json.loads(resp.strip().split('\n')[0])
                    state = data.get("state", "UNKNOWN")
                    state_version = data.get("state_version")
                    
                    if isinstance(state_version, int):
                        print(f"{C_PASS} Payload contains integer state_version: {state_version}")
                    else:
                        print(f"{C_WARN} Missing or invalid state_version in payload: {state_version}")
                        
                    if state == "IDLE_MUTED":
                        print(f"{C_PASS} Initial state is IDLE_MUTED.")
                    else:
                        print(f"{C_WARN} State is {state}, expected IDLE_MUTED.")
                        
                except json.JSONDecodeError:
                    print(f"{C_FAIL} Invalid NDJSON response from socket: {resp}")
            else:
                print(f"{C_WARN} No response received from status ping (could be waiting for explicit event).")
    except Exception as e:
        print(f"{C_FAIL} Socket communication error: {e}")


def check_audio_server():
    print_section("4. Audio Server & Echo Cancellation Status")
    
    pactl = run_cmd("pactl list modules short")
    if pactl.returncode == 0:
        if "module-echo-cancel" in pactl.stdout:
            print(f"{C_PASS} module-echo-cancel is loaded in PulseAudio/PipeWire.")
        else:
            print(f"{C_FAIL} module-echo-cancel is NOT loaded.")
            print(f"       -> Remediation: Run 'pactl load-module module-echo-cancel' or configure in PipeWire context.")
    else:
        print(f"{C_WARN} Could not query pactl (return code {pactl.returncode}).")


def check_sqlite_integrity():
    print_section("5. SQLite Knowledge Engine & Inotify Watcher Integrity")
    
    if not DB_PATH.exists():
        print(f"{C_FAIL} Database not found at {DB_PATH}")
        return
        
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from kakarot_daemon.system_db import get_db
        conn = get_db(DB_PATH.parent)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("PRAGMA journal_mode")
        jm = cursor.fetchone()[0]
        if jm.lower() == 'wal':
            print(f"{C_PASS} journal_mode = wal")
        else:
            print(f"{C_FAIL} journal_mode = {jm} (expected wal)")
            
        cursor.execute("PRAGMA mmap_size")
        ms = cursor.fetchone()[0]
        if ms >= 268435456:
            print(f"{C_PASS} mmap_size = {ms} (>= 256 MiB)")
        else:
            print(f"{C_FAIL} mmap_size = {ms} (expected >= 268435456)")
            
        cursor.execute("PRAGMA cache_size")
        cs = cursor.fetchone()[0]
        if cs == -8192:
            print(f"{C_PASS} cache_size = -8192")
        else:
            print(f"{C_WARN} cache_size = {cs} (expected -8192)")
            
        tables = ['app_inventory', 'file_index', 'user_notes', 'execution_journal']
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = [row[0] for row in cursor.fetchall()]
        
        for t in tables:
            if t in existing_tables:
                print(f"{C_PASS} Table exists: {t}")
                if t == 'user_notes':
                    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_notes'")
                    sql = cursor.fetchone()[0]
                    if 'fts5' in sql.lower():
                        print(f"{C_PASS} Table user_notes is using FTS5.")
                    else:
                        print(f"{C_WARN} Table user_notes does not appear to be FTS5.")
            else:
                print(f"{C_WARN} Table missing: {t}")

        conn.close()
    except Exception as e:
        print(f"{C_FAIL} SQLite check failed: {e}")

    probe_path = Path.home() / "Downloads" / ".kakarot_probe.tmp"
    print(f"\n{C_INFO} Running Live Inotify Probe...")
    try:
        probe_path.write_text("dummy inotify payload")
        time.sleep(0.4) 
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        if 'file_index' in existing_tables:
            cursor.execute("SELECT 1 FROM file_index WHERE path = ?", (str(probe_path),))
            if cursor.fetchone():
                print(f"{C_PASS} File creation indexed successfully.")
            else:
                print(f"{C_FAIL} Probe file not found in file_index after creation.")
        
        probe_path.unlink()
        time.sleep(0.4)
        
        if 'file_index' in existing_tables:
            cursor.execute("SELECT 1 FROM file_index WHERE path = ?", (str(probe_path),))
            if not cursor.fetchone():
                print(f"{C_PASS} File deletion reconciled in file_index successfully.")
            else:
                print(f"{C_FAIL} Probe file still in file_index after deletion.")
        
        conn.close()
    except Exception as e:
        print(f"{C_FAIL} Inotify probe failed: {e}")
    finally:
        if probe_path.exists():
            probe_path.unlink()

def check_hitl_pipeline():
    print_section("6. HITL Security Tier & Non-Blocking Consent Pipeline")
    print(f"{C_INFO} Triggering isolated dummy confirmation request over IPC socket...")
    
    if not SOCKET_PATH.exists():
        print(f"{C_FAIL} Cannot test HITL without socket at {SOCKET_PATH}")
        return
        
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3.0)
            client.connect(str(SOCKET_PATH))
            
            req_msg = json.dumps({"command": "trigger_hitl", "tool": "dummy_dangerous_tool"}) + "\n"
            client.sendall(req_msg.encode('utf-8'))
            
            resp = client.recv(4096).decode('utf-8')
            if resp:
                if "AWAITING_CONFIRMATION" in resp:
                    print(f"{C_PASS} Daemon entered AWAITING_CONFIRMATION state safely.")
                else:
                    print(f"{C_WARN} Daemon response did not indicate AWAITING_CONFIRMATION: {resp.strip()}")
            else:
                print(f"{C_WARN} No response from dummy HITL request.")
                
    except Exception as e:
        print(f"{C_FAIL} HITL IPC test failed: {e}")


def main():
    print(f"\n{C_INFO} Starting Kakarot Stack Audit...")
    check_process_identity()
    check_systemd_cgroups()
    check_ipc_socket()
    check_audio_server()
    check_sqlite_integrity()
    check_hitl_pipeline()
    
    print("\n" + "=" * 60)
    print(f"{C_INFO} Audit Complete. Please review any [WARN] or [FAIL] statuses for remediation.")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    main()
