"""loki_utils.py - Loki web API endpoints."""
import os
import json
import logging
from typing import Dict
from urllib.parse import urlparse, parse_qs

from logger import Logger

logger = Logger(name="loki_utils", level=logging.DEBUG)


class LokiUtils:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    @property
    def _engine(self):
        return getattr(self.shared_data, 'loki_engine', None)

    # ── GET endpoints (handler signature) ─────────────────────

    def get_status(self, handler):
        """GET /api/loki/status - engine state."""
        engine = self._engine
        if engine:
            data = engine.get_status()
        else:
            data = {
                'enabled': False, 'running': False,
                'gadget_ready': False, 'layout': 'us',
                'jobs_running': 0, 'jobs_total': 0,
            }
        self._send_json(handler, data)

    def get_scripts(self, handler):
        """GET /api/loki/scripts - user-saved scripts."""
        try:
            rows = self.shared_data.db.query(
                "SELECT id, name, description, category, target_os, "
                "created_at, updated_at FROM loki_scripts ORDER BY name"
            ) or []
            self._send_json(handler, {'scripts': rows})
        except Exception as e:
            logger.error("get_scripts error: %s", e)
            self._send_json(handler, {'scripts': []})

    def get_script(self, handler):
        """GET /api/loki/script?id=N - single script with content."""
        try:
            qs = parse_qs(urlparse(handler.path).query)
            script_id = int(qs.get('id', [0])[0])
            row = self.shared_data.db.query_one(
                "SELECT * FROM loki_scripts WHERE id = ?", (script_id,)
            )
            if row:
                self._send_json(handler, {'script': row})
            else:
                self._send_json(handler, {'script': None}, 404)
        except Exception as e:
            logger.error("get_script error: %s", e)
            self._send_json(handler, {'error': str(e)}, 500)

    def get_jobs(self, handler):
        """GET /api/loki/jobs - job list."""
        engine = self._engine
        if engine:
            jobs = engine.get_jobs()
        else:
            jobs = []
        self._send_json(handler, {'jobs': jobs})

    def get_payloads(self, handler):
        """GET /api/loki/payloads - built-in payload list."""
        payloads = []
        payload_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "loki", "payloads"
        )
        if os.path.isdir(payload_dir):
            for f in sorted(os.listdir(payload_dir)):
                if f.endswith('.js'):
                    path = os.path.join(payload_dir, f)
                    try:
                        with open(path, 'r') as fh:
                            content = fh.read()
                        # Extract description from first comment line
                        desc = ""
                        for line in content.split('\n'):
                            line = line.strip()
                            if line.startswith('//'):
                                desc = line[2:].strip()
                                break
                        payloads.append({
                            'name': f[:-3],  # without .js
                            'filename': f,
                            'description': desc,
                            'content': content,
                        })
                    except Exception:
                        pass
        self._send_json(handler, {'payloads': payloads})

    def get_layouts(self, handler):
        """GET /api/loki/layouts - available keyboard layouts."""
        try:
            from loki.layouts import available
            layouts = available()
        except Exception:
            layouts = ['us']
        self._send_json(handler, {'layouts': layouts})

    # ── POST endpoints (JSON data signature) ──────────────────

    def toggle_loki(self, data: Dict) -> Dict:
        """POST /api/loki/toggle - switch to/from LOKI mode."""
        enabled = bool(data.get('enabled', False))
        if enabled:
            self.shared_data.operation_mode = "LOKI"
        else:
            self.shared_data.operation_mode = "AUTO"
        return {'status': 'ok', 'enabled': enabled}

    def save_script(self, data: Dict) -> Dict:
        """POST /api/loki/script/save - save/update a script."""
        try:
            script_id = data.get('id')
            name = data.get('name', '').strip()
            description = data.get('description', '')
            content = data.get('content', '')
            category = data.get('category', 'general')
            target_os = data.get('target_os', 'any')

            if not name:
                return {'status': 'error', 'message': 'Name required'}

            db = self.shared_data.db
            if script_id:
                db.execute(
                    "UPDATE loki_scripts SET name=?, description=?, content=?, "
                    "category=?, target_os=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (name, description, content, category, target_os, script_id)
                )
            else:
                db.execute(
                    "INSERT INTO loki_scripts (name, description, content, category, target_os) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, description, content, category, target_os)
                )
            return {'status': 'ok'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def delete_script(self, data: Dict) -> Dict:
        """POST /api/loki/script/delete - delete a script."""
        try:
            script_id = data.get('id')
            if script_id:
                self.shared_data.db.execute(
                    "DELETE FROM loki_scripts WHERE id = ?", (script_id,)
                )
            return {'status': 'ok'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def run_script(self, data: Dict) -> Dict:
        """POST /api/loki/script/run - execute a HIDScript."""
        engine = self._engine
        if not engine:
            return {'status': 'error', 'message': 'Loki engine not available'}
        if not engine._running:
            return {'status': 'error', 'message': 'Loki not running. Enable it first.'}

        content = data.get('content', '')
        name = data.get('name', 'unnamed')
        if not content:
            return {'status': 'error', 'message': 'No script content'}

        try:
            job_id = engine.submit_job(name, content)
            return {'status': 'ok', 'job_id': job_id}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def cancel_job(self, data: Dict) -> Dict:
        """POST /api/loki/job/cancel - cancel a running job."""
        engine = self._engine
        if not engine:
            return {'status': 'error', 'message': 'Loki engine not available'}
        job_id = data.get('job_id', '')
        if engine.cancel_job(job_id):
            return {'status': 'ok'}
        return {'status': 'error', 'message': 'Job not found'}

    def clear_jobs(self, data: Dict) -> Dict:
        """POST /api/loki/jobs/clear - clear completed jobs."""
        engine = self._engine
        if engine and engine._jobs:
            engine.job_manager.clear_completed()
        return {'status': 'ok'}

    def install_gadget(self, data: Dict) -> Dict:
        """POST /api/loki/install - install HID gadget boot script."""
        from loki import LokiEngine
        result = LokiEngine.install_hid_gadget()
        return result

    def reboot(self, data: Dict) -> Dict:
        """POST /api/loki/reboot - reboot the Pi to activate HID gadget."""
        import subprocess
        try:
            logger.info("Reboot requested by Loki setup")
            subprocess.Popen(["sudo", "reboot"], close_fds=True)
            return {'status': 'ok', 'message': 'Rebooting...'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def quick_type(self, data: Dict) -> Dict:
        """POST /api/loki/quick - quick-type text without a full script."""
        engine = self._engine
        if not engine or not engine._running:
            return {'status': 'error', 'message': 'Loki not running'}

        text = data.get('text', '')
        if not text:
            return {'status': 'error', 'message': 'No text provided'}

        # Wrap as a simple HIDScript
        escaped = text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
        script = f'type("{escaped}");'
        try:
            job_id = engine.submit_job("quick-type", script)
            return {'status': 'ok', 'job_id': job_id}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    # ── Helpers ───────────────────────────────────────────────

    def _send_json(self, handler, data, status=200):
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(data).encode("utf-8"))
