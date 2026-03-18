"""image_utils.py - Image upload, processing, and gallery management."""
from __future__ import annotations
import os, json, re, shutil, io, logging
from io import BytesIO
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse, parse_qs, unquote
from PIL import Image
from logger import Logger

logger = Logger(name="image_utils.py", level=logging.DEBUG)


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

class ImageUtils:
    """Utilities for image management (NO persona/character logic here)."""

    # Fixed sizes used by the frontend spec for action icons
    STATUS_W, STATUS_H = 28, 28

    def __init__(self, shared_data, character_utils=None):
        self.logger = logger
        self.shared_data = shared_data
        self.character_utils = character_utils  # optional DI for renumber/help

        # batch resize options for manual tools
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

    # ---------- helpers ----------
    def _to_bmp(self, raw: bytes, w: Optional[int]=None, h: Optional[int]=None) -> bytes:
        with Image.open(BytesIO(raw)) as im:
            if im.mode != 'RGB': im = im.convert('RGB')
            if w and h:
                try: res = Image.Resampling.LANCZOS
                except AttributeError: res = Image.LANCZOS
                im = im.resize((w, h), res)
            out = BytesIO(); im.save(out, format='BMP'); return out.getvalue()

    def _safe(self, name: str) -> str:
        return os.path.basename((name or '').strip().replace('\x00', ''))

    def _mime(self, path: str) -> str:
        p = path.lower()
        if p.endswith('.bmp'): return 'image/bmp'
        if p.endswith('.png'): return 'image/png'
        if p.endswith('.jpg') or p.endswith('.jpeg'): return 'image/jpeg'
        if p.endswith('.gif'): return 'image/gif'
        if p.endswith('.ico'): return 'image/x-icon'
        if p.endswith('.webp'): return 'image/webp'
        return 'application/octet-stream'

    def _send_json(self, h, payload: dict, status: int=200):
        h.send_response(status); h.send_header('Content-Type','application/json'); h.end_headers()
        h.wfile.write(json.dumps(payload).encode('utf-8'))

    def _err(self, h, msg: str, code: int=500): self._send_json(h, {'status':'error','message':msg}, code)

    def _ensure_action_dir(self, action: str) -> str:
        p = os.path.join(self.status_images_dir, action); os.makedirs(p, exist_ok=True); return p

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

    # ---------- ACTION (status folder) IMAGES (no characters here) ----------
    def get_actions(self, h):
        try:
            actions = []
            for e in os.scandir(self.status_images_dir):
                if e.is_dir():
                    name = e.name
                    actions.append({'name': name, 'has_status_icon': os.path.exists(os.path.join(e.path, f"{name}.bmp"))})
            self._send_json(h, {'status':'success','actions':actions})
        except Exception as e:
            self.logger.error(e); self._err(h, str(e))

    def get_action_images(self, h):
        try:
            q = parse_qs(urlparse(h.path).query); action = (q.get('action',[None])[0] or '').strip()
            if not action: raise ValueError('Action parameter is required')
            adir = os.path.join(self.status_images_dir, action)
            if not os.path.exists(adir): raise FileNotFoundError(f"Action '{action}' does not exist")
            images = []
            for fn in os.listdir(adir):
                if fn.lower().endswith('.bmp'):
                    p = os.path.join(adir, fn)
                    try:
                        with Image.open(p) as img: w, hh = img.size
                    except Exception: w = hh = None
                    images.append({'name': fn, 'width': w, 'height': hh})
            self._send_json(h, {'status':'success','images':images})
        except Exception as e:
            self.logger.error(e); self._err(h, str(e))

    def get_status_icon(self, h):
        try:
            q = parse_qs(urlparse(h.path).query); action = (q.get('action',[None])[0] or '').strip()
            if not action: raise ValueError('action is required')
            p = os.path.join(self.status_images_dir, action, f"{action}.bmp")
            if not os.path.exists(p): h.send_response(404); h.end_headers(); return
            with open(p, 'rb') as f: data = f.read()
            h.send_response(200); h.send_header('Content-Type','image/bmp'); h.end_headers(); h.wfile.write(data)
        except Exception as e:
            self.logger.error(e); h.send_response(404); h.end_headers()

    def serve_status_image(self, h):
        try:
            url_path = unquote(urlparse(h.path).path); prefix = '/images/status/'
            if not url_path.startswith(prefix): h.send_error(400, "Bad Request"); return
            rel = url_path[len(prefix):]
            base = Path(self.status_images_dir).resolve()
            target = (base/rel).resolve()
            if not str(target).startswith(str(base)): h.send_error(403,"Forbidden"); return
            if not target.exists() or not target.is_file(): h.send_error(404,"Image not found"); return
            with open(target,'rb') as f: content = f.read()
            h.send_response(200); h.send_header('Content-Type', self._mime(str(target)))
            h.send_header('Content-Length', str(len(content))); h.end_headers(); h.wfile.write(content)
        except Exception as e:
            self.logger.error(e); h.send_error(500, "Internal Server Error")

    def upload_status_image(self, h):
        """Add/replace <action>/<action>.bmp (always 28x28 BMP)."""

        try:
            ctype, pdict = _parse_header(h.headers.get('Content-Type'))
            if ctype != 'multipart/form-data': raise ValueError('Content-Type must be multipart/form-data')
            pdict['boundary']=bytes(pdict['boundary'],'utf-8'); pdict['CONTENT-LENGTH']=int(h.headers.get('Content-Length'))
            form = _MultipartForm(fp=BytesIO(h.rfile.read(pdict['CONTENT-LENGTH'])),
                                    headers=h.headers, environ={'REQUEST_METHOD':'POST'}, keep_blank_values=True)
            for key in ('type','action_name','status_image'):
                if key not in form: raise ValueError(f'Missing field: {key}')
            if (form.getvalue('type') or '').strip() != 'action': raise ValueError("type must be 'action'")
            action = (form.getvalue('action_name') or '').strip()
            if not action: raise ValueError("action_name is required")
            file_item = form['status_image']
            if not getattr(file_item,'filename',''): raise ValueError('No file')

            adir = self._ensure_action_dir(action)
            raw = file_item.file.read()
            bmp = self._to_bmp(raw, self.STATUS_W, self.STATUS_H)
            with open(os.path.join(adir, f"{action}.bmp"), 'wb') as f: f.write(bmp)
            self._send_json(h, {'status':'success','message':'Status image added/updated','path':f"{action}/{action}.bmp"})
        except Exception as e:
            self.logger.error(e); self._err(h, str(e))

    # ---------- STATIC IMAGES ----------
    def list_static_images_with_dimensions(self, h):
        try:
            self._send_json(h, {'status':'success','images': self._list_images(self.static_images_dir, with_dims=True)})
        except Exception as e: self.logger.error(e); self._err(h, str(e))

    def upload_static_image(self, h):

        try:
            ctype, pdict = _parse_header(h.headers.get('Content-Type'))
            if ctype != 'multipart/form-data': raise ValueError('Content-Type must be multipart/form-data')
            pdict['boundary']=bytes(pdict['boundary'],'utf-8'); pdict['CONTENT-LENGTH']=int(h.headers.get('Content-Length'))
            form = _MultipartForm(fp=BytesIO(h.rfile.read(pdict['CONTENT-LENGTH'])),
                                    headers=h.headers, environ={'REQUEST_METHOD':'POST'}, keep_blank_values=True)
            if 'static_image' not in form or not getattr(form['static_image'],'filename',''): raise ValueError('No static_image provided')
            filename = self._safe(form['static_image'].filename); base, _ = os.path.splitext(filename); filename = base + '.bmp'
            raw = form['static_image'].file.read()
            if self.should_resize_images:
                out = self._to_bmp(raw, self.resize_width, self.resize_height)
            else:
                with Image.open(BytesIO(raw)) as im: w, h = im.size
                out = self._to_bmp(raw, w, h)
            with open(os.path.join(self.static_images_dir, filename),'wb') as f: f.write(out)
            self._send_json(h, {'status':'success','message':'Static image uploaded successfully'})
        except Exception as e:
            self.logger.error(e); self._err(h, str(e))

    def serve_static_image(self, h):
        try:
            path = unquote(urlparse(h.path).path)
            name = self._safe(os.path.basename(path))
            full = os.path.join(self.static_images_dir, name)
            if not os.path.exists(full): raise FileNotFoundError(name)
            with open(full,'rb') as f: data = f.read()
            h.send_response(200); h.send_header('Content-Type', self._mime(full)); h.end_headers(); h.wfile.write(data)
        except Exception as e:
            self.logger.error(e); h.send_response(404); h.end_headers()

    # ---------- WEB IMAGES & ACTION ICONS ----------
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
            with open(os.path.join(self.web_images_dir, filename), 'wb') as f: f.write(data)
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
            with open(os.path.join(self.actions_icons_dir, filename),'wb') as f: f.write(data)
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

    def replace_image(self, h):
        """Replace image. For type='action': status icon here; character images delegated to CharacterUtils."""

        try:
            ctype, pdict = _parse_header(h.headers.get('Content-Type'))
            if ctype != 'multipart/form-data': raise ValueError('Content-Type must be multipart/form-data')
            pdict['boundary']=bytes(pdict['boundary'],'utf-8'); pdict['CONTENT-LENGTH']=int(h.headers.get('Content-Length'))
            form = _MultipartForm(fp=BytesIO(h.rfile.read(pdict['CONTENT-LENGTH'])),
                                    headers=h.headers, environ={'REQUEST_METHOD':'POST'}, keep_blank_values=True)
            tp = form.getvalue('type'); image_name = self._safe(form.getvalue('image_name') or '')
            file_item = form['new_image'] if 'new_image' in form else None
            if not tp or not image_name or not file_item or not getattr(file_item,'filename',''):
                raise ValueError('type, image_name and new_image are required')

            if tp == 'action':
                action = self._safe(form.getvalue('action') or '')
                if not action: raise ValueError("action is required for type=action")
                # status icon = <action>.bmp -> handle here
                if image_name.lower() == f"{action.lower()}.bmp":
                    base = os.path.join(self.status_images_dir, action)
                    if not os.path.exists(os.path.join(base, image_name)):
                        raise FileNotFoundError(f"{image_name} not found")
                    raw = file_item.file.read()
                    out = self._to_bmp(raw, self.STATUS_W, self.STATUS_H)
                    with open(os.path.join(base, image_name),'wb') as f: f.write(out)
                else:
                    # delegate character image replacement
                    if not self.character_utils:
                        raise RuntimeError("CharacterUtils not wired into ImageUtils")
                    return self.character_utils.replace_character_image(h, form, action, image_name)
            elif tp == 'static':
                path = os.path.join(self.static_images_dir, image_name)
                if not os.path.exists(path): raise FileNotFoundError(image_name)
                raw = file_item.file.read()
                with Image.open(path) as im: w, hh = im.size
                out = self._to_bmp(raw, w, hh)
                with open(path, 'wb') as f: f.write(out)
            elif tp == 'web':
                path = os.path.join(self.web_images_dir, image_name)
                if not os.path.exists(path): raise FileNotFoundError(image_name)
                with open(path,'wb') as f: f.write(file_item.file.read())
            elif tp == 'icons':
                path = os.path.join(self.actions_icons_dir, image_name)
                if not os.path.exists(path): raise FileNotFoundError(image_name)
                with open(path,'wb') as f: f.write(file_item.file.read())
            else:
                raise ValueError("type must be 'action'|'static'|'web'|'icons'")
            self._send_json(h, {'status':'success','message':'Image replaced successfully'})
        except Exception as e:
            self.logger.error(e); self._err(h, str(e))

    def resize_images(self, h):
        """Batch-resize statics; when 'action' is requested, delegate to CharacterUtils."""
        try:
            data = json.loads(h.rfile.read(int(h.headers['Content-Length'])).decode('utf-8'))
            tp = data.get('type'); action = data.get('action'); names = data.get('image_names', [])
            w = int(data.get('width', 100)); hh = int(data.get('height', 100))
            if tp == 'static':
                base = self.static_images_dir
                for n in names:
                    p = os.path.join(base, self._safe(n))
                    if not os.path.exists(p): continue
                    with open(p,'rb') as f: raw=f.read()
                    with Image.open(BytesIO(raw)) as im: _w,_h = im.size
                    out = self._to_bmp(raw, w or _w, hh or _h)
                    with open(p,'wb') as f: f.write(out)
                self._send_json(h, {'status':'success'})
            elif tp == 'action':
                if not self.character_utils:
                    raise RuntimeError("CharacterUtils not wired into ImageUtils")
                return self.character_utils.resize_action_images(h, data)
            else:
                raise ValueError("Type must be 'static' or 'action'")
        except Exception as e:
            self.logger.error(e); self._err(h, str(e))

    # ---------- misc ----------
    def restore_default_images(self, h):
        try:
            images_dir = getattr(self.shared_data, "images_dir", None)
            default_images_dir = getattr(self.shared_data, "default_images_dir", None)
            if not default_images_dir or not os.path.exists(default_images_dir):
                raise FileNotFoundError(f"Default images directory not found: {default_images_dir}")
            if images_dir and os.path.exists(images_dir): shutil.rmtree(images_dir)
            shutil.copytree(default_images_dir, images_dir)
            self._send_json(h, {'status':'success','message':'Images restored successfully'})
        except Exception as e:
            self.logger.error(e); self._err(h, str(e))

    def set_resize_option(self, h):
        try:
            data = json.loads(h.rfile.read(int(h.headers['Content-Length'])).decode('utf-8'))
            self.should_resize_images = bool(data.get('resize', False))
            self.resize_width  = int(data.get('width', 100))
            self.resize_height = int(data.get('height', 100))
            self._send_json(h, {'status':'success','message':'Resize options updated'})
        except Exception as e:
            self.logger.error(e); self._err(h, str(e))

    def serve_bjorn_status_image(self, h):
        try:
            out = io.BytesIO()
            self.shared_data.bjorn_status_image.save(out, format="PNG")
            data = out.getvalue()
            h.send_response(200); h.send_header('Content-Type','image/png'); h.send_header('Cache-Control','no-cache')
            h.end_headers(); h.wfile.write(data)
        except BrokenPipeError:
            pass
        except Exception as e:
            self.logger.error(e)

    def serve_image(self, h):
        path = os.path.join(self.shared_data.web_dir, 'screen.png')
        try:
            with open(path,'rb') as f:
                h.send_response(200); h.send_header('Content-type','image/png')
                h.send_header('Cache-Control','max-age=0, must-revalidate')
                h.end_headers(); h.wfile.write(f.read())
        except FileNotFoundError:
            h.send_response(404); h.end_headers()
        except BrokenPipeError: pass
        except Exception as e: self.logger.error(e)
