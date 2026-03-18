"""action_utils.py - Unified web utilities for actions, images, characters, comments, and attacks.

Consolidates ActionUtils, CommentUtils, and AttackUtils into a single module.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import time
import traceback
import logging

from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, ImageDraw, ImageFont

from logger import Logger

# Single shared logger for the whole file
logger = Logger(name="action_utils.py", level=logging.DEBUG)


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


ALLOWED_IMAGE_EXTS = {'.bmp', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.webp'}


# =============================================================================
# Core unified Actions + Images + Characters
# =============================================================================
class ActionUtils:
    """
    One-stop utility for:
      - Action bundle management (create/delete actions: .py + images + comments)
      - Image management (status/static/character; serve/list/rename/replace/resize)
      - Character management (list/switch/create/delete; image IO)
    """

    # Fixed sizes required by spec
    STATUS_W, STATUS_H = 28, 28
    CHAR_W, CHAR_H = 78, 78

    def __init__(self, shared_data):
        """
        shared_data is expected to expose:
          - images_dir, default_images_dir
          - status_images_dir, static_images_dir, actions_icons_dir
          - actions_dir, default_actions_dir
          - default_comments_file
          - db (with needed methods)
          - config (dict-like) + save_config() + load_config()
          - load_images(), load_fonts()
          - bjorn_status_image, bjorn_character
          - web_dir
        """
        self.shared_data = shared_data
        self.logger = logger

        # Optional manual batch resize flags (for /resize_images only)
        self.should_resize_images = False
        self.resize_width = 100
        self.resize_height = 100
        # dirs
        self.status_images_dir = getattr(shared_data, "status_images_dir")
        self.static_images_dir = getattr(shared_data, "static_images_dir")
        self.web_dir          = getattr(shared_data, "web_dir")
        self.images_dir       = getattr(shared_data, "images_dir", None)

        self.web_images_dir   = getattr(shared_data, "web_images_dir", os.path.join(self.web_dir, "images"))
        self.actions_icons_dir= getattr(shared_data, "actions_icons_dir", os.path.join(self.images_dir or self.web_dir, "actions_icons"))
        for d in (self.status_images_dir, self.static_images_dir, self.web_images_dir, self.actions_icons_dir):
            try: os.makedirs(d, exist_ok=True)
            except Exception: pass
    # ---------- generic helpers ----------

    def _send_json(self, handler, data, status: int = 200):
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_error(self, handler, message: str, status: int = 500):
        self._send_json(handler, {"status": "error", "message": message}, status)

    def _ensure_dir(self, path: str | Path) -> str:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def _ensure_action_dir(self, action_name: str) -> str:
        return self._ensure_dir(Path(self.shared_data.status_images_dir) / action_name)
    def _err(self, h, msg: str, code: int=500): self._send_json(h, {'status':'error','message':msg}, code)

    def _get_mime(self, path: str) -> str:
        p = path.lower()
        if p.endswith(".bmp"):
            return "image/bmp"
        if p.endswith(".png"):
            return "image/png"
        if p.endswith(".jpg") or p.endswith(".jpeg"):
            return "image/jpeg"
        return "application/octet-stream"
    
    def _safe(self, name: str) -> str:
        return os.path.basename((name or '').strip().replace('\x00', ''))
    
    def _to_bmp_resized(self, raw: bytes, width: int, height: int) -> bytes:
        """Convert arbitrary image bytes to BMP, always resized to (width x height)."""
        with Image.open(BytesIO(raw)) as im:
            if im.mode != "RGB":
                im = im.convert("RGB")
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.LANCZOS
            im = im.resize((width, height), resample)
            out = BytesIO()
            im.save(out, format="BMP")
            return out.getvalue()

    def _resize_bmp(self, bmp_data: bytes, width: int, height: int) -> bytes:
        """Resize an existing BMP payload (used by batch resizing)."""
        return self._to_bmp_resized(bmp_data, width, height)

    def _initials(self, name: str) -> str:
        parts = re.split(r"[^A-Za-z0-9]+", name.strip())
        parts = [p for p in parts if p]
        if not parts:
            return "A"
        return "".join(s[0] for s in parts[:2]).upper()

    def _placeholder_icon(self, text: str, size: int) -> bytes:
        """
        Build a simple placeholder with initials (similar intent to makePlaceholderIconBlob),
        then return as BMP bytes.
        """
        img = Image.new("RGB", (size, size), "#0b0e13")
        draw = ImageDraw.Draw(img)

        # Ring
        ring_color = "#59b6ff"
        ring_w = max(2, size // 16)
        r = size // 2 - ring_w
        draw.ellipse(
            (ring_w, ring_w, size - ring_w, size - ring_w),
            outline=ring_color,
            width=ring_w,
        )

        # Font
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(10, size // 2))
        except Exception:
            font = ImageFont.load_default()

        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
        except AttributeError:
            tw, th = draw.textsize(text, font=font)
        draw.text(((size - tw) / 2, (size - th) / 2), text, fill=ring_color, font=font)

        out = BytesIO()
        img.save(out, format="BMP")
        return out.getvalue()

    # ---------- action bundle (scripts + comments + images) ----------

    def _extract_action_meta(self, content: str) -> Optional[Dict[str, Any]]:
        """Parse b_* metadata from a Python attack file."""
        try:
            tree = ast.parse(content)
            meta: Dict[str, Any] = {}
            for node in tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    target = node.targets[0]
                    if isinstance(target, ast.Name) and target.id.startswith("b_"):
                        try:
                            meta[target.id] = ast.literal_eval(node.value)
                        except Exception:
                            meta[target.id] = None
            return meta or None
        except Exception:
            return None

    def serve_bjorn_character(self, handler):
        try:
            # Fallback robust: use current character sprite, or static default "bjorn1"
            img = self.shared_data.bjorn_character or getattr(self.shared_data, 'bjorn1', None)
            
            if img is None:
                raise ValueError("No character image (bjorn_character or bjorn1) available")

            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='PNG')
            img_byte_arr = img_byte_arr.getvalue()
            
            handler.send_response(200)
            handler.send_header('Content-Type', 'image/png')
            handler.send_header('Cache-Control', 'no-cache')
            handler.end_headers()
            handler.wfile.write(img_byte_arr)
        except BrokenPipeError:
            pass
        except Exception as e:
            self.logger.error(f"Error serving status image: {e}")


    def serve_bjorn_say(self, handler):
        try:
            bjorn_says_data = {
                "text": self.shared_data.bjorn_says
            }
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps(bjorn_says_data).encode('utf-8'))
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                handler.send_response(500)
                handler.send_header("Content-Type", "application/json")
                handler.end_headers()
                handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
            except BrokenPipeError:
                pass

    def create_action(self, handler):
        """
        Create a complete action: Python script + images + comment section.

        Multipart form:
          - action_name (str)
          - attack_file (.py)
          - status_icon (image)
          - character_images (0..n images, optional)
          - create_comments_section ('1' to auto-create)
        """
        try:
            ctype = handler.headers.get("Content-Type", "")
            if not ctype.startswith("multipart/form-data"):
                raise ValueError("Content-Type must be multipart/form-data")

            content_length = int(handler.headers.get("Content-Length", 0))
            body = handler.rfile.read(content_length)

            form = _MultipartForm(
                fp=BytesIO(body),
                headers=handler.headers,
                environ={"REQUEST_METHOD": "POST"},
                keep_blank_values=True,
            )

            action_name = (form.getvalue("action_name") or "").strip()
            if not action_name:
                raise ValueError("action_name is required")

            if "attack_file" not in form or not getattr(form["attack_file"], "filename", ""):
                raise ValueError("attack_file (.py) is required")
            attack_file = form["attack_file"]

            if "status_icon" not in form or not getattr(form["status_icon"], "filename", ""):
                raise ValueError("status_icon is required")
            status_icon = form["status_icon"]

            create_comments = form.getvalue("create_comments_section") == "1"

            # 1) script + DB card
            self._import_python_script(action_name, attack_file)

            # 2) images (fixed sizes; ensure at least 1 character)
            self._create_action_images(action_name, status_icon, form)

            # 3) comments (optional)
            if create_comments:
                self._create_comment_section(action_name)

            self._send_json(handler, {"status": "success", "message": f'Action "{action_name}" created successfully'})
        except Exception as e:
            self.logger.error(f"create_action: {e}")
            self._send_error(handler, str(e), 400)

    def _import_python_script(self, action_name: str, file_item):
        """Persist the .py and upsert DB action card from b_* meta."""
        filename = os.path.basename(file_item.filename)
        module_name = os.path.splitext(filename)[0]
        content = file_item.file.read().decode("utf-8")

        meta = self._extract_action_meta(content)
        if not meta or "b_class" not in meta:
            raise ValueError("Python file must define b_class")

        dst = os.path.join(self.shared_data.actions_dir, filename)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)

        meta.setdefault("b_module", module_name)
        self.shared_data.db.upsert_simple_action(**meta)

    def delete_action(self, handler, data=None):
        """Delete action: python script + images + comment section."""
        try:
            if data is None:
                content_length = int(handler.headers.get("Content-Length", 0))
                body = handler.rfile.read(content_length) if content_length > 0 else b"{}"
                data = json.loads(body)
            elif not isinstance(data, dict):
                raise ValueError("Invalid JSON payload")

            action_name = (data.get("action_name") or "").strip()
            if not action_name:
                raise ValueError("action_name is required")

            self._remove_python_script(action_name)
            self._delete_action_images(action_name)
            self._delete_comment_section(action_name)

            self._send_json(handler, {"status": "success", "message": f'Action "{action_name}" deleted successfully'})
        except Exception as e:
            self.logger.error(f"delete_action: {e}")
            self._send_error(handler, str(e), 400)

    def _remove_python_script(self, action_name: str):
        row = self.shared_data.db.get_action_by_class(action_name)
        if not row:
            raise FileNotFoundError(f"Action '{action_name}' not found in DB")
        module_name = row["b_module"]
        path = os.path.join(self.shared_data.actions_dir, f"{module_name}.py")
        if os.path.exists(path):
            os.remove(path)
        self.shared_data.db.delete_action(action_name)

    def restore_defaults(self, handler):
        """Restore defaults for images, comments and actions (scripts)."""
        try:
            # Images
            images_dir = self.shared_data.images_dir
            default_images_dir = self.shared_data.default_images_dir
            if not os.path.exists(default_images_dir):
                raise FileNotFoundError(f"Default images directory not found: {default_images_dir}")
            if os.path.exists(images_dir):
                shutil.rmtree(images_dir)
            shutil.copytree(default_images_dir, images_dir)

            # Comments
            inserted = self.shared_data.db.import_comments_from_json(
                self.shared_data.default_comments_file, lang="fr", clear_existing=True
            )

            # Scripts
            actions_dir = self.shared_data.actions_dir
            default_actions_dir = self.shared_data.default_actions_dir
            if os.path.exists(default_actions_dir):
                if os.path.exists(actions_dir):
                    for f in os.listdir(actions_dir):
                        if f.endswith(".py"):
                            os.remove(os.path.join(actions_dir, f))
                for f in os.listdir(default_actions_dir):
                    if f.endswith(".py"):
                        shutil.copy2(os.path.join(default_actions_dir, f), os.path.join(actions_dir, f))

            # Rebuild cards
            self._rebuild_action_cards()

            self._send_json(
                handler,
                {"status": "success", "message": f"Defaults restored (actions, images, {inserted} comments)"},
            )
        except Exception as e:
            self.logger.error(f"restore_defaults: {e}")
            self._send_error(handler, str(e), 500)

    def _rebuild_action_cards(self):
        """
        Rebuild DB 'actions' + 'actions_studio' from filesystem .py files.
        - 'actions'      : info runtime (b_class, b_module, etc.)
        - 'actions_studio': studio payload (full meta as JSON)
        """
        actions_dir = self.shared_data.actions_dir

        # Minimum schema (in case migration hasn't run)
        self.shared_data.db.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                name       TEXT PRIMARY KEY,
                b_class    TEXT NOT NULL,
                b_module   TEXT NOT NULL,
                meta_json  TEXT
            )
        """)
        self.shared_data.db.execute("""
            CREATE TABLE IF NOT EXISTS actions_studio (
                action_name       TEXT PRIMARY KEY,
                studio_meta_json  TEXT
            )
        """)

        # Rebuild from disk
        self.shared_data.db.execute("DELETE FROM actions")
        self.shared_data.db.execute("DELETE FROM actions_studio")

        for filename in os.listdir(actions_dir):
            if not filename.endswith(".py"):
                continue

            filepath = os.path.join(actions_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            meta = self._extract_action_meta(content)
            if not (meta and "b_class" in meta):
                continue

            module_name = os.path.splitext(filename)[0]
            meta.setdefault("b_module", module_name)

            # Logical action name: use 'name' if present, fall back to b_class
            action_name = (meta.get("name") or meta["b_class"]).strip()

            # Upsert into actions
            self.shared_data.db.execute(
                """
                INSERT INTO actions (name, b_class, b_module, meta_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    b_class   = excluded.b_class,
                    b_module  = excluded.b_module,
                    meta_json = excluded.meta_json
                """,
                (action_name, meta["b_class"], meta["b_module"], json.dumps(meta, ensure_ascii=False))
            )

            # Upsert into actions_studio (store full meta or studio-relevant subset)
            self.shared_data.db.execute(
                """
                INSERT INTO actions_studio (action_name, studio_meta_json)
                VALUES (?, ?)
                ON CONFLICT(action_name) DO UPDATE SET
                    studio_meta_json = excluded.studio_meta_json
                """,
                (action_name, json.dumps(meta, ensure_ascii=False))
            )

    def _create_comment_section(self, action_name: str):
        self.shared_data.db.execute(
            "INSERT OR IGNORE INTO comments (text, status, theme, lang, weight) VALUES (?, ?, ?, ?, ?)",
            ("", action_name, action_name, "fr", 1),
        )

    def _delete_comment_section(self, action_name: str):
        self.shared_data.db.execute("DELETE FROM comments WHERE status=?", (action_name,))

    # ---------- images ----------

    def get_actions(self, handler):
        """List action folders and whether status icon exists."""
        try:
            actions_dir = self.shared_data.status_images_dir
            actions = []
            for entry in os.scandir(actions_dir):
                if entry.is_dir():
                    name = entry.name
                    has_status_icon = os.path.exists(os.path.join(entry.path, f"{name}.bmp"))
                    actions.append({"name": name, "has_status_icon": has_status_icon})
            self._send_json(handler, {"status": "success", "actions": actions})
        except Exception as e:
            self.logger.error(f"get_actions: {e}")
            self._send_error(handler, str(e))

    def get_action_images(self, handler):
        """List all BMP images for a given action with dimensions."""
        try:
            q = parse_qs(urlparse(handler.path).query)
            action = (q.get("action", [None])[0] or "").strip()
            if not action:
                raise ValueError("Action parameter is required")

            action_dir = os.path.join(self.shared_data.status_images_dir, action)
            if not os.path.exists(action_dir):
                raise FileNotFoundError(f"Action '{action}' does not exist")

            images = []
            for entry in os.listdir(action_dir):
                if entry.lower().endswith(".bmp"):
                    path = os.path.join(action_dir, entry)
                    with Image.open(path) as img:
                        w, h = img.size
                    images.append({"name": entry, "width": w, "height": h})

            self._send_json(handler, {"status": "success", "images": images})
        except Exception as e:
            self.logger.error(f"get_action_images: {e}")
            self._send_error(handler, str(e))

    def serve_status_image(self, handler):
        """Serve any file under /images/status/<...> safely."""
        try:
            url_path = unquote(urlparse(handler.path).path)
            prefix = "/images/status/"
            if not url_path.startswith(prefix):
                handler.send_error(400, "Bad Request")
                return

            rel = url_path[len(prefix) :]
            base = Path(self.shared_data.status_images_dir).resolve()
            target = (base / rel).resolve()

            if not str(target).startswith(str(base)):
                handler.send_error(403, "Forbidden")
                return
            if not target.exists():
                handler.send_error(404, "Image not found")
                return

            with open(target, "rb") as f:
                content = f.read()

            handler.send_response(200)
            handler.send_header("Content-Type", self._get_mime(str(target)))
            handler.send_header("Content-Length", str(len(content)))
            handler.end_headers()
            handler.wfile.write(content)
        except BrokenPipeError:
            pass
        except Exception as e:
            self.logger.error(f"serve_status_image: {e}")
            handler.send_error(500, "Internal Server Error")

    def list_static_images_with_dimensions(self, handler):
        """List static images (any format readable by PIL) with dimensions."""
        try:
            static_dir = self.shared_data.static_images_dir
            images = []
            for f in os.listdir(static_dir):
                path = os.path.join(static_dir, f)
                if not os.path.isfile(path):
                    continue
                try:
                    with Image.open(path) as img:
                        w, h = img.size
                    images.append({"name": f, "width": w, "height": h})
                except Exception:
                    continue
            self._send_json(handler, {"status": "success", "images": images})
        except Exception as e:
            self.logger.error(f"list_static_images_with_dimensions: {e}")
            self._send_error(handler, str(e))

    def upload_static_image(self, handler):
        """Upload a static image; store as BMP. Optional manual size via flags."""
        try:
            ctype, pdict = _parse_header(handler.headers.get("Content-Type"))
            if ctype != "multipart/form-data":
                raise ValueError("Content-Type must be multipart/form-data")
            pdict["boundary"] = bytes(pdict["boundary"], "utf-8")
            pdict["CONTENT-LENGTH"] = int(handler.headers.get("Content-Length"))

            form = _MultipartForm(
                fp=BytesIO(handler.rfile.read(pdict["CONTENT-LENGTH"])),
                headers=handler.headers,
                environ={"REQUEST_METHOD": "POST"},
                keep_blank_values=True,
            )
            if "static_image" not in form or not getattr(form["static_image"], "filename", ""):
                raise ValueError("No static_image file provided")

            filename = os.path.basename(form["static_image"].filename)
            base, _ = os.path.splitext(filename)
            filename = base + ".bmp"

            raw = form["static_image"].file.read()
            if self.should_resize_images:
                bmp = self._to_bmp_resized(raw, self.resize_width, self.resize_height)
            else:
                # Store as-is but normalized to BMP; keep original size by reading image size first
                with Image.open(BytesIO(raw)) as im:
                    w, h = im.size
                bmp = self._to_bmp_resized(raw, w, h)

            static_dir = self.shared_data.static_images_dir
            self._ensure_dir(static_dir)
            with open(os.path.join(static_dir, filename), "wb") as f:
                f.write(bmp)

            self._send_json(handler, {"status": "success", "message": "Static image uploaded successfully"})
        except Exception as e:
            self.logger.error(f"upload_static_image: {e}")
            self._send_error(handler, str(e))

    # ---------- CRUD that might touch action character files ----------
    def delete_images(self, h):
        """Delete images in 'static'|'web'|'icons' or action folder. When type='action', call CharacterUtils to renumber."""
        try:
            data = json.loads(h.rfile.read(int(h.headers['Content-Length'])).decode('utf-8'))
            tp = data.get('type'); action = data.get('action'); names = data.get('image_names', [])
            if not tp or not names: raise ValueError('type and image_names are required')
            if tp == 'action':
                if not action: raise ValueError("action is required for type=action")
                base = os.path.join(self.status_images_dir, self._safe(action))
                for n in names:
                    p = os.path.join(base, self._safe(n))
                    if os.path.exists(p): os.remove(p)
                if self.character_utils:
                    self.character_utils.update_character_image_numbers(action)
            elif tp == 'static':
                for n in names:
                    p = os.path.join(self.static_images_dir, self._safe(n))
                    if os.path.exists(p): os.remove(p)
            elif tp == 'web':
                for n in names:
                    p = os.path.join(self.web_images_dir, self._safe(n))
                    if os.path.exists(p): os.remove(p)
            elif tp == 'icons':
                for n in names:
                    p = os.path.join(self.actions_icons_dir, self._safe(n))
                    if os.path.exists(p): os.remove(p)
            else:
                raise ValueError("type must be 'action','static','web','icons'")
            self._send_json(h, {'status':'success','message':'Images deleted successfully'})
        except Exception as e:
            self.logger.error(e); self._err(h, str(e))


    def resize_images(self, handler):
        try:
            data = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])).decode("utf-8"))
            image_type = data.get("type")
            action_name = data.get("action")
            image_names = data.get("image_names", [])
            width = int(data.get("width", 100))
            height = int(data.get("height", 100))

            if image_type == "action":
                base = os.path.join(self.shared_data.status_images_dir, action_name)
                mode = "bmp_only"
            elif image_type == "static":
                base = self.shared_data.static_images_dir
                mode = "bmp_only"
            elif image_type == "web":
                base = self.web_images_dir
                mode = "preserve_format"
            elif image_type == "icons":
                base = self.actions_icons_dir
                mode = "preserve_format"
            else:
                raise ValueError("Invalid image type")

            for name in image_names:
                path = os.path.join(base, name)
                if not os.path.exists(path):
                    self.logger.error(f"Missing image: {path}")
                    continue

                # Ouvrir, redimensionner
                with Image.open(path) as im:
                    try:
                        resample = Image.Resampling.LANCZOS
                    except AttributeError:
                        resample = Image.LANCZOS
                    im = im.resize((width, height), resample)

                    if mode == "bmp_only":
                        im = im.convert("RGB")
                        im.save(path, "BMP")
                    else:
                        ext = os.path.splitext(name)[1].lower()
                        fmt = {
                            ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
                            ".gif": "GIF", ".ico": "ICO", ".bmp": "BMP", ".webp": "WEBP"
                        }.get(ext, "PNG")
                        if fmt in ("JPEG", "BMP"):  # formats sans alpha
                            im = im.convert("RGB")
                        im.save(path, fmt)

            self._send_json(handler, {"status": "success"})
        except Exception as e:
            self.logger.error(f"resize_images: {e}")
            self._send_error(handler, str(e))


    def rename_image(self, handler, data=None):
        """Rename a static image, an action image, or an action folder."""
        try:
            if data is None:
                data = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])).decode("utf-8"))
            elif not isinstance(data, dict):
                raise ValueError("Invalid JSON payload")
            entity_type = data.get("type")  # 'action' | 'static' | 'image'
            old_name = data.get("old_name")
            new_name = data.get("new_name")
            action = data.get("action")

            if not entity_type or not old_name or not new_name:
                raise ValueError("type, old_name, and new_name are required")

            if entity_type == "action":
                root = self.shared_data.status_images_dir
                oldp = os.path.join(root, old_name)
                newp = os.path.join(root, new_name)
                if not os.path.exists(oldp):
                    raise FileNotFoundError(f"Action '{old_name}' does not exist")
                os.rename(oldp, newp)
            elif entity_type == "static":
                root = self.shared_data.static_images_dir
                oldp = os.path.join(root, old_name)
                newp = os.path.join(root, new_name)
                if not os.path.exists(oldp):
                    raise FileNotFoundError(f"Static image '{old_name}' does not exist")
                os.rename(oldp, newp)
            elif entity_type == "web":
                root = self.web_images_dir
                oldp = os.path.join(root, old_name); newp = os.path.join(root, new_name)
                if not os.path.exists(oldp): raise FileNotFoundError(f"Web image '{old_name}' does not exist")
                os.rename(oldp, newp)

            elif entity_type == "icons":
                root = self.actions_icons_dir
                oldp = os.path.join(root, old_name); newp = os.path.join(root, new_name)
                if not os.path.exists(oldp): raise FileNotFoundError(f"Icon '{old_name}' does not exist")
                os.rename(oldp, newp)

            elif entity_type == "image":
                if not action:
                    raise ValueError("action is required to rename an action image")
                root = os.path.join(self.shared_data.status_images_dir, action)
                oldp = os.path.join(root, old_name)
                newp = os.path.join(root, new_name)
                if not os.path.exists(oldp):
                    raise FileNotFoundError(f"Image '{old_name}' does not exist in '{action}'")
                os.rename(oldp, newp)
                self._renumber_character_images(action)
            else:
                raise ValueError("type must be 'action', 'static', or 'image'")

            self._send_json(handler, {"status": "success", "message": "Renamed successfully"})
        except Exception as e:
            self.logger.error(f"rename_image: {e}")
            self._send_error(handler, str(e))

    def replace_image(self, h):

        try:
            ctype, pdict = _parse_header(h.headers.get('Content-Type'))
            if ctype != 'multipart/form-data':
                raise ValueError('Content-Type must be multipart/form-data')
            pdict['boundary'] = bytes(pdict['boundary'], 'utf-8')
            pdict['CONTENT-LENGTH'] = int(h.headers.get('Content-Length'))

            form = _MultipartForm(
                fp=BytesIO(h.rfile.read(pdict['CONTENT-LENGTH'])),
                headers=h.headers,
                environ={'REQUEST_METHOD': 'POST'},
                keep_blank_values=True,
            )

            tp = form.getvalue('type')
            image_name = self._safe(form.getvalue('image_name') or '')
            file_item = form['new_image'] if 'new_image' in form else None

            # Don't use "not file_item" (FieldStorage is not bool-safe)
            if not tp or not image_name or file_item is None or not getattr(file_item, 'filename', ''):
                raise ValueError('type, image_name and new_image are required')

            if tp == 'action':
                action = self._safe(form.getvalue('action') or '')
                if not action:
                    raise ValueError("action is required for type=action")

                base = os.path.join(self.status_images_dir, action)
                target = os.path.join(base, image_name)
                if not os.path.exists(target):
                    raise FileNotFoundError(f"{image_name} not found")

                raw = file_item.file.read()

                # Status icon <action>.bmp => forced BMP 28x28
                if image_name.lower() == f"{action.lower()}.bmp":
                    out = self._to_bmp_resized(raw, self.STATUS_W, self.STATUS_H)
                    with open(target, 'wb') as f:
                        f.write(out)
                else:
                    # Delegate to character utils for numbered character image
                    if not self.character_utils:
                        raise RuntimeError("CharacterUtils not wired into ImageUtils")
                    return self.character_utils.replace_character_image(h, form, action, image_name)

            elif tp == 'static':
                path = os.path.join(self.static_images_dir, image_name)
                if not os.path.exists(path):
                    raise FileNotFoundError(image_name)
                with Image.open(path) as im:
                    w, hh = im.size
                raw = file_item.file.read()
                out = self._to_bmp_resized(raw, w, hh)
                with open(path, 'wb') as f:
                    f.write(out)

            elif tp == 'web':
                path = os.path.join(self.web_images_dir, image_name)
                if not os.path.exists(path):
                    raise FileNotFoundError(image_name)
                with open(path, 'wb') as f:
                    f.write(file_item.file.read())

            elif tp == 'icons':
                path = os.path.join(self.actions_icons_dir, image_name)
                if not os.path.exists(path):
                    raise FileNotFoundError(image_name)
                with open(path, 'wb') as f:
                    f.write(file_item.file.read())

            else:
                raise ValueError("type must be 'action'|'static'|'web'|'icons'")

            self._send_json(h, {'status':'success','message':'Image replaced successfully'})

        except Exception as e:
            self.logger.error(e)
            self._err(h, str(e))


    def upload_status_image(self, handler):
        """
        Add or replace the STATUS image for an action; always saved as
        <status_images_dir>/<action>/<action>.bmp (28x28).
        Creates the action folder if it doesn't exist.
        """
        try:
            ctype, pdict = _parse_header(handler.headers.get("Content-Type"))
            if ctype != "multipart/form-data":
                raise ValueError("Content-Type must be multipart/form-data")
            pdict["boundary"] = bytes(pdict["boundary"], "utf-8")
            pdict["CONTENT-LENGTH"] = int(handler.headers.get("Content-Length"))

            form = _MultipartForm(
                fp=BytesIO(handler.rfile.read(pdict["CONTENT-LENGTH"])),
                headers=handler.headers,
                environ={"REQUEST_METHOD": "POST"},
                keep_blank_values=True,
            )

            required = ["type", "action_name", "status_image"]
            for key in required:
                if key not in form:
                    raise ValueError(f"Missing field: {key}")

            image_type = (form.getvalue("type") or "").strip()
            action_name = (form.getvalue("action_name") or "").strip()
            file_item = form["status_image"]

            if image_type != "action":
                raise ValueError("type must be 'action' for status upload")
            if not action_name or not getattr(file_item, "filename", ""):
                raise ValueError("action_name and status_image are required")

            action_dir = self._ensure_action_dir(action_name)
            raw = file_item.file.read()
            bmp = self._to_bmp_resized(raw, self.STATUS_W, self.STATUS_H)

            with open(os.path.join(action_dir, f"{action_name}.bmp"), "wb") as f:
                f.write(bmp)

            self._send_json(
                handler,
                {"status": "success", "message": "Status image added/updated", "path": f"{action_name}/{action_name}.bmp"},
            )
        except Exception as e:
            self.logger.error(f"upload_status_image: {e}")
            self._send_error(handler, str(e))

    def upload_character_images(self, handler):
        """
        Append character images for an action (numbered <action>1.bmp, <action>2.bmp, ...).
        Always resized to 78x78 BMP.
        """
        try:
            ctype, pdict = _parse_header(handler.headers.get("Content-Type"))
            if ctype != "multipart/form-data":
                raise ValueError("Content-Type must be multipart/form-data")
            pdict["boundary"] = bytes(pdict["boundary"], "utf-8")
            pdict["CONTENT-LENGTH"] = int(handler.headers.get("Content-Length"))

            form = _MultipartForm(
                fp=BytesIO(handler.rfile.read(pdict["CONTENT-LENGTH"])),
                headers=handler.headers,
                environ={"REQUEST_METHOD": "POST"},
                keep_blank_values=True,
            )

            if "action_name" not in form:
                raise ValueError("action_name is required")
            action_name = (form.getvalue("action_name") or "").strip()
            if not action_name:
                raise ValueError("action_name is required")
            if "character_images" not in form:
                raise ValueError("No character_images provided")

            action_dir = os.path.join(self.shared_data.status_images_dir, action_name)
            if not os.path.exists(action_dir):
                raise FileNotFoundError(f"Action '{action_name}' does not exist")

            # find next N
            nums: set[int] = set()
            pat = re.compile(rf"^{re.escape(action_name)}(\d+)\.bmp$", re.IGNORECASE)
            for p in Path(action_dir).glob("*.bmp"):
                m = pat.match(p.name)
                if m:
                    try:
                        nums.add(int(m.group(1)))
                    except ValueError:
                        pass
            next_num = max(nums or {0}) + 1

            items = form["character_images"]
            if not isinstance(items, list):
                items = [items]

            for it in items:
                if not getattr(it, "filename", ""):
                    continue
                raw = it.file.read()
                bmp = self._to_bmp_resized(raw, self.CHAR_W, self.CHAR_H)
                with open(os.path.join(action_dir, f"{action_name}{next_num}.bmp"), "wb") as f:
                    f.write(bmp)
                next_num += 1

            self._send_json(handler, {"status": "success", "message": "Character images uploaded"})
        except Exception as e:
            self.logger.error(f"upload_character_images: {e}")
            self._send_error(handler, str(e))

    def _renumber_character_images(self, action_name: str):
        """Ensure <action>N.bmp are sequential after deletions/renames."""
        action_dir = os.path.join(self.shared_data.status_images_dir, action_name)
        if not os.path.isdir(action_dir):
            return
        pairs = []
        pat = re.compile(rf"^{re.escape(action_name)}(\d+)\.bmp$", re.IGNORECASE)
        for fn in os.listdir(action_dir):
            m = pat.match(fn)
            if m:
                try:
                    pairs.append((int(m.group(1)), fn))
                except ValueError:
                    pass
        pairs.sort()
        idx = 1
        for _n, fn in pairs:
            new_fn = f"{action_name}{idx}.bmp"
            if fn != new_fn:
                os.rename(os.path.join(action_dir, fn), os.path.join(action_dir, new_fn))
            idx += 1

    def _create_action_images(self, action_name, status_icon, form):
        """
        Create action folder with:
          - <action>.bmp (status, 28x28)
          - <action>1.bmp.. (characters, 78x78); if none provided, derive 1 from status
        """
        action_dir = self._ensure_action_dir(action_name)

        if os.listdir(action_dir):  # prevent accidental overwrite
            raise FileExistsError(f"Action '{action_name}' already exists")

        # Status icon 28x28
        status_raw = status_icon.file.read()
        status_bmp = self._to_bmp_resized(status_raw, self.STATUS_W, self.STATUS_H)
        with open(os.path.join(action_dir, f"{action_name}.bmp"), "wb") as f:
            f.write(status_bmp)

        # Characters 78x78
        provided = False
        if "character_images" in form:
            items = form["character_images"]
            if not isinstance(items, list):
                items = [items]
            idx = 1
            for it in items:
                if not getattr(it, "filename", ""):
                    continue
                provided = True
                raw = it.file.read()
                bmp = self._to_bmp_resized(raw, self.CHAR_W, self.CHAR_H)
                with open(os.path.join(action_dir, f"{action_name}{idx}.bmp"), "wb") as f:
                    f.write(bmp)
                idx += 1

        if not provided:
            # Derive character image from status (upscale to 78x78)
            char_from_status = self._to_bmp_resized(status_bmp, self.CHAR_W, self.CHAR_H)
            with open(os.path.join(action_dir, f"{action_name}1.bmp"), "wb") as f:
                f.write(char_from_status)

    def get_status_icon(self, handler):
        """Serve <action>/<action>.bmp if it exists. No placeholder - let the frontend handle fallback."""
        try:
            q = parse_qs(urlparse(handler.path).query)
            action = (q.get("action", [None])[0] or "").strip()
            if not action:
                raise ValueError("action is required")

            action_dir = os.path.join(self.shared_data.status_images_dir, action)
            icon_path = os.path.join(action_dir, f"{action}.bmp")

            if not os.path.exists(icon_path):
                handler.send_response(404)
                handler.end_headers()
                return

            with open(icon_path, "rb") as f:
                data = f.read()

            handler.send_response(200)
            handler.send_header("Content-Type", "image/bmp")
            handler.end_headers()
            handler.wfile.write(data)

        except Exception as e:
            self.logger.error(f"get_status_icon: {e}")
            handler.send_response(404)
            handler.end_headers()


    def get_character_image(self, handler):
        """Serve a specific character image for an action."""
        try:
            q = parse_qs(urlparse(handler.path).query)
            action = (q.get("action", [None])[0] or "").strip()
            image_name = (q.get("image", [None])[0] or "").strip()
            if not action or not image_name:
                raise ValueError("action and image are required")
            path = os.path.join(self.shared_data.status_images_dir, action, image_name)
            if not os.path.exists(path):
                raise FileNotFoundError(f"{image_name} not found for '{action}'")
            with open(path, "rb") as f:
                data = f.read()
            handler.send_response(200)
            handler.send_header("Content-Type", "image/bmp")
            handler.end_headers()
            handler.wfile.write(data)
        except Exception as e:
            self.logger.error(f"get_character_image: {e}")
            handler.send_response(404)
            handler.end_headers()

    def restore_default_images(self, handler):
        """Restore all images from the default images bundle."""
        try:
            images_dir = self.shared_data.images_dir
            default_images_dir = self.shared_data.default_images_dir
            if not os.path.exists(default_images_dir):
                raise FileNotFoundError(f"Default images directory not found: {default_images_dir}")
            if os.path.exists(images_dir):
                shutil.rmtree(images_dir)
            shutil.copytree(default_images_dir, images_dir)
            self._send_json(handler, {"status": "success", "message": "Images restored successfully"})
        except Exception as e:
            self.logger.error(f"restore_default_images: {e}")
            self._send_error(handler, str(e))

    def set_resize_option(self, handler):
        """
        Update optional resize settings used ONLY by /resize_images endpoint.
        Status/character creation paths ignore these and use fixed sizes.
        """
        try:
            data = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])).decode("utf-8"))
            self.should_resize_images = bool(data.get("resize", False))
            self.resize_width = int(data.get("width", 100))
            self.resize_height = int(data.get("height", 100))
            self._send_json(handler, {"status": "success", "message": "Resize options updated"})
        except Exception as e:
            self.logger.error(f"set_resize_option: {e}")
            self._send_error(handler, str(e))

    def serve_bjorn_status_image(self, handler):
        """Serve in-memory Bjorn status image (PNG)."""
        try:
            out = io.BytesIO()
            self.shared_data.bjorn_status_image.save(out, format="PNG")
            data = out.getvalue()
            handler.send_response(200)
            handler.send_header("Content-Type", "image/png")
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            handler.wfile.write(data)
        except BrokenPipeError:
            pass
        except Exception as e:
            self.logger.error(f"serve_bjorn_status_image: {e}")

    def serve_image(self, handler):
        """Serve /web/screen.png (if present)."""
        path = os.path.join(self.shared_data.web_dir, "screen.png")
        try:
            with open(path, "rb") as f:
                handler.send_response(200)
                handler.send_header("Content-type", "image/png")
                handler.send_header("Cache-Control", "max-age=0, must-revalidate")
                handler.end_headers()
                handler.wfile.write(f.read())
        except FileNotFoundError:
            handler.send_response(404)
            handler.end_headers()
        except BrokenPipeError:
            pass
        except Exception as e:
            self.logger.error(f"serve_image: {e}")

    def serve_static_image(self, handler):
        """Serve a static image by filename from static_images_dir."""
        try:
            path = unquote(urlparse(handler.path).path)
            name = os.path.basename(path)
            full = os.path.join(self.shared_data.static_images_dir, name)
            if not os.path.exists(full):
                raise FileNotFoundError(f"Static image '{name}' not found")
            with open(full, "rb") as f:
                data = f.read()
            handler.send_response(200)
            handler.send_header("Content-Type", "image/bmp" if full.lower().endswith(".bmp") else "image/jpeg")
            handler.end_headers()
            handler.wfile.write(data)
        except BrokenPipeError:
            pass
        except Exception as e:
            self.logger.error(f"serve_static_image: {e}")
            handler.send_response(404)
            handler.end_headers()

    # ---------- characters ----------

    def _current_character(self) -> str:
        try:
            return self.shared_data.config.get("current_character", "BJORN") or "BJORN"
        except Exception:
            return "BJORN"

    def list_characters(self, handler):
        """List available characters and mark current one."""
        try:
            chars_dir = self.shared_data.settings_dir
            characters = []
            for entry in os.scandir(chars_dir):
                if entry.is_dir():
                    name = entry.name
                    idle_path = os.path.join(entry.path, "IDLE", "IDLE1.bmp")  # legacy path?
                    characters.append({"name": name, "has_idle_image": os.path.exists(idle_path)})
            self._send_json(
                handler,
                {"status": "success", "characters": characters, "current_character": self._current_character()},
            )
        except Exception as e:
            self.logger.error(f"list_characters: {e}")
            self._send_error(handler, str(e))

    def get_character_icon(self, handler):
        """Serve IDLE1.bmp: if character is current, under status_images_dir; else in settings/<char>/status."""
        try:
            q = parse_qs(urlparse(handler.path).query)
            character = (q.get("character", [None])[0] or "").strip()
            if not character:
                raise ValueError("character parameter is required")

            if character == self._current_character():
                idle = os.path.join(self.shared_data.status_images_dir, "IDLE", "IDLE1.bmp")
            else:
                idle = os.path.join(self.shared_data.settings_dir, character, "status", "IDLE", "IDLE1.bmp")

            if not os.path.exists(idle):
                raise FileNotFoundError(f"IDLE1.bmp for '{character}' not found")

            with open(idle, "rb") as f:
                data = f.read()
            handler.send_response(200)
            handler.send_header("Content-Type", "image/bmp")
            handler.end_headers()
            handler.wfile.write(data)
        except Exception as e:
            self.logger.error(f"get_character_icon: {e}")
            handler.send_response(404)
            handler.end_headers()

    def create_character(self, handler, data=None):
        """Create a new character by copying current character's images."""
        try:
            if data is None:
                data = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])).decode("utf-8"))
            elif not isinstance(data, dict):
                raise ValueError("Invalid JSON payload")
            name = (data.get("character_name") or "").strip()
            if not name:
                raise ValueError("character_name is required")

            new_dir = os.path.join(self.shared_data.settings_dir, name)
            if os.path.exists(new_dir):
                raise FileExistsError(f"Character '{name}' already exists")

            self._save_current_character_images(new_dir)
            self._send_json(handler, {"status": "success", "message": "Character created successfully"})
        except Exception as e:
            self.logger.error(f"create_character: {e}")
            self._send_error(handler, str(e))

    def switch_character(self, handler, data=None):
        """Switch character: persist current images, load selected images as active."""
        try:
            if data is None:
                data = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])).decode("utf-8"))
            elif not isinstance(data, dict):
                raise ValueError("Invalid JSON payload")
            target = (data.get("character_name") or "").strip()
            if not target:
                raise ValueError("character_name is required")

            current = self._current_character()
            if target == current:
                self._send_json(handler, {"status": "success", "message": "Character already selected"})
                return

            # Save current
            self._save_current_character_images(os.path.join(self.shared_data.settings_dir, current))

            # Load target
            src = os.path.join(self.shared_data.settings_dir, target)
            if not os.path.exists(src):
                raise FileNotFoundError(f"Character '{target}' does not exist")

            self._copy_character_images(src, self.shared_data.status_images_dir, self.shared_data.static_images_dir)

            # Update config
            self.shared_data.config["bjorn_name"] = target
            self.shared_data.config["current_character"] = target
            self.shared_data.save_config()
            self.shared_data.load_config()

            time.sleep(1)
            self.shared_data.load_images()

            self._send_json(handler, {"status": "success", "message": "Character switched successfully"})
        except Exception as e:
            self.logger.error(f"switch_character: {e}")
            self._send_error(handler, str(e))

    def delete_character(self, handler, data=None):
        """Delete a character; if it's the current one, switch back to BJORN first."""
        try:
            if data is None:
                data = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])).decode("utf-8"))
            elif not isinstance(data, dict):
                raise ValueError("Invalid JSON payload")
            name = (data.get("character_name") or "").strip()
            if not name:
                raise ValueError("character_name is required")
            if name == "BJORN":
                raise ValueError("Cannot delete the default 'BJORN' character")

            char_dir = os.path.join(self.shared_data.settings_dir, name)
            if not os.path.exists(char_dir):
                raise FileNotFoundError(f"Character '{name}' does not exist")

            if name == self._current_character():
                bjorn_dir = os.path.join(self.shared_data.settings_dir, "BJORN")
                if not os.path.exists(bjorn_dir):
                    raise FileNotFoundError("Default 'BJORN' character does not exist")

                self._copy_character_images(bjorn_dir, self.shared_data.status_images_dir, self.shared_data.static_images_dir)
                self.shared_data.config["bjorn_name"] = "BJORN"
                self.shared_data.config["current_character"] = "BJORN"
                self.shared_data.save_config()
                self.shared_data.load_config()
                self.shared_data.load_images()

            shutil.rmtree(char_dir)
            self._send_json(handler, {"status": "success", "message": "Character deleted successfully"})
        except Exception as e:
            self.logger.error(f"delete_character: {e}")
            self._send_error(handler, str(e))

    def _save_current_character_images(self, target_dir: str):
        """Save current active images to the character directory."""
        try:
            self._ensure_dir(target_dir)
            dst_status = os.path.join(target_dir, "status")
            if os.path.exists(dst_status):
                shutil.rmtree(dst_status)
            shutil.copytree(self.shared_data.status_images_dir, dst_status)

            dst_static = os.path.join(target_dir, "static")
            if os.path.exists(dst_static):
                shutil.rmtree(dst_static)
            shutil.copytree(self.shared_data.static_images_dir, dst_static)
        except Exception as e:
            self.logger.error(f"_save_current_character_images: {e}")

    def _copy_character_images(self, src_dir: str, dst_status_dir: str, dst_static_dir: str):
        """Activate a character: copy its stored images into the live folders."""
        try:
            src_status = os.path.join(src_dir, "status")
            if os.path.exists(src_status):
                if os.path.exists(dst_status_dir):
                    shutil.rmtree(dst_status_dir)
                shutil.copytree(src_status, dst_status_dir)

            src_static = os.path.join(src_dir, "static")
            if os.path.exists(src_static):
                if os.path.exists(dst_static_dir):
                    shutil.rmtree(dst_static_dir)
                shutil.copytree(src_static, dst_static_dir)
        except Exception as e:
            self.logger.error(f"_copy_character_images: {e}")


