#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web_login_profiler.py - Detect login forms and auth controls on web endpoints (no exploitation)."""

import json
import logging
import re
import ssl
import time
from http.client import HTTPConnection, HTTPSConnection, RemoteDisconnected
from typing import Dict, Optional, Tuple

from logger import Logger
from actions.bruteforce_common import ProgressTracker

logger = Logger(name="web_login_profiler.py", level=logging.DEBUG)

# -------------------- Action metadata (AST-friendly) --------------------
b_class = "WebLoginProfiler"
b_module = "web_login_profiler"
b_status = "WebLoginProfiler"
b_port = 80
b_parent = None
b_service = '["http","https"]'
b_trigger = "on_web_service"
b_priority = 55
b_action = "normal"
b_cooldown = 1800
b_rate_limit = "6/86400"
b_enabled = 1
b_timeout = 300
b_max_retries = 2
b_stealth_level = 5
b_risk_level = "low"
b_tags = ["web", "login", "auth", "profiler"]
b_category = "recon"
b_name = "Web Login Profiler"
b_description = "Detects login forms and auth controls on web endpoints."
b_author = "Bjorn Team"
b_version = "2.0.0"
b_icon = "WebLoginProfiler.png"

# Small curated list, cheap but high signal.
DEFAULT_PATHS = [
    "/",
    "/login",
    "/signin",
    "/auth",
    "/admin",
    "/administrator",
    "/wp-login.php",
    "/user/login",
    "/robots.txt",
]

ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


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


def _detect_signals(status: int, headers: Dict[str, str], body_snippet: str) -> Dict[str, object]:
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    www = h.get("www-authenticate", "")
    set_cookie = h.get("set-cookie", "")

    auth_type = None
    if status == 401 and "basic" in www.lower():
        auth_type = "basic"
    elif status == 401 and "digest" in www.lower():
        auth_type = "digest"

    # Very cheap login form heuristics
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

    # Rate limit / lockout hints
    rate_limited = (status == 429) or ("retry-after" in h) or ("x-ratelimit-remaining" in h)

    cookie_names = []
    if set_cookie:
        # Parse only cookie names cheaply
        for part in set_cookie.split(","):
            name = part.split(";", 1)[0].split("=", 1)[0].strip()
            if name and name not in cookie_names:
                cookie_names.append(name)

    framework_hints = []
    for cn in cookie_names:
        l = cn.lower()
        if l in {"csrftoken", "sessionid"}:
            framework_hints.append("django")
        elif l in {"laravel_session", "xsrf-token"}:
            framework_hints.append("laravel")
        elif l == "phpsessid":
            framework_hints.append("php")
        elif "wordpress" in l:
            framework_hints.append("wordpress")

    server = h.get("server", "")
    powered = h.get("x-powered-by", "")

    return {
        "auth_type": auth_type,
        "looks_like_login": bool(looks_like_login),
        "has_csrf": bool(has_csrf),
        "rate_limited_hint": bool(rate_limited),
        "server": server,
        "x_powered_by": powered,
        "cookie_names": cookie_names[:12],
        "framework_hints": sorted(set(framework_hints))[:6],
    }


class WebLoginProfiler:
    def __init__(self, shared_data):
        self.shared_data = shared_data
        self._ssl_ctx = ssl._create_unverified_context()

    def _db_upsert(self, *, mac: str, ip: str, hostname: str, port: int, path: str,
                   status: int, size: int, response_ms: int, content_type: str,
                   method: str, user_agent: str, headers_json: str):
        self.shared_data.db.execute(
            """
            INSERT INTO webenum (
                mac_address, ip, hostname, port, directory, status,
                size, response_time, content_type, tool, method,
                user_agent, headers, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'login_profiler', ?, ?, ?, 1)
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
                method or "GET",
                user_agent or "",
                headers_json or "",
            ),
        )

    def _fetch(self, *, ip: str, port: int, scheme: str, path: str, timeout_s: float,
               user_agent: str) -> Tuple[int, Dict[str, str], str, int, int]:
        started = time.time()
        body_snip = ""
        headers_out: Dict[str, str] = {}
        status = 0
        size = 0

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

            # Read only a small chunk (Pi-friendly) for fingerprinting.
            chunk = resp.read(65536)  # 64KB
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
        user_agent = str(getattr(self.shared_data, "web_probe_user_agent", "BjornWebProfiler/1.0"))
        paths = getattr(self.shared_data, "web_login_profiler_paths", None) or DEFAULT_PATHS
        if not isinstance(paths, list):
            paths = DEFAULT_PATHS

        self.shared_data.bjorn_orch_status = "WebLoginProfiler"
        self.shared_data.bjorn_status_text2 = f"{ip}:{port_i}"
        self.shared_data.comment_params = {"ip": ip, "port": str(port_i)}

        progress = ProgressTracker(self.shared_data, len(paths))
        found_login = 0

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
                )

                ctype = headers.get("Content-Type") or headers.get("content-type") or ""
                signals = _detect_signals(status, headers, body)
                if signals.get("looks_like_login") or signals.get("auth_type"):
                    found_login += 1

                headers_payload = {
                    "signals": signals,
                    "sample": {
                        "status": status,
                        "content_type": ctype,
                    },
                }

                try:
                    headers_json = json.dumps(headers_payload, ensure_ascii=True)
                except Exception:
                    headers_json = ""

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
                        method="GET",
                        user_agent=user_agent,
                        headers_json=headers_json,
                    )
                except Exception as e:
                    logger.error(f"DB write failed for {ip}:{port_i}{path}: {e}")

                self.shared_data.comment_params = {
                    "ip": ip,
                    "port": str(port_i),
                    "path": path,
                    "login": str(int(bool(signals.get("looks_like_login") or signals.get("auth_type")))),
                }

                progress.advance(1)

            progress.set_complete()
            # "success" means: profiler ran; not that a login exists.
            logger.info(f"WebLoginProfiler done for {ip}:{port_i} (login_surfaces={found_login})")
            return "success"
        except Exception as e:
            logger.error(f"WebLoginProfiler failed for {ip}:{port_i}: {e}")
            return "failed"
        finally:
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}
            self.shared_data.bjorn_status_text2 = ""

