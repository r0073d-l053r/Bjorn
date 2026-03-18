#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web_enum.py - Gobuster-powered web directory enumeration, streaming results to DB."""

import re
import socket
import subprocess
import threading
import logging
import time
import os
import select
from typing import List, Dict, Tuple, Optional, Set

from shared import SharedData
from logger import Logger

# -------------------- Logger & module meta --------------------
logger = Logger(name="web_enum.py", level=logging.DEBUG)

b_class     = "WebEnumeration"
b_module    = "web_enum"
b_status    = "WebEnumeration"
b_port      = 80
b_service   = '["http","https"]'
b_trigger   = 'on_any:["on_web_service","on_new_port:80","on_new_port:443","on_new_port:8080","on_new_port:8443","on_new_port:9443","on_new_port:8000","on_new_port:8888","on_new_port:81","on_new_port:5000","on_new_port:5001","on_new_port:7080","on_new_port:9080"]'
b_parent    = None
b_priority  = 9
b_cooldown  = 1800
b_rate_limit = '3/86400'
b_enabled   = 1
b_timeout = 600
b_max_retries = 1
b_stealth_level = 4
b_risk_level = "low"
b_action = "normal"
b_tags = ["web", "enum", "gobuster", "directories"]
b_category = "recon"
b_name = "Web Enumeration"
b_description = "Gobuster-powered web directory enumeration with streaming results to DB."
b_author = "Bjorn Team"
b_version = "2.0.0"
b_icon = "WebEnumeration.png"

# -------------------- Defaults & parsing --------------------
DEFAULT_WEB_STATUS_CODES = [
    200, 201, 202, 203, 204, 206,
    301, 302, 303, 307, 308,
    401, 403, 405,
    "5xx",
]

ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
CTL_RE  = re.compile(r"[\x00-\x1F\x7F]")  # non-printables

# Gobuster "dir" line examples handled:
# /admin   (Status: 301) [Size: 310] [--> http://10.0.0.5/admin/]
GOBUSTER_LINE = re.compile(
    r"""^(?P<path>\S+)\s*
        \(Status:\s*(?P<status>\d{3})\)\s*
        (?:\[Size:\s*(?P<size>\d+)\])?
        (?:\s*\[\-\-\>\s*(?P<redir>[^\]]+)\])?
        """,
    re.VERBOSE
)

# Regex to capture Gobuster progress from stderr
# e.g.: "Progress: 1024 / 4096 (25.00%)"
GOBUSTER_PROGRESS_RE = re.compile(r"Progress:\s+(?P<current>\d+)\s*/\s+(?P<total>\d+)")


def _normalize_status_policy(policy) -> Set[int]:
    """
    Convert a UI status policy into a set of HTTP status ints.
    """
    codes: Set[int] = set()
    if not policy:
        policy = DEFAULT_WEB_STATUS_CODES
    for item in policy:
        try:
            if isinstance(item, int):
                if 100 <= item <= 599:
                    codes.add(item)
            elif isinstance(item, str):
                s = item.strip().lower()
                if s.endswith("xx") and len(s) == 3 and s[0].isdigit():
                    base = int(s[0]) * 100
                    codes.update(range(base, base + 100))
                elif "-" in s:
                    a, b = s.split("-", 1)
                    a, b = int(a), int(b)
                    a, b = max(100, a), min(599, b)
                    if a <= b:
                        codes.update(range(a, b + 1))
                else:
                    v = int(s)
                    if 100 <= v <= 599:
                        codes.add(v)
        except Exception:
            logger.warning(f"Ignoring invalid status code token: {item!r}")
    return codes