# =============================================================================
# Comments (web_utils/comment_utils.py merged)
# =============================================================================

    def get_sections(self, handler):
        """Get list of comment sections (statuses) from DB."""
        try:
            rows = self.shared_data.db.query("SELECT DISTINCT status FROM comments ORDER BY status;")
            sections = [r["status"] for r in rows]

            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            response = json.dumps({'status': 'success', 'sections': sections})
            handler.wfile.write(response.encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in get_sections: {e}")
            handler.send_response(500)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            error_response = json.dumps({'status': 'error', 'message': str(e)})
            handler.wfile.write(error_response.encode('utf-8'))

    def get_comments(self, handler):
        """Get comments for a specific section from DB."""
        try:
            query_components = parse_qs(urlparse(handler.path).query)
            section = query_components.get('section', [None])[0]
            if not section:
                raise ValueError('Section parameter is required')

            rows = self.shared_data.db.query(
                "SELECT text FROM comments WHERE status=? ORDER BY id;",
                (section,)
            )
            comments = [r["text"] for r in rows]

            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            response = json.dumps({'status': 'success', 'comments': comments})
            handler.wfile.write(response.encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in get_comments: {e}")
            handler.send_response(500)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            error_response = json.dumps({'status': 'error', 'message': str(e)})
            handler.wfile.write(error_response.encode('utf-8'))

    def save_comments(self, data):
        """Save comment list for a section to DB (replaces existing)."""
        try:
            section = data.get('section')
            comments = data.get('comments')
            lang = data.get('lang', 'fr')
            theme = data.get('theme', section or 'general')
            weight = int(data.get('weight', 1))

            if not section or comments is None:
                return {'status': 'error', 'message': 'Section and comments are required'}

            if not isinstance(comments, list):
                return {'status': 'error', 'message': 'Comments must be a list of strings'}

            # Replace section content
            with self.shared_data.db.transaction(immediate=True):
                self.shared_data.db.execute("DELETE FROM comments WHERE status=? AND lang=?", (section, lang))
                rows = []
                for txt in comments:
                    t = str(txt).strip()
                    if not t:
                        continue
                    rows.append((t, section, theme, lang, weight))
                if rows:
                    self.shared_data.db.insert_comments(rows)

            return {'status': 'success', 'message': 'Comments saved successfully'}
        except Exception as e:
            self.logger.error(f"Error in save_comments: {e}")
            return {'status': 'error', 'message': str(e)}

    def restore_default_comments(self, data=None):
        """Restore default comments from JSON file to DB."""
        try:
            inserted = self.shared_data.db.import_comments_from_json(
                self.shared_data.default_comments_file,
                lang=(data.get('lang') if isinstance(data, dict) else None) or 'fr',
                clear_existing=True
            )
            return {
                'status': 'success',
                'message': f'Comments restored ({inserted} entries).'
            }
        except Exception as e:
            self.logger.error(f"Error in restore_default_comments: {e}")
            self.logger.error(traceback.format_exc())
            return {'status': 'error', 'message': str(e)}

    def delete_comment_section(self, data):
        """Delete a comment section and its associated comments from DB."""
        try:
            section_name = data.get('section')
            lang = data.get('lang', 'fr')

            if not section_name:
                return {'status': 'error', 'message': "Section name is required."}

            if not re.match(r'^[\w\-\s]+$', section_name):
                return {'status': 'error', 'message': "Invalid section name."}

            count = self.shared_data.db.execute(
                "DELETE FROM comments WHERE status=? AND lang=?;",
                (section_name, lang)
            )
            if count == 0:
                return {'status': 'error', 'message': f"Section '{section_name}' not found for lang='{lang}'."}

            return {'status': 'success', 'message': 'Section deleted successfully.'}
        except Exception as e:
            self.logger.error(f"Error in delete_comment_section: {e}")
            self.logger.error(traceback.format_exc())
            return {'status': 'error', 'message': str(e)}


# =============================================================================
# Attacks (web_utils/attack_utils.py merged)
# =============================================================================


    def _write_json(self, handler, obj: dict, code: int = 200):
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps(obj).encode('utf-8'))

    def _send_error_response(self, handler, message: str, status_code: int = 500):
        handler.send_response(status_code)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        response = {'status': 'error', 'message': message}
        handler.wfile.write(json.dumps(response).encode('utf-8'))

    def _extract_action_meta_from_content(self, content: str) -> dict | None:
        """Extract action metadata (b_* variables) from Python content."""
        try:
            tree = ast.parse(content)
            meta = {}
            for node in tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    key = node.targets[0].id
                    if key.startswith("b_"):
                        val = ast.literal_eval(node.value) if isinstance(
                            node.value, (ast.Constant, ast.List, ast.Dict, ast.Tuple)
                        ) else None
                        meta[key] = val
            return meta if meta else None
        except Exception:
            return None

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

    # ---------- endpoints ----------

    # def get_attacks(self, handler):
    #     """List all attack cards from database."""
    #     try:
    #         cards = self.shared_data.db.list_action_cards()
    #         resp = {"attacks": [{"name": c["name"], "image": c["image"]} for c in cards]}
    #         handler.send_response(200)
    #         handler.send_header('Content-Type', 'application/json')
    #         handler.end_headers()
    #         handler.wfile.write(json.dumps(resp).encode('utf-8'))
    #     except Exception as e:
    #         self.logger.error(f"get_attacks error: {e}")
    #         self._send_error_response(handler, str(e))
                    
    def get_attacks(self, handler):
        """List all attack cards from DB (name + enabled)."""
        try:
            cards = self.shared_data.db.list_action_cards()  # maps b_enabled -> enabled
            attacks = []
            for c in cards:
                name = c.get("name") or c.get("b_class")
                if not name:
                    continue
                enabled = int(c.get("enabled", c.get("b_enabled", 0)) or 0)
                attacks.append({"name": name, "enabled": enabled})
            resp = {"attacks": attacks}
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps(resp).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"get_attacks error: {e}")
            self._send_error_response(handler, str(e))


    def set_action_enabled(self, handler, data=None):
        """Body: { action_name: str, enabled: 0|1 }"""
        try:
            if data is None:
                length = int(handler.headers.get('Content-Length', 0))
                body = handler.rfile.read(length) if length else b'{}'
                data = json.loads(body or b'{}')
            elif not isinstance(data, dict):
                raise ValueError("Invalid JSON payload")

            action_name = (data.get('action_name') or '').strip()
            enabled = 1 if int(data.get('enabled', 0)) else 0
            if not action_name:
                raise ValueError("action_name is required")

            # Update the correct column using existing DB API
            rowcount = self.shared_data.db.execute(
                "UPDATE actions SET b_enabled = ? WHERE b_class = ?;",
                (enabled, action_name)
            )
            if not rowcount:
                raise ValueError(f"Action '{action_name}' not found (b_class)")

            # Best-effort sync to actions_studio when present.
            try:
                self.shared_data.db.execute(
                    "UPDATE actions_studio SET b_enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE b_class = ?;",
                    (enabled, action_name)
                )
            except Exception as e:
                self.logger.debug(f"set_action_enabled studio sync skipped for {action_name}: {e}")

            out = {"status": "success", "action_name": action_name, "enabled": enabled}
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps(out).encode('utf-8'))

        except Exception as e:
            self.logger.error(f"set_action_enabled error: {e}")
            self._send_error_response(handler, str(e))



    def get_attack_content(self, handler):
        """Get source code content of an attack."""
        try:
            params = dict(parse_qs(handler.path.split('?')[-1]))
            attack_name = (params.get('name', [''])[0] or '').strip()
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
        """Import a new attack from uploaded .py file, parse b_* meta, upsert DB."""
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

            # Optional copy to defaults
            if handler.headers.get('Import-Default', 'false').lower() == 'true':
                os.makedirs(self.shared_data.default_actions_dir, exist_ok=True)
                shutil.copyfile(dst, os.path.join(self.shared_data.default_actions_dir, filename))

            self._write_json(handler, {"status": "success", "message": "Attack imported successfully."})
        except Exception as e:
            self.logger.error(f"Error importing attack: {e}")
            self._send_error_response(handler, str(e))

    def remove_attack(self, handler, data=None):
        """Remove an attack (file + DB row)."""
        try:
            if data is None:
                body = handler.rfile.read(int(handler.headers.get('Content-Length', 0)) or 0)
                data = json.loads(body or "{}")
            elif not isinstance(data, dict):
                raise ValueError("Invalid JSON payload")
            attack_name = (data.get("name") or "").strip()
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

    def save_attack(self, handler, data=None):
        """Save/update attack source code and refresh DB metadata if b_class changed."""
        try:
            if data is None:
                body = handler.rfile.read(int(handler.headers.get('Content-Length', 0)) or 0)
                data = json.loads(body or "{}")
            elif not isinstance(data, dict):
                raise ValueError("Invalid JSON payload")
            attack_name = (data.get('name') or '').strip()
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

    def restore_attack(self, handler, data=None):
        """Restore an attack from default_actions_dir and re-upsert metadata."""
        try:
            if data is None:
                body = handler.rfile.read(int(handler.headers.get('Content-Length', 0)) or 0)
                data = json.loads(body or "{}")
            elif not isinstance(data, dict):
                raise ValueError("Invalid JSON payload")
            attack_name = (data.get('name') or '').strip()
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
            if rel.startswith("../"):
                handler.send_error(400, "Invalid path")
                return

            image_path = os.path.join(self.shared_data.actions_icons_dir, rel)
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
    # ---------- WEB IMAGES & ACTION ICONS ----------
    def _list_images(self, directory: str, with_dims: bool=False):
        if not os.path.isdir(directory): return []
        items = []
        for fname in os.listdir(directory):
            p = os.path.join(directory, fname)
            if not os.path.isfile(p): continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ALLOWED_IMAGE_EXTS: continue
            if with_dims:
                try:
                    with Image.open(p) as img: w, h = img.size
                    items.append({'name': fname, 'width': w, 'height': h})
                except Exception:
                    items.append({'name': fname, 'width': None, 'height': None})
            else:
                items.append(fname)
        return items
    def _mime(self, path: str) -> str:
        p = path.lower()
        if p.endswith('.bmp'): return 'image/bmp'
        if p.endswith('.png'): return 'image/png'
        if p.endswith('.jpg') or p.endswith('.jpeg'): return 'image/jpeg'
        if p.endswith('.gif'): return 'image/gif'
        if p.endswith('.ico'): return 'image/x-icon'
        if p.endswith('.webp'): return 'image/webp'
        return 'application/octet-stream'
    
    def list_web_images_with_dimensions(self, h):
        try: self._send_json(h, {'status':'success','images': self._list_images(self.web_images_dir, with_dims=True)})
        except Exception as e: self.logger.error(e); self._err(h, str(e))

    def upload_web_image(self, h):

        try:
            ctype, pdict = _parse_header(h.headers.get('Content-Type'))
            if ctype != 'multipart/form-data': raise ValueError('Content-Type must be multipart/form-data')
            pdict['boundary']=bytes(pdict['boundary'],'utf-8'); pdict['CONTENT-LENGTH']=int(h.headers.get('Content-Length'))
            form = _MultipartForm(fp=BytesIO(h.rfile.read(pdict['CONTENT-LENGTH'])),
                                    headers=h.headers, environ={'REQUEST_METHOD':'POST'}, keep_blank_values=True)
            if 'web_image' not in form or not getattr(form['web_image'],'filename',''): raise ValueError('No web_image file provided')
            file_item = form['web_image']; filename = self._safe(file_item.filename)
            base, ext = os.path.splitext(filename); 
            if ext.lower() not in ALLOWED_IMAGE_EXTS: filename = base + '.png'
            data = file_item.file.read()
            if self.should_resize_images:
                with Image.open(BytesIO(data)) as im:
                    try: resample = Image.Resampling.LANCZOS
                    except AttributeError: resample = Image.LANCZOS
                    im = im.resize((self.resize_width, self.resize_height), resample)
                    out = BytesIO()
                    ext = os.path.splitext(filename)[1].lower()
                    fmt = {'.png':'PNG','.jpg':'JPEG','.jpeg':'JPEG','.gif':'GIF','.ico':'ICO','.bmp':'BMP','.webp':'WEBP'}.get(ext, 'PNG')
                    if fmt in ('JPEG','BMP'): im = im.convert('RGB')
                    im.save(out, fmt)
                    data = out.getvalue()
            with open(os.path.join(self.web_images_dir, filename), 'wb') as f:
                f.write(data)
            self._send_json(h, {'status':'success','message':'Web image uploaded','file':filename})
        except Exception as e:
            self.logger.error(e); self._err(h, str(e))

    def serve_web_image(self, h):
        try:
            url_path = unquote(urlparse(h.path).path); prefix='/web/images/'
            if not url_path.startswith(prefix): h.send_error(400,"Bad Request"); return
            rel = self._safe(url_path[len(prefix):]); target = os.path.join(self.web_images_dir, rel)
            if not os.path.isfile(target): h.send_error(404,"Not found"); return
            with open(target,'rb') as f: content = f.read()
            h.send_response(200); h.send_header('Content-Type', self._mime(target))
            h.send_header('Content-Length', str(len(content))); h.end_headers(); h.wfile.write(content)
        except Exception as e:
            self.logger.error(e); h.send_error(500,"Internal Server Error")

    def list_actions_icons_with_dimensions(self, h):
        try: self._send_json(h, {'status':'success','images': self._list_images(self.actions_icons_dir, with_dims=True)})
        except Exception as e: self.logger.error(e); self._err(h, str(e))

    def upload_actions_icon(self, h):

        try:
            ctype, pdict = _parse_header(h.headers.get('Content-Type'))
            if ctype != 'multipart/form-data': raise ValueError('Content-Type must be multipart/form-data')
            pdict['boundary']=bytes(pdict['boundary'],'utf-8'); pdict['CONTENT-LENGTH']=int(h.headers.get('Content-Length'))
            form = _MultipartForm(fp=BytesIO(h.rfile.read(pdict['CONTENT-LENGTH'])),
                                    headers=h.headers, environ={'REQUEST_METHOD':'POST'}, keep_blank_values=True)
            if 'icon_image' not in form or not getattr(form['icon_image'],'filename',''): raise ValueError('No icon_image file provided')
            file_item = form['icon_image']; filename = self._safe(file_item.filename)
            base, ext = os.path.splitext(filename); 
            if ext.lower() not in ALLOWED_IMAGE_EXTS: filename = base + '.png'
            data = file_item.file.read()
            if self.should_resize_images:
                with Image.open(BytesIO(data)) as im:
                    try: resample = Image.Resampling.LANCZOS
                    except AttributeError: resample = Image.LANCZOS
                    im = im.resize((self.resize_width, self.resize_height), resample)
                    out = BytesIO()
                    ext = os.path.splitext(filename)[1].lower()
                    fmt = {'.png':'PNG','.jpg':'JPEG','.jpeg':'JPEG','.gif':'GIF','.ico':'ICO','.bmp':'BMP','.webp':'WEBP'}.get(ext, 'PNG')
                    if fmt in ('JPEG','BMP'): im = im.convert('RGB')
                    im.save(out, fmt)
                    data = out.getvalue()
            with open(os.path.join(self.actions_icons_dir, filename), 'wb') as f:
                f.write(data)
            self._send_json(h, {'status':'success','message':'Action icon uploaded','file':filename})
        except Exception as e:
            self.logger.error(e); self._err(h, str(e))

    def serve_actions_icon(self, h):
        try:
            rel = h.path[len('/actions_icons/'):].lstrip('/')
            rel = os.path.normpath(rel).replace("\\","/")
            if rel.startswith("../"): h.send_error(400,"Invalid path"); return
            image_path = os.path.join(self.actions_icons_dir, rel)
            if not os.path.exists(image_path): h.send_error(404,"Image not found"); return
            with open(image_path,'rb') as f: content = f.read()
            h.send_response(200); h.send_header('Content-Type', self._mime(image_path))
            h.send_header('Content-Length', str(len(content))); h.end_headers(); h.wfile.write(content)
        except Exception as e:
            self.logger.error(e); h.send_error(500,"Internal Server Error")
