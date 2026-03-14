# web_utils/netkb_utils.py
"""
Network Knowledge Base utilities.
Handles network discovery data, host information, and action queue management.
"""
from __future__ import annotations
import json
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs
import logging
from logger import Logger
logger = Logger(name="netkb_utils.py", level=logging.DEBUG)

class NetKBUtils:
    """Utilities for network knowledge base management."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

    def serve_netkb_data_json(self, handler):
        """Serve network knowledge base as simple JSON (IPs, ports, actions)."""
        try:
            hosts = self.shared_data.db.get_all_hosts()
            actions_meta = self.shared_data.db.list_actions()
            action_names = [a["b_class"] for a in actions_meta]

            alive = [h for h in hosts if int(h.get("alive") or 0) == 1]
            response_data = {
                "ips": [h.get("ips", "") for h in alive],
                "ports": {h.get("ips", ""): (h.get("ports", "") or "").split(';') for h in alive},
                "actions": action_names
            }
            handler.send_response(200)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps(response_data).encode("utf-8"))
        except Exception as e:
            handler.send_response(500)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))

    def serve_netkb_data(self, handler):
        """Serve detailed network knowledge base data with action statuses."""
        try:
            db = self.shared_data.db
            hosts = db.get_all_hosts()
            actions = [a["b_class"] for a in db.list_actions()]

            response = []
            for h in hosts:
                mac = h.get("mac_address", "")
                ips_txt = h.get("ips", "") or ""
                ips_list = [p for p in ips_txt.split(';') if p]
                primary_ip = ips_list[0] if ips_list else ""

                row = {
                    "mac": mac,
                    "ip": primary_ip,
                    "ips": ips_list,
                    "hostname": h.get("hostnames", ""),
                    "ports": (h.get("ports", "") or "").split(';') if h.get("ports") else [],
                    "alive": int(h.get("alive") or 0) == 1,
                    "vendor": h.get("vendor", ""),
                    "essid": h.get("essid", ""),
                    "actions": []
                }

                # Get action status from queue (compatible with UI 'raw' format)
                for a in actions:
                    st = db.get_action_status_from_queue(a, mac)
                    if st:
                        ts = st.get("completed_at") or st.get("started_at") or st.get("created_at") or ""
                        ts_compact = ts.replace("-", "").replace(":", "").replace(" ", "_") if ts else ""
                        status_raw = f"{st['status']}_{ts_compact}" if ts_compact else ""
                    else:
                        status_raw = ""
                    row["actions"].append({"name": a, "status": status_raw})

                response.append(row)

            handler.send_response(200)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps(response).encode("utf-8"))

        except Exception as e:
            handler.send_response(500)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))

    def serve_network_data(self, handler):
        """Serve network data as HTML table."""
        try:
            import html as _html
            rows = ['<table><tr><th>ESSID</th><th>IP</th><th>Hostname</th><th>MAC Address</th><th>Vendor</th><th>Ports</th></tr>']
            for h in self.shared_data.db.get_all_hosts():
                if int(h.get("alive") or 0) != 1:
                    continue
                rows.append(
                    f"<tr><td>{_html.escape(str(h.get('essid') or ''))}</td>"
                    f"<td>{_html.escape(str(h.get('ips') or ''))}</td>"
                    f"<td>{_html.escape(str(h.get('hostnames') or ''))}</td>"
                    f"<td>{_html.escape(str(h.get('mac_address') or ''))}</td>"
                    f"<td>{_html.escape(str(h.get('vendor') or ''))}</td>"
                    f"<td>{_html.escape(str(h.get('ports') or ''))}</td></tr>"
                )
            rows.append("</table>")
            table_html = "\n".join(rows)
            handler.send_response(200)
            handler.send_header("Content-type", "text/html")
            handler.end_headers()
            handler.wfile.write(table_html.encode("utf-8"))
        except Exception as e:
            handler.send_response(500)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))

    def delete_netkb_action(self, data):
        """Clear action history (queue) for a host found by IP."""
        try:
            ip = (data or {}).get("ip")
            action = (data or {}).get("action")
            cancel_active = bool((data or {}).get("cancel") or (data or {}).get("cancel_active"))

            if not ip or not action:
                return {"status": "error", "message": "Missing 'ip' or 'action'"}

            # Find MAC by IP
            rows = self.shared_data.db.query(
                "SELECT mac_address FROM hosts WHERE ips LIKE ? LIMIT 1", (f"%{ip}%",)
            )
            if not rows:
                return {"status": "error", "message": f"No host found for IP {ip}"}
            mac = rows[0]["mac_address"]

            cancelled = 0
            if cancel_active:
                cancelled = self.shared_data.db.execute("""
                    UPDATE action_queue
                    SET status='cancelled',
                        completed_at=CURRENT_TIMESTAMP,
                        error_message=COALESCE(error_message,'user_cancelled')
                    WHERE mac_address=? AND action_name=?
                    AND status IN ('scheduled','pending','running')
                """, (mac, action))

            # Clear finished statuses
            cleared = self.shared_data.db.execute(
                """
                DELETE FROM action_queue
                WHERE mac_address=? AND action_name=?
                AND status IN ('success','failed','expired','cancelled')
                """,
                (mac, action),
            )

            msg = f"Action '{action}' cleared for IP {ip} (deleted {cleared}"
            if cancel_active:
                msg += f", cancelled {cancelled}"
            msg += ")"

            return {"status": "success", "message": msg}

        except Exception as e:
            self.logger.error(f"delete_netkb_action error: {e}")
            return {"status": "error", "message": str(e)}

    def delete_all_actions(self, data=None):
        """Cancel running actions then clear entire action queue."""
        try:
            # First cancel any running/pending/scheduled actions
            cancelled = self.shared_data.db.execute("""
                UPDATE action_queue
                SET status='cancelled',
                    completed_at=CURRENT_TIMESTAMP,
                    error_message=COALESCE(error_message,'user_cancelled')
                WHERE status IN ('scheduled','pending','running')
            """)
            # Then delete everything
            deleted = self.shared_data.db.execute("DELETE FROM action_queue")
            return {
                "status": "success",
                "message": f"Cancelled {cancelled} active, cleared {deleted} total entries"
            }
        except Exception as e:
            self.logger.error(f"delete_all_actions error: {e}")
            return {"status": "error", "message": str(e)}

    def serve_attempt_history(self, handler):
        """Get action attempt history with superseded detection."""
        try:
            from urllib.parse import urlparse, parse_qs
            url = urlparse(handler.path or "")
            qs = parse_qs(url.query or "")

            action = (qs.get("action", [""])[0] or "").strip()
            mac = (qs.get("mac", qs.get("mac_address", [""]))[0] or "").strip()
            port = int((qs.get("port", ["0"])[0] or 0))
            limit = int((qs.get("limit", ["200"])[0] or 200))
            include_superseded = (qs.get("include_superseded", ["true"])[0] or "true").lower() in ("1", "true", "yes", "on")

            if not action or not mac:
                raise ValueError("missing required parameters: action, mac")

            db = self.shared_data.db

            rows = db.query("""
                SELECT id, action_name, mac_address, ip, port, hostname, service,
                    status, retry_count, max_retries,
                    priority,
                    created_at, started_at, completed_at, scheduled_for,
                    error_message, result_summary,
                    COALESCE(completed_at, started_at, scheduled_for, created_at) AS ts
                FROM action_queue
                WHERE action_name = ?
                AND COALESCE(mac_address,'') = ?
                AND COALESCE(port,0) = ?
                ORDER BY datetime(COALESCE(completed_at, started_at, scheduled_for, created_at)) DESC
                LIMIT ?
            """, (action, mac, port, limit))

            # Compute "superseded" status
            last_success_ts = None
            for r in rows:
                st = (r.get("status") or "").lower()
                if st == "success":
                    last_success_ts = r.get("ts")
                    break

            attempts = []
            for r in rows:
                st_raw = (r.get("status") or "").lower()
                is_sup = False
                if st_raw in ("failed", "expired", "cancelled") and last_success_ts:
                    ts = r.get("ts") or ""
                    if ts and ts < last_success_ts:
                        is_sup = True

                st_display = "superseded" if is_sup else st_raw

                attempts.append({
                    "id": r.get("id"),
                    "action_name": r.get("action_name"),
                    "mac_address": r.get("mac_address"),
                    "ip": r.get("ip"),
                    "port": r.get("port"),
                    "hostname": r.get("hostname"),
                    "service": r.get("service"),
                    "status": st_raw,
                    "status_display": st_display,
                    "superseded": bool(is_sup),
                    "retry_count": r.get("retry_count"),
                    "max_retries": r.get("max_retries"),
                    "priority": r.get("priority"),
                    "ts": r.get("ts"),
                    "created_at": r.get("created_at"),
                    "started_at": r.get("started_at"),
                    "completed_at": r.get("completed_at"),
                    "scheduled_for": r.get("scheduled_for"),
                    "error_message": r.get("error_message"),
                    "result_summary": r.get("result_summary"),
                })

            if not include_superseded:
                attempts = [a for a in attempts if not (a["superseded"] and a["status"] in ("failed", "expired", "cancelled"))]

            handler.send_response(200)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps(attempts).encode("utf-8"))

        except Exception as e:
            handler.send_response(400)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))

    def serve_action_queue(self, handler):
        """Return action queue with effective priority calculation."""
        try:
            db = self.shared_data.db
            rows = db.query("""
                SELECT id, action_name, mac_address, ip, port, hostname, service, priority, status,
                    retry_count, max_retries, created_at, scheduled_for, started_at, completed_at,
                    expires_at, error_message, result_summary, tags, metadata,
                    MIN(100, priority + CAST((strftime('%s','now') - strftime('%s',created_at))/300 AS INTEGER)) AS priority_effective
                FROM action_queue
                ORDER BY 
                    CASE status
                        WHEN 'running'   THEN 0
                        WHEN 'pending'   THEN 1
                        WHEN 'scheduled' THEN 2
                        WHEN 'success'   THEN 3
                        WHEN 'failed'    THEN 4
                        WHEN 'expired'   THEN 5
                        WHEN 'cancelled' THEN 6
                        ELSE 7
                    END,
                    CASE 
                        WHEN status = 'pending' THEN priority_effective 
                        ELSE priority 
                    END DESC,
                    CASE 
                        WHEN status = 'pending' THEN datetime(COALESCE(scheduled_for, created_at))
                        ELSE datetime(COALESCE(completed_at, started_at, scheduled_for, created_at))
                    END ASC
                LIMIT 1000
            """)
            out = []
            for r in rows:
                md = {}
                if r.get("metadata"):
                    try:
                        md = json.loads(r["metadata"])
                    except Exception:
                        md = {}
                tg = []
                if r.get("tags"):
                    try:
                        tg = json.loads(r["tags"])
                    except Exception:
                        tg = []
                out.append({
                    "id": r["id"],
                    "action_name": r["action_name"],
                    "mac_address": r["mac_address"],
                    "ip": r["ip"],
                    "port": r["port"],
                    "hostname": r["hostname"],
                    "service": r["service"],
                    "priority": r["priority"],
                    "priority_effective": r["priority_effective"],
                    "status": r["status"],
                    "retry_count": r["retry_count"],
                    "max_retries": r["max_retries"],
                    "created_at": r["created_at"],
                    "scheduled_for": r["scheduled_for"],
                    "started_at": r["started_at"],
                    "completed_at": r["completed_at"],
                    "expires_at": r["expires_at"],
                    "error_message": r["error_message"],
                    "result_summary": r["result_summary"],
                    "tags": tg,
                    "metadata": md,
                    "timeout": int(md.get("timeout", 900))
                })
            handler.send_response(200)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps(out).encode("utf-8"))
        except Exception as e:
            handler.send_response(500)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))

    def handle_queue_cmd(self, handler):
        """Handle queue commands: cancel, retry, bump, delete."""
        try:
            ln = int(handler.headers.get("Content-Length", "0") or 0)
            payload = json.loads(handler.rfile.read(ln) or "{}")
            cmd = (payload.get("cmd") or "").strip().lower()
            qid = int(payload.get("id"))
            delta = int(payload.get("delta") or 10)

            db = self.shared_data.db
            rc = 0

            if cmd == "cancel":
                rc = db.execute("""
                    UPDATE action_queue
                    SET status='cancelled', completed_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status IN ('scheduled','pending','running')
                """, (qid,))

            elif cmd == "retry":
                rc = db.execute("""
                    UPDATE action_queue
                    SET status='pending',
                        scheduled_for=datetime('now'),
                        error_message=NULL,
                        result_summary=NULL,
                        started_at=NULL,
                        completed_at=NULL
                    WHERE id=? AND status IN ('failed','expired','cancelled','scheduled')
                """, (qid,))

            elif cmd == "bump":
                rc = db.execute("""
                    UPDATE action_queue
                    SET priority = MIN(100, COALESCE(priority,50) + ?)
                    WHERE id=?
                """, (delta, qid))

            elif cmd == "delete":
                rc = db.execute("""
                    DELETE FROM action_queue
                    WHERE id=? AND status IN ('success','failed','expired','cancelled')
                """, (qid,))
            else:
                raise ValueError("unknown cmd")

            handler.send_response(200)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "success", "rowcount": rc}).encode("utf-8"))
        except Exception as e:
            handler.send_response(400)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))
