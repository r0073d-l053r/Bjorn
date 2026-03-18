#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web_enum.py - Gobuster-based web directory enumeration, results written to DB."""

import re
import socket
import subprocess
import threading
import logging
from typing import List, Dict, Tuple, Optional

from shared import SharedData
from logger import Logger

# -------------------- Logger & module meta --------------------
logger = Logger(name="web_enum.py", level=logging.DEBUG)

b_class     = "WebEnumeration"
b_module    = "web_enum"
b_status    = "WebEnumeration"
b_port      = 80
b_service    = '["http","https"]'
b_trigger    = 'on_any:["on_web_service","on_new_port:80","on_new_port:443","on_new_port:8080","on_new_port:8443","on_new_port:9443","on_new_port:8000","on_new_port:8888","on_new_port:81","on_new_port:5000","on_new_port:5001","on_new_port:7080","on_new_port:9080"]'
b_parent    = None
b_priority  = 90       # adjust if needed
b_cooldown  = 1800     # 30 minutes between runs
b_rate_limit = '3/86400'  # 3 times per day max
b_enabled   = 0

# -------------------- Status parsing helpers --------------------
VALID_STATUS_CODES = {
    200: "OK", 201: "Created", 204: "No Content",
    301: "Moved Permanently", 302: "Found (Temp Redirect)",
    303: "See Other", 307: "Temporary Redirect", 308: "Permanent Redirect",
    401: "Unauthorized", 403: "Forbidden", 405: "Method Not Allowed"
}
ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
CTL_RE  = re.compile(r"[\x00-\x1F\x7F]")  # non-printables

# Gobuster "dir" line examples handled:
# /admin   (Status: 301) [Size: 310] [--> http://10.0.0.5/admin/]
# /images  (Status: 200) [Size: 12345]
GOBUSTER_LINE = re.compile(
    r"""^(?P<path>\S+)\s*
        \(Status:\s*(?P<status>\d{3})\)\s*
        (?:\[Size:\s*(?P<size>\d+)\])?
        (?:\s*\[\-\-\>\s*(?P<redir>[^\]]+)\])?
        """,
    re.VERBOSE
)


class WebEnumeration:
    """
    Orchestrates Gobuster web dir enum and writes normalized results into DB.
    In-memory only: no CSV, no temp files.
    """
    def __init__(self, shared_data: SharedData):
        self.shared_data = shared_data
        self.gobuster_path = "/usr/bin/gobuster"  # verify with `which gobuster`
        self.wordlist = self.shared_data.common_wordlist
        self.lock = threading.Lock()

        # ---- Sanity checks
        import os
        if not os.path.exists(self.gobuster_path):
            raise FileNotFoundError(f"Gobuster not found at {self.gobuster_path}")
        if not os.path.exists(self.wordlist):
            raise FileNotFoundError(f"Wordlist not found: {self.wordlist}")

        logger.info("WebEnumeration initialized (stdout mode, no files).")

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

    # -------------------- Gobuster runner (stdout) --------------------
    def _run_gobuster_stdout(self, url: str) -> Optional[str]:
        base_cmd = [
            self.gobuster_path, "dir",
            "-u", url,
            "-w", self.wordlist,
            "-t", "10",
            "--quiet",
            "--no-color",
        ]

        def run(cmd):
            return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Try with -z first
        cmd = base_cmd + ["-z"]
        logger.info(f"Running Gobuster on {url}...")
        try:
            res = run(cmd)
            if res.returncode == 0:
                logger.success(f"Gobuster OK on {url}")
                return res.stdout or ""
            # Fallback if -z is unknown
            if "unknown flag" in (res.stderr or "").lower() or "invalid" in (res.stderr or "").lower():
                logger.info("Gobuster doesn't support -z, retrying without it.")
                res2 = run(base_cmd)
                if res2.returncode == 0:
                    logger.success(f"Gobuster OK on {url} (no -z)")
                    return res2.stdout or ""
                logger.info(f"Gobuster failed on {url}: {res2.stderr.strip()}")
                return None
            logger.info(f"Gobuster failed on {url}: {res.stderr.strip()}")
            return None
        except Exception as e:
            logger.error(f"Gobuster exception on {url}: {e}")
            return None


    def _parse_gobuster_text(self, text: str) -> List[Dict]:
        """
        Parse gobuster stdout lines into entries:
        { 'path': '/admin', 'status': 301, 'size': 310, 'redirect': 'http://...'|None }
        """
        entries: List[Dict] = []
        if not text:
            return entries

        for raw in text.splitlines():
            # 1) strip ANSI/control BEFORE regex
            line = ANSI_RE.sub("", raw)
            line = CTL_RE.sub("", line)
            line = line.strip()
            if not line:
                continue

            m = GOBUSTER_LINE.match(line)
            if not m:
                logger.debug(f"Unparsed line: {line}")
                continue

            # 2) extract all fields NOW
            path  = m.group("path") or ""
            status = int(m.group("status"))
            size   = int(m.group("size") or 0)
            redir  = m.group("redir")

            # 3) normalize path
            if not path.startswith("/"):
                path = "/" + path
            path = "/" + path.strip("/")

            entries.append({
                "path": path,
                "status": status,
                "size": size,
                "redirect": redir.strip() if redir else None
            })

        logger.info(f"Parsed {len(entries)} entries from gobuster stdout")
        return entries

    # -------------------- Public API --------------------
    def execute(self, ip: str, port: int, row: Dict, status_key: str) -> str:
        """
        Run gobuster on (ip,port), parse stdout, upsert each finding into DB.
        Returns: 'success' | 'failed' | 'interrupted'
        """
        try:
            if self.shared_data.orchestrator_should_exit:
                logger.info("Interrupted before start (orchestrator flag).")
                return "interrupted"

            scheme = self._scheme_for_port(port)
            base_url = f"{scheme}://{ip}:{port}"
            logger.info(f"Enumerating {base_url} ...")
            self.shared_data.bjornorch_status = "WebEnumeration"

            if self.shared_data.orchestrator_should_exit:
                logger.info("Interrupted before gobuster run.")
                return "interrupted"

            stdout_text = self._run_gobuster_stdout(base_url)
            if stdout_text is None:
                return "failed"

            if self.shared_data.orchestrator_should_exit:
                logger.info("Interrupted after gobuster run (stdout captured).")
                return "interrupted"

            entries = self._parse_gobuster_text(stdout_text)
            if not entries:
                logger.warning(f"No entries for {base_url}.")
                return "success"  # scan ran fine but no findings

            mac_address, hostname = self._extract_identity(row)
            if not hostname:
                hostname = self._reverse_dns(ip)

            for e in entries:
                self._db_add_result(
                    mac_address=mac_address,
                    ip=ip,
                    hostname=hostname,
                    port=port,
                    directory=e["path"],
                    status=e["status"],
                    size=e.get("size", 0),
                    response_time=0,     # gobuster doesn't expose timing here
                    content_type=None,   # unknown here; a later HEAD/GET probe can fill it
                    tool="gobuster"
                )

            return "success"

        except Exception as e:
            logger.error(f"Execute error on {ip}:{port}: {e}")
            return "failed"


# -------------------- CLI mode (debug/manual) --------------------
if __name__ == "__main__":
    shared_data = SharedData()
    try:
        web_enum = WebEnumeration(shared_data)
        logger.info("Starting web directory enumeration...")

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
