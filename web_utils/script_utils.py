"""script_utils.py - Script management, execution, monitoring, and output capture."""
from __future__ import annotations
import json
import subprocess
import os
import time
import threading
import importlib.util
import ast
import html
from pathlib import Path


# --- Multipart form helpers (replaces cgi module removed in Python 3.13) ---
def _parse_header(line):
    parts = line.split(';')
    key = parts[0].strip()
    pdict = {}
    for p in parts[1:]:
        if '=' in p:
            k, v = p.strip().split('=', 1)
            pdict[k.strip()] = v.strip().strip('"')
    return key, pdict


class _FormField:
    __slots__ = ('name', 'filename', 'file', 'value')
    def __init__(self, name, filename=None, data=b''):
        self.name = name
        self.filename = filename
        if filename:
            self.file = BytesIO(data)
            self.value = data
        else:
            self.value = data.decode('utf-8', errors='replace').strip()
            self.file = None


class _MultipartForm:
    """Minimal replacement for _MultipartForm."""
    def __init__(self, fp, headers, environ=None, keep_blank_values=False):
        import re as _re
        self._fields = {}
        ct = headers.get('Content-Type', '') if hasattr(headers, 'get') else ''
        _, params = _parse_header(ct)
        boundary = params.get('boundary', '').encode()
        if hasattr(fp, 'read'):
            cl = headers.get('Content-Length') if hasattr(headers, 'get') else None
            body = fp.read(int(cl)) if cl else fp.read()
        else:
            body = fp
        for part in body.split(b'--' + boundary)[1:]:
            part = part.strip(b'\r\n')
            if part == b'--' or not part:
                continue
            sep = b'\r\n\r\n' if b'\r\n\r\n' in part else b'\n\n'
            if sep not in part:
                continue
            hdr, data = part.split(sep, 1)
            hdr_s = hdr.decode('utf-8', errors='replace')
            nm = _re.search(r'name="([^"]*)"', hdr_s)
            fn = _re.search(r'filename="([^"]*)"', hdr_s)
            if not nm:
                continue
            name = nm.group(1)
            filename = fn.group(1) if fn else None
            field = _FormField(name, filename, data)
            if name in self._fields:
                existing = self._fields[name]
                if isinstance(existing, list):
                    existing.append(field)
                else:
                    self._fields[name] = [existing, field]
            else:
                self._fields[name] = field

    def __contains__(self, key):
        return key in self._fields

    def __getitem__(self, key):
        return self._fields[key]

    def getvalue(self, key, default=None):
        if key not in self._fields:
            return default
        f = self._fields[key]
        if isinstance(f, list):
            return [x.value for x in f]
        return f.value
from typing import Any, Dict, Optional, List
from io import BytesIO
import logging
from logger import Logger
logger = Logger(name="script_utils.py", level=logging.DEBUG)

# AST parse cache: {path: (mtime, format)} - avoids re-parsing on every list_scripts call
_format_cache: dict = {}
_vars_cache: dict = {}
_MAX_CACHE_ENTRIES = 200


def _detect_script_format(script_path: str) -> str:
    """Check if a script uses Bjorn action format (has b_class) or is a free script. Cached by mtime."""
    try:
        mtime = os.path.getmtime(script_path)
        cached = _format_cache.get(script_path)
        if cached and cached[0] == mtime:
            return cached[1]
    except OSError:
        return "free"

    fmt = "free"
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=script_path)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "b_class":
                        fmt = "bjorn"
                        break
                if fmt == "bjorn":
                    break
    except Exception:
        pass

    if len(_format_cache) >= _MAX_CACHE_ENTRIES:
        _format_cache.clear()
    _format_cache[script_path] = (mtime, fmt)
    return fmt


def _extract_module_vars(script_path: str, *var_names: str) -> dict:
    """Safely extract module-level variable assignments via AST (no exec). Cached by mtime."""
    try:
        mtime = os.path.getmtime(script_path)
        cache_key = (script_path, var_names)
        cached = _vars_cache.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]
    except OSError:
        return {}

    result = {}
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=script_path)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in var_names:
                    try:
                        result[target.id] = ast.literal_eval(node.value)
                    except Exception:
                        pass
    except Exception:
        pass

    if len(_vars_cache) >= _MAX_CACHE_ENTRIES:
        _vars_cache.clear()
    _vars_cache[cache_key] = (mtime, result)
    return result