class WebEnumeration:
    """
    Orchestrates Gobuster web dir enum and writes normalized results into DB.
    Streaming mode: Reads stdout/stderr in real-time for DB inserts and Progress UI.
    """
    def __init__(self, shared_data: SharedData):
        self.shared_data = shared_data
        import shutil
        self.gobuster_path = shutil.which("gobuster") or "/usr/bin/gobuster"
        self.wordlist = self.shared_data.common_wordlist
        self.lock = threading.Lock()
        
        # Wordlist size cache (for % calculation)
        self.wordlist_size = 0
        self._count_wordlist_lines()

        # ---- Sanity checks
        self._available = True
        if not os.path.exists(self.gobuster_path):
            logger.error(f"Gobuster not found at {self.gobuster_path}")
            self._available = False
        if not os.path.exists(self.wordlist):
            logger.error(f"Wordlist not found: {self.wordlist}")
            self._available = False

        # Status code policy from UI; create if missing
        if not hasattr(self.shared_data, "web_status_codes") or not self.shared_data.web_status_codes:
            self.shared_data.web_status_codes = DEFAULT_WEB_STATUS_CODES.copy()

        logger.info(
            f"WebEnumeration initialized (Streaming Mode). "
            f"Wordlist lines: {self.wordlist_size}. "
            f"Policy: {self.shared_data.web_status_codes}"
        )

    def _count_wordlist_lines(self):
        """Count wordlist lines once for progress % calculation."""
        if self.wordlist and os.path.exists(self.wordlist):
            try:
                # Fast buffered read
                with open(self.wordlist, 'rb') as f:
                    self.wordlist_size = sum(1 for _ in f)
            except Exception as e:
                logger.error(f"Error counting wordlist lines: {e}")
                self.wordlist_size = 0

    # -------------------- Utilities --------------------
    def _scheme_for_port(self, port: int) -> str:
        https_ports = {443, 8443, 9443, 10443, 9444, 5000, 5001, 7080, 9080}
        return "https" if int(port) in https_ports else "http"

    def _reverse_dns(self, ip: str) -> Optional[str]:
        try:
            name, _, _ = socket.gethostbyaddr(ip)
            return name
        except Exception:
            return None

    def _extract_identity(self, row: Dict) -> Tuple[str, Optional[str]]:
        """Return (mac_address, hostname) from a row with tolerant keys."""
        mac = row.get("mac_address") or row.get("mac") or row.get("MAC") or ""
        hostname = row.get("hostname") or row.get("Hostname") or None
        return str(mac), (str(hostname) if hostname else None)

    # -------------------- Filter helper --------------------
    def _allowed_status_set(self) -> Set[int]:
        """Recalculated each run to reflect live UI updates."""
        try:
            return _normalize_status_policy(getattr(self.shared_data, "web_status_codes", None))
        except Exception as e:
            logger.error(f"Failed to load shared_data.web_status_codes: {e}")
            return _normalize_status_policy(DEFAULT_WEB_STATUS_CODES)

    # -------------------- DB Writer --------------------
    def _db_add_result(self,
                       mac_address: str,
                       ip: str,
                       hostname: Optional[str],
                       port: int,
                       directory: str,
                       status: int,
                       size: int = 0,
                       response_time: int = 0,
                       content_type: Optional[str] = None,
                       tool: str = "gobuster") -> None:
        """Upsert a single record into `webenum`."""
        try:
            self.shared_data.db.execute("""
                INSERT INTO webenum (
                    mac_address, ip, hostname, port, directory, status,
                    size, response_time, content_type, tool, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(mac_address, ip, port, directory) DO UPDATE SET
                    status        = excluded.status,
                    size          = excluded.size,
                    response_time = excluded.response_time,
                    content_type  = excluded.content_type,
                    hostname      = COALESCE(excluded.hostname, webenum.hostname),
                    tool          = COALESCE(excluded.tool, webenum.tool),
                    last_seen     = CURRENT_TIMESTAMP,
                    is_active     = 1
            """, (mac_address, ip, hostname, int(port), directory, int(status),
                  int(size or 0), int(response_time or 0), content_type, tool))
            logger.debug(f"DB upsert: {ip}:{port}{directory} -> {status} (size={size})")
        except Exception as e:
            logger.error(f"DB insert error for {ip}:{port}{directory}: {e}")

    # -------------------- Public API (Streaming Version) --------------------
    def execute(self, ip: str, port: int, row: Dict, status_key: str) -> str:
        """
        Run gobuster on (ip,port), STREAM stdout/stderr, upsert findings real-time.
        Updates bjorn_progress with 0-100% completion.
        Returns: 'success' | 'failed' | 'interrupted'
        """
        if not self._available:
            return 'failed'

        try:
            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            scheme = self._scheme_for_port(port)
            base_url = f"{scheme}://{ip}:{port}"
            
            # Setup Initial UI
            self.shared_data.comment_params = {"ip": ip, "port": str(port), "url": base_url}
            self.shared_data.bjorn_orch_status = "WebEnumeration"
            self.shared_data.bjorn_progress = "0%"
            
            logger.info(f"Enumerating {base_url} (Stream Mode)...")

            # Prepare Identity & Policy
            mac_address, hostname = self._extract_identity(row)
            if not hostname:
                hostname = self._reverse_dns(ip)
            allowed = self._allowed_status_set()

            # Command Construction
            # NOTE: Removed "--quiet" and "-z" to ensure we get Progress info on stderr
            # But we use --no-color to make parsing easier
            cmd = [
                self.gobuster_path, "dir",
                "-u", base_url,
                "-w", self.wordlist,
                "-t", "10",        # Safe for RPi Zero
                "--no-color",
                "--no-progress=false", # Force progress bar even if redirected
            ]

            process = None
            findings_count = 0
            stop_requested = False
             
            # For progress calc
            total_lines = self.wordlist_size if self.wordlist_size > 0 else 1
            last_progress_update = 0

            try:
                # Merge stdout and stderr so we can read everything in one loop
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                )

                # Use select() (on Linux) so we can react quickly to stop requests
                # without blocking forever on readline().
                while True:
                    if self.shared_data.orchestrator_should_exit:
                        stop_requested = True
                        break

                    if process.poll() is not None:
                        # Process exited; drain remaining buffered output if any
                        line = process.stdout.readline() if process.stdout else ""
                        if not line:
                            break
                    else:
                        line = ""
                        if process.stdout:
                            if os.name != "nt":
                                r, _, _ = select.select([process.stdout], [], [], 0.2)
                                if r:
                                    line = process.stdout.readline()
                            else:
                                # Windows: select() doesn't work on pipes; best-effort read.
                                line = process.stdout.readline()

                        if not line:
                            continue

                    # 3. Clean Line
                    clean_line = ANSI_RE.sub("", line).strip()
                    clean_line = CTL_RE.sub("", clean_line).strip()
                    if not clean_line:
                        continue

                    # 4. Check for Progress
                    if "Progress:" in clean_line:
                        now = time.time()
                        # Update UI max every 0.5s to save CPU
                        if now - last_progress_update > 0.5:
                            m_prog = GOBUSTER_PROGRESS_RE.search(clean_line)
                            if m_prog:
                                curr = int(m_prog.group("current"))
                                # Calculate %
                                pct = (curr / total_lines) * 100
                                pct = min(pct, 100.0)
                                self.shared_data.bjorn_progress = f"{int(pct)}%"
                            last_progress_update = now
                        continue

                    # 5. Check for Findings (Standard Gobuster Line)
                    m_res = GOBUSTER_LINE.match(clean_line)
                    if m_res:
                        st = int(m_res.group("status"))
                        
                        # Apply Filtering Logic BEFORE DB
                        if st in allowed:
                            path = m_res.group("path")
                            if not path.startswith("/"): path = "/" + path
                            size = int(m_res.group("size") or 0)
                            redir = m_res.group("redir")

                            # Insert into DB Immediately
                            self._db_add_result(
                                mac_address=mac_address,
                                ip=ip,
                                hostname=hostname,
                                port=port,
                                directory=path,
                                status=st,
                                size=size,
                                response_time=0,
                                content_type=None,
                                tool="gobuster"
                            )
                            
                            findings_count += 1
                            # Live feedback in comments
                            self.shared_data.comment_params = {
                                "url": base_url, 
                                "found": str(findings_count),
                                "last": path
                            }
                        continue

                    # (Optional) Log errors/unknown lines if needed
                    # if "error" in clean_line.lower(): logger.debug(f"Gobuster err: {clean_line}")

                # End of loop
                if stop_requested:
                    logger.info("Interrupted by orchestrator.")
                    return "interrupted"
                self.shared_data.bjorn_progress = "100%"
                return "success"

            except Exception as e:
                logger.error(f"Execute error on {base_url}: {e}")
                if process:
                    try:
                        process.terminate()
                    except Exception:
                        pass
                return "failed"
            finally:
                if process:
                    try:
                        if stop_requested and process.poll() is None:
                            process.terminate()
                        # Always reap the child to avoid zombies.
                        try:
                            process.wait(timeout=2)
                        except Exception:
                            try:
                                process.kill()
                            except Exception:
                                pass
                            try:
                                process.wait(timeout=2)
                            except Exception:
                                pass
                    finally:
                        try:
                            if process.stdout:
                                process.stdout.close()
                        except Exception:
                            pass
                self.shared_data.bjorn_progress = ""
                self.shared_data.comment_params = {}

        except Exception as e:
            logger.error(f"General execution error: {e}")
            return "failed"


# -------------------- CLI mode (debug/manual) --------------------
if __name__ == "__main__":
    shared_data = SharedData()
    try:
        web_enum = WebEnumeration(shared_data)
        logger.info("Starting web directory enumeration (CLI)...")

        rows = shared_data.read_data()
        for row in rows:
            ip = row.get("IPs") or row.get("ip")
            if not ip:
                continue
            port = row.get("port") or 80
            logger.info(f"Execute WebEnumeration on {ip}:{port} ...")
            status = web_enum.execute(ip, int(port), row, "enum_web_directories")
            
            if status == "success":
                logger.success(f"Enumeration successful for {ip}:{port}.")
            elif status == "interrupted":
                logger.warning(f"Enumeration interrupted for {ip}:{port}.")
                break
            else:
                logger.failed(f"Enumeration failed for {ip}:{port}.")

        logger.info("Web directory enumeration completed.")
    except Exception as e:
        logger.error(f"General execution error: {e}")
