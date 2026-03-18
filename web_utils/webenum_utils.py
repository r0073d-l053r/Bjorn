"""webenum_utils.py - REST utilities for web enumeration data."""
from __future__ import annotations
import json
import base64
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional, List
import logging
from logger import Logger
logger = Logger(name="webenum_utils.py", level=logging.DEBUG)
class WebEnumUtils:
    """
    REST utilities for Web Enumeration (table `webenum`).

    Resilient to missing `shared_data` at construction:
    - If `self.shared_data` is None, handlers try to read `handler.shared_data`.
    Expects a DB adapter at `shared_data.db` exposing: query, query_one, execute.
    """

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

        # Anti-flapping: serve a recent non-empty payload when DB hiccups
        self._last_payload: Dict[str, Any] = {}
        self._last_ts: float = 0.0
        self._snapshot_ttl: float = 8.0  # seconds

    # ---------------------- Internal helpers ----------------------

    def _resolve_shared(self, handler) -> Any:
        """Resolve SharedData from self or the HTTP handler."""
        sd = self.shared_data or getattr(handler, "shared_data", None)
        if sd is None or getattr(sd, "db", None) is None:
            # Return a clear 503 later if unavailable
            raise RuntimeError("SharedData.db is not available (wire shared_data into WebEnumUtils or handler).")
        return sd

    def _to_jsonable(self, obj):
        if obj is None or isinstance(obj, (bool, int, float, str)):
            return obj
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, bytes):
            return {"_b64": base64.b64encode(obj).decode("ascii")}
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: self._to_jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [self._to_jsonable(v) for v in obj]
        return str(obj)

    def _json(self, handler, code: int, obj):
        safe = self._to_jsonable(obj)
        payload = json.dumps(safe, ensure_ascii=False).encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        try:
            handler.wfile.write(payload)
        except BrokenPipeError:
            pass

    # ---------------------- Stats & DB helpers ----------------------

    def _get_webenum_stats(self, db) -> Dict[str, int]:
        """Global stats for filters/summary badges."""
        try:
            stats = db.query_one("""
                SELECT 
                    COUNT(*) as total_results,
                    COUNT(DISTINCT hostname) as unique_hosts,
                    COUNT(CASE WHEN status BETWEEN 200 AND 299 THEN 1 END) as success_2xx,
                    COUNT(CASE WHEN status BETWEEN 300 AND 399 THEN 1 END) as redirect_3xx,
                    COUNT(CASE WHEN status BETWEEN 400 AND 499 THEN 1 END) as client_error_4xx,
                    COUNT(CASE WHEN status >= 500 THEN 1 END) as server_error_5xx
                FROM webenum 
                WHERE is_active = 1
            """) or {}
            return {
                'total_results': stats.get('total_results', 0) or 0,
                'unique_hosts': stats.get('unique_hosts', 0) or 0,
                'success_2xx': stats.get('success_2xx', 0) or 0,
                'redirect_3xx': stats.get('redirect_3xx', 0) or 0,
                'client_error_4xx': stats.get('client_error_4xx', 0) or 0,
                'server_error_5xx': stats.get('server_error_5xx', 0) or 0
            }
        except Exception as e:
            self.logger.error(f"Error getting webenum stats: {e}")
            return {
                'total_results': 0,
                'unique_hosts': 0,
                'success_2xx': 0,
                'redirect_3xx': 0,
                'client_error_4xx': 0,
                'server_error_5xx': 0
            }

    def add_webenum_result(
        self,
        db,
        mac_address: str,
        ip: str,
        hostname: Optional[str],
        port: int,
        directory: str,
        status: int,
        size: int = 0,
        response_time: int = 0,
        content_type: Optional[str] = None,
        tool: str = 'gobuster'
    ) -> None:
        """Insert/Upsert a single result into `webenum`."""
        try:
            db.execute("""
                INSERT INTO webenum (
                    mac_address, ip, hostname, port, directory, status, 
                    size, response_time, content_type, tool, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(mac_address, ip, port, directory) DO UPDATE SET
                    status = excluded.status,
                    size = excluded.size,
                    response_time = excluded.response_time,
                    content_type = excluded.content_type,
                    hostname = COALESCE(excluded.hostname, webenum.hostname),
                    tool = COALESCE(excluded.tool, webenum.tool),
                    last_seen = CURRENT_TIMESTAMP,
                    is_active = 1
            """, (mac_address, ip, hostname, port, directory, status,
                  size, response_time, content_type, tool))
            self.logger.debug(f"Added webenum result: {ip}:{port}{directory} -> {status}")
        except Exception as e:
            self.logger.error(f"Error adding webenum result: {e}")

    # ---------------------- REST handlers ----------------------

    def serve_webenum_data(self, handler):
        """GET /api/webenum/results : list + pagination + filters + stats."""
        try:
            sd = self._resolve_shared(handler)
            db = sd.db

            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(handler.path).query)

            # Pagination
            page = max(1, int(query.get('page', ['1'])[0]))
            limit = max(1, min(500, int(query.get('limit', ['50'])[0])))
            offset = (page - 1) * limit

            # Filters
            host_filter = (query.get('host', [''])[0]).strip()
            status_filter = (query.get('status', [''])[0]).strip()
            port_filter = (query.get('port', [''])[0]).strip()
            date_filter = (query.get('date', [''])[0]).strip()
            search = (query.get('search', [''])[0]).strip()

            # WHERE construction
            where_clauses = ["is_active = 1"]
            params: List[Any] = []

            if host_filter:
                # Match either hostname or IP when the frontend sends "host"
                where_clauses.append("(hostname = ? OR ip = ?)")
                params.extend([host_filter, host_filter])

            if status_filter:
                if status_filter == '2xx':
                    where_clauses.append("status BETWEEN 200 AND 299")
                elif status_filter == '3xx':
                    where_clauses.append("status BETWEEN 300 AND 399")
                elif status_filter == '4xx':
                    where_clauses.append("status BETWEEN 400 AND 499")
                elif status_filter == '5xx':
                    where_clauses.append("status >= 500")
                else:
                    try:
                        s_val = int(status_filter)
                        where_clauses.append("status = ?")
                        params.append(s_val)
                    except ValueError:
                        pass

            if port_filter:
                try:
                    where_clauses.append("port = ?")
                    params.append(int(port_filter))
                except ValueError:
                    pass

            if date_filter:
                # expected YYYY-MM-DD
                where_clauses.append("DATE(scan_date) = ?")
                params.append(date_filter)

            if search:
                where_clauses.append("""(
                    hostname LIKE ? OR 
                    ip LIKE ? OR 
                    directory LIKE ? OR 
                    CAST(status AS TEXT) LIKE ?
                )""")
                search_term = f"%{search}%"
                params.extend([search_term] * 4)

            where_sql = " AND ".join(where_clauses)

            # Main query - alias columns to match the frontend schema
            results = db.query(f"""
                SELECT
                    id,
                    mac_address AS mac,
                    ip,
                    COALESCE(hostname, ip) AS host,
                    port,
                    directory,
                    status,
                    size,
                    response_time,
                    content_type,
                    scan_date,
                    tool
                FROM webenum
                WHERE {where_sql}
                ORDER BY scan_date DESC, host ASC, port ASC
                LIMIT ? OFFSET ?
            """, params + [limit, offset])

            # Total for pagination
            total_row = db.query_one(f"""
                SELECT COUNT(*) AS total FROM webenum WHERE {where_sql}
            """, params) or {"total": 0}
            total = total_row.get("total", 0) or 0

            # Stats + filter options
            stats = self._get_webenum_stats(db)

            hosts = db.query("""
                SELECT DISTINCT hostname
                FROM webenum
                WHERE hostname IS NOT NULL AND hostname <> '' AND is_active = 1
                ORDER BY hostname
            """)
            ports = db.query("""
                SELECT DISTINCT port
                FROM webenum
                WHERE is_active = 1
                ORDER BY port
            """)

            payload = {
                "results": results,
                "total": total,
                "page": page,
                "limit": limit,
                "stats": stats,
                "filters": {
                    "hosts": [h['hostname'] for h in hosts if 'hostname' in h],
                    "ports": [p['port'] for p in ports if 'port' in p]
                }
            }

            # Anti-flapping: if now empty but a recent snapshot exists, return it
            now = time.time()
            if total == 0 and self._last_payload and (now - self._last_ts) <= self._snapshot_ttl:
                return self._json(handler, 200, self._last_payload)

            # Update snapshot
            self._last_payload = payload
            self._last_ts = now
            return self._json(handler, 200, payload)

        except RuntimeError as e:
            # Clear 503 when shared_data/db is not wired
            self.logger.error(str(e))
            return self._json(handler, 503, {"status": "error", "message": str(e)})
        except Exception as e:
            self.logger.error(f"Error serving webenum data: {e}")
            now = time.time()
            if self._last_payload and (now - self._last_ts) <= self._snapshot_ttl:
                self.logger.warning("/api/webenum/results fallback to snapshot after error")
                return self._json(handler, 200, self._last_payload)
            return self._json(handler, 500, {"status": "error", "message": str(e)})

    def import_webenum_results(self, handler, data: Dict[str, Any]):
        """POST /api/webenum/import : bulk import {results:[...] }."""
        try:
            sd = self._resolve_shared(handler)
            db = sd.db

            results = data.get('results', []) or []
            imported = 0

            for r in results:
                # Accept both (`hostname`, `mac_address`) and (`host`, `mac`)
                hostname = r.get('hostname') or r.get('host')
                mac_address = r.get('mac_address') or r.get('mac') or ''
                self.add_webenum_result(
                    db=db,
                    mac_address=mac_address,
                    ip=r.get('ip', '') or '',
                    hostname=hostname,
                    port=int(r.get('port', 80) or 80),
                    directory=r.get('directory', '/') or '/',
                    status=int(r.get('status', 0) or 0),
                    size=int(r.get('size', 0) or 0),
                    response_time=int(r.get('response_time', 0) or 0),
                    content_type=r.get('content_type'),
                    tool=r.get('tool', 'import') or 'import'
                )
                imported += 1

            return self._json(handler, 200, {
                "status": "success",
                "message": f"Imported {imported} web enumeration results",
                "imported": imported
            })

        except RuntimeError as e:
            self.logger.error(str(e))
            return self._json(handler, 503, {"status": "error", "message": str(e)})
        except Exception as e:
            self.logger.error(f"Error importing webenum results: {e}")
            return self._json(handler, 500, {
                "status": "error",
                "message": str(e)
            })
