"""studio_utils.py - Action/edge/host management for the visual workflow editor."""
from __future__ import annotations
import json
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs
import logging
from logger import Logger
logger = Logger(name="studio_utils.py", level=logging.DEBUG)

class StudioUtils:
    """Utilities for studio visual editor operations."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

    def studio_get_actions_studio(self, handler):
        """Get all studio actions with positions and metadata."""
        try:
            rows = self.shared_data.db.get_studio_actions()
            return self._write_json(handler, {"status": "ok", "data": rows})
        except Exception as e:
            self.logger.error(f"studio_get_actions error: {e}")
            return self._write_json(handler, {"status": "error", "message": str(e)}, 500)

    def studio_get_actions_db(self, handler):
        """Get all runtime actions from DB."""
        try:
            rows = self.shared_data.db.get_db_actions()
            return self._write_json(handler, {"status": "ok", "data": rows})
        except Exception as e:
            self.logger.error(f"studio_get_actions_db error: {e}")
            return self._write_json(handler, {"status": "error", "message": str(e)}, 500)

    def studio_get_edges(self, handler):
        """Get all studio edges (connections between actions)."""
        try:
            rows = self.shared_data.db.get_studio_edges()
            return self._write_json(handler, {"status": "ok", "data": rows})
        except Exception as e:
            self.logger.error(f"studio_get_edges error: {e}")
            return self._write_json(handler, {"status": "error", "message": str(e)}, 500)

    def studio_get_hosts(self, handler):
        """Get hosts for studio (real + simulated)."""
        try:
            qs = parse_qs(urlparse(handler.path).query)
            include_real = qs.get('include_real', ['1'])[0] not in ('0', 'false', 'False')
            rows = self.shared_data.db.get_studio_hosts(include_real=include_real)
            return self._write_json(handler, {"status": "ok", "data": rows})
        except Exception as e:
            self.logger.error(f"studio_get_hosts error: {e}")
            return self._write_json(handler, {"status": "error", "message": str(e)}, 500)

    def studio_load_layout(self, handler):
        """Load a saved studio layout."""
        try:
            qs = parse_qs(urlparse(handler.path).query)
            name = (qs.get('name', [''])[0] or '').strip()
            if not name:
                return self._write_json(handler, {"status": "error", "message": "Missing layout name"}, 400)
            
            row = self.shared_data.db.load_studio_layout(name)
            if not row:
                return self._write_json(handler, {"status": "error", "message": "Layout not found"}, 404)
            return self._write_json(handler, {"status": "ok", "data": row})
        except Exception as e:
            self.logger.error(f"studio_load_layout error: {e}")
            return self._write_json(handler, {"status": "error", "message": str(e)}, 500)

    def studio_sync_actions_studio(self):
        """Import values from 'actions' table to 'actions_studio' (non-destructive)."""
        try:
            self.shared_data.db._sync_actions_studio_schema_and_rows()
            return {
                "status": "ok",
                "message": "Import from 'actions' completed (non-destructive). Save manually."
            }
        except Exception as e:
            self.logger.error(f"studio_sync_actions error: {e}")
            return {"status": "error", "message": str(e)}

    def studio_update_action(self, data: dict):
        """Update action studio properties."""
        try:
            b_class = (data.get('b_class') or '').strip()
            updates = data.get('updates') or {}
            if not b_class or not isinstance(updates, dict) or not updates:
                return {"status": "error", "message": "Missing b_class or updates"}
            self.shared_data.db.update_studio_action(b_class, updates)
            return {"status": "ok", "message": "Action updated"}
        except Exception as e:
            self.logger.error(f"studio_update_action error: {e}")
            return {"status": "error", "message": str(e)}

    def studio_upsert_edge(self, data: dict):
        """Create or update an edge between actions."""
        try:
            fa = (data.get('from_action') or '').strip()
            ta = (data.get('to_action') or '').strip()
            et = (data.get('edge_type') or 'requires').strip()
            md = data.get('metadata')
            if not fa or not ta:
                return {"status": "error", "message": "Missing from_action or to_action"}
            self.shared_data.db.upsert_studio_edge(fa, ta, et, md)
            return {"status": "ok", "message": "Edge upserted"}
        except Exception as e:
            self.logger.error(f"studio_upsert_edge error: {e}")
            return {"status": "error", "message": str(e)}

    def studio_delete_edge(self, data: dict):
        """Delete an edge."""
        try:
            edge_id = data.get('edge_id')
            if edge_id is None:
                return {"status": "error", "message": "Missing edge_id"}
            self.shared_data.db.delete_studio_edge(int(edge_id))
            return {"status": "ok", "message": "Edge deleted"}
        except Exception as e:
            self.logger.error(f"studio_delete_edge error: {e}")
            return {"status": "error", "message": str(e)}

    def studio_upsert_host(self, data: dict):
        """Create or update a simulated host."""
        try:
            mac = (data.get('mac_address') or '').strip()
            payload = data.get('data') or {}
            if not mac or not isinstance(payload, dict):
                return {"status": "error", "message": "Missing mac_address or data"}
            self.shared_data.db.upsert_studio_host(mac, payload)
            return {"status": "ok", "message": "Host upserted"}
        except Exception as e:
            self.logger.error(f"studio_upsert_host error: {e}")
            return {"status": "error", "message": str(e)}

    def studio_save_layout(self, data: dict):
        """Save a studio layout."""
        try:
            name = (data.get('name') or '').strip()
            layout_data = data.get('layout_data')
            desc = data.get('description')
            if not name or layout_data is None:
                return {"status": "error", "message": "Missing name or layout_data"}
            self.shared_data.db.save_studio_layout(name, layout_data, desc)
            return {"status": "ok", "message": "Layout saved"}
        except Exception as e:
            self.logger.error(f"studio_save_layout error: {e}")
            return {"status": "error", "message": str(e)}

    def studio_apply_to_runtime(self):
        """Apply studio settings to runtime actions."""
        try:
            self.shared_data.db.apply_studio_to_runtime()
            return {"status": "ok", "message": "Studio configuration applied to runtime actions"}
        except Exception as e:
            self.logger.error(f"studio_apply_to_runtime error: {e}")
            return {"status": "error", "message": str(e)}

    def studio_save_bundle(self, data: dict):
        """Save complete studio state (actions, edges, layout)."""
        try:
            actions = data.get('actions') or []
            edges = data.get('edges') or []
            layout = data.get('layout') or {}

            # Update action positions and properties
            for a in actions:
                b_class = (a.get('b_class') or '').strip()
                if not b_class:
                    continue
                updates = {}
                for k in ('studio_x', 'studio_y', 'b_module', 'b_status', 'b_action', 'b_enabled',
                         'b_priority', 'b_timeout', 'b_max_retries', 'b_cooldown', 'b_rate_limit',
                         'b_port', 'b_service', 'b_tags', 'b_trigger', 'b_requires'):
                    if k in a and a[k] is not None:
                        updates[k] = a[k]
                if updates:
                    self.shared_data.db.update_studio_action(b_class, updates)

            # Upsert edges
            for e in edges:
                fa = (e.get('from_action') or '').strip()
                ta = (e.get('to_action') or '').strip()
                et = (e.get('edge_type') or 'requires').strip()
                if fa and ta:
                    self.shared_data.db.upsert_studio_edge(fa, ta, et, e.get('metadata'))

            # Save layout
            try:
                self.shared_data.db.save_studio_layout('autosave', layout, 'autosave from UI')
            except Exception:
                pass

            return {"status": "ok", "message": "Studio saved"}
        except Exception as e:
            self.logger.error(f"studio_save_bundle error: {e}")
            return {"status": "error", "message": str(e)}

    def studio_upsert_host_flat(self, data: dict):
        """Upsert host with flat data structure."""
        try:
            mac = (data.get('mac_address') or '').strip()
            if not mac:
                return {"status": "error", "message": "Missing mac_address"}

            payload = {
                "hostname": data.get('hostname'),
                "ips": data.get('ips'),
                "ports": data.get('ports'),
                "services": data.get('services'),
                "vulns": data.get('vulns'),
                "creds": data.get('creds'),
                "alive": data.get('alive'),
                "is_simulated": data.get('is_simulated', 1),
            }
            self.shared_data.db.upsert_studio_host(mac, payload)
            return {"status": "ok", "message": "Host upserted"}
        except Exception as e:
            self.logger.error(f"studio_upsert_host_flat error: {e}")
            return {"status": "error", "message": str(e)}

    def studio_delete_host(self, data: dict):
        """Delete a studio host."""
        try:
            mac = (data.get('mac_address') or '').strip()
            if not mac:
                return {"status": "error", "message": "Missing mac_address"}
            self.shared_data.db.delete_studio_host(mac)
            return {"status": "ok", "message": "Host deleted"}
        except Exception as e:
            self.logger.error(f"studio_delete_host error: {e}")
            return {"status": "error", "message": str(e)}

    def _write_json(self, handler, obj: dict, code: int = 200):
        """Write JSON response."""
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps(obj).encode('utf-8'))
