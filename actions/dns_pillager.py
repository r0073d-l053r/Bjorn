"""dns_pillager.py - DNS recon: reverse lookups, record enumeration, zone transfers, subdomain brute."""

import os
import json
import socket
import logging
import threading
import time
import datetime

from typing import Dict, List, Optional, Tuple, Set
from concurrent.futures import ThreadPoolExecutor, as_completed

from shared import SharedData
from logger import Logger

logger = Logger(name="dns_pillager.py", level=logging.DEBUG)

# ---------------------------------------------------------------------------
# Graceful import for dnspython (socket fallback if unavailable)
# ---------------------------------------------------------------------------
_HAS_DNSPYTHON = False
try:
    import dns.resolver
    import dns.zone
    import dns.query
    import dns.reversename
    import dns.rdatatype
    import dns.exception
    _HAS_DNSPYTHON = True
    logger.info("dnspython library loaded successfully.")
except ImportError:
    logger.warning(
        "dnspython not installed. DNS operations will use socket fallback "
        "(limited functionality). Install with: pip install dnspython"
    )

# ---------------------------------------------------------------------------
# Action metadata (AST-friendly, consumed by sync_actions / orchestrator)
# ---------------------------------------------------------------------------
b_class         = "DNSPillager"
b_module        = "dns_pillager"
b_status        = "dns_pillager"
b_port          = 53
b_service       = '["dns"]'
b_trigger       = 'on_any:["on_host_alive","on_new_port:53"]'
b_parent        = None
b_action        = "normal"
b_requires      = '{"action":"NetworkScanner","status":"success","scope":"global"}'
b_priority      = 20
b_cooldown      = 7200
b_rate_limit    = "5/86400"
b_timeout       = 300
b_max_retries   = 2
b_stealth_level = 7
b_risk_level    = "low"
b_enabled       = 1
b_tags          = ["dns", "recon", "enumeration"]

b_category      = "recon"
b_name          = "DNS Pillager"
b_description   = (
    "Comprehensive DNS reconnaissance and enumeration action. "
    "Performs reverse DNS, record enumeration (A/AAAA/MX/NS/TXT/CNAME/SOA/SRV/PTR), "
    "zone transfer attempts, and subdomain brute-force discovery. "
    "Requires: dnspython (pip install dnspython) for full functionality; "
    "falls back to socket-based lookups if unavailable."
)
b_author        = "Bjorn Team"
b_version       = "2.0.0"
b_icon          = "DNSPillager.png"

b_args = {
    "threads": {
        "type": "number",
        "label": "Subdomain Threads",
        "min": 1,
        "max": 50,
        "step": 1,
        "default": 10,
        "help": "Number of threads for subdomain brute-force enumeration."
    },
    "wordlist": {
        "type": "text",
        "label": "Subdomain Wordlist",
        "default": "",
        "placeholder": "/path/to/wordlist.txt",
        "help": "Path to a custom subdomain wordlist file. Leave empty for built-in list (~100 entries)."
    },
    "timeout": {
        "type": "number",
        "label": "DNS Query Timeout (s)",
        "min": 1,
        "max": 30,
        "step": 1,
        "default": 3,
        "help": "Timeout in seconds for individual DNS queries."
    },
    "enable_axfr": {
        "type": "checkbox",
        "label": "Attempt Zone Transfer (AXFR)",
        "default": True,
        "help": "Try AXFR zone transfers against discovered nameservers."
    },
    "enable_subdomains": {
        "type": "checkbox",
        "label": "Enable Subdomain Brute-Force",
        "default": True,
        "help": "Enumerate subdomains using wordlist."
    },
}

b_examples = [
    {"threads": 10, "wordlist": "", "timeout": 3, "enable_axfr": True, "enable_subdomains": True},
    {"threads": 5, "wordlist": "/home/bjorn/wordlists/subdomains.txt", "timeout": 5, "enable_axfr": False, "enable_subdomains": True},
]

b_docs_url = "docs/actions/DNSPillager.md"

# ---------------------------------------------------------------------------
# Data directories
# ---------------------------------------------------------------------------
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUTPUT_DIR = os.path.join(_DATA_DIR, "output", "dns")

