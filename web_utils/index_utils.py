"""index_utils.py - Dashboard index page data and system status endpoints."""
from __future__ import annotations
import os
import json
import time
import socket
import platform
import glob
import subprocess
import psutil
import resource
import logging  
from logger import Logger
from typing import Optional
from datetime import datetime
from typing import Any, Dict, Tuple


# Singleton module (avoids re-creation on every request)
logger = Logger(name="index_utils.py", level=logging.DEBUG)


class IndexUtils:
    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.logger = logger

        self.db = shared_data.db
        
        # Stats assembly cache (dynamic fields)
        self._last_stats: Dict[str, Any] = {}
        self._last_stats_ts: float = 0.0
        self._cache_ttl: float = 5.0  # 5s

        # System info cache (rarely changes)
        self._system_info_cache: Dict[str, Any] = {}
        self._system_info_ts: float = 0.0
        self._system_cache_ttl: float = 300.0  # 5 min

        # Wardrive cache (known WiFi count)
        self._wardrive_cache_mem: Optional[int] = None
        self._wardrive_ts_mem: float = 0.0
        self._wardrive_ttl: float = 600.0  # 10 min



    def _fds_usage(self):
        try:
            used = len(os.listdir(f"/proc/{os.getpid()}/fd"))
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            # self.logger.info(f"FD usage: {used} used / {soft} soft limit / {hard} hard limit")
            return used, soft
        except Exception as e:
            # self.logger.debug(f"FD usage error: {e}")
            return 0, 0

    def _open_fds_count(self) -> int:
        """Count total open file descriptors (global /proc)."""
        try:
            return len(glob.glob("/proc/*/fd/*"))
        except Exception as e:
            # self.logger.debug(f"FD probe error: {e}")
            return 0
        
    # ---------------------- JSON helpers ----------------------
    def _to_jsonable(self, obj):
        if obj is None or isinstance(obj, (bool, int, float, str)):
            return obj
        if isinstance(obj, bytes):
            import base64
            return {"_b64": base64.b64encode(obj).decode("ascii")}
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: self._to_jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [self._to_jsonable(v) for v in obj]
        return str(obj)

    def _json(self, handler, code: int, obj):
        payload = json.dumps(self._to_jsonable(obj), ensure_ascii=False).encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        try:
            handler.wfile.write(payload)
        except BrokenPipeError:
            pass

    # ---------------------- Helpers FS ----------------------
    def _read_text(self, path: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
        except Exception:
            return None

    # ---------------------- Config store -------------------------
    def _cfg_get(self, key: str, default=None):
        try:
            row = self.db.query_one("SELECT value FROM config WHERE key=? LIMIT 1;", (key,))
            if not row or row.get("value") is None:
                return default
            raw = row["value"]
            try:
                return json.loads(raw)
            except Exception:
                return raw
        except Exception:
            return default

    def _cfg_set(self, key: str, value) -> None:
        try:
            s = json.dumps(value, ensure_ascii=False)
        except Exception:
            s = json.dumps(str(value), ensure_ascii=False)
        self.db.execute(
            """
            INSERT INTO config(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, s),
        )

    # ---------------------- System info ----------------------
    def _get_system_info(self) -> Dict[str, Any]:
        now = time.time()
        if self._system_info_cache and (now - self._system_info_ts) < self._system_cache_ttl:
            return self._system_info_cache

        os_name, os_ver = self._os_release()
        arch = self._arch_bits()
        model = self._pi_model()
        epd_connected = self._check_epd_connected()
        epd_type = self._cfg_get("epd_type", "epd2in13_V4")

        self._system_info_cache = {
            "os_name": os_name,
            "os_version": os_ver,
            "arch": arch,
            "model": model,
            "waveshare_epd_connected": epd_connected,
            "waveshare_epd_type": epd_type if epd_connected else None,
        }
        self._system_info_ts = now
        return self._system_info_cache

    def _os_release(self) -> Tuple[str, str]:
        data = {}
        txt = self._read_text("/etc/os-release") or ""
        for line in txt.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"')
        name = data.get("PRETTY_NAME") or data.get("NAME") or platform.system()
        ver = data.get("VERSION_ID") or data.get("VERSION") or platform.version()
        return (name, ver)

    def _arch_bits(self) -> str:
        try:
            a = platform.architecture()[0]
            return "64-bit" if "64" in a else "32-bit"
        except Exception:
            return "unknown"

    def _pi_model(self) -> str:
        dt_model = self._read_text("/proc/device-tree/model")
        if dt_model:
            return dt_model.replace("\x00", "").strip()
        return platform.machine()

    def _check_epd_connected(self) -> bool:
        # I2C first, fallback to SPI
        try:
            result = subprocess.run(["i2cdetect", "-y", "1"], capture_output=True, text=True, timeout=1)
            if result.returncode == 0:
                output = result.stdout
                if any(addr in output for addr in ["3c", "3d", "48"]):
                    return True
        except Exception:
            pass
        return os.path.exists("/dev/spidev0.0")

    def _uptime_str(self) -> str:
        try:
            up = self._read_text("/proc/uptime")
            seconds = int(float(up.split()[0])) if up else 0
        except Exception:
            seconds = 0
        d, r = divmod(seconds, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)
        return f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"

    def _first_init_ts(self) -> int:
        v = self._cfg_get("first_init_ts")
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
        try:
            row = self.db.query_one(
                """
                SELECT strftime('%s', MIN(created_at)) AS ts FROM (
                    SELECT created_at FROM comments
                    UNION SELECT created_at FROM action_queue
                    UNION SELECT first_seen FROM hostnames_history
                )
                """
            )
            if row and row.get("ts"):
                ts = int(row["ts"])
                self._cfg_set("first_init_ts", ts)
                return ts
        except Exception:
            pass
        return 0

    def _battery_probe(self) -> Dict[str, Any]:
        try:
            # Prefer runtime battery telemetry (PiSugar/shared_data) when available.
            present = bool(getattr(self.shared_data, "battery_present", False))
            last_update = float(getattr(self.shared_data, "battery_last_update", 0.0))
            source = str(getattr(self.shared_data, "battery_source", "shared"))
            if last_update > 0 and (present or source == "none"):
                level = int(getattr(self.shared_data, "battery_percent", 0))
                charging = bool(getattr(self.shared_data, "battery_is_charging", False))
                state = "Charging" if charging else "Discharging"
                if not present:
                    state = "No battery"
                return {
                    "present": present,
                    "level_pct": max(0, min(100, level)),
                    "state": state,
                    "charging": charging,
                    "voltage": getattr(self.shared_data, "battery_voltage", None),
                    "source": source,
                    "updated_at": last_update,
                }
        except Exception:
            pass

        base = "/sys/class/power_supply"
        try:
            if not os.path.isdir(base):
                return {"present": False}
            bat = None
            for n in os.listdir(base):
                if n.startswith("BAT"):
                    bat = os.path.join(base, n)
                    break
            if not bat:
                return {"present": False}
            cap = self._read_text(os.path.join(bat, "capacity"))
            stat = (self._read_text(os.path.join(bat, "status")) or "Unknown").lower()
            lvl = int(cap) if cap and cap.isdigit() else 0
            if stat.startswith("full"):
                state = "Full"
            elif stat.startswith("char"):
                state = "Charging"
            elif stat.startswith("dis"):
                state = "Discharging"
            else:
                state = "Unknown"
            return {"present": True, "level_pct": max(0, min(100, lvl)), "state": state}
        except Exception as e:
            # self.logger.debug(f"Battery probe error: {e}")
            return {"present": False}

    # ---------------------- Network ----------------------
    def _quick_internet(self, timeout: float = 1.0) -> bool:
        try:
            for server in ["1.1.1.1", "8.8.8.8"]:
                try:
                    with socket.create_connection((server, 53), timeout=timeout):
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    def _ip_for(self, ifname: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["ip", "-4", "addr", "show", "dev", ifname], capture_output=True, text=True, timeout=1
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("inet "):
                        return line.split()[1].split("/")[0]
        except Exception:
            pass
        return None

    def _gw_dns(self) -> Tuple[Optional[str], Optional[str]]:
        gw = None
        dns = None
        try:
            out = subprocess.run(["ip", "route"], capture_output=True, text=True, timeout=1)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("default "):
                        parts = line.split()
                        if "via" in parts:
                            idx = parts.index("via")
                            if idx + 1 < len(parts):
                                gw = parts[idx + 1]
                                break
        except Exception:
            pass
        rc = self._read_text("/etc/resolv.conf") or ""
        for line in rc.splitlines():
            line = line.strip()
            if line.startswith("nameserver "):
                dns = line.split()[1]
                break
        return gw, dns

    def _wifi_ssid(self) -> Optional[str]:
        try:
            out = subprocess.run(["iw", "dev", "wlan0", "link"], capture_output=True, text=True, timeout=1)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    line = line.strip()
                    if line.lower().startswith("ssid:"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
        try:
            out = subprocess.run(["wpa_cli", "status"], capture_output=True, text=True, timeout=1)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    if line.startswith("ssid="):
                        return line.split("=", 1)[1]
        except Exception:
            pass
        return None

    def _detect_gadget_lease(self, prefix: str) -> Optional[str]:
        for iface in ["usb0", "usb1", "rndis0", "eth1", "bnep0", "pan0"]:
            ip = self._ip_for(iface)
            if ip and ip.startswith(prefix):
                return ip
        try:
            out = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True, timeout=1)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    parts = line.split()
                    if parts and parts[0].startswith(prefix):
                        return parts[0]
        except Exception:
            pass
        return None

    def _bt_connected_device(self) -> Optional[str]:
        try:
            out = subprocess.run(["bluetoothctl", "info"], capture_output=True, text=True, timeout=1)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("Name:"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return None

    # ---------------------- GPS ----------------------
    def _gps_status(self) -> Dict[str, Any]:
        gps_enabled = self._cfg_get("gps_enabled", False)
        if not gps_enabled:
            return {"connected": False, "status": "Not configured"}
        gps_device = self._cfg_get("gps_device", "/dev/ttyUSB0")
        if not os.path.exists(gps_device):
            return {"connected": False, "status": "Device not found"}
        try:
            import gpsd
            gpsd.connect()
            packet = gpsd.get_current()
            return {
                "connected": True,
                "fix_quality": packet.mode,
                "sats": packet.sats,
                "lat": round(packet.lat, 6) if packet.lat else None,
                "lon": round(packet.lon, 6) if packet.lon else None,
                "alt": round(packet.alt, 1) if packet.alt else None,
                "speed": round(packet.hspeed, 1) if packet.hspeed else None,
            }
        except Exception:
            return {"connected": True, "status": "No fix", "fix_quality": 0}

    # ---------------------- Stats DB (fallback) ----------------------
    def _count_open_ports_total_db(self) -> int:
        try:
            row = self.db.query_one("SELECT COUNT(*) AS c FROM ports WHERE state='open';")
            return int(row["c"]) if row else 0
        except Exception:
            return 0

    def _cpu_pct(self) -> int:
        # OPTIMIZATION: Use shared_data from display loop to avoid blocking 0.5s
        # Old method:
        # try:
        #     return int(psutil.cpu_percent(interval=0.5))
        # except Exception:
        #     return 0
        return int(getattr(self.shared_data, "system_cpu", 0))

    def _mem_bytes(self) -> Tuple[int, int]:
        # OPTIMIZATION: Use shared_data from display loop
        # Old method:
        # try:
        #     vm = psutil.virtual_memory()
        #     return int(vm.total - vm.available), int(vm.total)
        # except Exception:
        #     try:
        #         info = self._read_text("/proc/meminfo") or ""
        #         def kb(k):
        #             line = next((l for l in info.splitlines() if l.startswith(k + ":")), None)
        #             return int(line.split()[1]) * 1024 if line else 0
        #         total = kb("MemTotal")
        #         free = kb("MemFree") + kb("Buffers") + kb("Cached")
        #         used = max(0, total - free)
        #         return used, total
        #     except Exception:
        #         return 0, 0
        return int(getattr(self.shared_data, "system_mem_used", 0)), int(getattr(self.shared_data, "system_mem_total", 0))

    def _disk_bytes(self) -> Tuple[int, int]:
        try:
            usage = psutil.disk_usage("/")
            return int(usage.used), int(usage.total)
        except Exception:
            try:
                st = os.statvfs("/")
                total = st.f_frsize * st.f_blocks
                free = st.f_frsize * st.f_bavail
                return int(total - free), int(total)
            except Exception:
                return 0, 0

    def _alive_hosts_db(self) -> Tuple[int, int]:
        try:
            row = self.db.query_one(
                """
                SELECT
                    SUM(CASE WHEN alive=1 THEN 1 ELSE 0 END) AS alive,
                    COUNT(*) AS total
                FROM hosts
                """
            )
            if row:
                return int(row["alive"] or 0), int(row["total"] or 0)
        except Exception:
            pass
        return 0, 0

    def _vulns_total_db(self) -> int:
        try:
            row = self.db.query_one("SELECT COUNT(*) AS c FROM vulnerabilities WHERE is_active=1;")
            return int(row["c"]) if row else 0
        except Exception:
            return 0

    def _credentials_count_db(self) -> int:
        try:
            row = self.db.query_one("SELECT COUNT(*) AS c FROM creds;")
            return int(row["c"]) if row else 0
        except Exception:
            return 0

    def _files_count_fs(self) -> int:
        try:
            data_dir = "/home/bjorn/Bjorn/data/output/data_stolen"
            if os.path.exists(data_dir):
                return sum(len(files) for _, _, files in os.walk(data_dir))
            return 0
        except Exception:
            return 0

    def _scripts_count_db(self) -> int:
        try:
            row = self.db.query_one("SELECT COUNT(*) AS c FROM scripts;")
            return int(row["c"]) if row else 0
        except Exception:
            return 0

    def _zombies_count_db(self) -> int:
        try:
            row = self.db.query_one("SELECT COALESCE(zombie_count, 0) AS c FROM stats WHERE id=1;")
            if row and row.get("c") is not None:
                return int(row["c"])
        except Exception:
            pass
        try:
            row = self.db.query_one("SELECT COUNT(*) AS c FROM agents WHERE LOWER(status)='online';")
            return int(row["c"]) if row else 0
        except Exception:
            return 0

    # ---------------------- Known WiFi (NM profiles) ----------------------
    def _run_nmcli(self, args: list[str], timeout: float = 4.0) -> Optional[str]:
        import shutil, os as _os
        nmcli_path = shutil.which("nmcli") or "/usr/bin/nmcli"
        env = _os.environ.copy()
        env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        env.setdefault("DBUS_SYSTEM_BUS_ADDRESS", "unix:path=/run/dbus/system_bus_socket")
        env.setdefault("LC_ALL", "C")

        try:
            out = subprocess.run(
                [nmcli_path, *args],
                capture_output=True, text=True, timeout=timeout, env=env
            )
            if out.returncode == 0:
                stdout = out.stdout or ""
                # self.logger.debug(f"nmcli ok args={args} -> {len(stdout.splitlines())} lines")
                return stdout
            # self.logger.debug(f"nmcli rc={out.returncode} args={args} stderr={(out.stderr or '').strip()}")
            return None
        except FileNotFoundError:
            # self.logger.debug("nmcli not found")
            return None
        except Exception as e:
            # self.logger.debug(f"nmcli exception {args}: {e}")
            return None

    def _known_wifi_count_nmcli(self) -> int:
        # Try 1: simple (one value per line)
        out = self._run_nmcli(["-t", "-g", "TYPE", "connection", "show"])
        if out:
            cnt = sum(1 for line in out.splitlines()
                      if (line or "").strip().lower() in ("wifi", "802-11-wireless"))
            if cnt > 0:
                # self.logger.debug(f"known wifi via nmcli TYPE = {cnt}")
                return cnt

        # Try 2: NAME,TYPE
        out = self._run_nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"])
        if out:
            cnt = 0
            for line in out.splitlines():
                line = (line or "").strip()
                if not line:
                    continue
                typ = line.rsplit(":", 1)[-1].strip().lower()
                if typ in ("wifi", "802-11-wireless"):
                    cnt += 1
            if cnt > 0:
                # self.logger.debug(f"known wifi via nmcli NAME,TYPE = {cnt}")
                return cnt

        # Try 3: connection.type
        out = self._run_nmcli(["-t", "-g", "connection.type", "connection", "show"])
        if out:
            cnt = sum(1 for line in out.splitlines()
                      if (line or "").strip().lower() in ("wifi", "802-11-wireless"))
            if cnt > 0:
                # self.logger.debug(f"known wifi via nmcli connection.type = {cnt}")
                return cnt

        # Fallback: wpa_supplicant.conf
        try:
            conf = self._read_text("/etc/wpa_supplicant/wpa_supplicant.conf") or ""
            if conf:
                blocks = conf.count("\nnetwork={")
                if conf.strip().startswith("network={"):
                    blocks += 1
                if blocks > 0:
                    # self.logger.debug(f"known wifi via wpa_supplicant.conf = {blocks}")
                    return blocks
        except Exception:
            pass

        # Last resort: persisted config value
        v = self._cfg_get("wardrive_known", 0)
        # self.logger.debug(f"known wifi via cfg fallback = {v}")
        return int(v) if isinstance(v, (int, float)) else 0

    # Wardrive cache: in-memory (per-process) + DB (shared across workers)
    def _wardrive_known_cached(self) -> int:
        now = time.time()

        # 1) in-memory cache
        if self._wardrive_cache_mem is not None and (now - self._wardrive_ts_mem) < self._wardrive_ttl:
            return int(self._wardrive_cache_mem)

        # 2) shared DB cache
        try:
            row = self.db.query_one("SELECT value FROM config WHERE key='wardrive_cache' LIMIT 1;")
            if row and row.get("value"):
                d = json.loads(row["value"])
                ts = float(d.get("ts", 0))
                if now - ts < self._wardrive_ttl:
                    val = int(d.get("val", 0))
                    self._wardrive_cache_mem = val
                    self._wardrive_ts_mem = now
                    return val
        except Exception:
            pass

        # 3) refresh if needed
        val = int(self._known_wifi_count_nmcli())

        # update caches
        self._wardrive_cache_mem = val
        self._wardrive_ts_mem = now
        self._cfg_set("wardrive_cache", {"val": val, "ts": now})

        return val

    # ---------------------- Direct shared_data access ----------------------
    def _count_open_ports_total(self) -> int:
        try:
            val = int(getattr(self.shared_data, "port_count", -1))
            return val if val >= 0 else self._count_open_ports_total_db()
        except Exception:
            return self._count_open_ports_total_db()

    def _alive_hosts(self) -> Tuple[int, int]:
        try:
            alive = int(getattr(self.shared_data, "target_count", -1))
            total = int(getattr(self.shared_data, "network_kb_count", -1))
            if alive >= 0 and total >= 0:
                return alive, total
        except Exception:
            pass
        return self._alive_hosts_db()

    def _vulns_total(self) -> int:
        try:
            val = int(getattr(self.shared_data, "vuln_count", -1))
            return val if val >= 0 else self._vulns_total_db()
        except Exception:
            return self._vulns_total_db()

    def _credentials_count(self) -> int:
        try:
            val = int(getattr(self.shared_data, "cred_count", -1))
            return val if val >= 0 else self._credentials_count_db()
        except Exception:
            return self._credentials_count_db()

    def _files_count(self) -> int:
        try:
            val = int(getattr(self.shared_data, "data_count", -1))
            return val if val >= 0 else self._files_count_fs()
        except Exception:
            return self._files_count_fs()

    def _scripts_count(self) -> int:
        try:
            val = int(getattr(self.shared_data, "attacks_count", -1))
            return val if val >= 0 else self._scripts_count_db()
        except Exception:
            return self._scripts_count_db()

    def _zombies_count(self) -> int:
        try:
            val = int(getattr(self.shared_data, "zombie_count", -1))
            return val if val >= 0 else self._zombies_count_db()
        except Exception:
            return self._zombies_count_db()

    def level_bjorn(self) -> int:
        try:
            val = int(getattr(self.shared_data, "level_count", -1))
            return val if val >= 0 else int(self._cfg_get("level_count", 1))
        except Exception:
            return int(self._cfg_get("level_count", 1))

    def _mode_str(self) -> str:
        try:
            manual = bool(getattr(self.shared_data, "manual_mode", False))
            return "MANUAL" if manual else "AUTO"
        except Exception:
            return str(self._cfg_get("bjorn_mode", "AUTO")).upper()

    # ---------------------- Vuln delta since last scan ----------------------
    def _vulns_delta(self) -> int:
        last_scan_ts = self._cfg_get("vuln_last_scan_ts")
        if not last_scan_ts:
            return 0
        try:
            row = self.db.query_one(
                """
                SELECT COUNT(*) AS c
                FROM vulnerability_history
                WHERE event='new'
                AND CAST(strftime('%s', seen_at) AS INTEGER) >= ?
                """,
                (int(last_scan_ts),),
            )
            new_count = int(row["c"]) if row else 0
            row = self.db.query_one(
                """
                SELECT COUNT(*) AS c
                FROM vulnerability_history
                WHERE event='inactive'
                AND CAST(strftime('%s', seen_at) AS INTEGER) >= ?
                """,
                (int(last_scan_ts),),
            )
            removed_count = int(row["c"]) if row else 0
            return new_count - removed_count
        except Exception:
            return 0

    # ---------------------- Main stats assembly ----------------------
    def _assemble_stats(self) -> Dict[str, Any]:
        now = time.time()
        if self._last_stats and (now - self._last_stats_ts) < self._cache_ttl:
            return self._last_stats

        try:
            # Comptages rapides via shared_data (+ fallback DB)
            alive, total = self._alive_hosts()
            open_ports_total = self._count_open_ports_total()
            vulns_total = self._vulns_total()
            vulns_delta = self._vulns_delta()
            creds = self._credentials_count()
            zombies = self._zombies_count()
            files_count = self._files_count()
            scripts_count = self._scripts_count()
            wardrive = self._wardrive_known_cached()

            # System
            sys_info = self._get_system_info()
            uptime = self._uptime_str()
            first_init = self._first_init_ts()

            # Meta Bjorn
            bjorn_level = self.level_bjorn()

            # Ressources
            cpu_pct = self._cpu_pct()
            ram_used, ram_total = self._mem_bytes()
            sto_used, sto_total = self._disk_bytes()

            # Batterie
            batt = self._battery_probe()

            # Network
            internet_ok = self._quick_internet()
            gw, dns = self._gw_dns()
            wifi_ip = self._ip_for("wlan0")
            wifi_ssid = self._wifi_ssid() if wifi_ip else None
            eth_ip = self._ip_for("eth0")
            wifi_radio = self._wifi_radio_on()
            bt_radio   = self._bt_radio_on()
            eth_link   = self._eth_link_up("eth0")
            usb_phys   = self._usb_gadget_active()

            # USB/BT gadgets
            usb_lease = self._detect_gadget_lease("172.20.1.")
            bt_lease = self._detect_gadget_lease("172.20.2.")
            bt_device = self._bt_connected_device() if bt_lease else None

            # GPS
            gps_data = self._gps_status()

            # Mode
            mode = self._mode_str()

            # FDs
            fds_count = self._open_fds_count()
            fds_used, fds_limit = self._fds_usage()

            stats = {
                "timestamp": int(time.time()),
                "first_init_ts": int(first_init),
                "mode": mode,
                "uptime": uptime,
                "bjorn_level": bjorn_level,
                "internet_access": bool(internet_ok),

                # Hosts & ports
                "known_hosts_total": int(total),
                "alive_hosts": int(alive),
                "open_ports_alive_total": int(open_ports_total),

                # Security counters
                "wardrive_known": int(wardrive),
                "vulnerabilities": int(vulns_total),
                "vulns_delta": int(vulns_delta),
                "attack_scripts": int(scripts_count),
                "zombies": int(zombies),
                "credentials": int(creds),
                "files_found": int(files_count),

                "system": {
                    "os_name": sys_info["os_name"],
                    "os_version": sys_info["os_version"],
                    "arch": sys_info["arch"],
                    "model": sys_info["model"],
                    "waveshare_epd_connected": sys_info["waveshare_epd_connected"],
                    "waveshare_epd_type": sys_info["waveshare_epd_type"],
                    "cpu_pct": int(cpu_pct),
                    "ram_used_bytes": int(ram_used),
                    "ram_total_bytes": int(ram_total),
                    "storage_used_bytes": int(sto_used),
                    "storage_total_bytes": int(sto_total),
                    "open_fds": int(fds_used),        
                    "fds_limit": int(fds_limit),     
                    "fds_global": int(fds_count),   

                },

                "gps": {**gps_data},
                "battery": {**batt},

                "connectivity": {
                    "wifi": bool(wifi_ip),
                    "wifi_radio_on": bool(wifi_radio),        # <--- NEW
                    "wifi_ssid": wifi_ssid,
                    "wifi_ip": wifi_ip,
                    "wifi_gw": gw if wifi_ip else None,
                    "wifi_dns": dns if wifi_ip else None,

                    "ethernet": bool(eth_ip),
                    "eth_link_up": bool(eth_link),            # <--- NEW
                    "eth_ip": eth_ip,
                    "eth_gw": gw if eth_ip else None,
                    "eth_dns": dns if eth_ip else None,

                    "usb_gadget": bool(usb_lease),
                    "usb_phys_on": bool(usb_phys),            # <--- NEW (radio/phys)
                    "usb_lease_ip": usb_lease,
                    "usb_mode": self._get_usb_mode(),

                    "bt_gadget": bool(bt_lease),
                    "bt_radio_on": bool(bt_radio),            # <--- NEW
                    "bt_lease_ip": bt_lease,
                    "bt_connected_to": bt_device,
                },

            }

            self._last_stats = stats
            self._last_stats_ts = now
            return stats

        except Exception as e:
            if hasattr(self.logger, "error"):
                self.logger.error(f"Error assembling stats: {e}")
            if self._last_stats:
                return self._last_stats
            return self._get_fallback_stats()

    def _get_usb_mode(self) -> str:
        try:
            udc = self._read_text("/sys/kernel/config/usb_gadget/g1/UDC")
            if udc:
                return "OTG"
        except Exception:
            pass
        return "Device"

    def _get_fallback_stats(self) -> Dict[str, Any]:
        return {
            "timestamp": int(time.time()),
            "status": "error",
            "message": "Stats collection error - using fallback",
            "alive_hosts": 0,
            "known_hosts_total": 0,
            "open_ports_alive_total": 0,
            "vulnerabilities": 0,
            "internet_access": False,
            "system": {
                "os_name": "Unknown",
                "cpu_pct": 0,
                "ram_used_bytes": 0,
                "ram_total_bytes": 0,
                "storage_used_bytes": 0,
                "storage_total_bytes": 0,
            },
            "connectivity": {"wifi": False, "ethernet": False, "usb_gadget": False, "bt_gadget": False},
            "gps": {"connected": False},
            "battery": {"present": False},
        }

    # ---------------------- REST ----------------------
    def dashboard_stats(self, handler):
        try:
            stats = self._assemble_stats()
            return self._json(handler, 200, stats)
        except Exception as e:
            if hasattr(self.logger, "error"):
                self.logger.error(f"/api/bjorn/stats error: {e}")
                self.logger.error("Serving cached stats after error")
            if self._last_stats:
                return self._json(handler, 200, self._last_stats)
            return self._json(
                handler,
                500,
                {"status": "error", "message": str(e), "fallback": self._get_fallback_stats()},
            )

    def set_config(self, handler, data: Dict[str, Any]):
        key = (data.get("key") or "").strip()
        if not key:
            return self._json(handler, 400, {"status": "error", "message": "key required"})
        try:
            self._cfg_set(key, data.get("value"))
            if key in ["epd_type", "bjorn_mode", "gps_enabled"]:
                self._system_info_cache = {}
                self._last_stats = {}
            return self._json(handler, 200, {"status": "ok", "key": key})
        except Exception as e:
            return self._json(handler, 400, {"status": "error", "message": str(e)})

    def mark_vuln_scan_baseline(self, handler):
        now = int(time.time())
        self._cfg_set("vuln_last_scan_ts", now)
        return self._json(handler, 200, {"status": "ok", "vuln_last_scan_ts": now})



    def reload_generate_actions_json(self, handler):
        """Reload actions.json by running generate_actions_json."""
        try:
            self.shared_data.generate_actions_json()
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json; charset=utf-8')
            handler.end_headers()
            handler.wfile.write(json.dumps({'status': 'success', 'message': 'actions.json reloaded successfully.'}).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in reload_generate_actions_json: {e}")
            handler.send_response(500)
            handler.send_header('Content-Type', 'application/json; charset=utf-8')
            handler.end_headers()
            handler.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode('utf-8'))


    def clear_shared_config_json(self, handler, restart=True):
        """Reset config to defaults in DB."""
        try:
            self.shared_data.config = self.shared_data.get_default_config()
            self.shared_data.save_config()   # -> DB
            if restart:
                self.restart_bjorn_service(handler)
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status":"success","message":"Configuration reset to defaults"}).encode("utf-8"))
        except Exception as e:
            handler.send_response(500)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status":"error","message":str(e)}).encode("utf-8"))



    def serve_favicon(self, handler):
        handler.send_response(200)
        handler.send_header("Content-type", "image/x-icon")
        handler.end_headers()
        favicon_path = os.path.join(self.shared_data.web_dir, '/images/favicon.ico')
        self.logger.info(f"Serving favicon from {favicon_path}")
        try:
            with open(favicon_path, 'rb') as file:
                handler.wfile.write(file.read())
        except FileNotFoundError:
            self.logger.error(f"Favicon not found at {favicon_path}")
            handler.send_response(404)
            handler.end_headers()

    def serve_manifest(self, handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.end_headers()
        manifest_path = os.path.join(self.shared_data.web_dir, 'manifest.json')
        try:
            with open(manifest_path, 'r') as file:
                handler.wfile.write(file.read().encode('utf-8'))
        except FileNotFoundError:
            handler.send_response(404)
            handler.end_headers()
    
    def serve_apple_touch_icon(self, handler):
        handler.send_response(200)
        handler.send_header("Content-type", "image/png")
        handler.end_headers()
        icon_path = os.path.join(self.shared_data.web_dir, 'icons/apple-touch-icon.png')
        try:
            with open(icon_path, 'rb') as file:
                handler.wfile.write(file.read())
        except FileNotFoundError:
            handler.send_response(404)
            handler.end_headers()



    # --- Radio / link probes ---
    def _wifi_radio_on(self) -> bool:
        # nmcli (NetworkManager)
        try:
            out = subprocess.run(["nmcli", "radio", "wifi"], capture_output=True, text=True, timeout=1)
            if out.returncode == 0:
                return out.stdout.strip().lower().startswith("enabled")
        except Exception:
            pass
        # rfkill (fallback)
        try:
            out = subprocess.run(["rfkill", "list"], capture_output=True, text=True, timeout=1)
            if out.returncode == 0:
                block = out.stdout.lower()
                if "wireless" in block or "wlan" in block or "wifi" in block:
                    if "soft blocked: yes" in block or "hard blocked: yes" in block:
                        return False
                    return True
        except Exception:
            pass
        return False

    def _bt_radio_on(self) -> bool:
        import shutil, os as _os
        btctl = shutil.which("bluetoothctl") or "/usr/bin/bluetoothctl"
        env = _os.environ.copy()
        env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        # needed when running as a systemd service
        env.setdefault("DBUS_SYSTEM_BUS_ADDRESS", "unix:path=/run/dbus/system_bus_socket")

        try:
            out = subprocess.run([btctl, "show"], capture_output=True, text=True, timeout=1.2, env=env)
            if out.returncode == 0:
                txt = (out.stdout or "").lower()
                if "no default controller available" in txt:
                    # Try listing and targeting the first controller
                    ls = subprocess.run([btctl, "list"], capture_output=True, text=True, timeout=1.2, env=env)
                    if ls.returncode == 0:
                        for line in (ls.stdout or "").splitlines():
                            # ex: "Controller AA:BB:CC:DD:EE:FF host [default]"
                            if "controller " in line.lower():
                                mac = line.split()[1]
                                sh = subprocess.run([btctl, "-a", mac, "show"], capture_output=True, text=True, timeout=1.2, env=env)
                                if sh.returncode == 0 and "powered: yes" in (sh.stdout or "").lower():
                                    return True
                    return False
                # normal case
                if "powered: yes" in txt:
                    return True
        except Exception:
            pass

        # Fallback rfkill
        try:
            out = subprocess.run(["rfkill", "list"], capture_output=True, text=True, timeout=1.0, env=env)
            if out.returncode == 0:
                block = (out.stdout or "").lower()
                if "bluetooth" in block:
                    if "soft blocked: yes" in block or "hard blocked: yes" in block:
                        return False
                    return True
        except Exception:
            pass
        return False


    def _eth_link_up(self, ifname: str = "eth0") -> bool:
        # ip link show eth0
        try:
            out = subprocess.run(["ip", "link", "show", ifname], capture_output=True, text=True, timeout=1)
            if out.returncode == 0:
                # "state UP" ou "LOWER_UP"
                t = out.stdout.upper()
                return ("STATE UP" in t) or ("LOWER_UP" in t)
        except Exception:
            pass
        # ethtool fallback
        try:
            out = subprocess.run(["ethtool", ifname], capture_output=True, text=True, timeout=1)
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    if line.strip().lower().startswith("link detected:"):
                        return line.split(":",1)[1].strip().lower() == "yes"
        except Exception:
            pass
        return False

    def _usb_gadget_active(self) -> bool:
        # active if a UDC is attached
        try:
            udc = self._read_text("/sys/kernel/config/usb_gadget/g1/UDC")
            return bool(udc and udc.strip())
        except Exception:
            return False
