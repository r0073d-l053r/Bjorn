# web_utils/network_utils.py
"""
Network utilities for WiFi/network operations.
Handles WiFi scanning, connection, known networks management.
Compatible with both legacy NM keyfiles and Trixie netplan.
"""
from __future__ import annotations
import json
import subprocess
import logging
import os
import glob
import re
from typing import Any, Dict, Optional, List
from logger import Logger

logger = Logger(name="network_utils.py", level=logging.DEBUG)


class NetworkUtils:
    """Utilities for network and WiFi management."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
        """Run a command, returning CompletedProcess."""
        return subprocess.run(
            cmd, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, **kw,
        )

    @staticmethod
    def _json_response(handler, code: int, payload: dict):
        handler.send_response(code)
        handler.send_header("Content-type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(payload).encode('utf-8'))

    # ── known networks ───────────────────────────────────────────────

    def get_known_wifi(self, handler):
        """List known WiFi networks with priorities.

        Uses nmcli terse output.  On Trixie, netplan-generated profiles
        (named ``netplan-wlan0-*``) appear alongside user-created NM
        profiles — both are returned.
        """
        try:
            result = self._run(
                ['nmcli', '-t', '-f', 'NAME,TYPE,AUTOCONNECT-PRIORITY', 'connection', 'show']
            )
            self.logger.debug(f"nmcli connection show output:\n{result.stdout}")

            known_networks: list[dict] = []
            for line in result.stdout.strip().splitlines():
                if not line.strip():
                    continue
                # nmcli -t uses ':' as delimiter — SSIDs with ':' are
                # escaped by nmcli (backslash-colon), so split from
                # the right to be safe: last field = priority,
                # second-to-last = type, rest = name.
                parts = line.rsplit(':', 2)
                if len(parts) == 3:
                    name, conn_type, priority_str = parts
                elif len(parts) == 2:
                    name, conn_type = parts
                    priority_str = '0'
                else:
                    self.logger.warning(f"Unexpected line format: {line}")
                    continue

                # Unescape nmcli backslash-colon
                name = name.replace('\\:', ':')

                if conn_type.strip().lower() not in (
                    '802-11-wireless', 'wireless', 'wifi',
                ):
                    continue

                try:
                    priority_int = int(priority_str.strip())
                except (ValueError, AttributeError):
                    priority_int = 0

                known_networks.append({
                    'ssid': name.strip(),
                    'priority': priority_int,
                })

            known_networks.sort(key=lambda x: x['priority'], reverse=True)
            self._json_response(handler, 200, {"known_networks": known_networks})

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Error getting known Wi-Fi networks: {e.stderr.strip()}")
            self._json_response(handler, 500, {"error": e.stderr.strip()})
        except Exception as e:
            self.logger.error(f"Error getting known Wi-Fi networks: {e}")
            self._json_response(handler, 500, {"error": str(e)})

    def delete_known_wifi(self, data):
        """Delete a known WiFi connection."""
        ssid = data.get('ssid')
        try:
            if not ssid:
                return {"status": "error", "message": "Missing SSID"}
            self._run(['sudo', 'nmcli', 'connection', 'delete', ssid])
            self.logger.info(f"Deleted Wi-Fi connection: {ssid}")
            return {"status": "success", "message": f"Network {ssid} deleted"}
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Error deleting Wi-Fi connection {ssid}: {e.stderr.strip()}")
            return {"status": "error", "message": e.stderr.strip()}
        except Exception as e:
            self.logger.error(f"Unexpected error deleting Wi-Fi connection {ssid}: {e}")
            return {"status": "error", "message": str(e)}

    def connect_known_wifi(self, data):
        """Connect to a known WiFi network."""
        ssid = data.get('ssid', '')
        try:
            if not self.check_connection_exists(ssid):
                return {"status": "error", "message": f"Network '{ssid}' not found in saved connections."}
            self._run(['sudo', 'nmcli', 'connection', 'up', ssid])
            self.logger.info(f"Connected to known Wi-Fi network: {ssid}")
            return {"status": "success", "message": f"Connected to {ssid}"}
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Error connecting to known Wi-Fi network {ssid}: {e.stderr.strip()}")
            return {"status": "error", "message": e.stderr.strip()}
        except Exception as e:
            self.logger.error(f"Unexpected error connecting to known Wi-Fi network {ssid}: {e}")
            return {"status": "error", "message": str(e)}

    def update_wifi_priority(self, data):
        """Update WiFi connection priority.

        Works for both NM-native and netplan-generated profiles.
        For netplan profiles (prefixed ``netplan-``), nmcli modify
        writes a persistent override into
        /etc/NetworkManager/system-connections/.
        """
        ssid = data.get('ssid', '')
        try:
            priority = int(data['priority'])
            self._run([
                'sudo', 'nmcli', 'connection', 'modify', ssid,
                'connection.autoconnect-priority', str(priority),
            ])
            self.logger.info(f"Priority updated for {ssid} to {priority}")
            return {"status": "success", "message": "Priority updated"}
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Error updating Wi-Fi priority: {e.stderr.strip()}")
            return {"status": "error", "message": e.stderr.strip()}
        except Exception as e:
            self.logger.error(f"Unexpected error updating Wi-Fi priority: {e}")
            return {"status": "error", "message": str(e)}

    # ── scanning ─────────────────────────────────────────────────────

    def scan_wifi(self, handler):
        """Scan for available WiFi networks.

        Uses ``nmcli -t`` (terse) output for reliable parsing.
        Signal is returned as a percentage 0-100.
        """
        try:
            # Trigger a rescan first (best-effort)
            subprocess.run(
                ['sudo', 'nmcli', 'device', 'wifi', 'rescan'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )

            result = self._run([
                'sudo', 'nmcli', '-t', '-f',
                'SSID,SIGNAL,SECURITY,IN-USE',
                'device', 'wifi', 'list',
            ])

            networks = self._parse_terse_scan(result.stdout)
            current_ssid = self.get_current_ssid()
            self.logger.info(f"Found {len(networks)} networks, current={current_ssid}")

            self._json_response(handler, 200, {
                "networks": networks,
                "current_ssid": current_ssid,
            })
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Error scanning Wi-Fi networks: {e.stderr.strip()}")
            self._json_response(handler, 500, {"error": e.stderr.strip()})

    @staticmethod
    def _parse_terse_scan(output: str) -> list[dict]:
        """Parse ``nmcli -t -f SSID,SIGNAL,SECURITY,IN-USE device wifi list``.

        Terse output uses ':' as separator.  SSIDs containing ':'
        are escaped by nmcli as ``\\:``.
        Returns a deduplicated list sorted by signal descending.
        """
        seen: dict[str, dict] = {}
        for line in output.strip().splitlines():
            if not line.strip():
                continue

            # Split from the right: IN-USE (last), SECURITY, SIGNAL, rest=SSID
            # IN-USE is '*' or '' — always one char field at the end
            parts = line.rsplit(':', 3)
            if len(parts) < 4:
                continue

            raw_ssid, signal_str, security, in_use = parts

            # Unescape nmcli backslash-colon in SSID
            ssid = raw_ssid.replace('\\:', ':').strip()
            if not ssid:
                continue

            try:
                signal = int(signal_str.strip())
            except (ValueError, AttributeError):
                signal = 0

            # Normalize security string
            security = security.strip()
            if not security or security == '--':
                security = 'Open'

            # Keep the strongest signal per SSID
            if ssid not in seen or signal > seen[ssid]['signal']:
                seen[ssid] = {
                    'ssid': ssid,
                    'signal': signal,
                    'security': security,
                    'in_use': in_use.strip() == '*',
                }

        result = sorted(seen.values(), key=lambda n: n['signal'], reverse=True)
        return result

    def get_current_ssid(self) -> Optional[str]:
        """Get currently connected SSID."""
        try:
            result = self._run(['nmcli', '-t', '-f', 'active,ssid', 'dev', 'wifi'])
            for line in result.stdout.strip().splitlines():
                parts = line.split(':', 1)
                if len(parts) == 2 and parts[0] == 'yes':
                    return parts[1]
            return None
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Error getting current SSID: {e.stderr.strip()}")
            return None

    # ── connect ──────────────────────────────────────────────────────

    def connect_wifi(self, data):
        """Connect to WiFi network (new or existing).

        On Trixie, ``nmcli device wifi connect`` creates a persistent
        NM keyfile in /etc/NetworkManager/system-connections/,
        which survives reboots even when netplan manages the initial
        Wi-Fi profile.
        """
        ssid = data.get('ssid', '')
        password = data.get('password', '')
        try:
            if self.check_connection_exists(ssid):
                self._run(['sudo', 'nmcli', 'connection', 'up', ssid])
                return {"status": "success", "message": f"Connected to {ssid}"}

            cmd = ['sudo', 'nmcli', 'device', 'wifi', 'connect', ssid]
            if password:
                cmd += ['password', password]
            self._run(cmd)
            return {"status": "success", "message": f"Connected to {ssid}"}
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Error connecting to network {ssid}: {e.stderr.strip()}")
            return {"status": "error", "message": e.stderr.strip()}
        except Exception as e:
            self.logger.error(f"Error in connect_wifi: {e}")
            return {"status": "error", "message": str(e)}

    def check_connection_exists(self, ssid: str) -> bool:
        """Check if a WiFi connection profile exists (exact match)."""
        try:
            result = self._run(['nmcli', '-t', '-f', 'NAME', 'connection', 'show'])
            for name in result.stdout.strip().splitlines():
                # nmcli escapes ':' in names with backslash
                if name.replace('\\:', ':').strip() == ssid:
                    return True
            return False
        except subprocess.CalledProcessError:
            return False

    def validate_network_configuration(self, ssid: str) -> bool:
        """Validate that a WiFi connection profile exists (exact match)."""
        return self.check_connection_exists(ssid)

    # ── potfile import ───────────────────────────────────────────────

    def import_potfiles(self, data=None):
        """Import WiFi credentials from .pot/.potfile files.

        Creates NM connection profiles via nmcli — these are stored
        in /etc/NetworkManager/system-connections/ and persist across
        reboots on both legacy and Trixie builds.
        """
        try:
            potfiles_folder = self.shared_data.potfiles_dir
            potfile_paths = (
                glob.glob(f"{potfiles_folder}/*.pot")
                + glob.glob(f"{potfiles_folder}/*.potfile")
            )

            networks_added: list[str] = []
            networks_skipped: list[str] = []
            networks_failed: list[str] = []
            DEFAULT_PRIORITY = 5

            for potfile_path in potfile_paths:
                try:
                    with open(potfile_path, 'r', errors='replace') as potfile:
                        for line in potfile:
                            line = line.strip()
                            if not line or ':' not in line:
                                continue

                            ssid, password = self._parse_potfile_line(line)
                            if not ssid or not password:
                                continue

                            if self.check_connection_exists(ssid):
                                networks_skipped.append(ssid)
                                continue

                            try:
                                self._run([
                                    'sudo', 'nmcli', 'connection', 'add',
                                    'type', 'wifi',
                                    'con-name', ssid,
                                    'ifname', '*',
                                    'ssid', ssid,
                                    'wifi-sec.key-mgmt', 'wpa-psk',
                                    'wifi-sec.psk', password,
                                    'connection.autoconnect', 'yes',
                                    'connection.autoconnect-priority', str(DEFAULT_PRIORITY),
                                ])
                                networks_added.append(ssid)
                                self.logger.info(f"Imported network {ssid} from {potfile_path}")
                            except subprocess.CalledProcessError as e:
                                networks_failed.append(ssid)
                                self.logger.error(f"Failed to add network {ssid}: {e.stderr.strip()}")
                except OSError as e:
                    self.logger.error(f"Failed to read potfile {potfile_path}: {e}")

            return {
                "status": "success",
                "networks_added": networks_added,
                "imported": len(networks_added),
                "skipped": len(networks_skipped),
                "failed": len(networks_failed),
            }
        except Exception as e:
            self.logger.error(f"Unexpected error importing potfiles: {e}")
            return {"status": "error", "message": str(e)}

    @staticmethod
    def _parse_potfile_line(line: str) -> tuple[str, str]:
        """Parse a single potfile line, returning (ssid, password) or ('', '')."""
        # Format 1: $WPAPSK$SSID#hash:password
        if line.startswith('$WPAPSK$') and '#' in line:
            try:
                ssid_hash_part, password = line.split(':', 1)
                ssid = ssid_hash_part.split('#')[0].replace('$WPAPSK$', '')
                return ssid.strip(), password.strip()
            except ValueError:
                return '', ''

        # Format 2: MAC:MAC:SSID:password (4 colon-separated fields)
        parts = line.split(':')
        if len(parts) == 4:
            return parts[2].strip(), parts[3].strip()

        # Format 3: SSID:password (2 colon-separated fields)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()

        return '', ''

    # ── preconfigured file management (legacy compat) ────────────────

    def delete_preconfigured_file(self, handler):
        """Delete the legacy preconfigured.nmconnection file.

        On Trixie this file typically does not exist (Wi-Fi is managed
        by netplan). The endpoint returns 200/success even if the file
        is missing to avoid breaking the frontend.
        """
        path = '/etc/NetworkManager/system-connections/preconfigured.nmconnection'
        try:
            if os.path.exists(path):
                os.remove(path)
                self.logger.info("Deleted preconfigured.nmconnection")
            else:
                self.logger.info("preconfigured.nmconnection not found (Trixie/netplan — this is normal)")
            self._json_response(handler, 200, {"status": "success"})
        except Exception as e:
            self.logger.error(f"Error deleting preconfigured file: {e}")
            self._json_response(handler, 500, {"status": "error", "message": str(e)})

    def create_preconfigured_file(self, handler):
        """Create a preconfigured.nmconnection file (legacy compat).

        On Trixie this is a no-op: Wi-Fi is managed by netplan.
        Returns success regardless to avoid breaking the frontend.
        """
        self.logger.warning("create_preconfigured_file called — no-op on Trixie/netplan builds")
        self._json_response(handler, 200, {
            "status": "success",
            "message": "No action needed on netplan-managed builds",
        })

    # ── potfile upload ────────────────────────────────────────────────

    def upload_potfile(self, handler):
        """Upload a .pot/.potfile file to the potfiles directory.

        Accepts multipart/form-data with a 'potfile' field.
        Saves to shared_data.potfiles_dir.
        Manual multipart parsing — no cgi module (removed in Python 3.13).
        """
        try:
            content_type = handler.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._json_response(handler, 400, {
                    "status": "error",
                    "message": "Content-Type must be multipart/form-data",
                })
                return

            boundary = content_type.split("=")[1].encode()
            content_length = int(handler.headers.get("Content-Length", 0))
            body = handler.rfile.read(content_length)
            parts = body.split(b"--" + boundary)

            filename = None
            file_data = None

            for part in parts:
                if b"Content-Disposition" not in part:
                    continue
                if b'name="potfile"' not in part:
                    continue
                if b"filename=" not in part:
                    continue

                headers_raw, data = part.split(b"\r\n\r\n", 1)
                headers_str = headers_raw.decode(errors="replace")
                match = re.search(r'filename="(.+?)"', headers_str)
                if match:
                    filename = os.path.basename(match.group(1))
                    # Strip trailing boundary markers
                    file_data = data.rstrip(b"\r\n--").rstrip(b"\r\n")
                break

            if not filename or file_data is None:
                self._json_response(handler, 400, {
                    "status": "error",
                    "message": "No potfile provided",
                })
                return

            # Sanitise filename
            safe_name = "".join(
                c for c in filename if c.isalnum() or c in ".-_"
            ) or "uploaded.potfile"

            dest_dir = self.shared_data.potfiles_dir
            os.makedirs(dest_dir, exist_ok=True)
            dest_path = os.path.join(dest_dir, safe_name)

            with open(dest_path, "wb") as f:
                f.write(file_data)

            self.logger.info(f"Uploaded potfile: {safe_name} ({len(file_data)} bytes)")
            self._json_response(handler, 200, {
                "status": "success",
                "filename": safe_name,
            })
        except Exception as e:
            self.logger.error(f"Error uploading potfile: {e}")
            self._json_response(handler, 500, {
                "status": "error",
                "message": str(e),
            })
