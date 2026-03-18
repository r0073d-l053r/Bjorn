"""scanning.py - Network scanner: host discovery, MAC/hostname resolution, and port scanning.

DB-first design - all results go straight to SQLite. RPi Zero optimized.
"""

import os
import re
import threading
import socket
import time
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime


import netifaces
from getmac import get_mac_address as gma
import ipaddress
import nmap

from logger import Logger

logger = Logger(name="scanning.py", level=logging.DEBUG)

b_class = "NetworkScanner"
b_module = "scanning"
b_status = "NetworkScanner"
b_port = None
b_parent = None
b_priority = 1
b_action = "global"
b_trigger = "on_interval:180"
b_requires = '{"max_concurrent": 1}'
b_enabled = 1
b_timeout = 300
b_max_retries = 1
b_stealth_level = 3
b_risk_level = "low"
b_tags = ["scan", "discovery", "network", "nmap"]
b_category = "recon"
b_name = "Network Scanner"
b_description = "Host discovery, MAC/hostname resolution, and port scanning via nmap."
b_author = "Bjorn Team"
b_version = "2.0.0"
b_icon = "NetworkScanner.png"

# --- Module-level constants (avoid re-creating per call) ---
_MAC_RE = re.compile(r'([0-9A-Fa-f]{2})([-:])(?:[0-9A-Fa-f]{2}\2){4}[0-9A-Fa-f]{2}')
_BAD_MACS = frozenset({"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"})

# RPi Zero safe defaults (overridable via shared config)
_MAX_HOST_THREADS = 2
_MAX_PORT_THREADS = 4
_PORT_TIMEOUT = 0.8
_MAC_RETRIES = 2
_MAC_RETRY_DELAY = 0.5
_ARPING_TIMEOUT = 1.0
_NMAP_DISCOVERY_TIMEOUT_S = 90
_NMAP_DISCOVERY_ARGS = "-sn -PR --max-retries 1 --host-timeout 8s"
_SCAN_MIN_INTERVAL_S = 600


def _normalize_mac(s):
    if not s:
        return None
    m = _MAC_RE.search(str(s))
    if not m:
        return None
    return m.group(0).replace('-', ':').lower()


def _is_bad_mac(mac):
    if not mac:
        return True
    mac_l = mac.lower()
    if mac_l in _BAD_MACS:
        return True
    parts = mac_l.split(':')
    if len(parts) == 6 and len(set(parts)) == 1:
        return True
    return False


