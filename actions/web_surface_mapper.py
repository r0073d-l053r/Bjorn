#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web_surface_mapper.py - Aggregate login_profiler findings into a per-target risk score."""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from logger import Logger
from actions.bruteforce_common import ProgressTracker

logger = Logger(name="web_surface_mapper.py", level=logging.DEBUG)

# -------------------- Action metadata (AST-friendly) --------------------
b_class = "WebSurfaceMapper"
b_module = "web_surface_mapper"
b_status = "WebSurfaceMapper"
b_port = 80
b_parent = None
b_service = '["http","https"]'
b_trigger = "on_success:WebLoginProfiler"
b_priority = 45
b_action = "normal"
b_cooldown = 600
b_rate_limit = "48/86400"
b_enabled = 1
b_timeout = 300
b_max_retries = 2
b_stealth_level = 6
b_risk_level = "low"
b_tags = ["web", "login", "risk", "mapper"]
b_category = "recon"
b_name = "Web Surface Mapper"
b_description = "Aggregates login profiler findings into a per-target risk score."
b_author = "Bjorn Team"
b_version = "2.0.0"
b_icon = "WebSurfaceMapper.png"


def _scheme_for_port(port: int) -> str:
    https_ports = {443, 8443, 9443, 10443, 9444, 5000, 5001, 7080, 9080}
    return "https" if int(port) in https_ports else "http"


def _safe_json_loads(s: str) -> dict:
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


def _score_signals(signals: dict) -> int:
    """
    Heuristic risk score 0..100.
    This is not an "attack recommendation"; it's a prioritization for recon.
    """
    if not isinstance(signals, dict):
        return 0
    score = 0

    auth = str(signals.get("auth_type") or "").lower()
    if auth in {"basic", "digest"}:
        score += 45

    if bool(signals.get("looks_like_login")):
        score += 35

    if bool(signals.get("has_csrf")):
        score += 10

    if bool(signals.get("rate_limited_hint")):
        # Defensive signal: reduces priority for noisy follow-ups.
        score -= 25

    hints = signals.get("framework_hints") or []
    if isinstance(hints, list) and hints:
        score += min(10, 3 * len(hints))

    return max(0, min(100, int(score)))


class WebSurfaceMapper:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    def _db_upsert_summary(
        self,
        *,
        mac: str,
        ip: str,
        hostname: str,
        port: int,
        scheme: str,
        summary: dict,
    ):
        directory = "/__surface_summary__"
        payload = json.dumps(summary, ensure_ascii=True)
        self.shared_data.db.execute(
            """
            INSERT INTO webenum (
                mac_address, ip, hostname, port, directory, status,
                size, response_time, content_type, tool, method,
                user_agent, headers, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'surface_mapper', 'SUMMARY', '', ?, 1)
            ON CONFLICT(mac_address, ip, port, directory) DO UPDATE SET
                status = excluded.status,
                size = excluded.size,
                response_time = excluded.response_time,
                content_type = excluded.content_type,
                hostname = COALESCE(excluded.hostname, webenum.hostname),
                headers = COALESCE(excluded.headers, webenum.headers),
                last_seen = CURRENT_TIMESTAMP,
                is_active = 1
            """,
            (
                mac or "",
                ip or "",
                hostname or "",
                int(port),
                directory,
                200,
                len(payload),
                0,
                "application/json",
                payload,
            ),
        )

    def execute(self, ip, port, row, status_key) -> str:
        if self.shared_data.orchestrator_should_exit:
            return "interrupted"

        mac = (row.get("MAC Address") or row.get("mac_address") or row.get("mac") or "").strip()
        hostname = (row.get("Hostname") or row.get("hostname") or "").strip()
        if ";" in hostname:
            hostname = hostname.split(";", 1)[0].strip()

        try:
            port_i = int(port) if str(port).strip() else 80
        except Exception:
            port_i = 80

        scheme = _scheme_for_port(port_i)

        self.shared_data.bjorn_orch_status = "WebSurfaceMapper"
        self.shared_data.bjorn_status_text2 = f"{ip}:{port_i}"
        self.shared_data.comment_params = {"ip": ip, "port": str(port_i), "phase": "score"}

        # Load recent profiler rows for this target.
        rows: List[Dict[str, Any]] = []
        try:
            rows = self.shared_data.db.query(
                """
                SELECT directory, status, content_type, headers, response_time, last_seen
                FROM webenum
                WHERE mac_address=? AND ip=? AND port=? AND is_active=1 AND tool='login_profiler'
                ORDER BY last_seen DESC
                """,
                (mac or "", ip, int(port_i)),
            )
        except Exception as e:
            logger.error(f"DB query failed (webenum login_profiler): {e}")
            rows = []

        progress = ProgressTracker(self.shared_data, max(1, len(rows)))
        scored: List[Tuple[int, str, int, str, dict]] = []

        try:
            for r in rows:
                if self.shared_data.orchestrator_should_exit:
                    return "interrupted"

                directory = str(r.get("directory") or "/")
                status = int(r.get("status") or 0)
                ctype = str(r.get("content_type") or "")
                h = _safe_json_loads(str(r.get("headers") or ""))
                signals = h.get("signals") if isinstance(h, dict) else {}
                score = _score_signals(signals if isinstance(signals, dict) else {})
                scored.append((score, directory, status, ctype, signals if isinstance(signals, dict) else {}))

                self.shared_data.comment_params = {
                    "ip": ip,
                    "port": str(port_i),
                    "path": directory,
                    "score": str(score),
                }
                progress.advance(1)

            scored.sort(key=lambda t: (t[0], t[2]), reverse=True)
            top = scored[:5]
            avg = int(sum(s for s, *_ in scored) / max(1, len(scored))) if scored else 0
            top_path = top[0][1] if top else ""
            top_score = top[0][0] if top else 0

            summary = {
                "ip": ip,
                "port": int(port_i),
                "scheme": scheme,
                "count_profiled": int(len(rows)),
                "avg_score": int(avg),
                "top": [
                    {"score": int(s), "path": p, "status": int(st), "content_type": ct, "signals": sig}
                    for (s, p, st, ct, sig) in top
                ],
                "ts_epoch": int(time.time()),
            }

            try:
                self._db_upsert_summary(
                    mac=mac,
                    ip=ip,
                    hostname=hostname,
                    port=port_i,
                    scheme=scheme,
                    summary=summary,
                )
            except Exception as e:
                logger.error(f"DB upsert summary failed: {e}")

            self.shared_data.comment_params = {
                "ip": ip,
                "port": str(port_i),
                "count": str(len(rows)),
                "top_path": top_path,
                "top_score": str(top_score),
                "avg_score": str(avg),
            }

            progress.set_complete()
            return "success"
        except Exception as e:
            logger.error(f"WebSurfaceMapper failed for {ip}:{port_i}: {e}")
            return "failed"
        finally:
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}
            self.shared_data.bjorn_status_text2 = ""

