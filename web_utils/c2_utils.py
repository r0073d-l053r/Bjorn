"""c2_utils.py - Command and control agent management endpoints."""
from c2_manager import c2_manager
import base64
import time
from pathlib import Path
import json
from datetime import datetime
import logging
from logger import Logger
logger = Logger(name="c2_utils.py", level=logging.DEBUG)


class C2Utils:
    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data
        # Anti-flap: cache last healthy agent snapshot
        self._last_agents = []
        self._last_agents_ts = 0.0
        self._snapshot_ttl = 10.0     # grace period (s) if /c2/agents fails

    # ---------------------- JSON helpers ----------------------

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

    # ---------------------- Agent normalization ----------------------

    def _normalize_agent(self, a):
        """Normalize agent fields (id, last_seen as ISO) without breaking other fields."""
        a = dict(a) if isinstance(a, dict) else {}
        a["id"] = a.get("id") or a.get("agent_id") or a.get("client_id")

        ls = a.get("last_seen")
        if isinstance(ls, (int, float)):
            # epoch seconds to ISO
            try:
                a["last_seen"] = datetime.fromtimestamp(ls).isoformat()
            except Exception:
                a["last_seen"] = None
        elif isinstance(ls, str):
            # ISO (with or without Z)
            try:
                dt = datetime.fromisoformat(ls.replace("Z", "+00:00"))
                a["last_seen"] = dt.isoformat()
            except Exception:
                # unknown format, leave as-is
                pass
        elif isinstance(ls, datetime):
            a["last_seen"] = ls.isoformat()
        else:
            a["last_seen"] = None

        return a

    # ---------------------- REST handlers ----------------------

    def c2_start(self, handler, data):
        port = int(data.get("port", 5555))
        res = c2_manager.start(port=port)
        return self._json(handler, 200, res)

    def c2_stop(self, handler):
        res = c2_manager.stop()
        return self._json(handler, 200, res)

    def c2_status(self, handler):
        return self._json(handler, 200, c2_manager.status())

    def c2_agents(self, handler):
        """Return agent list as JSON array.
        Anti-flap: if list_agents() returns [] but we have a recent snapshot (< TTL), serve that instead.
        """
        try:
            raw = c2_manager.list_agents() or []
            agents = [self._normalize_agent(x) for x in raw]

            now = time.time()
            if len(agents) == 0 and len(self._last_agents) > 0 and (now - self._last_agents_ts) <= self._snapshot_ttl:
                # Quick fallback: serve last non-empty snapshot
                return self._json(handler, 200, self._last_agents)

            # Fresh snapshot (even if actually empty)
            self._last_agents = agents
            self._last_agents_ts = now
            return self._json(handler, 200, agents)

        except Exception as e:
            # On error, serve recent snapshot if available
            now = time.time()
            if len(self._last_agents) > 0 and (now - self._last_agents_ts) <= self._snapshot_ttl:
                self.logger.warning(f"/c2/agents fallback to snapshot after error: {e}")
                return self._json(handler, 200, self._last_agents)
            return self._json(handler, 500, {"status": "error", "message": str(e)})

    def c2_command(self, handler, data):
        targets = data.get("targets") or []
        command = (data.get("command") or "").strip()
        if not targets or not command:
            return self._json(handler, 400, {"status": "error", "message": "targets and command required"})
        return self._json(handler, 200, c2_manager.send_command(targets, command))

    def c2_broadcast(self, handler, data):
        command = (data.get("command") or "").strip()
        if not command:
            return self._json(handler, 400, {"status": "error", "message": "command required"})
        return self._json(handler, 200, c2_manager.broadcast(command))

    def c2_deploy(self, handler, data):
        required = ("client_id", "ssh_host", "ssh_user", "ssh_pass")
        if not all(k in data and str(data.get(k)).strip() for k in required):
            return self._json(handler, 400, {"status": "error", "message": "missing fields"})
        payload = {
            "client_id": data.get("client_id").strip(),
            "ssh_host":  data.get("ssh_host").strip(),
            "ssh_user":  data.get("ssh_user").strip(),
            "ssh_pass":  data.get("ssh_pass").strip(),
        }
        if data.get("lab_user"):
            payload["lab_user"] = data.get("lab_user").strip()
        if data.get("lab_password"):
            payload["lab_password"] = data.get("lab_password").strip()
        res = c2_manager.deploy_client(**payload)
        return self._json(handler, 200, res)

    def c2_stale_agents(self, handler, threshold: int = 300):
        try:
            agents = c2_manager.db.get_stale_agents(threshold)
            return self._json(handler, 200, {"status": "ok", "count": len(agents), "agents": agents})
        except Exception as e:
            return self._json(handler, 500, {"status": "error", "message": str(e)})

    def c2_purge_agents(self, handler, data):
        try:
            threshold = int(data.get("threshold", 86400))
            purged = c2_manager.db.purge_stale_agents(threshold)
            return self._json(handler, 200, {"status": "ok", "purged": purged})
        except Exception as e:
            return self._json(handler, 500, {"status": "error", "message": str(e)})

    # ---------------------- SSE: event stream ----------------------

    def c2_events_sse(self, handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")  # needed behind Nginx/Traefik
        handler.end_headers()

        # Tell client to back off on reconnect (avoids thundering herd)
        try:
            handler.wfile.write(b"retry: 5000\n\n")  # 5s
            handler.wfile.flush()
        except Exception:
            return

        def push(event: dict):
            try:
                t = event.get('type')
                if t:
                    handler.wfile.write(f"event: {t}\n".encode("utf-8"))
                safe = self._to_jsonable(event)
                payload = f"data: {json.dumps(safe, ensure_ascii=False)}\n\n"
                handler.wfile.write(payload.encode("utf-8"))
                handler.wfile.flush()
            except Exception:
                # Connection broken: unsubscribe cleanly
                try:
                    c2_manager.bus.unsubscribe(push)
                except Exception:
                    pass

        c2_manager.bus.subscribe(push)
        try:
            # Periodic keep-alive to maintain the stream
            while True:
                time.sleep(15)
                try:
                    handler.wfile.write(b": keep-alive\n\n")  # SSE comment
                    handler.wfile.flush()
                except Exception:
                    break
        finally:
            try:
                c2_manager.bus.unsubscribe(push)
            except Exception:
                pass

    # ---------------------- Client file management ----------------------

    def c2_download_client(self, handler, filename):
        """Serve generated client file for download"""
        try:
            # Security check - prevent directory traversal
            if '..' in filename or '/' in filename or '\\' in filename:
                handler.send_error(403, "Forbidden")
                return

            clients_dir = Path(__file__).parent / "c2_data" / "clients"
            filepath = clients_dir / filename

            if not filepath.exists() or not filepath.is_file():
                handler.send_error(404, "File not found")
                return

            handler.send_response(200)
            handler.send_header('Content-Type', 'application/octet-stream')
            handler.send_header('Content-Disposition', f'attachment; filename="{filename}"')

            with open(filepath, 'rb') as f:
                content = f.read()

            handler.send_header('Content-Length', str(len(content)))
            handler.end_headers()
            handler.wfile.write(content)

        except Exception as e:
            self.logger.error(f"Error downloading client: {e}")
            handler.send_error(500, str(e))

    def c2_list_clients(self, handler):
        """List all generated client files"""
        try:
            clients_dir = Path(__file__).parent / "c2_data" / "clients"

            clients = []
            if clients_dir.exists():
                for file in clients_dir.glob("*.py"):
                    clients.append({
                        "filename": file.name,
                        "size": file.stat().st_size,
                        "modified": file.stat().st_mtime
                    })

            return self._json(handler, 200, {"status": "ok", "clients": clients})

        except Exception as e:
            return self._json(handler, 500, {"status": "error", "message": str(e)})

    def c2_remove_client(self, handler, data):
        """Remove a client completely"""
        client_id = (data.get("client_id") or "").strip()
        if not client_id:
            return self._json(handler, 400, {"status": "error", "message": "client_id required"})

        res = c2_manager.remove_client(client_id)
        return self._json(handler, 200, res)

    def c2_generate_client(self, handler, data):
        """Enhanced client generation with platform support"""
        cid = (data.get("client_id") or "").strip()
        if not cid:
            cid = f"zombie_{int(time.time())}"

        platform = data.get("platform", "universal")
        lab_user = (data.get("lab_user") or "testuser").strip()
        lab_pass = (data.get("lab_password") or "testpass").strip()

        res = c2_manager.generate_client(
            client_id=cid,
            platform=platform,
            lab_user=lab_user,
            lab_password=lab_pass
        )
        return self._json(handler, 200, res)
