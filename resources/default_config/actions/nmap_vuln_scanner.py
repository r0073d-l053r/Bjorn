"""nmap_vuln_scanner.py - CPE + CVE vulnerability scanning via nmap/vulners."""

import nmap
import json
import logging
from typing import Dict, List, Set, Any, Optional
from datetime import datetime, timedelta

from shared import SharedData
from logger import Logger

logger = Logger(name="NmapVulnScanner.py", level=logging.DEBUG)

# Scheduler parameters
b_class = "NmapVulnScanner"
b_module = "nmap_vuln_scanner"
b_status = "NmapVulnScanner"
b_port = None
b_parent = None
b_action = "normal"
b_service = []
b_trigger  = "on_port_change"
b_requires = '{"action":"NetworkScanner","status":"success","scope":"global"}'
b_priority = 11
b_cooldown   = 0            
b_enabled = 0
b_rate_limit = None



class NmapVulnScanner:
    """Vulnerability scanner via nmap (fast CPE/CVE mode)."""
    
    def __init__(self, shared_data: SharedData):
        self.shared_data = shared_data
        self.nm = nmap.PortScanner()
        logger.info("NmapVulnScanner initialized")

    # ---------------------------- Public API ---------------------------- #

    def execute(self, ip: str, port: str, row: Dict, status_key: str) -> str:
        try:
            logger.info(f"Starting vulnerability scan for {ip}")
            self.shared_data.bjorn_orch_status = "NmapVulnScanner"

            # 1) metadata from the queue
            meta = {}
            try:
                meta = json.loads(row.get('metadata') or '{}')
            except Exception:
                pass

            # 2) resolve ports (order: row -> metadata -> DB by MAC -> DB by IP)
            ports_str = (
                row.get("Ports") or row.get("ports") or
                meta.get("ports_snapshot") or ""
            )

            mac = (
                row.get("MAC Address") or row.get("mac_address") or
                ""
            )

            if not ports_str and mac:
                r = self.shared_data.db.query(
                    "SELECT ports FROM hosts WHERE mac_address=? LIMIT 1", (mac,)
                )
                if r and r[0].get('ports'):
                    ports_str = r[0]['ports']

            if not ports_str and ip:
                r = self.shared_data.db.query(
                    "SELECT mac_address, ports FROM hosts WHERE ips LIKE ? LIMIT 1",
                    (f"%{ip}%",)
                )
                if r:
                    mac = mac or r[0].get('mac_address') or mac
                    ports_str = r[0].get('ports') or ports_str

            if not ports_str:
                logger.warning(f"No ports to scan for {ip}")
                return 'failed'

            ports = [p.strip() for p in ports_str.split(';') if p.strip()]
            mac = mac or row.get("MAC Address") or ""

            # Skip already-scanned ports (unless TTL expired)
            ports = self._filter_ports_already_scanned(mac, ports)
            if not ports:
                logger.info(f"No new/changed ports to scan for {ip}")
                # Still touch statuses to deactivate stale entries
                self.save_vulnerabilities(mac, ip, [])
                return 'success'


            # Scan (fast mode by default)
            findings = self.scan_vulnerabilities(ip, ports)
            
            # Persistence (split CVE/CPE)
            self.save_vulnerabilities(mac, ip, findings)
            logger.success(f"Vuln scan done on {ip}: {len(findings)} entries")
            return 'success'
                
        except Exception as e:
            logger.error(f"NmapVulnScanner failed for {ip}: {e}")
            return 'failed'

    def _filter_ports_already_scanned(self, mac: str, ports: List[str]) -> List[str]:
        """
        Return ports to scan, excluding recently scanned ones.
        Config:
            vuln_rescan_on_change_only (bool, default True)
            vuln_rescan_ttl_seconds    (int, 0 = disabled)
        """
        if not ports:
            return []

        if not bool(self.shared_data.config.get('vuln_rescan_on_change_only', True)):
            return ports  # no filtering

        # Ports already covered by detected_software (is_active=1)
        rows = self.shared_data.db.query("""
            SELECT port, last_seen
            FROM detected_software
            WHERE mac_address=? AND is_active=1 AND port IS NOT NULL
        """, (mac,))
        seen = {}
        for r in rows:
            try:
                p = str(r['port'])
                ls = r.get('last_seen')
                seen[p] = ls
            except Exception:
                pass

        ttl = int(self.shared_data.config.get('vuln_rescan_ttl_seconds', 0) or 0)
        if ttl > 0:
            cutoff = datetime.utcnow() - timedelta(seconds=ttl)
            def fresh(port: str) -> bool:
                ls = seen.get(port)
                if not ls:
                    return False
                try:
                    dt = datetime.fromisoformat(ls.replace('Z',''))
                    return dt >= cutoff
                except Exception:
                    return True  # if in doubt, consider it fresh
            return [p for p in ports if (p not in seen) or (not fresh(p))]
        else:
            # No TTL: if already scanned/active => skip
            return [p for p in ports if p not in seen]

    # ---------------------------- Scanning ------------------------------ #

    def scan_vulnerabilities(self, ip: str, ports: List[str]) -> List[Dict]:
        """
        Fast mode (default):
          - nmap -sV --version-light on a reduced port set
          - CPE extracted directly from service detection
          - (optional) --script=vulners to extract CVE (if script installed)
        Fallback (vuln_fast=False): legacy mode with 'vuln' scripts, etc.
        """
        fast = bool(self.shared_data.config.get('vuln_fast', True))
        use_vulners = bool(self.shared_data.config.get('nse_vulners', False))
        max_ports = int(self.shared_data.config.get('vuln_max_ports', 10 if fast else 20))

        p_list = [str(p).split('/')[0] for p in ports if str(p).strip()]
        port_list = ','.join(p_list[:max_ports]) if p_list else ''

        if not port_list:
            logger.warning("No valid ports for scan")
            return []

        if fast:
            return self._scan_fast_cpe_cve(ip, port_list, use_vulners)
        else:
            return self._scan_heavy(ip, port_list)

    def _scan_fast_cpe_cve(self, ip: str, port_list: str, use_vulners: bool) -> List[Dict]:
        """Fast scan to extract CPE and (optionally) CVE via vulners."""
        vulns: List[Dict] = []

        args = "-sV --version-light -T4 --max-retries 1 --host-timeout 30s --script-timeout 10s"
        if use_vulners:
            args += " --script vulners --script-args mincvss=0.0"

        logger.info(f"[FAST] nmap {ip} -p {port_list} ({args})")
        try:
            self.nm.scan(hosts=ip, ports=port_list, arguments=args)
        except Exception as e:
            logger.error(f"Fast scan failed to start: {e}")
            return vulns

        if ip not in self.nm.all_hosts():
            return vulns

        host = self.nm[ip]

        for proto in host.all_protocols():
            for port in host[proto].keys():
                port_info = host[proto][port]
                service = port_info.get('name', '') or ''

                # 1) CPE from -sV
                cpe_values = self._extract_cpe_values(port_info)
                for cpe in cpe_values:
                    vulns.append({
                        'port': port,
                        'service': service,
                        'vuln_id': f"CPE:{cpe}",
                        'script': 'service-detect',
                        'details': f"CPE detected: {cpe}"[:500]
                    })

                # 2) CVE via 'vulners' script (if enabled)
                try:
                    script_out = (port_info.get('script') or {}).get('vulners')
                    if script_out:
                        for cve in self.extract_cves(script_out):
                            vulns.append({
                                'port': port,
                                'service': service,
                                'vuln_id': cve,
                                'script': 'vulners',
                                'details': str(script_out)[:500]
                            })
                except Exception:
                    pass

        return vulns

    def _scan_heavy(self, ip: str, port_list: str) -> List[Dict]:
        """Legacy strategy (slower) with vuln category scripts, etc."""
        vulnerabilities: List[Dict] = []
        vuln_scripts = [
            'vuln','exploit','http-vuln-*','smb-vuln-*',
            'ssl-*','ssh-*','ftp-vuln-*','mysql-vuln-*',
        ]
        script_arg = ','.join(vuln_scripts)

        args = f"-sV --script={script_arg} -T3 --script-timeout 20s"
        logger.info(f"[HEAVY] nmap {ip} -p {port_list} ({args})")
        try:
            self.nm.scan(hosts=ip, ports=port_list, arguments=args)
        except Exception as e:
            logger.error(f"Heavy scan failed to start: {e}")
            return vulnerabilities

        if ip in self.nm.all_hosts():
            host = self.nm[ip]
            discovered_ports: Set[str] = set()

            for proto in host.all_protocols():
                for port in host[proto].keys():
                    discovered_ports.add(str(port))
                    port_info = host[proto][port]
                    service = port_info.get('name', '') or ''

                    if 'script' in port_info:
                        for script_name, output in (port_info.get('script') or {}).items():
                            for cve in self.extract_cves(str(output)):
                                vulnerabilities.append({
                                    'port': port,
                                    'service': service,
                                    'vuln_id': cve,
                                    'script': script_name,
                                    'details': str(output)[:500]
                                })
                            if 'vuln' in (script_name or '') and not self.extract_cves(str(output)):
                                # Skip findings without CVE IDs
                                pass

            if bool(self.shared_data.config.get('scan_cpe', False)):
                ports_for_cpe = list(discovered_ports) if discovered_ports else port_list.split(',')
                cpes = self.scan_cpe(ip, ports_for_cpe[:10])
                vulnerabilities.extend(cpes)

        return vulnerabilities

    # ---------------------------- Helpers -------------------------------- #

    def _extract_cpe_values(self, port_info: Dict[str, Any]) -> List[str]:
        """Normalize all CPE formats returned by python-nmap."""
        cpe = port_info.get('cpe')
        if not cpe:
            return []
        if isinstance(cpe, str):
            parts = [x.strip() for x in cpe.splitlines() if x.strip()]
            return parts or [cpe]
        if isinstance(cpe, (list, tuple, set)):
            return [str(x).strip() for x in cpe if str(x).strip()]
        try:
            return [str(cpe).strip()] if str(cpe).strip() else []
        except Exception:
            return []

    def extract_cves(self, text: str) -> List[str]:
        """Extract CVE identifiers from text."""
        import re
        if not text:
            return []
        cve_pattern = r'CVE-\d{4}-\d{4,7}'
        return re.findall(cve_pattern, str(text), re.IGNORECASE)

    def scan_cpe(self, ip: str, ports: List[str]) -> List[Dict]:
        """(Heavy fallback) Detailed CPE scan if requested."""
        cpe_vulns: List[Dict] = []
        try:
            port_list = ','.join([str(p) for p in ports if str(p).strip()])
            if not port_list:
                return cpe_vulns

            args = "-sV --version-all -T3 --max-retries 2 --host-timeout 45s"
            logger.info(f"[CPE] nmap {ip} -p {port_list} ({args})")
            self.nm.scan(hosts=ip, ports=port_list, arguments=args)
            
            if ip in self.nm.all_hosts():
                host = self.nm[ip]
                for proto in host.all_protocols():
                    for port in host[proto].keys():
                        port_info = host[proto][port]
                        service = port_info.get('name', '') or ''
                        for cpe in self._extract_cpe_values(port_info):
                            cpe_vulns.append({
                                'port': port,
                                'service': service,
                                'vuln_id': f"CPE:{cpe}",
                                'script': 'version-scan',
                                'details': f"CPE detected: {cpe}"[:500]
                            })
        except Exception as e:
            logger.error(f"CPE scan error: {e}")
        return cpe_vulns
    
    # ---------------------------- Persistence ---------------------------- #

    def save_vulnerabilities(self, mac: str, ip: str, findings: List[Dict]):
        """Split CPE/CVE, update statuses, and persist new findings with full info."""

        # Fetch hostname from DB
        hostname = None
        try:
            host_row = self.shared_data.db.query_one(
                "SELECT hostnames FROM hosts WHERE mac_address=? LIMIT 1", 
                (mac,)
            )
            if host_row and host_row.get('hostnames'):
                hostname = host_row['hostnames'].split(';')[0]
        except Exception as e:
            logger.debug(f"Could not fetch hostname: {e}")
        
        # Group by port with full info
        findings_by_port = {}
        for f in findings:
            port = int(f.get('port', 0) or 0)
            
            if port not in findings_by_port:
                findings_by_port[port] = {
                    'cves': set(), 
                    'cpes': set(), 
                    'findings': []
                }
            
            findings_by_port[port]['findings'].append(f)
            
            vid = str(f.get('vuln_id', ''))
            if vid.upper().startswith('CVE-'):
                findings_by_port[port]['cves'].add(vid)
            elif vid.upper().startswith('CPE:'):
                findings_by_port[port]['cpes'].add(vid.split(':', 1)[1])
            elif vid.lower().startswith('cpe:'):
                findings_by_port[port]['cpes'].add(vid)

        # 1) Process CVEs by port
        for port, data in findings_by_port.items():
            if data['cves']:
                for cve in data['cves']:
                    try:
                        # Check if already exists
                        existing = self.shared_data.db.query_one(
                            "SELECT id FROM vulnerabilities WHERE mac_address=? AND vuln_id=? AND port=? LIMIT 1",
                            (mac, cve, port)
                        )
                        
                        if existing:
                            # Update with IP and hostname
                            self.shared_data.db.execute("""
                                UPDATE vulnerabilities 
                                SET ip=?, hostname=?, last_seen=CURRENT_TIMESTAMP, is_active=1
                                WHERE mac_address=? AND vuln_id=? AND port=?
                            """, (ip, hostname, mac, cve, port))
                        else:
                            # New entry with full info
                            self.shared_data.db.execute("""
                                INSERT INTO vulnerabilities(mac_address, ip, hostname, port, vuln_id, is_active)
                                VALUES(?,?,?,?,?,1)
                            """, (mac, ip, hostname, port, cve))
                        
                        logger.debug(f"Saved CVE {cve} for {ip}:{port}")
                        
                    except Exception as e:
                        logger.error(f"Failed to save CVE {cve}: {e}")

        # 2) Process CPEs
        for port, data in findings_by_port.items():
            for cpe in data['cpes']:
                try:
                    self.shared_data.db.add_detected_software(
                        mac_address=mac, 
                        cpe=cpe, 
                        ip=ip, 
                        hostname=hostname, 
                        port=port
                    )
                except Exception as e:
                    logger.error(f"Failed to save CPE {cpe}: {e}")

        logger.info(f"Saved vulnerabilities for {ip} ({mac}): {len(findings_by_port)} ports processed")