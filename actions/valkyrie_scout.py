#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""valkyrie_scout.py - Probe common web paths for auth surfaces, headers, and debug leaks."""

import json
import logging
import re
import ssl
import time
from http.client import HTTPConnection, HTTPSConnection, RemoteDisconnected
from typing import Dict, List, Optional, Tuple

from logger import Logger
from actions.bruteforce_common import ProgressTracker

logger = Logger(name="valkyrie_scout.py", level=logging.DEBUG)

# -------------------- Action metadata (AST-friendly) --------------------
b_class = "ValkyrieScout"
b_module = "valkyrie_scout"
b_status = "ValkyrieScout"
b_port = 80
b_parent = None
b_service = '["http","https"]'
b_trigger = "on_web_service"
b_priority = 50
b_action = "normal"
b_cooldown = 1800
b_rate_limit = "8/86400"
b_enabled = 0  # keep disabled by default; enable via Actions UI/DB when ready.
b_timeout = 300
b_max_retries = 2
b_stealth_level = 5
b_risk_level = "low"
b_tags = ["web", "recon", "auth", "paths"]
b_category = "recon"
b_name = "Valkyrie Scout"
b_description = "Probes common web paths for auth surfaces, headers, and debug leaks."
b_author = "Bjorn Team"
b_version = "2.0.0"
b_icon = "ValkyrieScout.png"

# Small default list to keep the action cheap on Pi Zero.
DEFAULT_PATHS = [
    "/",
    "/robots.txt",
    "/login",
    "/signin",
    "/auth",
    "/admin",
    "/administrator",
    "/wp-login.php",
    "/user/login",
]

# Keep patterns minimal and high-signal.
SQLI_ERRORS = [
    "error in your sql syntax",
    "mysql_fetch",
    "unclosed quotation mark",
    "ora-",
    "postgresql",
    "sqlite error",
]
LFI_HINTS = [
    "include(",
    "require(",
    "include_once(",
    "require_once(",
]
DEBUG_HINTS = [
    "stack trace",
    "traceback",
    "exception",
    "fatal error",
    "notice:",
    "warning:",
    "debug",
]


def _scheme_for_port(port: int) -> str:
    https_ports = {443, 8443, 9443, 10443, 9444, 5000, 5001, 7080, 9080}
    return "https" if int(port) in https_ports else "http"


def _first_hostname_from_row(row: Dict) -> str:
    try:
        hn = (row.get("Hostname") or row.get("hostname") or row.get("hostnames") or "").strip()
        if ";" in hn:
            hn = hn.split(";", 1)[0].strip()
        return hn
    except Exception:
        return ""


def _lower_headers(headers: Dict[str, str]) -> Dict[str, str]:
    out = {}
    for k, v in (headers or {}).items():
        if not k:
            continue
        out[str(k).lower()] = str(v)
    return out


