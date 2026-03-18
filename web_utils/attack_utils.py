"""attack_utils.py - Attack listing, import/export, and action metadata management."""
from __future__ import annotations
import json
import os
import ast
import shutil
from io import BytesIO


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
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs
import logging
from logger import Logger
logger = Logger(name="attack_utils.py", level=logging.DEBUG)

class AttackUtils:
    """Utilities for attack/action management."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

    def get_first_class_name(self, filepath: str) -> str:
        """Extract first class name from Python file using AST."""
        try:
            with open(filepath, 'r', encoding='utf-8') as file:
                tree = ast.parse(file.read(), filename=filepath)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    self.logger.debug(f"Found class: {node.name} in {filepath}")
                    return node.name
        except Exception as e:
            self.logger.error(f"Error parsing file {filepath}: {e}")
        self.logger.warning(f"No class found in {filepath}")
        return ''

    def get_first_class_name_from_content(self, content: str) -> str:
        """Extract first class name from Python content using AST."""
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    self.logger.debug(f"Found class in content: {node.name}")
                    return node.name
        except Exception as e:
            self.logger.error(f"Error parsing content: {e}")
        self.logger.warning("No class found in provided content.")
        return ''

    def _extract_action_meta_from_content(self, content: str) -> dict | None:
        """Extract action metadata (b_* variables) from Python content."""
        try:
            tree = ast.parse(content)
            meta = {}
            for node in tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    key = node.targets[0].id
                    if key.startswith("b_"):
                        val = ast.literal_eval(node.value) if isinstance(node.value, (ast.Constant, ast.List, ast.Dict, ast.Tuple)) else None
                        meta[key] = val
            return meta if meta else None
        except Exception:
            return None

    def get_attacks(self, handler):
        """List all attack cards from database."""
        try:
            cards = self.shared_data.db.list_action_cards()
            resp = {"attacks": [{"name": c["name"], "image": c["image"]} for c in cards]}
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps(resp).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"get_attacks error: {e}")
            self._send_error_response(handler, str(e))

    def get_attack_content(self, handler):
        """Get source code content of an attack."""
        try:
            query = handler.path.split('?')[-1]
            from urllib.parse import parse_qs, unquote
            params = dict(parse_qs(query))
            attack_name = unquote(params.get('name', [''])[0])
            if not attack_name:
                raise ValueError("Attack name not provided.")
            
            row = self.shared_data.db.get_action_by_class(attack_name)
            if not row:
                raise FileNotFoundError(f"Attack '{attack_name}' not found in DB.")
            
            module_name = row["b_module"]
            script_path = os.path.join(self.shared_data.actions_dir, f"{module_name}.py")
            with open(script_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self._write_json(handler, {"status": "success", "content": content})
        except Exception as e:
            self.logger.error(f"Error retrieving attack content: {e}")
            self._send_error_response(handler, str(e))

    def add_attack(self, handler):
        """Import a new attack from uploaded file."""
        try:
            ctype = handler.headers.get('Content-Type') or ""
            if 'multipart/form-data' not in ctype:
                raise ValueError("Content-Type must be multipart/form-data.")
            
            form = _MultipartForm(fp=handler.rfile, headers=handler.headers, environ={'REQUEST_METHOD': 'POST'})
            if 'attack_file' not in form:
                raise ValueError("No attack_file field in form.")
            
            file_item = form['attack_file']
            if not file_item.filename.endswith('.py'):
                raise ValueError("Only .py files are allowed.")
            
            filename = file_item.filename
            module_name = os.path.splitext(filename)[0]
            content = file_item.file.read().decode('utf-8')

            # Parse metadata without exec
            meta = self._extract_action_meta_from_content(content)
            if not meta or "b_class" not in meta:
                raise ValueError("File must define b_class (and ideally b_module/b_port).")

            # Write file
            dst = os.path.join(self.shared_data.actions_dir, filename)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(content)

            # Upsert DB
            meta.setdefault("b_module", module_name)
            self.shared_data.db.upsert_simple_action(**meta)

            # Optional: copy to default actions
            if handler.headers.get('Import-Default', 'false').lower() == 'true':
                os.makedirs(self.shared_data.default_actions_dir, exist_ok=True)
                shutil.copyfile(dst, os.path.join(self.shared_data.default_actions_dir, filename))

            self._write_json(handler, {"status": "success", "message": "Attack imported successfully."})
        except Exception as e:
            self.logger.error(f"Error importing attack: {e}")
            self._send_error_response(handler, str(e))

    def remove_attack(self, handler):
        """Remove an attack."""
        try:
            body = handler.rfile.read(int(handler.headers.get('Content-Length', 0)) or 0)
            data = json.loads(body or "{}")
            attack_name = data.get("name") or ""
            if not attack_name:
                raise ValueError("Attack name not provided.")

            row = self.shared_data.db.get_action_by_class(attack_name)
            if not row:
                raise FileNotFoundError(f"Attack '{attack_name}' not found in DB.")
            
            module_name = row["b_module"]
            path = os.path.join(self.shared_data.actions_dir, f"{module_name}.py")
            if os.path.exists(path):
                os.remove(path)

            self.shared_data.db.delete_action(attack_name)
            self._write_json(handler, {"status": "success", "message": "Attack removed successfully."})
        except Exception as e:
            self.logger.error(f"Error removing attack: {e}")
            self._send_error_response(handler, str(e))

    def save_attack(self, handler):
        """Save/update attack source code."""
        try:
            body = handler.rfile.read(int(handler.headers.get('Content-Length', 0)) or 0)
            data = json.loads(body or "{}")
            attack_name = data.get('name') or ""
            content = data.get('content') or ""
            if not attack_name or not content:
                raise ValueError("Missing name or content.")

            row = self.shared_data.db.get_action_by_class(attack_name)
            if not row:
                raise FileNotFoundError(f"Attack '{attack_name}' not found in DB.")
            
            module_name = row["b_module"]
            script_path = os.path.join(self.shared_data.actions_dir, f"{module_name}.py")

            with open(script_path, "w", encoding="utf-8") as f:
                f.write(content)

            # If b_class changed, update DB
            meta = self._extract_action_meta_from_content(content) or {}
            new_b_class = meta.get("b_class")
            if new_b_class and new_b_class != attack_name:
                self.shared_data.db.delete_action(attack_name)
                meta.setdefault("b_module", module_name)
                self.shared_data.db.upsert_simple_action(**meta)
            else:
                meta.setdefault("b_class", attack_name)
                meta.setdefault("b_module", module_name)
                self.shared_data.db.upsert_simple_action(**meta)

            self._write_json(handler, {"status": "success", "message": "Attack saved successfully."})
        except Exception as e:
            self.logger.error(f"Error saving attack: {e}")
            self._send_error_response(handler, str(e))

    def restore_attack(self, handler):
        """Restore attack to default version."""
        try:
            body = handler.rfile.read(int(handler.headers.get('Content-Length', 0)) or 0)
            data = json.loads(body or "{}")
            attack_name = data.get('name') or ""
            if not attack_name:
                raise ValueError("Attack name not provided.")

            row = self.shared_data.db.get_action_by_class(attack_name)
            if not row:
                raise FileNotFoundError(f"Attack '{attack_name}' not found in DB.")
            
            module_name = row["b_module"]
            filename = f"{module_name}.py"

            src = os.path.join(self.shared_data.default_actions_dir, filename)
            dst = os.path.join(self.shared_data.actions_dir, filename)
            if not os.path.exists(src):
                raise FileNotFoundError(f"Default version not found: {src}")
            
            shutil.copyfile(src, dst)

            # Parse and upsert metadata
            with open(dst, "r", encoding="utf-8") as f:
                meta = self._extract_action_meta_from_content(f.read()) or {}
            meta.setdefault("b_class", attack_name)
            meta.setdefault("b_module", module_name)
            self.shared_data.db.upsert_simple_action(**meta)

            self._write_json(handler, {"status": "success", "message": "Attack restored to default successfully."})
        except Exception as e:
            self.logger.error(f"Error restoring attack: {e}")
            self._send_error_response(handler, str(e))

    def serve_actions_icons(self, handler):
        """Serve action icons from actions_icons_dir."""
        try:
            rel = handler.path[len('/actions_icons/'):]
            rel = os.path.normpath(rel).replace("\\", "/")

            # Robust path traversal prevention: resolve to absolute and verify containment
            image_path = os.path.realpath(os.path.join(self.shared_data.actions_icons_dir, rel))
            base_dir = os.path.realpath(self.shared_data.actions_icons_dir)
            if not image_path.startswith(base_dir + os.sep) and image_path != base_dir:
                handler.send_error(400, "Invalid path")
                return

            if not os.path.exists(image_path):
                handler.send_error(404, "Image not found")
                return

            if image_path.endswith('.bmp'):
                mime = 'image/bmp'
            elif image_path.endswith('.png'):
                mime = 'image/png'
            elif image_path.endswith('.jpg') or image_path.endswith('.jpeg'):
                mime = 'image/jpeg'
            else:
                mime = 'application/octet-stream'

            with open(image_path, 'rb') as f:
                content = f.read()

            handler.send_response(200)
            handler.send_header('Content-Type', mime)
            handler.send_header('Content-Length', str(len(content)))
            handler.end_headers()
            handler.wfile.write(content)
            self.logger.info(f"Served action icon: {image_path}")
        except Exception as e:
            self.logger.error(f"Error serving action icon {handler.path}: {e}")
            handler.send_error(500, "Internal Server Error")

    def _write_json(self, handler, obj: dict, code: int = 200):
        """Write JSON response."""
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps(obj).encode('utf-8'))

    def _send_error_response(self, handler, message: str, status_code: int = 500):
        """Send error response in JSON format."""
        handler.send_response(status_code)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        response = {'status': 'error', 'message': message}
        handler.wfile.write(json.dumps(response).encode('utf-8'))