class NetworkScanner:
    """
    Network scanner that populates SQLite (hosts + stats). No CSV/JSON.
    Uses ThreadPoolExecutor for bounded concurrency (RPi Zero safe).
    No 'IP:<ip>' stubs are ever written to the DB; unresolved IPs are tracked in-memory.
    """
    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.logger = logger
        self.blacklistcheck = shared_data.blacklistcheck
        self.mac_scan_blacklist = set(shared_data.mac_scan_blacklist or [])
        self.ip_scan_blacklist = set(shared_data.ip_scan_blacklist or [])
        self.hostname_scan_blacklist = set(shared_data.hostname_scan_blacklist or [])
        self.lock = threading.Lock()
        self.nm = nmap.PortScanner()
        self.running = False
        # Local stop flag for this action instance.
        # IMPORTANT: actions must never mutate shared_data.orchestrator_should_exit (global stop signal).
        self._stop_event = threading.Event()
        self.thread = None
        self.scan_interface = None

        cfg = getattr(self.shared_data, "config", {}) or {}
        self.max_host_threads = max(1, min(8, int(cfg.get("scan_max_host_threads", _MAX_HOST_THREADS))))
        self.max_port_threads = max(1, min(16, int(cfg.get("scan_max_port_threads", _MAX_PORT_THREADS))))
        self.port_timeout = max(0.3, min(3.0, float(cfg.get("scan_port_timeout_s", _PORT_TIMEOUT))))
        self.mac_retries = max(1, min(5, int(cfg.get("scan_mac_retries", _MAC_RETRIES))))
        self.mac_retry_delay = max(0.2, min(2.0, float(cfg.get("scan_mac_retry_delay_s", _MAC_RETRY_DELAY))))
        self.arping_timeout = max(1.0, min(5.0, float(cfg.get("scan_arping_timeout_s", _ARPING_TIMEOUT))))
        self.discovery_timeout_s = max(
            20, min(300, int(cfg.get("scan_nmap_discovery_timeout_s", _NMAP_DISCOVERY_TIMEOUT_S)))
        )
        self.discovery_args = str(cfg.get("scan_nmap_discovery_args", _NMAP_DISCOVERY_ARGS)).strip() or _NMAP_DISCOVERY_ARGS
        self.scan_min_interval_s = max(60, int(cfg.get("scan_min_interval_s", _SCAN_MIN_INTERVAL_S)))
        self._last_scan_started = 0.0

        # progress
        self.total_hosts = 0
        self.scanned_hosts = 0
        self.total_ports = 0
        self.scanned_ports = 0

    # ---------- progress ----------
    def update_progress(self, phase, increment=1):
        with self.lock:
            if phase == 'host':
                self.scanned_hosts += increment
                host_part = (self.scanned_hosts / self.total_hosts) * 50 if self.total_hosts else 0
                total = host_part
            elif phase == 'port':
                self.scanned_ports += increment
                port_part = (self.scanned_ports / self.total_ports) * 50 if self.total_ports else 0
                total = 50 + port_part
            else:
                total = 0
            total = min(max(total, 0), 100)
            self.shared_data.bjorn_progress = f"{int(total)}%"

    def _should_stop(self) -> bool:
        # Treat orchestrator flag as read-only, and combine with local stop event.
        return bool(getattr(self.shared_data, "orchestrator_should_exit", False)) or self._stop_event.is_set()

    # ---------- network ----------
    def get_network(self):
        if self._should_stop():
            return None
        try:
            if self.shared_data.use_custom_network:
                net = ipaddress.ip_network(self.shared_data.custom_network, strict=False)
                self.logger.info(f"Using custom network: {net}")
                return net

            interface = self.shared_data.default_network_interface
            if interface.startswith('bnep'):
                for alt in ['wlan0', 'eth0']:
                    if alt in netifaces.interfaces():
                        interface = alt
                        self.logger.info(f"Switching from bnep* to {interface}")
                        break

            addrs = netifaces.ifaddresses(interface)
            ip_info = addrs.get(netifaces.AF_INET)
            if not ip_info:
                self.logger.error(f"No IPv4 address found for interface {interface}.")
                return None

            ip_address = ip_info[0]['addr']
            netmask = ip_info[0]['netmask']
            network = ipaddress.IPv4Network(f"{ip_address}/{netmask}", strict=False)
            self.scan_interface = interface
            self.logger.info(f"Using network: {network} via {interface}")
            return network
        except Exception as e:
            self.logger.error(f"Error in get_network: {e}")
            return None

    # ---------- vendor / essid ----------
    def load_mac_vendor_map(self):
        vendor_map = {}
        path = self.shared_data.nmap_prefixes_file
        if not path or not os.path.exists(path):
            self.logger.debug(f"nmap_prefixes not found at {path}")
            return vendor_map
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        pref, vend = parts
                        vendor_map[pref.strip().upper()] = vend.strip()
        except Exception as e:
            self.logger.error(f"load_mac_vendor_map error: {e}")
        return vendor_map

    def mac_to_vendor(self, mac, vendor_map):
        if not mac or len(mac.split(':')) < 3:
            return ""
        pref = ''.join(mac.split(':')[:3]).upper()
        return vendor_map.get(pref, "")

    def get_current_essid(self):
        try:
            result = subprocess.run(
                ['iwgetid', '-r'],
                capture_output=True, text=True, timeout=5
            )
            return (result.stdout or "").strip()
        except Exception:
            return ""

    # ---------- hostname / mac ----------
    def validate_hostname(self, ip, hostname):
        if not hostname:
            return ""
        try:
            infos = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
            ips = {ai[4][0] for ai in infos}
            return hostname if ip in ips else ""
        except Exception:
            return ""

    def get_mac_address(self, ip, hostname):
        """
        Try multiple strategies to resolve a real MAC for the given IP.
        RETURNS: normalized MAC like 'aa:bb:cc:dd:ee:ff' or None.
        NEVER returns 'IP:<ip>'.
        RPi Zero: reduced retries and timeouts.
        """
        if self._should_stop():
            return None

        try:
            mac = None

            # 1) getmac (reduced retries for RPi Zero)
            retries = self.mac_retries
            while not mac and retries > 0 and not self._should_stop():
                try:
                    mac = _normalize_mac(gma(ip=ip))
                except Exception:
                    mac = None
                if not mac:
                    time.sleep(self.mac_retry_delay)
                    retries -= 1

            # 2) targeted arp-scan
            if not mac and not self._should_stop():
                try:
                    iface = self.scan_interface or self.shared_data.default_network_interface or "wlan0"
                    result = subprocess.run(
                        ['sudo', 'arp-scan', '--interface', iface, '-q', ip],
                        capture_output=True, text=True, timeout=5
                    )
                    out = result.stdout or ""
                    for line in out.splitlines():
                        if line.strip().startswith(ip):
                            cand = _normalize_mac(line)
                            if cand:
                                mac = cand
                                break
                    if not mac:
                        cand = _normalize_mac(out)
                        if cand:
                            mac = cand
                except Exception as e:
                    self.logger.debug(f"arp-scan fallback failed for {ip}: {e}")

            # 3) ip neigh
            if not mac and not self._should_stop():
                try:
                    result = subprocess.run(
                        ['ip', 'neigh', 'show', ip],
                        capture_output=True, text=True, timeout=3
                    )
                    cand = _normalize_mac(result.stdout or "")
                    if cand:
                        mac = cand
                except Exception:
                    pass

            # 4) filter invalid/broadcast
            if _is_bad_mac(mac):
                mac = None

            return mac

        except Exception as e:
            self.logger.error(f"Error in get_mac_address: {e}")
            return None

    # ---------- port scanning ----------
    class PortScannerWorker:
        """Port scanner using ThreadPoolExecutor for RPi Zero safety."""
        def __init__(self, outer, target, open_ports, portstart, portend, extra_ports):
            self.outer = outer
            self.target = target
            self.open_ports = open_ports
            self.portstart = int(portstart)
            self.portend = int(portend)
            self.extra_ports = [int(p) for p in (extra_ports or [])]

        def scan_one(self, port):
            if self.outer._should_stop():
                return
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.outer.port_timeout)
            try:
                s.connect((self.target, port))
                with self.outer.lock:
                    self.open_ports.setdefault(self.target, []).append(port)
            except Exception:
                pass
            finally:
                try:
                    s.close()
                except Exception:
                    pass
            self.outer.update_progress('port', 1)

        def run(self):
            if self.outer._should_stop():
                return
            ports = list(range(self.portstart, self.portend)) + self.extra_ports
            if not ports:
                return

            with ThreadPoolExecutor(max_workers=self.outer.max_port_threads) as pool:
                futures = []
                for port in ports:
                    if self.outer._should_stop():
                        break
                    futures.append(pool.submit(self.scan_one, port))
                for f in as_completed(futures):
                    if self.outer._should_stop():
                        break
                    try:
                        f.result(timeout=self.outer.port_timeout + 1)
                    except Exception:
                        pass

    # ---------- main scan block ----------
    class ScanPorts:
        class IpData:
            def __init__(self):
                self.ip_list = []
                self.hostname_list = []
                self.mac_list = []

        def __init__(self, outer, network, portstart, portend, extra_ports):
            self.outer = outer
            self.network = network
            self.portstart = int(portstart)
            self.portend = int(portend)
            self.extra_ports = [int(p) for p in (extra_ports or [])]
            self.ip_data = self.IpData()
            self.ip_hostname_list = []  # tuples (ip, hostname, mac)
            self.open_ports = {}
            self.all_ports = []

            # per-run pending cache for unresolved IPs (no DB writes)
            self.pending = {}

        def scan_network_and_collect(self):
            if self.outer._should_stop():
                return
            with self.outer.lock:
                self.outer.shared_data.bjorn_progress = "1%"
            t0 = time.time()
            try:
                self.outer.nm.scan(
                    hosts=str(self.network),
                    arguments=self.outer.discovery_args,
                    timeout=self.outer.discovery_timeout_s,
                )
            except Exception as e:
                self.outer.logger.error(f"Nmap host discovery failed: {e}")
                return

            hosts = list(self.outer.nm.all_hosts())
            if self.outer.blacklistcheck:
                hosts = [ip for ip in hosts if ip not in self.outer.ip_scan_blacklist]

            self.outer.total_hosts = len(hosts)
            self.outer.scanned_hosts = 0
            self.outer.update_progress('host', 0)

            elapsed = time.time() - t0
            self.outer.logger.info(f"Host discovery: {len(hosts)} candidate(s) (took {elapsed:.1f}s)")

            # Update comment for display
            self.outer.shared_data.comment_params = {
                "hosts_found": str(len(hosts)),
                "network": str(self.network),
                "elapsed": f"{elapsed:.1f}"
            }

            # existing hosts (for quick merge)
            try:
                existing_rows = self.outer.shared_data.db.get_all_hosts()
            except Exception as e:
                self.outer.logger.error(f"DB get_all_hosts failed: {e}")
                existing_rows = []
            self.existing_map = {h['mac_address']: h for h in existing_rows}
            self.seen_now = set()

            # vendor/essid
            self.vendor_map = self.outer.load_mac_vendor_map()
            self.essid = self.outer.get_current_essid()

            # per-host threads with bounded pool
            max_threads = min(self.outer.max_host_threads, len(hosts)) if hosts else 1
            with ThreadPoolExecutor(max_workers=max_threads) as pool:
                futures = {}
                for host in hosts:
                    if self.outer._should_stop():
                        break
                    f = pool.submit(self.scan_host, host)
                    futures[f] = host

                for f in as_completed(futures):
                    if self.outer._should_stop():
                        break
                    try:
                        f.result(timeout=30)
                    except Exception as e:
                        ip = futures.get(f, "?")
                        self.outer.logger.error(f"Host scan thread failed for {ip}: {e}")

            self.outer.logger.info(
                f"Host mapping completed: {self.outer.scanned_hosts}/{self.outer.total_hosts} processed, "
                f"{len(self.ip_hostname_list)} MAC(s) found, {len(self.pending)} unresolved IP(s)"
            )

            # mark unseen as alive=0
            existing_macs = set(self.existing_map.keys())
            for mac in existing_macs - self.seen_now:
                try:
                    self.outer.shared_data.db.update_host(mac_address=mac, alive=0)
                except Exception as e:
                    self.outer.logger.error(f"Failed to mark {mac} as dead: {e}")

            # feed ip_data
            for ip, hostname, mac in self.ip_hostname_list:
                self.ip_data.ip_list.append(ip)
                self.ip_data.hostname_list.append(hostname)
                self.ip_data.mac_list.append(mac)

        def scan_host(self, ip):
            if self.outer._should_stop():
                return
            if self.outer.blacklistcheck and ip in self.outer.ip_scan_blacklist:
                return
            try:
                # ARP ping to help populate neighbor cache (subprocess with timeout)
                try:
                    subprocess.run(
                        ['arping', '-c', '2', '-w', str(self.outer.arping_timeout), ip],
                        capture_output=True, timeout=self.outer.arping_timeout + 2
                    )
                except Exception:
                    pass

                # Hostname (validated)
                hostname = ""
                try:
                    hostname = self.outer.nm[ip].hostname()
                except Exception:
                    pass
                hostname = self.outer.validate_hostname(ip, hostname)

                if self.outer.blacklistcheck and hostname and hostname in self.outer.hostname_scan_blacklist:
                    self.outer.update_progress('host', 1)
                    return

                time.sleep(0.5)  # let ARP breathe (reduced from 1.0 for RPi Zero speed)

                mac = self.outer.get_mac_address(ip, hostname)
                if mac:
                    mac = mac.lower()

                if self.outer.blacklistcheck and mac in self.outer.mac_scan_blacklist:
                    self.outer.update_progress('host', 1)
                    return

                if not mac:
                    # No MAC -> keep it in-memory only (no DB writes)
                    slot = self.pending.setdefault(
                        ip,
                        {'hostnames': set(), 'ports': set(), 'first_seen': int(time.time()), 'essid': self.essid}
                    )
                    if hostname:
                        slot['hostnames'].add(hostname)
                    self.outer.logger.debug(f"Pending (no MAC yet): {ip} hostname={hostname or '-'}")
                else:
                    # MAC found -> write/update in DB
                    self.seen_now.add(mac)
                    vendor = self.outer.mac_to_vendor(mac, self.vendor_map)

                    prev = self.existing_map.get(mac)
                    ips_set, hosts_set, ports_set = set(), set(), set()

                    if prev:
                        if prev.get('ips'):
                            ips_set.update(p for p in prev['ips'].split(';') if p)
                        if prev.get('hostnames'):
                            hosts_set.update(h for h in prev['hostnames'].split(';') if h)
                        if prev.get('ports'):
                            ports_set.update(p for p in prev['ports'].split(';') if p)

                    if ip:
                        ips_set.add(ip)

                    current_hn = ""
                    if hostname:
                        try:
                            self.outer.shared_data.db.update_hostname(mac, hostname)
                        except Exception as e:
                            self.outer.logger.error(f"Failed to update hostname for {mac}: {e}")
                        current_hn = hostname
                    else:
                        current_hn = (prev.get('hostnames') or "").split(';', 1)[0] if prev else ""

                    ips_sorted = ';'.join(sorted(
                        ips_set,
                        key=lambda x: tuple(map(int, x.split('.'))) if x.count('.') == 3 else (0, 0, 0, 0)
                    )) if ips_set else None

                    try:
                        self.outer.shared_data.db.update_host(
                            mac_address=mac,
                            ips=ips_sorted,
                            hostnames=None,
                            alive=1,
                            ports=None,
                            vendor=vendor or (prev.get('vendor') if prev else ""),
                            essid=self.essid or (prev.get('essid') if prev else None)
                        )
                    except Exception as e:
                        self.outer.logger.error(f"Failed to update host {mac}: {e}")

                    # refresh local cache
                    self.existing_map[mac] = dict(
                        mac_address=mac,
                        ips=ips_sorted or (prev.get('ips') if prev else ""),
                        hostnames=current_hn or (prev.get('hostnames') if prev else ""),
                        alive=1,
                        ports=';'.join(sorted(ports_set)) if ports_set else (prev.get('ports') if prev else ""),
                        vendor=vendor or (prev.get('vendor') if prev else ""),
                        essid=self.essid or (prev.get('essid') if prev else "")
                    )

                    with self.outer.lock:
                        self.ip_hostname_list.append((ip, hostname or "", mac))

                    # Update comment params for live display
                    self.outer.shared_data.comment_params = {
                        "ip": ip, "mac": mac,
                        "hostname": hostname or "unknown",
                        "vendor": vendor or "unknown"
                    }
                    self.outer.logger.debug(f"MAC for {ip}: {mac} (hostname: {hostname or '-'})")

            except Exception as e:
                self.outer.logger.error(f"Error scanning host {ip}: {e}")
            finally:
                self.outer.update_progress('host', 1)
                time.sleep(0.02)  # reduced from 0.05

        def start(self):
            if self.outer._should_stop():
                return
            self.scan_network_and_collect()
            if self.outer._should_stop():
                return

            # init structures for ports
            self.open_ports = {ip: [] for ip in self.ip_data.ip_list}

            # port-scan summary
            total_targets = len(self.ip_data.ip_list)
            range_size = max(0, self.portend - self.portstart)
            self.outer.total_ports = total_targets * (range_size + len(self.extra_ports))
            self.outer.scanned_ports = 0
            self.outer.update_progress('port', 0)
            self.outer.logger.info(
                f"Port scan: {total_targets} host(s), range {self.portstart}-{self.portend-1} "
                f"(+{len(self.extra_ports)} extra)"
            )

            for idx, ip in enumerate(self.ip_data.ip_list, 1):
                if self.outer._should_stop():
                    return

                # Update comment params for live display
                self.outer.shared_data.comment_params = {
                    "ip": ip, "progress": f"{idx}/{total_targets}",
                    "ports_found": str(sum(len(v) for v in self.open_ports.values()))
                }

                worker = self.outer.PortScannerWorker(
                    self.outer, ip, self.open_ports,
                    self.portstart, self.portend, self.extra_ports
                )
                worker.run()

                if idx % 10 == 0 or idx == total_targets:
                    found = sum(len(v) for v in self.open_ports.values())
                    self.outer.logger.info(
                        f"Port scan progress: {idx}/{total_targets} hosts, {found} open ports so far"
                    )

            # unique list of open ports
            self.all_ports = sorted(list({p for plist in self.open_ports.values() for p in plist}))
            alive_macs = set(self.ip_data.mac_list)
            total_open = sum(len(v) for v in self.open_ports.values())
            self.outer.logger.info(f"Port scan done: {total_open} open ports across {total_targets} host(s)")
            return self.ip_data, self.open_ports, self.all_ports, alive_macs

    # ---------- orchestration ----------
    def scan(self):
        # Reset only local stop flag for this action. Never touch orchestrator_should_exit here.
        self._stop_event.clear()
        try:
            if self._should_stop():
                self.logger.info("Orchestrator switched to manual mode. Stopping scanner.")
                return

            now = time.time()
            elapsed = now - self._last_scan_started if self._last_scan_started else 1e9
            if elapsed < self.scan_min_interval_s:
                remaining = int(self.scan_min_interval_s - elapsed)
                self.logger.info_throttled(
                    f"Network scan skipped (min interval active, remaining={remaining}s)",
                    key="scanner_min_interval_skip",
                    interval_s=15.0,
                )
                return
            self._last_scan_started = now

            self.shared_data.bjorn_orch_status = "NetworkScanner"
            self.shared_data.comment_params = {}
            self.logger.info("Starting Network Scanner")

            # network
            network = self.get_network() if not self.shared_data.use_custom_network \
                else ipaddress.ip_network(self.shared_data.custom_network, strict=False)

            if network is None:
                self.logger.error("No network available. Aborting scan.")
                return

            self.shared_data.bjorn_status_text2 = str(network)
            self.shared_data.comment_params = {"network": str(network)}
            portstart = int(self.shared_data.portstart)
            portend = int(self.shared_data.portend)
            extra_ports = self.shared_data.portlist

            scanner = self.ScanPorts(self, network, portstart, portend, extra_ports)
            result = scanner.start()
            if result is None:
                self.logger.info("Scan interrupted (manual mode).")
                return

            ip_data, open_ports_by_ip, all_ports, alive_macs = result

            if self._should_stop():
                self.logger.info("Scan canceled before DB finalization.")
                return

            # push ports -> DB (merge by MAC)
            ip_to_mac = {ip: mac for ip, _, mac in zip(ip_data.ip_list, ip_data.hostname_list, ip_data.mac_list)}

            try:
                existing_map = {h['mac_address']: h for h in self.shared_data.db.get_all_hosts()}
            except Exception as e:
                self.logger.error(f"DB get_all_hosts for port merge failed: {e}")
                existing_map = {}

            for ip, ports in open_ports_by_ip.items():
                mac = ip_to_mac.get(ip)
                if not mac:
                    slot = scanner.pending.setdefault(
                        ip,
                        {'hostnames': set(), 'ports': set(), 'first_seen': int(time.time()), 'essid': scanner.essid}
                    )
                    slot['ports'].update(ports or [])
                    continue

                prev = existing_map.get(mac)
                ports_set = set()
                if prev and prev.get('ports'):
                    try:
                        ports_set.update([p for p in prev['ports'].split(';') if p])
                    except Exception:
                        pass
                ports_set.update(str(p) for p in (ports or []))

                try:
                    self.shared_data.db.update_host(
                        mac_address=mac,
                        ports=';'.join(sorted(ports_set, key=lambda x: int(x))),
                        alive=1
                    )
                except Exception as e:
                    self.logger.error(f"Failed to update ports for {mac}: {e}")

            # Late resolution pass
            unresolved_before = len(scanner.pending)
            for ip, data in list(scanner.pending.items()):
                if self._should_stop():
                    break
                try:
                    guess_hostname = next(iter(data['hostnames']), "")
                except Exception:
                    guess_hostname = ""
                mac = self.get_mac_address(ip, guess_hostname)
                if not mac:
                    continue

                mac = mac.lower()
                vendor = self.mac_to_vendor(mac, scanner.vendor_map)
                try:
                    self.shared_data.db.update_host(
                        mac_address=mac,
                        ips=ip,
                        hostnames=';'.join(data['hostnames']) or None,
                        vendor=vendor,
                        essid=data.get('essid'),
                        alive=1
                    )
                    if data['ports']:
                        self.shared_data.db.update_host(
                            mac_address=mac,
                            ports=';'.join(str(p) for p in sorted(data['ports'], key=int)),
                            alive=1
                        )
                except Exception as e:
                    self.logger.error(f"Failed to resolve pending IP {ip}: {e}")
                    continue
                del scanner.pending[ip]

            if scanner.pending:
                self.logger.info(
                    f"Unresolved IPs (kept in-memory only this run): {len(scanner.pending)} "
                    f"(resolved during late pass: {unresolved_before - len(scanner.pending)})"
                )

            # stats
            try:
                rows = self.shared_data.db.get_all_hosts()
            except Exception as e:
                self.logger.error(f"DB get_all_hosts for stats failed: {e}")
                rows = []

            alive_hosts = [r for r in rows if int(r.get('alive') or 0) == 1]
            all_known = len(rows)

            total_open_ports = 0
            for r in alive_hosts:
                ports_txt = r.get('ports') or ""
                if ports_txt:
                    try:
                        total_open_ports += len([p for p in ports_txt.split(';') if p])
                    except Exception:
                        pass

            try:
                vulnerabilities_count = self.shared_data.db.count_distinct_vulnerabilities(alive_only=True)
            except Exception:
                vulnerabilities_count = 0

            try:
                self.shared_data.db.set_stats(
                    total_open_ports=total_open_ports,
                    alive_hosts_count=len(alive_hosts),
                    all_known_hosts_count=all_known,
                    vulnerabilities_count=int(vulnerabilities_count)
                )
            except Exception as e:
                self.logger.error(f"Failed to set stats: {e}")

            # Update comment params with final stats
            self.shared_data.comment_params = {
                "alive_hosts": str(len(alive_hosts)),
                "total_ports": str(total_open_ports),
                "vulns": str(int(vulnerabilities_count)),
                "network": str(network)
            }

            # WAL checkpoint + optimize
            try:
                if hasattr(self.shared_data, "db") and hasattr(self.shared_data.db, "execute"):
                    self.shared_data.db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    self.shared_data.db.execute("PRAGMA optimize;")
                self.logger.debug("WAL checkpoint TRUNCATE + PRAGMA optimize executed.")
            except Exception as e:
                self.logger.debug(f"Checkpoint/optimize skipped or failed: {e}")

            self.shared_data.bjorn_progress = ""
            self.logger.info("Network scan complete (DB updated).")

        except Exception as e:
            if self._should_stop():
                self.logger.info("Orchestrator switched to manual mode. Gracefully stopping the network scanner.")
            else:
                self.logger.error(f"Error in scan: {e}")
        finally:
            with self.lock:
                self.shared_data.bjorn_progress = ""

    # ---------- thread wrapper ----------
    def start(self):
        if not self.running:
            self.running = True
            self._stop_event.clear()
            # Non-daemon so orchestrator can join it reliably (no orphan thread).
            self.thread = threading.Thread(target=self.scan_wrapper, daemon=False)
            self.thread.start()
            logger.info("NetworkScanner started.")

    def scan_wrapper(self):
        try:
            self.scan()
        finally:
            with self.lock:
                self.shared_data.bjorn_progress = ""
                self.running = False
                logger.debug("bjorn_progress reset to empty string")

    def stop(self):
        if self.running:
            self.running = False
            self._stop_event.set()
            try:
                if hasattr(self, "thread") and self.thread.is_alive():
                    self.thread.join(timeout=15)
            except Exception:
                pass
            logger.info("NetworkScanner stopped.")


if __name__ == "__main__":
    from shared import SharedData
    sd = SharedData()
    scanner = NetworkScanner(sd)
    scanner.scan()