def _detect_signals(status: int, headers: Dict[str, str], body_snippet: str) -> Dict[str, object]:
    h = _lower_headers(headers)
    www = h.get("www-authenticate", "")
    set_cookie = h.get("set-cookie", "")

    auth_type = None
    if status == 401 and "basic" in www.lower():
        auth_type = "basic"
    elif status == 401 and "digest" in www.lower():
        auth_type = "digest"

    snippet = (body_snippet or "").lower()
    has_form = "<form" in snippet
    has_password = "type=\"password\"" in snippet or "type='password'" in snippet
    looks_like_login = bool(has_form and has_password) or any(x in snippet for x in ["login", "sign in", "connexion"])

    csrf_markers = [
        "csrfmiddlewaretoken",
        "authenticity_token",
        "csrf_token",
        "name=\"_token\"",
        "name='_token'",
    ]
    has_csrf = any(m in snippet for m in csrf_markers)

    missing_headers = []
    for header in [
        "x-frame-options",
        "x-content-type-options",
        "content-security-policy",
        "referrer-policy",
    ]:
        if header not in h:
            missing_headers.append(header)
    # HSTS is only relevant on HTTPS.
    if "strict-transport-security" not in h:
        missing_headers.append("strict-transport-security")

    rate_limited_hint = (status == 429) or ("retry-after" in h) or ("x-ratelimit-remaining" in h)

    # Very cheap "issue hints"
    issues = []
    for s in SQLI_ERRORS:
        if s in snippet:
            issues.append("sqli_error_hint")
            break
    for s in LFI_HINTS:
        if s in snippet:
            issues.append("lfi_hint")
            break
    for s in DEBUG_HINTS:
        if s in snippet:
            issues.append("debug_hint")
            break

    cookie_names = []
    if set_cookie:
        for part in set_cookie.split(","):
            name = part.split(";", 1)[0].split("=", 1)[0].strip()
            if name and name not in cookie_names:
                cookie_names.append(name)

    return {
        "auth_type": auth_type,
        "looks_like_login": bool(looks_like_login),
        "has_csrf": bool(has_csrf),
        "missing_security_headers": missing_headers[:12],
        "rate_limited_hint": bool(rate_limited_hint),
        "issues": issues[:8],
        "cookie_names": cookie_names[:12],
        "server": h.get("server", ""),
        "x_powered_by": h.get("x-powered-by", ""),
    }