# ---------------------------------------------------------------------------
# Built-in subdomain wordlist (~100 common entries)
# ---------------------------------------------------------------------------
BUILTIN_SUBDOMAINS = [
    "www", "mail", "ftp", "localhost", "webmail", "smtp", "pop", "ns1", "ns2",
    "ns3", "ns4", "dns", "dns1", "dns2", "mx", "mx1", "mx2", "imap", "pop3",
    "blog", "dev", "staging", "test", "testing", "beta", "alpha", "demo",
    "admin", "administrator", "panel", "cpanel", "webmin", "portal",
    "api", "api2", "api3", "gateway", "gw", "proxy", "cdn", "media",
    "static", "assets", "img", "images", "files", "download", "upload",
    "vpn", "remote", "ssh", "rdp", "citrix", "owa", "exchange",
    "db", "database", "mysql", "postgres", "sql", "mongodb", "redis", "elastic",
    "shop", "store", "app", "apps", "mobile", "m",
    "intranet", "extranet", "internal", "external", "private", "public",
    "cloud", "aws", "azure", "gcp", "s3", "storage",
    "git", "gitlab", "github", "svn", "repo", "ci", "cd", "jenkins", "build",
    "monitor", "monitoring", "grafana", "prometheus", "kibana", "nagios", "zabbix",
    "log", "logs", "syslog", "elk",
    "chat", "slack", "teams", "jira", "confluence", "wiki",
    "backup", "backups", "bak", "archive",
    "secure", "security", "sso", "auth", "login", "oauth",
    "docs", "doc", "help", "support", "kb", "status",
    "calendar", "crm", "erp", "hr",
    "web", "web1", "web2", "server", "server1", "server2",
    "host", "node", "worker", "master",
]

# DNS record types to enumerate
DNS_RECORD_TYPES = ["A", "AAAA", "MX", "NS", "TXT", "CNAME", "SOA", "SRV", "PTR"]


