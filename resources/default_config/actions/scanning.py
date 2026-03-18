"""scanning.py - Network scanner: nmap host discovery, port scan, MAC resolve, all DB-backed."""

import os
import threading
import socket
import time
import logging
import subprocess
from datetime import datetime

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


class NetworkScanner:
    """
    Network scanner that populates SQLite (hosts + stats). No CSV/JSON.
    Keeps the original fast logic: nmap discovery, per-host threads, per-port threads.
    NEW: no 'IP:<ip>' stubs are ever written to the DB; unresolved IPs are tracked in-memory.
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
        self.scan_interface = None

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

    # ---------- network ----------
    def get_network(self):
        if self.shared_data.orchestrator_should_exit:
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
            with open(path, 'r') as f:
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
            essid = subprocess.check_output(['iwgetid', '-r'], stderr=subprocess.STDOUT, universal_newlines=True).strip()
            return essid or ""
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
        """
        if self.shared_data.orchestrator_should_exit:
            return None

        import re

        MAC_RE = re.compile(r'([0-9A-Fa-f]{2})([-:])(?:[0-9A-Fa-f]{2}\2){4}[0-9A-Fa-f]{2}')
        BAD_MACS = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}

        def _normalize_mac(s: str | None) -> str | None:
            if not s:
                return None
            m = MAC_RE.search(s)
            if not m:
                return None
            return m.group(0).replace('-', ':').lower()

        def _is_bad_mac(mac: str | None) -> bool:
            if not mac:
                return True
            mac_l = mac.lower()
            if mac_l in BAD_MACS:
                return True
            parts = mac_l.split(':')
            if len(parts) == 6 and len(set(parts)) == 1:
                return True
            return False

        try:
            mac = None

            # 1) getmac (retry a few times)
            retries = 6
            while not mac and retries > 0 and not self.shared_data.orchestrator_should_exit:
                try:
                    from getmac import get_mac_address as gma
                    mac = _normalize_mac(gma(ip=ip))
                except Exception:
                    mac = None
                if not mac:
                    time.sleep(1.5)
                    retries -= 1

            # 2) targeted arp-scan
            if not mac:
                try:
                    iface = self.scan_interface or self.shared_data.default_network_interface or "wlan0"
                    out = subprocess.check_output(
                        ['sudo', 'arp-scan', '--interface', iface, '-q', ip],
                        universal_newlines=True, stderr=subprocess.STDOUT
                    )
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
            if not mac:
                try:
                    neigh = subprocess.check_output(['ip', 'neigh', 'show', ip],
                                                    universal_newlines=True, stderr=subprocess.STDOUT)
                    cand = _normalize_mac(neigh)
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
        def __init__(self, outer, target, open_ports, portstart, portend, extra_ports):
            self.outer = outer
            self.target = target
            self.open_ports = open_ports
            self.portstart = int(portstart)
            self.portend = int(portend)
            self.extra_ports = [int(p) for p in (extra_ports or [])]

        def scan_one(self, port):
            if self.outer.shared_data.orchestrator_should_exit:
                return
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
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
            if self.outer.shared_data.orchestrator_should_exit:
                return
            threads = []
            for port in range(self.portstart, self.portend):
                if self.outer.shared_data.orchestrator_should_exit:
                    break
                t = threading.Thread(target=self.scan_one, args=(port,))
                t.start()
                threads.append(t)
            for port in self.extra_ports:
                if self.outer.shared_data.orchestrator_should_exit:
                    break
                t = threading.Thread(target=self.scan_one, args=(port,))
                t.start()
                threads.append(t)
            for t in threads:
                if self.outer.shared_data.orchestrator_should_exit:
                    break
                t.join()

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
            self.host_threads = []
            self.open_ports = {}
            self.all_ports = []

            # NEW: per-run pending cache for unresolved IPs (no DB writes)
            # ip -> {'hostnames': set(), 'ports': set(), 'first_seen': ts, 'essid': str}
            self.pending = {}

        def scan_network_and_collect(self):
            if self.outer.shared_data.orchestrator_should_exit:
                return

            t0 = time.time()
            self.outer.nm.scan(hosts=str(self.network), arguments='-sn -PR')
            hosts = list(self.outer.nm.all_hosts())
            if self.outer.blacklistcheck:
                hosts = [ip for ip in hosts if ip not in self.outer.ip_scan_blacklist]

            self.outer.total_hosts = len(hosts)
            self.outer.scanned_hosts = 0
            self.outer.update_progress('host', 0)
            self.outer.logger.info(f"Host discovery: {len(hosts)} candidate(s) (took {time.time()-t0:.1f}s)")

            # existing hosts (for quick merge)
            existing_rows = self.outer.shared_data.db.get_all_hosts()
            self.existing_map = {h['mac_address']: h for h in existing_rows}
            self.seen_now = set()

            # vendor/essid
            self.vendor_map = self.outer.load_mac_vendor_map()
            self.essid = self.outer.get_current_essid()

            # per-host threads
            for host in hosts:
                if self.outer.shared_data.orchestrator_should_exit:
                    return
                t = threading.Thread(target=self.scan_host, args=(host,))
                t.start()
                self.host_threads.append(t)

            # wait
            for t in self.host_threads:
                if self.outer.shared_data.orchestrator_should_exit:
                    return
                t.join()

            self.outer.logger.info(
                f"Host mapping completed: {self.outer.scanned_hosts}/{self.outer.total_hosts} processed, "
                f"{len(self.ip_hostname_list)} MAC(s) found, {len(self.pending)} unresolved IP(s)"
            )

            # mark unseen as alive=0
            existing_macs = set(self.existing_map.keys())
            for mac in existing_macs - self.seen_now:
                self.outer.shared_data.db.update_host(mac_address=mac, alive=0)

            # feed ip_data
            for ip, hostname, mac in self.ip_hostname_list:
                self.ip_data.ip_list.append(ip)
                self.ip_data.hostname_list.append(hostname)
                self.ip_data.mac_list.append(mac)

        def scan_host(self, ip):
            if self.outer.shared_data.orchestrator_should_exit:
                return
            if self.outer.blacklistcheck and ip in self.outer.ip_scan_blacklist:
                return
            try:
                # ARP ping to help populate neighbor cache
                os.system(f"arping -c 2 -w 2 {ip} > /dev/null 2>&1")

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

                time.sleep(1.0)  # let ARP breathe

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

                    # Update current hostname + track history
                    current_hn = ""
                    if hostname:
                        self.outer.shared_data.db.update_hostname(mac, hostname)
                        current_hn = hostname
                    else:
                        current_hn = (prev.get('hostnames') or "").split(';', 1)[0] if prev else ""

                    ips_sorted = ';'.join(sorted(
                        ips_set,
                        key=lambda x: tuple(map(int, x.split('.'))) if x.count('.') == 3 else (0, 0, 0, 0)
                    )) if ips_set else None

                    self.outer.shared_data.db.update_host(
                        mac_address=mac,
                        ips=ips_sorted,
                        hostnames=None,
                        alive=1,
                        ports=None,
                        vendor=vendor or (prev.get('vendor') if prev else ""),
                        essid=self.essid or (prev.get('essid') if prev else None)
                    )

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
                    self.outer.logger.debug(f"MAC for {ip}: {mac} (hostname: {hostname or '-'})")

            except Exception as e:
                self.outer.logger.error(f"Error scanning host {ip}: {e}")
            finally:
                self.outer.update_progress('host', 1)
                time.sleep(0.05)

        def start(self):
            if self.outer.shared_data.orchestrator_should_exit:
                return
            self.scan_network_and_collect()
            if self.outer.shared_data.orchestrator_should_exit:
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

            # per-IP port scan (threads per port, original logic)
            for idx, ip in enumerate(self.ip_data.ip_list, 1):
                if self.outer.shared_data.orchestrator_should_exit:
                    return
                worker = self.outer.PortScannerWorker(self.outer, ip, self.open_ports, self.portstart, self.portend, self.extra_ports)
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
        self.shared_data.orchestrator_should_exit = False
        try:
            if self.shared_data.orchestrator_should_exit:
                self.logger.info("Orchestrator switched to manual mode. Stopping scanner.")
                return

            self.shared_data.bjorn_orch_status = "NetworkScanner"
            self.logger.info("Starting Network Scanner")

            # network
            network = self.get_network() if not self.shared_data.use_custom_network \
                else ipaddress.ip_network(self.shared_data.custom_network, strict=False)

            if network is None:
                self.logger.error("No network available. Aborting scan.")
                return

            self.shared_data.bjorn_status_text2 = str(network)
            portstart = int(self.shared_data.portstart)
            portend = int(self.shared_data.portend)
            extra_ports = self.shared_data.portlist

            scanner = self.ScanPorts(self, network, portstart, portend, extra_ports)
            result = scanner.start()
            if result is None:
                self.logger.info("Scan interrupted (manual mode).")
                return

            ip_data, open_ports_by_ip, all_ports, alive_macs = result

            if self.shared_data.orchestrator_should_exit:
                self.logger.info("Scan canceled before DB finalization.")
                return

            # push ports -> DB (merge by MAC). Only for IPs with known MAC.
            # map ip->mac
            ip_to_mac = {ip: mac for ip, _, mac in zip(ip_data.ip_list, ip_data.hostname_list, ip_data.mac_list)}

            # existing cache
            existing_map = {h['mac_address']: h for h in self.shared_data.db.get_all_hosts()}

            for ip, ports in open_ports_by_ip.items():
                mac = ip_to_mac.get(ip)
                if not mac:
                    # store to pending (no DB write)
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

                self.shared_data.db.update_host(
                    mac_address=mac,
                    ports=';'.join(sorted(ports_set, key=lambda x: int(x))),
                    alive=1
                )

            # Late resolution pass: try to resolve pending IPs before stats
            unresolved_before = len(scanner.pending)
            for ip, data in list(scanner.pending.items()):
                if self.shared_data.orchestrator_should_exit:
                    break
                try:
                    guess_hostname = next(iter(data['hostnames']), "")
                except Exception:
                    guess_hostname = ""
                mac = self.get_mac_address(ip, guess_hostname)
                if not mac:
                    continue  # still unresolved for this run

                mac = mac.lower()
                vendor = self.mac_to_vendor(mac, scanner.vendor_map)
                # create/update host now
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
                del scanner.pending[ip]

            if scanner.pending:
                self.logger.info(
                    f"Unresolved IPs (kept in-memory only this run): {len(scanner.pending)} "
                    f"(resolved during late pass: {unresolved_before - len(scanner.pending)})"
                )

            # stats (alive, total ports, distinct vulnerabilities on alive)
            rows = self.shared_data.db.get_all_hosts()
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

            self.shared_data.db.set_stats(
                total_open_ports=total_open_ports,
                alive_hosts_count=len(alive_hosts),
                all_known_hosts_count=all_known,
                vulnerabilities_count=int(vulnerabilities_count)
            )

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
            if self.shared_data.orchestrator_should_exit:
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
            self.thread = threading.Thread(target=self.scan_wrapper, daemon=True)
            self.thread.start()
            logger.info("NetworkScanner started.")

    def scan_wrapper(self):
        try:
            self.scan()
        finally:
            with self.lock:
                self.shared_data.bjorn_progress = ""
                logger.debug("bjorn_progress reset to empty string")

    def stop(self):
        if self.running:
            self.running = False
            self.shared_data.orchestrator_should_exit = True
            try:
                if hasattr(self, "thread") and self.thread.is_alive():
                    self.thread.join()
            except Exception:
                pass
            logger.info("NetworkScanner stopped.")


if __name__ == "__main__":
    # SharedData must provide .db (BjornDatabase) and fields:
    # default_network_interface, use_custom_network, custom_network,
    # portstart, portend, portlist, blacklistcheck, mac/ip/hostname blacklists,
    # bjorn_progress, bjorn_orch_status, bjorn_status_text2, orchestrator_should_exit.
    from shared import SharedData
    sd = SharedData()
    scanner = NetworkScanner(sd)
    scanner.scan()