class ValkyrieScout:
    def __init__(self, shared_data):
        self.shared_data = shared_data
        self._ssl_ctx = ssl._create_unverified_context()

    def _fetch(
        self,
        *,
        ip: str,
        port: int,
        scheme: str,
        path: str,
        timeout_s: float,
        user_agent: str,
        max_bytes: int,
    ) -> Tuple[int, Dict[str, str], str, int, int]:
        started = time.time()
        headers_out: Dict[str, str] = {}
        status = 0
        size = 0
        body_snip = ""

        conn = None
        try:
            if scheme == "https":
                conn = HTTPSConnection(ip, port=port, timeout=timeout_s, context=self._ssl_ctx)
            else:
                conn = HTTPConnection(ip, port=port, timeout=timeout_s)

            conn.request("GET", path, headers={"User-Agent": user_agent, "Accept": "*/*"})
            resp = conn.getresponse()
            status = int(resp.status or 0)
            for k, v in resp.getheaders():
                if k and v:
                    headers_out[str(k)] = str(v)

            chunk = resp.read(max_bytes)
            size = len(chunk or b"")
            try:
                body_snip = (chunk or b"").decode("utf-8", errors="ignore")
            except Exception:
                body_snip = ""
        except (ConnectionError, TimeoutError, RemoteDisconnected):
            status = 0
        except Exception:
            status = 0
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

        elapsed_ms = int((time.time() - started) * 1000)
        return status, headers_out, body_snip, size, elapsed_ms

    def _db_upsert(
        self,
        *,
        mac: str,
        ip: str,
        hostname: str,
        port: int,
        path: str,
        status: int,
        size: int,
        response_ms: int,
        content_type: str,
        payload: dict,
        user_agent: str,
    ):
        try:
            headers_json = json.dumps(payload, ensure_ascii=True)
        except Exception:
            headers_json = ""

        self.shared_data.db.execute(
            """
            INSERT INTO webenum (
                mac_address, ip, hostname, port, directory, status,
                size, response_time, content_type, tool, method,
                user_agent, headers, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'valkyrie_scout', 'GET', ?, ?, 1)
            ON CONFLICT(mac_address, ip, port, directory) DO UPDATE SET
                status = excluded.status,
                size = excluded.size,
                response_time = excluded.response_time,
                content_type = excluded.content_type,
                hostname = COALESCE(excluded.hostname, webenum.hostname),
                user_agent = COALESCE(excluded.user_agent, webenum.user_agent),
                headers = COALESCE(excluded.headers, webenum.headers),
                last_seen = CURRENT_TIMESTAMP,
                is_active = 1
            """,
            (
                mac or "",
                ip or "",
                hostname or "",
                int(port),
                path or "/",
                int(status),
                int(size or 0),
                int(response_ms or 0),
                content_type or "",
                user_agent or "",
                headers_json,
            ),
        )

    def execute(self, ip, port, row, status_key) -> str:
        if self.shared_data.orchestrator_should_exit:
            return "interrupted"

        try:
            port_i = int(port) if str(port).strip() else int(getattr(self, "port", 80) or 80)
        except Exception:
            port_i = 80

        scheme = _scheme_for_port(port_i)
        hostname = _first_hostname_from_row(row)
        mac = (row.get("MAC Address") or row.get("mac_address") or row.get("mac") or "").strip()

        timeout_s = float(getattr(self.shared_data, "web_probe_timeout_s", 4.0))
        user_agent = str(getattr(self.shared_data, "web_probe_user_agent", "BjornWebScout/1.0"))
        max_bytes = int(getattr(self.shared_data, "web_probe_max_bytes", 65536))
        delay_s = float(getattr(self.shared_data, "valkyrie_delay_s", 0.05))

        paths = getattr(self.shared_data, "valkyrie_scout_paths", None)
        if not isinstance(paths, list) or not paths:
            paths = DEFAULT_PATHS

        # UI
        self.shared_data.bjorn_orch_status = "ValkyrieScout"
        self.shared_data.bjorn_status_text2 = f"{ip}:{port_i}"
        self.shared_data.comment_params = {"ip": ip, "port": str(port_i)}

        progress = ProgressTracker(self.shared_data, len(paths))

        try:
            for p in paths:
                if self.shared_data.orchestrator_should_exit:
                    return "interrupted"

                path = str(p or "/").strip()
                if not path.startswith("/"):
                    path = "/" + path

                status, headers, body, size, elapsed_ms = self._fetch(
                    ip=ip,
                    port=port_i,
                    scheme=scheme,
                    path=path,
                    timeout_s=timeout_s,
                    user_agent=user_agent,
                    max_bytes=max_bytes,
                )

                # Only keep minimal info; do not store full HTML.
                ctype = headers.get("Content-Type") or headers.get("content-type") or ""
                signals = _detect_signals(status, headers, body)

                payload = {
                    "signals": signals,
                    "sample": {"status": int(status), "content_type": ctype, "rt_ms": int(elapsed_ms)},
                }

                try:
                    self._db_upsert(
                        mac=mac,
                        ip=ip,
                        hostname=hostname,
                        port=port_i,
                        path=path,
                        status=status or 0,
                        size=size,
                        response_ms=elapsed_ms,
                        content_type=ctype,
                        payload=payload,
                        user_agent=user_agent,
                    )
                except Exception as e:
                    logger.error(f"DB write failed for {ip}:{port_i}{path}: {e}")

                self.shared_data.comment_params = {
                    "ip": ip,
                    "port": str(port_i),
                    "path": path,
                    "status": str(status),
                    "login": str(int(bool(signals.get("looks_like_login") or signals.get("auth_type")))),
                }
                progress.advance(1)

                if delay_s > 0:
                    time.sleep(delay_s)

            progress.set_complete()
            return "success"
        except Exception as e:
            logger.error(f"ValkyrieScout failed for {ip}:{port_i}: {e}")
            return "failed"
        finally:
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}
            self.shared_data.bjorn_status_text2 = ""


# -------------------- Optional CLI (debug/manual) --------------------
if __name__ == "__main__":
    import argparse
    from shared import SharedData

    parser = argparse.ArgumentParser(description="ValkyrieScout (light web scout)")
    parser.add_argument("--ip", required=True)
    parser.add_argument("--port", default="80")
    args = parser.parse_args()

    sd = SharedData()
    act = ValkyrieScout(sd)
    row = {"MAC Address": sd.get_raspberry_mac() or "__GLOBAL__", "Hostname": ""}
    print(act.execute(args.ip, args.port, row, "ValkyrieScout"))