class DNSPillager:
    """
    DNS reconnaissance action for the Bjorn orchestrator.
    Performs reverse DNS, record enumeration, zone transfer attempts,
    and subdomain brute-force discovery.
    """

    def __init__(self, shared_data: SharedData):
        self.shared_data = shared_data

        # IP -> (MAC, hostname) identity cache from DB
        self._ip_to_identity: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self._refresh_ip_identity_cache()

        # DNS resolver setup (dnspython)
        self._resolver = None
        if _HAS_DNSPYTHON:
            self._resolver = dns.resolver.Resolver()
            self._resolver.timeout = 3
            self._resolver.lifetime = 5

        # Ensure output directory exists
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create output directory {OUTPUT_DIR}: {e}")

        # Thread safety
        self._lock = threading.Lock()

        logger.info("DNSPillager initialized (dnspython=%s)", _HAS_DNSPYTHON)

    # --------------------- Identity cache (hosts) ---------------------

    def _refresh_ip_identity_cache(self) -> None:
        """Rebuild IP -> (MAC, current_hostname) from DB.hosts."""
        self._ip_to_identity.clear()
        try:
            rows = self.shared_data.db.get_all_hosts()
        except Exception as e:
            logger.error(f"DB get_all_hosts failed: {e}")
            rows = []

        for r in rows:
            mac = r.get("mac_address") or ""
            if not mac:
                continue
            hostnames_txt = r.get("hostnames") or ""
            current_hn = hostnames_txt.split(';', 1)[0] if hostnames_txt else ""
            ips_txt = r.get("ips") or ""
            if not ips_txt:
                continue
            for ip_addr in [p.strip() for p in ips_txt.split(';') if p.strip()]:
                self._ip_to_identity[ip_addr] = (mac, current_hn)

    def _mac_for_ip(self, ip: str) -> Optional[str]:
        if ip not in self._ip_to_identity:
            self._refresh_ip_identity_cache()
        return self._ip_to_identity.get(ip, (None, None))[0]

    def _hostname_for_ip(self, ip: str) -> Optional[str]:
        if ip not in self._ip_to_identity:
            self._refresh_ip_identity_cache()
        return self._ip_to_identity.get(ip, (None, None))[1]

    # --------------------- Public API (Orchestrator) ---------------------

    def execute(self, ip: str, port: str, row: Dict, status_key: str) -> str:
        """
        Execute DNS reconnaissance on the given target.

        Args:
            ip: Target IP address
            port: Target port (typically 53)
            row: Row dict from orchestrator (contains MAC, hostname, etc.)
            status_key: Status tracking key

        Returns:
            'success' | 'failed' | 'interrupted'
        """
        self.shared_data.bjorn_orch_status = "DNSPillager"
        self.shared_data.bjorn_progress = "0%"
        self.shared_data.comment_params = {"ip": ip, "port": str(port), "phase": "init"}

        results = {
            "target_ip": ip,
            "port": str(port),
            "timestamp": datetime.datetime.now().isoformat(),
            "reverse_dns": None,
            "domain": None,
            "records": {},
            "zone_transfer": {},
            "subdomains": [],
            "errors": [],
        }

        try:
            # --- Check for early exit ---
            if self.shared_data.orchestrator_should_exit:
                logger.info("Orchestrator exit signal before start.")
                return "interrupted"

            mac = row.get("MAC Address") or row.get("mac_address") or self._mac_for_ip(ip) or ""
            hostname = (
                row.get("Hostname") or row.get("hostname")
                or self._hostname_for_ip(ip)
                or ""
            )

            # =========================================================
            # Phase 1: Reverse DNS lookup (0% -> 10%)
            # =========================================================
            self.shared_data.comment_params = {"ip": ip, "phase": "reverse_dns"}
            logger.info(f"[{ip}] Phase 1: Reverse DNS lookup")

            reverse_hostname = self._reverse_dns(ip)
            if reverse_hostname:
                results["reverse_dns"] = reverse_hostname
                logger.info(f"[{ip}] Reverse DNS: {reverse_hostname}")
                self.shared_data.log_milestone(b_class, "ReverseDNS", f"IP: {ip} -> {reverse_hostname}")
                # Update hostname if we found something new
                if not hostname or hostname == ip:
                    hostname = reverse_hostname
            else:
                logger.info(f"[{ip}] No reverse DNS result.")

            self.shared_data.bjorn_progress = "10%"

            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            # =========================================================
            # Phase 2: Extract domain and enumerate DNS records (10% -> 35%)
            # =========================================================
            domain = self._extract_domain(hostname)
            results["domain"] = domain

            if domain:
                self.shared_data.comment_params = {"ip": ip, "phase": "records", "domain": domain}
                logger.info(f"[{ip}] Phase 2: DNS record enumeration for {domain}")
                self.shared_data.log_milestone(b_class, "EnumerateRecords", f"Domain: {domain}")

                record_results = {}
                total_types = len(DNS_RECORD_TYPES)
                for idx, rtype in enumerate(DNS_RECORD_TYPES):
                    if self.shared_data.orchestrator_should_exit:
                        return "interrupted"

                    records = self._query_records(domain, rtype)
                    if records:
                        record_results[rtype] = records
                        logger.info(f"[{ip}] {rtype} records for {domain}: {records}")

                    # Progress: 10% -> 35% across record types
                    pct = 10 + int((idx + 1) / total_types * 25)
                    self.shared_data.bjorn_progress = f"{pct}%"

                results["records"] = record_results
            else:
                logger.warning(f"[{ip}] No domain could be extracted. Skipping record enumeration.")
                self.shared_data.bjorn_progress = "35%"

            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            # =========================================================
            # Phase 3: Zone transfer (AXFR) attempt (35% -> 45%)
            # =========================================================
            self.shared_data.bjorn_progress = "35%"
            self.shared_data.comment_params = {"ip": ip, "phase": "zone_transfer", "domain": domain or ip}

            if domain and _HAS_DNSPYTHON:
                logger.info(f"[{ip}] Phase 3: Zone transfer attempt for {domain}")
                nameservers = results["records"].get("NS", [])
                # Also try the target IP itself as a nameserver
                ns_targets = list(set(nameservers + [ip]))
                zone_results = {}

                for ns_idx, ns in enumerate(ns_targets):
                    if self.shared_data.orchestrator_should_exit:
                        return "interrupted"

                    axfr_records = self._attempt_zone_transfer(domain, ns)
                    if axfr_records:
                        zone_results[ns] = axfr_records
                        logger.success(f"[{ip}] Zone transfer SUCCESS from {ns}: {len(axfr_records)} records")
                        self.shared_data.log_milestone(b_class, "AXFRSuccess", f"NS: {ns} | Records: {len(axfr_records)}")

                    # Progress within 35% -> 45%
                    if ns_targets:
                        pct = 35 + int((ns_idx + 1) / len(ns_targets) * 10)
                        self.shared_data.bjorn_progress = f"{pct}%"

                results["zone_transfer"] = zone_results
            else:
                if not _HAS_DNSPYTHON:
                    results["errors"].append("Zone transfer skipped: dnspython not available")
                elif not domain:
                    results["errors"].append("Zone transfer skipped: no domain found")
                logger.info(f"[{ip}] Skipping zone transfer (dnspython={_HAS_DNSPYTHON}, domain={domain})")

            self.shared_data.bjorn_progress = "45%"

            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            # =========================================================
            # Phase 4: Subdomain brute-force (45% -> 95%)
            # =========================================================
            self.shared_data.comment_params = {"ip": ip, "phase": "subdomains", "domain": domain or ip}

            if domain:
                logger.info(f"[{ip}] Phase 4: Subdomain brute-force for {domain}")
                self.shared_data.log_milestone(b_class, "SubdomainEnum", f"Domain: {domain}")
                wordlist = self._load_wordlist()
                thread_count = min(10, max(1, len(wordlist)))

                discovered = self._enumerate_subdomains(domain, wordlist, thread_count)
                results["subdomains"] = discovered
                logger.info(f"[{ip}] Subdomain enumeration found {len(discovered)} live subdomains")
            else:
                logger.info(f"[{ip}] Skipping subdomain enumeration: no domain available")
                results["errors"].append("Subdomain enumeration skipped: no domain found")

            self.shared_data.bjorn_progress = "95%"

            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            # =========================================================
            # Phase 5: Save results and update DB (95% -> 100%)
            # =========================================================
            self.shared_data.comment_params = {"ip": ip, "phase": "saving"}
            logger.info(f"[{ip}] Phase 5: Saving results")

            # Save JSON output
            self._save_results(ip, results)

            # Update DB hostname if reverse DNS discovered new data
            if reverse_hostname and mac:
                self._update_db_hostname(mac, ip, reverse_hostname)

            self.shared_data.bjorn_progress = "100%"
            self.shared_data.log_milestone(b_class, "Complete", f"Records: {sum(len(v) for v in results['records'].values())} | Subdomains: {len(results['subdomains'])}")

            # Summary comment
            record_count = sum(len(v) for v in results["records"].values())
            zone_count = sum(len(v) for v in results["zone_transfer"].values())
            sub_count = len(results["subdomains"])
            self.shared_data.comment_params = {
                "ip": ip,
                "domain": domain or "N/A",
                "records": str(record_count),
                "zones": str(zone_count),
                "subdomains": str(sub_count),
            }

            logger.success(
                f"[{ip}] DNS Pillager complete: domain={domain}, "
                f"records={record_count}, zone_transfers={zone_count}, subdomains={sub_count}"
            )
            return "success"

        except Exception as e:
            logger.error(f"[{ip}] DNSPillager execute failed: {e}")
            results["errors"].append(str(e))
            # Still try to save partial results
            try:
                self._save_results(ip, results)
            except Exception:
                pass
            return "failed"

        finally:
            self.shared_data.bjorn_progress = ""

    # --------------------- Reverse DNS ---------------------

    def _reverse_dns(self, ip: str) -> Optional[str]:
        """Perform reverse DNS lookup on the IP address."""
        # Try dnspython first
        if _HAS_DNSPYTHON and self._resolver:
            try:
                rev_name = dns.reversename.from_address(ip)
                answers = self._resolver.resolve(rev_name, "PTR")
                for rdata in answers:
                    hostname = str(rdata).rstrip(".")
                    if hostname:
                        return hostname
            except Exception as e:
                logger.debug(f"dnspython reverse DNS failed for {ip}: {e}")

        # Socket fallback
        try:
            hostname, _, _ = socket.gethostbyaddr(ip)
            if hostname and hostname != ip:
                return hostname
        except (socket.herror, socket.gaierror, OSError) as e:
            logger.debug(f"Socket reverse DNS failed for {ip}: {e}")

        return None

    # --------------------- Domain extraction ---------------------

    @staticmethod
    def _extract_domain(hostname: str) -> Optional[str]:
        """
        Extract the registerable domain from a hostname.
        e.g., 'mail.sub.example.com' -> 'example.com'
             'host1.internal.lan' -> 'internal.lan'
             '192.168.1.1' -> None
        """
        if not hostname:
            return None

        # Skip raw IPs
        hostname = hostname.strip().rstrip(".")
        parts = hostname.split(".")
        if len(parts) < 2:
            return None

        # Check if it looks like an IP address
        try:
            socket.inet_aton(hostname)
            return None  # It's an IP, not a hostname
        except (socket.error, OSError):
            pass

        # For simple TLDs, take the last 2 parts
        # For compound TLDs (co.uk, com.au), take the last 3 parts
        compound_tlds = {
            "co.uk", "co.jp", "co.kr", "co.nz", "co.za", "co.in",
            "com.au", "com.br", "com.cn", "com.mx", "com.tw",
            "org.uk", "net.au", "ac.uk", "gov.uk",
        }
        if len(parts) >= 3:
            possible_compound = f"{parts[-2]}.{parts[-1]}"
            if possible_compound.lower() in compound_tlds:
                return ".".join(parts[-3:])

        return ".".join(parts[-2:])

    # --------------------- DNS record queries ---------------------

    def _query_records(self, domain: str, record_type: str) -> List[str]:
        """Query DNS records of a given type for a domain."""
        records = []

        # Try dnspython first
        if _HAS_DNSPYTHON and self._resolver:
            try:
                answers = self._resolver.resolve(domain, record_type)
                for rdata in answers:
                    value = str(rdata).rstrip(".")
                    if value:
                        records.append(value)
                return records
            except dns.resolver.NXDOMAIN:
                logger.debug(f"NXDOMAIN for {domain} {record_type}")
            except dns.resolver.NoAnswer:
                logger.debug(f"No answer for {domain} {record_type}")
            except dns.resolver.NoNameservers:
                logger.debug(f"No nameservers for {domain} {record_type}")
            except dns.exception.Timeout:
                logger.debug(f"Timeout querying {domain} {record_type}")
            except Exception as e:
                logger.debug(f"dnspython query failed for {domain} {record_type}: {e}")

        # Socket fallback (limited to A records only)
        if record_type == "A" and not records:
            try:
                ips = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
                for info in ips:
                    addr = info[4][0]
                    if addr and addr not in records:
                        records.append(addr)
            except (socket.gaierror, OSError) as e:
                logger.debug(f"Socket fallback failed for {domain} A: {e}")

        # Socket fallback for AAAA
        if record_type == "AAAA" and not records:
            try:
                ips = socket.getaddrinfo(domain, None, socket.AF_INET6, socket.SOCK_STREAM)
                for info in ips:
                    addr = info[4][0]
                    if addr and addr not in records:
                        records.append(addr)
            except (socket.gaierror, OSError) as e:
                logger.debug(f"Socket fallback failed for {domain} AAAA: {e}")

        return records

    # --------------------- Zone transfer (AXFR) ---------------------

    def _attempt_zone_transfer(self, domain: str, nameserver: str) -> List[Dict]:
        """
        Attempt an AXFR zone transfer from a nameserver.
        Returns a list of record dicts on success, empty list on failure.
        """
        if not _HAS_DNSPYTHON:
            return []

        records = []
        # Resolve NS hostname to IP if needed
        ns_ip = self._resolve_ns_to_ip(nameserver)
        if not ns_ip:
            logger.debug(f"Cannot resolve NS {nameserver} to IP, skipping AXFR")
            return []

        try:
            zone = dns.zone.from_xfr(
                dns.query.xfr(ns_ip, domain, timeout=10, lifetime=30)
            )
            for name, node in zone.nodes.items():
                for rdataset in node.rdatasets:
                    for rdata in rdataset:
                        records.append({
                            "name": str(name),
                            "type": dns.rdatatype.to_text(rdataset.rdtype),
                            "ttl": rdataset.ttl,
                            "value": str(rdata),
                        })
        except dns.exception.FormError:
            logger.debug(f"AXFR refused by {nameserver} ({ns_ip}) for {domain}")
        except dns.exception.Timeout:
            logger.debug(f"AXFR timeout from {nameserver} ({ns_ip}) for {domain}")
        except ConnectionError as e:
            logger.debug(f"AXFR connection error from {nameserver}: {e}")
        except OSError as e:
            logger.debug(f"AXFR OS error from {nameserver}: {e}")
        except Exception as e:
            logger.debug(f"AXFR failed from {nameserver} ({ns_ip}) for {domain}: {e}")

        return records

    def _resolve_ns_to_ip(self, nameserver: str) -> Optional[str]:
        """Resolve a nameserver hostname to an IP address."""
        ns = nameserver.strip().rstrip(".")

        # Check if already an IP
        try:
            socket.inet_aton(ns)
            return ns
        except (socket.error, OSError):
            pass

        # Try to resolve
        if _HAS_DNSPYTHON and self._resolver:
            try:
                answers = self._resolver.resolve(ns, "A")
                for rdata in answers:
                    return str(rdata)
            except Exception:
                pass

        # Socket fallback
        try:
            result = socket.getaddrinfo(ns, 53, socket.AF_INET, socket.SOCK_STREAM)
            if result:
                return result[0][4][0]
        except Exception:
            pass

        return None

    # --------------------- Subdomain enumeration ---------------------

    def _load_wordlist(self) -> List[str]:
        """Load subdomain wordlist from file or use built-in list."""
        # Check for configured wordlist path
        wordlist_path = ""
        if hasattr(self.shared_data, "config") and self.shared_data.config:
            wordlist_path = self.shared_data.config.get("dns_wordlist", "")

        if wordlist_path and os.path.isfile(wordlist_path):
            try:
                with open(wordlist_path, "r", encoding="utf-8", errors="ignore") as f:
                    words = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                if words:
                    logger.info(f"Loaded {len(words)} subdomains from {wordlist_path}")
                    return words
            except Exception as e:
                logger.error(f"Failed to load wordlist {wordlist_path}: {e}")

        logger.info(f"Using built-in subdomain wordlist ({len(BUILTIN_SUBDOMAINS)} entries)")
        return list(BUILTIN_SUBDOMAINS)

    def _enumerate_subdomains(
        self, domain: str, wordlist: List[str], thread_count: int
    ) -> List[Dict]:
        """
        Brute-force subdomain enumeration using ThreadPoolExecutor.
        Returns a list of discovered subdomain dicts.
        """
        discovered: List[Dict] = []
        total = len(wordlist)
        if total == 0:
            return discovered

        completed = [0]  # mutable counter for thread-safe progress

        def check_subdomain(sub: str) -> Optional[Dict]:
            """Check if a subdomain resolves."""
            if self.shared_data.orchestrator_should_exit:
                return None

            fqdn = f"{sub}.{domain}"
            result = None

            # Try dnspython
            if _HAS_DNSPYTHON and self._resolver:
                try:
                    answers = self._resolver.resolve(fqdn, "A")
                    ips = [str(rdata) for rdata in answers]
                    if ips:
                        result = {
                            "subdomain": sub,
                            "fqdn": fqdn,
                            "ips": ips,
                            "method": "dns",
                        }
                except Exception:
                    pass

            # Socket fallback
            if result is None:
                try:
                    addr_info = socket.getaddrinfo(fqdn, None, socket.AF_INET, socket.SOCK_STREAM)
                    ips = list(set(info[4][0] for info in addr_info))
                    if ips:
                        result = {
                            "subdomain": sub,
                            "fqdn": fqdn,
                            "ips": ips,
                            "method": "socket",
                        }
                except (socket.gaierror, OSError):
                    pass

            # Update progress atomically
            with self._lock:
                completed[0] += 1
                # Progress: 45% -> 95% across subdomain enumeration
                pct = 45 + int((completed[0] / total) * 50)
                pct = min(pct, 95)
                self.shared_data.bjorn_progress = f"{pct}%"

            return result

        try:
            with ThreadPoolExecutor(max_workers=thread_count) as executor:
                futures = {
                    executor.submit(check_subdomain, sub): sub for sub in wordlist
                }

                for future in as_completed(futures):
                    if self.shared_data.orchestrator_should_exit:
                        # Cancel remaining futures
                        for f in futures:
                            f.cancel()
                        logger.info("Subdomain enumeration interrupted by orchestrator.")
                        break

                    try:
                        result = future.result(timeout=15)
                        if result:
                            with self._lock:
                                discovered.append(result)
                            logger.info(
                                f"Subdomain found: {result['fqdn']} -> {result['ips']}"
                            )
                            self.shared_data.comment_params = {
                                "ip": domain,
                                "phase": "subdomains",
                                "found": str(len(discovered)),
                                "last": result["fqdn"],
                            }
                    except Exception as e:
                        logger.debug(f"Subdomain future error: {e}")

        except Exception as e:
            logger.error(f"Subdomain enumeration thread pool error: {e}")

        return discovered

    # --------------------- Result saving ---------------------

    def _save_results(self, ip: str, results: Dict) -> None:
        """Save DNS reconnaissance results to a JSON file."""
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            safe_ip = ip.replace(":", "_").replace(".", "_")
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"dns_{safe_ip}_{timestamp}.json"
            filepath = os.path.join(OUTPUT_DIR, filename)

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, default=str)

            logger.info(f"Results saved to {filepath}")
        except Exception as e:
            logger.error(f"Failed to save results for {ip}: {e}")

    # --------------------- DB hostname update ---------------------

    def _update_db_hostname(self, mac: str, ip: str, new_hostname: str) -> None:
        """Update the hostname in the hosts DB table if we found new DNS data."""
        if not mac or not new_hostname:
            return

        try:
            rows = self.shared_data.db.query(
                "SELECT hostnames FROM hosts WHERE mac_address=? LIMIT 1", (mac,)
            )
            if not rows:
                return

            existing = rows[0].get("hostnames") or ""
            existing_set = set(h.strip() for h in existing.split(";") if h.strip())

            if new_hostname not in existing_set:
                existing_set.add(new_hostname)
                updated = ";".join(sorted(existing_set))
                self.shared_data.db.execute(
                    "UPDATE hosts SET hostnames=? WHERE mac_address=?",
                    (updated, mac),
                )
                logger.info(f"Updated DB hostname for MAC {mac}: added {new_hostname}")
                # Refresh our local cache
                self._refresh_ip_identity_cache()

        except Exception as e:
            logger.error(f"Failed to update DB hostname for MAC {mac}: {e}")


# ---------------------------------------------------------------------------
# CLI mode (debug / manual execution)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    shared_data = SharedData()
    try:
        pillager = DNSPillager(shared_data)
        logger.info("DNS Pillager module ready (CLI mode).")

        rows = shared_data.read_data()
        for row in rows:
            ip = row.get("IPs") or row.get("ip")
            if not ip:
                continue
            port = row.get("port") or 53
            logger.info(f"Execute DNSPillager on {ip}:{port} ...")
            status = pillager.execute(ip, str(port), row, "dns_pillager")

            if status == "success":
                logger.success(f"DNS recon successful for {ip}:{port}.")
            elif status == "interrupted":
                logger.warning(f"DNS recon interrupted for {ip}:{port}.")
                break
            else:
                logger.failed(f"DNS recon failed for {ip}:{port}.")

        logger.info("DNS Pillager CLI execution completed.")
    except Exception as e:
        logger.error(f"Error: {e}")
        exit(1)
