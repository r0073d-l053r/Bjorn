"""character_utils.py - Character switching, creation, and image management."""
from __future__ import annotations
import os
import re
import json
import shutil
import time
import logging
from pathlib import Path
from io import BytesIO
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs

import io
from PIL import Image


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

from logger import Logger

logger = Logger(name="character_utils.py", level=logging.DEBUG)


class CharacterUtils:
    """Utilities for character/persona management."""

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.logger = logger

    # --------- helpers ---------

    def _send_error_response(self, handler, message: str, status_code: int = 500):
        handler.send_response(status_code)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps({'status': 'error', 'message': message}).encode('utf-8'))

    def _to_bmp_bytes(self, raw: bytes, width: int | None = None, height: int | None = None) -> bytes:
        """Convert any image bytes to BMP (optionally resize)."""
        with Image.open(BytesIO(raw)) as im:
            if im.mode != 'RGB':
                im = im.convert('RGB')
            if width and height:
                try:
                    resample = Image.Resampling.LANCZOS
                except AttributeError:
                    resample = Image.LANCZOS
                im = im.resize((width, height), resample)
            out = BytesIO()
            im.save(out, format='BMP')
            return out.getvalue()

    def get_existing_character_numbers(self, action_dir: str | Path, action_name: str) -> set[int]:
        """Return the set of numbers already used for character images (e.g. <action>1.bmp, <action>2.bmp)."""
        d = Path(action_dir)
        if not d.exists():
            return set()
        nums: set[int] = set()
        pat = re.compile(rf"^{re.escape(action_name)}(\d+)\.bmp$", re.IGNORECASE)
        for p in d.glob("*.bmp"):
            m = pat.match(p.name)
            if m:
                try:
                    nums.add(int(m.group(1)))
                except ValueError:
                    pass
        return nums

    # --------- endpoints ---------

    def get_current_character(self):
        """Read current character from config (DB)."""
        try:
            return self.shared_data.config.get('current_character', 'BJORN') or 'BJORN'
        except Exception:
            return 'BJORN'

    def serve_bjorn_say(self, handler):
        try:
            bjorn_says_data = {"text": self.shared_data.bjorn_says}
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps(bjorn_says_data).encode('utf-8'))
        except Exception as e:
            handler.send_response(500)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

    def serve_bjorn_character(self, handler):
        try:
            img_byte_arr = io.BytesIO()
            self.shared_data.bjorn_character.save(img_byte_arr, format='PNG')
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

    def list_characters(self, handler):
        """List all available characters with metadata."""
        try:
            characters_dir = self.shared_data.settings_dir
            characters = []

            for entry in os.scandir(characters_dir):
                if entry.is_dir():
                    character_name = entry.name
                    idle_image_path = os.path.join(entry.path, 'IDLE', 'IDLE1.bmp')  # legacy path?
                    has_idle_image = os.path.exists(idle_image_path)
                    characters.append({'name': character_name, 'has_idle_image': has_idle_image})

            current_character = self.get_current_character()

            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            resp = {'status': 'success', 'characters': characters, 'current_character': current_character}
            handler.wfile.write(json.dumps(resp).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in list_characters: {e}")
            self._send_error_response(handler, str(e))

    def get_character_icon(self, handler):
        """Serve character icon (IDLE1.bmp)."""
        try:
            query_components = parse_qs(urlparse(handler.path).query)
            character = (query_components.get('character', [None])[0] or '').strip()
            if not character:
                raise ValueError('Character parameter is required')

            current_character = self.get_current_character()
            if character == current_character:
                # Active character images live in status_images_dir/IDLE/IDLE1.bmp
                idle_image_path = os.path.join(self.shared_data.status_images_dir, 'IDLE', 'IDLE1.bmp')
            else:
                idle_image_path = os.path.join(self.shared_data.settings_dir, character, 'status', 'IDLE', 'IDLE1.bmp')

            if not os.path.exists(idle_image_path):
                raise FileNotFoundError(f"IDLE1.bmp for character '{character}' not found")

            with open(idle_image_path, 'rb') as f:
                image_data = f.read()

            handler.send_response(200)
            handler.send_header('Content-Type', 'image/bmp')
            handler.end_headers()
            handler.wfile.write(image_data)
        except Exception as e:
            self.logger.error(f"Error in get_character_icon: {e}")
            handler.send_error(404)

    def create_character(self, handler):
        """Create a new character by copying current character's images."""
        try:
            content_length = int(handler.headers['Content-Length'])
            post_data = handler.rfile.read(content_length).decode('utf-8')
            data = json.loads(post_data)
            new_character_name = (data.get('character_name') or '').strip()

            if not new_character_name:
                raise ValueError('Character name is required')

            new_character_dir = os.path.join(self.shared_data.settings_dir, new_character_name)
            if os.path.exists(new_character_dir):
                raise FileExistsError(f"Character '{new_character_name}' already exists")

            self.save_current_character_images(new_character_dir)

            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({'status': 'success', 'message': 'Character created successfully'}).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in create_character: {e}")
            self._send_error_response(handler, str(e))

    def switch_character(self, handler):
        """Switch to a different character, saving current modifications first."""
        try:
            content_length = int(handler.headers['Content-Length'])
            post_data = handler.rfile.read(content_length).decode('utf-8')
            data = json.loads(post_data)
            selected_character_name = (data.get('character_name') or '').strip()

            if not selected_character_name:
                raise ValueError('Character name is required')

            current_character = self.get_current_character()
            if selected_character_name == current_character:
                handler.send_response(200)
                handler.send_header('Content-Type', 'application/json')
                handler.end_headers()
                handler.wfile.write(json.dumps({'status': 'success', 'message': 'Character already selected'}).encode('utf-8'))
                return

            # Save current character's images
            current_character_dir = os.path.join(self.shared_data.settings_dir, current_character)
            self.save_current_character_images(current_character_dir)

            # Check new character exists
            selected_character_dir = os.path.join(self.shared_data.settings_dir, selected_character_name)
            if not os.path.exists(selected_character_dir):
                raise FileNotFoundError(f"Character '{selected_character_name}' does not exist")

            # Activate
            self.copy_character_images(
                selected_character_dir,
                self.shared_data.status_images_dir,
                self.shared_data.static_images_dir
            )

            # Update config
            self.shared_data.config['bjorn_name'] = selected_character_name
            self.shared_data.config['current_character'] = selected_character_name
            self.shared_data.save_config()
            self.shared_data.load_config()

            time.sleep(1)
            self.shared_data.load_images()

            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({'status': 'success', 'message': 'Character switched successfully'}).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in switch_character: {e}")
            self._send_error_response(handler, str(e))

    def delete_character(self, handler):
        """Delete a character, handling current character case."""
        try:
            content_length = int(handler.headers['Content-Length'])
            post_data = handler.rfile.read(content_length).decode('utf-8')
            data = json.loads(post_data)
            character_name = (data.get('character_name') or '').strip()

            if not character_name:
                raise ValueError('Character name is required')

            if character_name == 'BJORN':
                raise ValueError("Cannot delete the default 'BJORN' character")

            character_dir = os.path.join(self.shared_data.settings_dir, character_name)
            if not os.path.exists(character_dir):
                raise FileNotFoundError(f"Character '{character_name}' does not exist")

            current_character = self.get_current_character()
            if character_name == current_character:
                bjorn_dir = os.path.join(self.shared_data.settings_dir, 'BJORN')
                if not os.path.exists(bjorn_dir):
                    raise FileNotFoundError("Default 'BJORN' character does not exist")

                self.copy_character_images(
                    bjorn_dir,
                    self.shared_data.status_images_dir,
                    self.shared_data.static_images_dir
                )

                self.shared_data.config['bjorn_name'] = 'BJORN'
                self.shared_data.config['current_character'] = 'BJORN'
                self.shared_data.save_config()
                self.shared_data.load_config()
                self.shared_data.load_images()

            shutil.rmtree(character_dir)

            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({'status': 'success', 'message': 'Character deleted successfully'}).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in delete_character: {e}")
            self._send_error_response(handler, str(e))

    def save_current_character_images(self, character_dir):
        """Save current character's status and static images."""
        try:
            if not os.path.exists(character_dir):
                os.makedirs(character_dir)

            dest_status_dir = os.path.join(character_dir, 'status')
            if os.path.exists(dest_status_dir):
                shutil.rmtree(dest_status_dir)
            shutil.copytree(self.shared_data.status_images_dir, dest_status_dir)

            dest_static_dir = os.path.join(character_dir, 'static')
            if os.path.exists(dest_static_dir):
                shutil.rmtree(dest_static_dir)
            shutil.copytree(self.shared_data.static_images_dir, dest_static_dir)
        except Exception as e:
            self.logger.error(f"Error in save_current_character_images: {e}")

    def copy_character_images(self, source_dir, dest_status_dir, dest_static_dir):
        """Copy character images from source to destination directories."""
        try:
            source_status_dir = os.path.join(source_dir, 'status')
            if os.path.exists(source_status_dir):
                if os.path.exists(dest_status_dir):
                    shutil.rmtree(dest_status_dir)
                shutil.copytree(source_status_dir, dest_status_dir)

            source_static_dir = os.path.join(source_dir, 'static')
            if os.path.exists(source_static_dir):
                if os.path.exists(dest_static_dir):
                    shutil.rmtree(dest_static_dir)
                shutil.copytree(source_static_dir, dest_static_dir)
        except Exception as e:
            self.logger.error(f"Error in copy_character_images: {e}")

    def upload_character_images(self, handler):
        """Add character images for an existing action (always BMP, auto-numbered)."""
        try:
            ctype, pdict = _parse_header(handler.headers.get('Content-Type'))
            if ctype != 'multipart/form-data':
                raise ValueError('Content-Type must be multipart/form-data')

            pdict['boundary'] = bytes(pdict['boundary'], "utf-8")
            pdict['CONTENT-LENGTH'] = int(handler.headers.get('Content-Length'))

            form = _MultipartForm(
                fp=io.BytesIO(handler.rfile.read(pdict['CONTENT-LENGTH'])),
                headers=handler.headers,
                environ={'REQUEST_METHOD': 'POST'},
                keep_blank_values=True
            )

            if 'action_name' not in form:
                raise ValueError("Action name is required")

            action_name = (form.getvalue('action_name') or '').strip()
            if not action_name:
                raise ValueError("Action name is required")

            if 'character_images' not in form:
                raise ValueError('No image file provided')

            action_dir = os.path.join(self.shared_data.status_images_dir, action_name)
            if not os.path.exists(action_dir):
                raise FileNotFoundError(f"Action '{action_name}' does not exist")

            existing_numbers = self.get_existing_character_numbers(action_dir, action_name)
            next_number = max(existing_numbers, default=0) + 1

            file_items = form['character_images']
            if not isinstance(file_items, list):
                file_items = [file_items]

            for file_item in file_items:
                if not getattr(file_item, 'filename', ''):
                    continue
                raw = file_item.file.read()
                bmp = self._to_bmp_bytes(raw)
                out_path = os.path.join(action_dir, f"{action_name}{next_number}.bmp")
                with open(out_path, 'wb') as f:
                    f.write(bmp)
                next_number += 1

            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({'status': 'success', 'message': 'Character images added successfully'}).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in upload_character_images: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            self._send_error_response(handler, str(e))


    def reload_fonts(self, handler):
        """Reload fonts via load_fonts."""
        try:
            self.shared_data.load_fonts()
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({'status': 'success', 'message': 'Fonts loaded successfully.'}).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in load_fonts: {e}")
            handler.send_response(500)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode('utf-8'))

    def reload_images(self, handler):
        """Reload images via load_images."""
        try:
            self.shared_data.load_images()
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({'status': 'success', 'message': 'Images reloaded successfully.'}).encode('utf-8'))
        except Exception as e:
            self.logger.error(f"Error in reload_images: {e}")
            handler.send_response(500)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode('utf-8'))