class ScriptUtils:
    """Utilities for script management and execution."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data
        self._last_custom_scan = 0.0

    def get_script_description(self, script_path: Path) -> str:
        """Extract description from script comments."""
        try:
            with open(script_path, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f.readlines()[:10]]
                
            description = []
            for line in lines:
                if line.startswith('#'):
                    clean_line = html.escape(line[1:].strip())
                    description.append(clean_line)
                elif line.startswith('"""') or line.startswith("'''"):
                    break
                elif line and not description:
                    break
                    
            description_text = '\n'.join(description) if description else "No description available"
            return description_text
        except Exception as e:
            self.logger.error(f"Error reading script description: {e}")
            return "Error reading description"

    def _resolve_action_path(self, b_module: str) -> str:
        """Resolve filesystem path for an action module (handles custom/ prefix)."""
        return os.path.join(self.shared_data.actions_dir, f"{b_module}.py")

    def _auto_register_custom_scripts(self, known_modules: set):
        """Scan custom_scripts_dir for .py files not yet in DB. Throttled to once per 30s."""
        now = time.time()
        if now - self._last_custom_scan < 30:
            return
        self._last_custom_scan = now

        custom_dir = self.shared_data.custom_scripts_dir
        if not os.path.isdir(custom_dir):
            return
        for fname in os.listdir(custom_dir):
            if not fname.endswith(".py") or fname == "__init__.py":
                continue
            stem = fname[:-3]
            module_key = f"custom/{stem}"
            if module_key in known_modules:
                continue
            # Auto-register
            script_path = os.path.join(custom_dir, fname)
            fmt = _detect_script_format(script_path)
            meta = _extract_module_vars(
                script_path,
                "b_class", "b_name", "b_description", "b_author",
                "b_version", "b_args", "b_tags", "b_examples", "b_icon"
            )
            b_class = meta.get("b_class", f"Custom_{stem}")
            try:
                self.shared_data.db.upsert_simple_action(
                    b_class=b_class,
                    b_module=module_key,
                    b_action="custom",
                    b_name=meta.get("b_name", stem),
                    b_description=meta.get("b_description", "Custom script"),
                    b_author=meta.get("b_author"),
                    b_version=meta.get("b_version"),
                    b_icon=meta.get("b_icon"),
                    b_args=json.dumps(meta["b_args"]) if "b_args" in meta else None,
                    b_tags=json.dumps(meta["b_tags"]) if "b_tags" in meta else None,
                    b_examples=json.dumps(meta["b_examples"]) if "b_examples" in meta else None,
                    b_enabled=1,
                    b_priority=50,
                )
                self.logger.info(f"Auto-registered custom script: {module_key} ({fmt})")
            except Exception as e:
                self.logger.warning(f"Failed to auto-register {module_key}: {e}")

    def list_scripts(self) -> Dict:
        """List all actions with metadata for the launcher."""
        try:
            actions_out: list[dict] = []
            db_actions = self.shared_data.db.list_actions()

            # Auto-register untracked custom scripts
            known_modules = {(r.get("b_module") or "").strip() for r in db_actions}
            self._auto_register_custom_scripts(known_modules)
            # Re-query if new scripts were registered
            new_known = {(r.get("b_module") or "").strip() for r in self.shared_data.db.list_actions()}
            if new_known != known_modules:
                db_actions = self.shared_data.db.list_actions()

            for row in db_actions:
                b_class = (row.get("b_class") or "").strip()
                b_module = (row.get("b_module") or "").strip()
                action_path = self._resolve_action_path(b_module)

                # Load b_args from DB (priority)
                db_args_raw = row.get("b_args")
                if isinstance(db_args_raw, str):
                    db_args_raw_str = db_args_raw.strip()
                    if (db_args_raw_str.startswith("{") and db_args_raw_str.endswith("}")) or \
                       (db_args_raw_str.startswith("[") and db_args_raw_str.endswith("]")):
                        try:
                            b_args = json.loads(db_args_raw_str)
                        except Exception:
                            b_args = {}
                    else:
                        b_args = {}
                elif db_args_raw is None:
                    b_args = {}
                else:
                    b_args = db_args_raw

                # Basic metadata from DB
                b_name = row.get("b_name")
                b_description = row.get("b_description") or row.get("b_status") or "No description available"
                b_author = row.get("b_author")
                b_version = row.get("b_version")
                b_icon = row.get("b_icon")
                b_docs_url = row.get("b_docs_url")

                b_examples = None
                if row.get("b_examples") is not None:
                    try:
                        if isinstance(row["b_examples"], str):
                            b_examples = json.loads(row["b_examples"])
                        else:
                            b_examples = row["b_examples"]
                    except Exception:
                        b_examples = None

                # Enrich metadata from module file (AST for static fields, exec only for dynamic b_args)
                try:
                    if os.path.exists(action_path):
                        # Static metadata via AST (no exec, no sys.modules pollution)
                        static_vars = _extract_module_vars(
                            action_path,
                            "b_name", "b_description", "b_author", "b_version",
                            "b_icon", "b_docs_url", "b_examples", "b_args"
                        )
                        if static_vars.get("b_name"): b_name = static_vars["b_name"]
                        if static_vars.get("b_description"): b_description = static_vars["b_description"]
                        if static_vars.get("b_author"): b_author = static_vars["b_author"]
                        if static_vars.get("b_version"): b_version = static_vars["b_version"]
                        if static_vars.get("b_icon"): b_icon = static_vars["b_icon"]
                        if static_vars.get("b_docs_url"): b_docs_url = static_vars["b_docs_url"]
                        if static_vars.get("b_examples"): b_examples = static_vars["b_examples"]
                        if static_vars.get("b_args") and not b_args:
                            b_args = static_vars["b_args"]

                        # Only exec module if it has compute_dynamic_b_args (rare)
                        # Check via simple text search first to avoid unnecessary imports
                        try:
                            with open(action_path, "r", encoding="utf-8") as _f:
                                has_dynamic = "compute_dynamic_b_args" in _f.read()
                        except Exception:
                            has_dynamic = False

                        if has_dynamic:
                            import sys as _sys
                            spec = importlib.util.spec_from_file_location(f"_tmp_{b_module}", action_path)
                            module = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(module)
                            if hasattr(module, "compute_dynamic_b_args"):
                                try:
                                    b_args = module.compute_dynamic_b_args(b_args or {})
                                except Exception as e:
                                    self.logger.warning(f"compute_dynamic_b_args failed for {b_module}: {e}")
                            # Remove from sys.modules to prevent accumulation
                            _sys.modules.pop(f"_tmp_{b_module}", None)

                except Exception as e:
                    self.logger.warning(f"Could not enrich {b_module}: {e}")

                # Parse tags
                tags_raw = row.get("b_tags")
                if isinstance(tags_raw, str):
                    t = tags_raw.strip()
                    if (t.startswith("{") and t.endswith("}")) or (t.startswith("[") and t.endswith("]")):
                        try:
                            tags = json.loads(t)
                        except Exception:
                            tags = tags_raw
                    else:
                        tags = tags_raw
                else:
                    tags = tags_raw

                # Display name
                display_name = b_name or (f"{b_module}.py" if b_module else (f"{b_class}.py" if b_class else "Unnamed"))

                # Icon URL
                icon_url = self._normalize_icon_url(b_icon, b_class)

                # Custom script detection
                is_custom = b_module.startswith("custom/")
                script_format = ""
                if is_custom and os.path.exists(action_path):
                    script_format = _detect_script_format(action_path)

                # Build action info
                action_info = {
                    "name": display_name,
                    "path": action_path,
                    "b_module": b_module,
                    "b_class": b_class,
                    "category": row.get("b_action", "normal") or "normal",
                    "type": "action",
                    "description": b_description or "No description available",
                    "b_args": b_args,
                    "enabled": bool(row.get("b_enabled", 1)),
                    "priority": row.get("b_priority", 50),
                    "tags": tags,
                    "b_author": b_author,
                    "b_version": b_version,
                    "b_icon": icon_url,
                    "b_docs_url": b_docs_url,
                    "b_examples": b_examples,
                    "is_custom": is_custom,
                    "script_format": script_format,
                    "is_running": False,
                    "output": []
                }

                # Runtime state
                with self.shared_data.scripts_lock:
                    if action_path in self.shared_data.running_scripts:
                        runinfo = self.shared_data.running_scripts[action_path]
                        action_info["is_running"] = runinfo.get("is_running", False)
                        action_info["output"] = runinfo.get("output", [])
                        action_info["last_error"] = runinfo.get("last_error", "")

                actions_out.append(action_info)

            actions_out.sort(key=lambda x: x["name"])
            return {"status": "success", "data": actions_out}

        except Exception as e:
            self.logger.error(f"Error listing actions: {e}")
            return {"status": "error", "message": str(e)}

    def _normalize_icon_url(self, raw_icon: str | None, b_class: str) -> str:
        """Normalize icon URL for frontend consumption."""
        def _default_icon_url(b_class: str) -> str | None:
            if not b_class:
                return None
            fname = f"{b_class}.png"
            icon_fs = os.path.join(self.shared_data.actions_icons_dir, fname)
            return f"/actions_icons/{fname}" if os.path.exists(icon_fs) else None

        if raw_icon:
            s = str(raw_icon).strip()
            if s.startswith("http://") or s.startswith("https://"):
                return s
            if "/" not in s and "\\" not in s:
                return f"/actions_icons/{s}"
            url = _default_icon_url(b_class)
            if url:
                return url

        url = _default_icon_url(b_class)
        if url:
            return url

        return "/actions/actions_icons/default.png"

    def run_script(self, data: Dict) -> Dict:
        """Run an action/script with arguments."""
        try:
            script_key = data.get("script_name")
            args = data.get("args", "")
            
            if not script_key:
                return {"status": "error", "message": "Script name is required"}
            
            # Find action in database
            action = None
            for a in self.shared_data.db.list_actions():
                if a["b_class"] == script_key or a["b_module"] == script_key:
                    action = a
                    break
            
            if not action:
                return {"status": "error", "message": f"Action {script_key} not found"}
            
            module_name = action["b_module"]
            script_path = self._resolve_action_path(module_name)

            if not os.path.exists(script_path):
                return {"status": "error", "message": f"Script file {script_path} not found"}

            is_custom = module_name.startswith("custom/")
            script_format = _detect_script_format(script_path) if is_custom else "bjorn"

            # Check if already running
            with self.shared_data.scripts_lock:
                if script_path in self.shared_data.running_scripts and \
                   self.shared_data.running_scripts[script_path].get("is_running", False):
                    return {"status": "error", "message": f"Script {module_name} is already running"}

                # Prepare environment
                env = dict(os.environ)
                env["PYTHONUNBUFFERED"] = "1"
                env["BJORN_EMBEDDED"] = "1"

                # Build command based on script format
                if script_format == "free":
                    # Free scripts run directly as standalone Python
                    cmd = ["sudo", "python3", "-u", script_path]
                else:
                    # Bjorn-format actions go through action_runner (bootstraps shared_data)
                    runner_path = os.path.join(self.shared_data.current_dir, "action_runner.py")
                    cmd = ["sudo", "python3", "-u", runner_path, module_name, action["b_class"]]
                if args:
                    cmd.extend(args.split())

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    universal_newlines=True,
                    env=env,
                    cwd=self.shared_data.current_dir
                )
                
                # Store process info
                self.shared_data.running_scripts[script_path] = {
                    "process": process,
                    "output": [],
                    "start_time": time.time(),
                    "is_running": True,
                    "last_error": "",
                    "b_class": action["b_class"],
                    "b_module": module_name,
                }
            
            # Start monitoring thread
            threading.Thread(
                target=self.monitor_script_output,
                args=(script_path, process),
                daemon=True
            ).start()
            
            return {
                "status": "success",
                "message": f"Started {module_name}",
                "data": {
                    "is_running": True,
                    "output": [],
                    "script_path": script_path
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error running script: {e}")
            return {"status": "error", "message": str(e)}

    def stop_script(self, data: Dict) -> Dict:
        """Stop a running script."""
        try:
            script_name = data.get('script_name')
            
            if not script_name:
                return {"status": "error", "message": "Script name is required"}
            
            # Handle both paths and names
            if not script_name.startswith('/'):
                for path, info in self.shared_data.running_scripts.items():
                    if info.get("b_module") == script_name or info.get("b_class") == script_name:
                        script_name = path
                        break
            
            with self.shared_data.scripts_lock:
                if script_name not in self.shared_data.running_scripts:
                    return {"status": "error", "message": f"Script {script_name} not found or not running"}
                
                script_info = self.shared_data.running_scripts[script_name]
                if script_info["process"]:
                    script_info["process"].terminate()
                    try:
                        script_info["process"].wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        script_info["process"].kill()
                        script_info["process"].wait()
                    
                    script_info["output"].append("Script stopped by user")
                    script_info["is_running"] = False
                    script_info["process"] = None
            
            return {"status": "success", "message": f"Script {script_name} stopped"}
            
        except Exception as e:
            self.logger.error(f"Error stopping script: {e}")
            return {"status": "error", "message": str(e)}

    def get_script_output(self, data: Dict) -> Dict:
        """Get output for a running or completed script."""
        try:
            script_name = data.get('script_name')
            
            if not script_name:
                return {"status": "error", "message": "Script name is required"}
            
            self.logger.debug(f"Getting output for: {script_name}")
            
            with self.shared_data.scripts_lock:
                # Direct path lookup
                if script_name in self.shared_data.running_scripts:
                    script_info = self.shared_data.running_scripts[script_name]
                    return {
                        "status": "success",
                        "data": {
                            "output": script_info["output"],
                            "is_running": script_info.get("is_running", False),
                            "runtime": time.time() - script_info.get("start_time", time.time()),
                            "last_error": script_info.get("last_error", "")
                        }
                    }
                
                # Try basename lookup
                script_basename = os.path.basename(script_name)
                for key, info in self.shared_data.running_scripts.items():
                    if os.path.basename(key) == script_basename:
                        return {
                            "status": "success",
                            "data": {
                                "output": info["output"],
                                "is_running": info.get("is_running", False),
                                "runtime": time.time() - info.get("start_time", time.time()),
                                "last_error": info.get("last_error", "")
                            }
                        }
                
                # Try module/class name lookup
                for key, info in self.shared_data.running_scripts.items():
                    if info.get("b_module") == script_name or info.get("b_class") == script_name:
                        return {
                            "status": "success",
                            "data": {
                                "output": info["output"],
                                "is_running": info.get("is_running", False),
                                "runtime": time.time() - info.get("start_time", time.time()),
                                "last_error": info.get("last_error", "")
                            }
                        }
            
            # Not found - return empty
            return {
                "status": "success",
                "data": {
                    "output": [],
                    "is_running": False,
                    "runtime": 0,
                    "last_error": ""
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error getting script output: {e}")
            return {"status": "error", "message": str(e)}

    MAX_OUTPUT_LINES = 2000

    def monitor_script_output(self, script_path: str, process: subprocess.Popen):
        """Monitor script output in real-time with bounded buffer."""
        try:
            self.logger.debug(f"Starting output monitoring for: {script_path}")

            while True:
                line = process.stdout.readline()

                if not line and process.poll() is not None:
                    break

                if line:
                    line = line.rstrip()
                    with self.shared_data.scripts_lock:
                        if script_path in self.shared_data.running_scripts:
                            output = self.shared_data.running_scripts[script_path]["output"]
                            output.append(line)
                            # Cap output to prevent unbounded memory growth
                            if len(output) > self.MAX_OUTPUT_LINES:
                                del output[:len(output) - self.MAX_OUTPUT_LINES]

            # Process ended - close stdout FD explicitly
            if process.stdout:
                process.stdout.close()

            return_code = process.poll()
            with self.shared_data.scripts_lock:
                if script_path in self.shared_data.running_scripts:
                    info = self.shared_data.running_scripts[script_path]
                    info["process"] = None
                    info["is_running"] = False

                    if return_code == 0:
                        info["output"].append("Script completed successfully")
                    else:
                        info["output"].append(f"Script exited with code {return_code}")
                        info["last_error"] = f"Exit code: {return_code}"

                # Prune old finished entries (keep max 20 historical)
                self._prune_finished_scripts()

            self.logger.info(f"Script {script_path} finished with code {return_code}")

        except Exception as e:
            self.logger.error(f"Error monitoring output for {script_path}: {e}")
            with self.shared_data.scripts_lock:
                if script_path in self.shared_data.running_scripts:
                    info = self.shared_data.running_scripts[script_path]
                    info["output"].append(f"Monitoring error: {str(e)}")
                    info["last_error"] = str(e)
                    info["process"] = None
                    info["is_running"] = False
        finally:
            # Ensure process resources are released
            try:
                if process.stdout and not process.stdout.closed:
                    process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
            except Exception:
                pass

    def _prune_finished_scripts(self):
        """Remove oldest finished script entries to bound memory. Caller must hold scripts_lock."""
        MAX_FINISHED = 20
        finished = [
            (k, v.get("start_time", 0))
            for k, v in self.shared_data.running_scripts.items()
            if not v.get("is_running", False) and v.get("process") is None
        ]
        if len(finished) > MAX_FINISHED:
            finished.sort(key=lambda x: x[1])
            for k, _ in finished[:len(finished) - MAX_FINISHED]:
                del self.shared_data.running_scripts[k]

    def upload_script(self, handler) -> None:
        """Upload a new script file."""
        try:
            form = _MultipartForm(
                fp=handler.rfile,
                headers=handler.headers,
                environ={'REQUEST_METHOD': 'POST'}
            )
            if 'script_file' not in form:
                resp = {"status": "error", "message": "Missing 'script_file'"}
                handler.send_response(400)
            else:
                file_item = form['script_file']
                if not file_item.filename.endswith('.py'):
                    resp = {"status": "error", "message": "Only .py allowed"}
                    handler.send_response(400)
                else:
                    script_name = os.path.basename(file_item.filename)
                    script_path = Path(self.shared_data.actions_dir) / script_name
                    if script_path.exists():
                        resp = {"status": "error", "message": f"Script '{script_name}' already exists."}
                        handler.send_response(400)
                    else:
                        with open(script_path, 'wb') as f:
                            f.write(file_item.file.read())

                        description = self.get_script_description(script_path)

                        self.shared_data.db.add_script(
                            name=script_name,
                            type_="script",
                            path=str(script_path),
                            category="general",
                            description=description
                        )

                        resp = {"status": "success", "message": f"Script '{script_name}' uploaded."}
                        handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps(resp).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error uploading script: {e}")
            handler.send_response(500)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

    def delete_script(self, data: Dict) -> Dict:
        """Delete a script."""
        try:
            script_name = data.get('script_name')
            if not script_name:
                return {"status": "error", "message": "Missing script_name"}

            rows = self.shared_data.db.query("SELECT * FROM scripts WHERE name=?", (script_name,))
            if not rows:
                return {"status": "error", "message": f"Script '{script_name}' not found in DB"}
            row = rows[0]
            is_project = row["type"] == "project"
            path = Path(row["path"])

            if is_project and path.exists():
                import shutil
                shutil.rmtree(path)
            else:
                script_path = Path(self.shared_data.actions_dir) / script_name
                if script_path.exists():
                    with self.shared_data.scripts_lock:
                        if str(script_path) in self.shared_data.running_scripts and \
                           self.shared_data.running_scripts[str(script_path)].get("is_running", False):
                            return {"status": "error", "message": f"Script '{script_name}' is running."}
                    script_path.unlink()

            self.shared_data.db.delete_script(script_name)
            return {"status": "success", "message": f"{'Project' if is_project else 'Script'} '{script_name}' deleted."}
        except Exception as e:
            self.logger.error(f"Error deleting script: {e}")
            return {"status": "error", "message": str(e)}

    # --- Custom scripts management ---

    def upload_custom_script(self, handler) -> None:
        """Upload a custom script to actions/custom/."""
        try:
            form = _MultipartForm(
                fp=handler.rfile,
                headers=handler.headers,
                environ={'REQUEST_METHOD': 'POST'}
            )
            if 'script_file' not in form:
                resp = {"status": "error", "message": "Missing 'script_file'"}
                handler.send_response(400)
            else:
                file_item = form['script_file']
                if not file_item.filename.endswith('.py'):
                    resp = {"status": "error", "message": "Only .py files allowed"}
                    handler.send_response(400)
                else:
                    script_name = os.path.basename(file_item.filename)
                    stem = script_name[:-3]
                    script_path = Path(self.shared_data.custom_scripts_dir) / script_name

                    if script_path.exists():
                        resp = {"status": "error", "message": f"Script '{script_name}' already exists. Delete it first."}
                        handler.send_response(400)
                    else:
                        with open(script_path, 'wb') as f:
                            f.write(file_item.file.read())

                        # Extract metadata via AST (safe, no exec)
                        fmt = _detect_script_format(str(script_path))
                        meta = _extract_module_vars(
                            str(script_path),
                            "b_class", "b_name", "b_description", "b_author",
                            "b_version", "b_args", "b_tags", "b_examples", "b_icon"
                        )

                        b_class = meta.get("b_class", f"Custom_{stem}")
                        module_key = f"custom/{stem}"

                        self.shared_data.db.upsert_simple_action(
                            b_class=b_class,
                            b_module=module_key,
                            b_action="custom",
                            b_name=meta.get("b_name", stem),
                            b_description=meta.get("b_description", "Custom script"),
                            b_author=meta.get("b_author"),
                            b_version=meta.get("b_version"),
                            b_icon=meta.get("b_icon"),
                            b_args=json.dumps(meta["b_args"]) if "b_args" in meta else None,
                            b_tags=json.dumps(meta["b_tags"]) if "b_tags" in meta else None,
                            b_examples=json.dumps(meta["b_examples"]) if "b_examples" in meta else None,
                            b_enabled=1,
                            b_priority=50,
                        )

                        resp = {
                            "status": "success",
                            "message": f"Custom script '{script_name}' uploaded ({fmt} format).",
                            "data": {"b_class": b_class, "b_module": module_key, "format": fmt}
                        }
                        handler.send_response(200)

            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps(resp).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error uploading custom script: {e}")
            handler.send_response(500)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

    def delete_custom_script(self, data: Dict) -> Dict:
        """Delete a custom script (refuses to delete built-in actions)."""
        try:
            b_class = data.get("script_name") or data.get("b_class")
            if not b_class:
                return {"status": "error", "message": "Missing script_name"}

            # Look up in actions table
            action = self.shared_data.db.get_action_by_class(b_class)
            if not action:
                return {"status": "error", "message": f"Action '{b_class}' not found"}

            b_module = action.get("b_module", "")
            if action.get("b_action") != "custom" and not b_module.startswith("custom/"):
                return {"status": "error", "message": "Cannot delete built-in actions"}

            script_path = self._resolve_action_path(b_module)

            # Check if running
            with self.shared_data.scripts_lock:
                if script_path in self.shared_data.running_scripts and \
                   self.shared_data.running_scripts[script_path].get("is_running", False):
                    return {"status": "error", "message": f"Script '{b_class}' is currently running. Stop it first."}

            # Delete file
            if os.path.exists(script_path):
                os.remove(script_path)

            # Delete from DB
            self.shared_data.db.delete_action(b_class)

            return {"status": "success", "message": f"Custom script '{b_class}' deleted."}
        except Exception as e:
            self.logger.error(f"Error deleting custom script: {e}")
            return {"status": "error", "message": str(e)}

    def upload_project(self, handler) -> None:
        """Upload a project with multiple files."""
        try:
            form = _MultipartForm(
                fp=handler.rfile,
                headers=handler.headers,
                environ={'REQUEST_METHOD': 'POST'}
            )
            if 'main_file' not in form:
                raise ValueError("Missing main_file")
            main_file_path = form.getvalue('main_file')
            project_name = Path(main_file_path).parts[0]
            project_dir = Path(self.shared_data.actions_dir) / project_name
            project_dir.mkdir(exist_ok=True)

            files = form['project_files[]']
            if not isinstance(files, list):
                files = [files]
            for fileitem in files:
                if fileitem.filename:
                    relative_path = Path(fileitem.filename).relative_to(project_name)
                    file_path = project_dir / relative_path
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(file_path, 'wb') as f:
                        f.write(fileitem.file.read())

            description = self.get_script_description(project_dir / Path(main_file_path).name)

            self.shared_data.db.add_script(
                name=project_name,
                type_="project",
                path=str(project_dir),
                main_file=main_file_path,
                category="projects",
                description=description
            )

            resp = {"status": "success", "message": f"Project '{project_name}' uploaded."}
            handler.send_response(200)
        except Exception as e:
            self.logger.error(f"Error uploading project: {e}")
            resp = {"status": "error", "message": str(e)}
            handler.send_response(400)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps(resp).encode('utf-8'))

    def get_action_args_schema(self, data: Dict) -> Dict:
        """Get the arguments schema for a specific action."""
        try:
            action_name = data.get("action_name")
            
            if not action_name:
                return {"status": "error", "message": "Action name is required"}
            
            action = None
            for a in self.shared_data.db.list_actions():
                if a["b_class"] == action_name or a["b_module"] == action_name:
                    action = a
                    break
            
            if not action:
                return {"status": "error", "message": f"Action {action_name} not found"}
            
            module_name = action["b_module"]
            action_path = os.path.join(self.shared_data.actions_dir, f"{module_name}.py")
            
            b_args = {}
            
            if os.path.exists(action_path):
                try:
                    spec = importlib.util.spec_from_file_location(module_name, action_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    
                    if hasattr(module, 'b_args'):
                        b_args = module.b_args
                        
                    if hasattr(module, 'compute_dynamic_b_args'):
                        b_args = module.compute_dynamic_b_args(b_args)
                        
                except Exception as e:
                    self.logger.warning(f"Could not load b_args for {module_name}: {e}")
            
            return {
                "status": "success",
                "data": {
                    "action_name": action_name,
                    "module": module_name,
                    "args_schema": b_args,
                    "description": action.get("b_description", ""),
                    "enabled": bool(action.get("b_enabled", 1))
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error getting action args schema: {e}")
            return {"status": "error", "message": str(e)}

    def get_running_scripts(self) -> Dict:
        """Get list of all currently running scripts."""
        try:
            running = []
            
            with self.shared_data.scripts_lock:
                for path, info in self.shared_data.running_scripts.items():
                    if info.get("is_running", False):
                        running.append({
                            "path": path,
                            "name": os.path.basename(path),
                            "module": info.get("b_module", ""),
                            "class": info.get("b_class", ""),
                            "start_time": info.get("start_time", 0),
                            "runtime": time.time() - info.get("start_time", time.time()),
                            "output_lines": len(info.get("output", []))
                        })
            
            return {"status": "success", "data": running}
            
        except Exception as e:
            self.logger.error(f"Error getting running scripts: {e}")
            return {"status": "error", "message": str(e)}

    def clear_script_output(self, data: Dict) -> Dict:
        """Clear output for a specific script."""
        try:
            script_name = data.get('script_name')
            
            if not script_name:
                return {"status": "error", "message": "Script name is required"}
            
            cleared = False
            with self.shared_data.scripts_lock:
                if script_name in self.shared_data.running_scripts:
                    self.shared_data.running_scripts[script_name]["output"] = []
                    cleared = True
                else:
                    for key, info in self.shared_data.running_scripts.items():
                        if (os.path.basename(key) == script_name or
                            info.get("b_module") == script_name or
                            info.get("b_class") == script_name):
                            info["output"] = []
                            cleared = True
                            break
            
            if cleared:
                return {"status": "success", "message": "Output cleared"}
            else:
                return {"status": "error", "message": "Script not found"}
            
        except Exception as e:
            self.logger.error(f"Error clearing script output: {e}")
            return {"status": "error", "message": str(e)}

    def export_script_logs(self, data: Dict) -> Dict:
        """Export logs for a script to a file."""
        try:
            from datetime import datetime
            import csv
            
            script_name = data.get('script_name')
            format_type = data.get('format', 'txt')
            
            if not script_name:
                return {"status": "error", "message": "Script name is required"}
            
            output = []
            script_info = None
            
            with self.shared_data.scripts_lock:
                if script_name in self.shared_data.running_scripts:
                    script_info = self.shared_data.running_scripts[script_name]
                else:
                    for key, info in self.shared_data.running_scripts.items():
                        if (os.path.basename(key) == script_name or
                            info.get("b_module") == script_name or
                            info.get("b_class") == script_name):
                            script_info = info
                            break
            
            if not script_info:
                return {"status": "error", "message": "Script not found"}
            
            output = script_info.get("output", [])
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{script_name}_{timestamp}.{format_type}"
            filepath = os.path.join(self.shared_data.output_dir, filename)
            
            if format_type == 'json':
                with open(filepath, 'w') as f:
                    json.dump({
                        "script": script_name,
                        "timestamp": timestamp,
                        "logs": output
                    }, f, indent=2)
            elif format_type == 'csv':
                with open(filepath, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow(['Timestamp', 'Message'])
                    for line in output:
                        writer.writerow([datetime.now().isoformat(), line])
            else:
                with open(filepath, 'w') as f:
                    f.write('\n'.join(output))
            
            return {
                "status": "success",
                "message": f"Logs exported to {filename}",
                "data": {
                    "filename": filename,
                    "path": filepath,
                    "lines": len(output)
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error exporting logs: {e}")
            return {"status": "error", "message": str(e)}
