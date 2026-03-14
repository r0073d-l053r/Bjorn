# web_utils/file_utils.py
"""
File management utilities.
Handles file operations, uploads, downloads, directory management.
"""
from __future__ import annotations
import os
import json
import shutil
from pathlib import Path
from io import BytesIO
from typing import Any, Dict, Optional
from urllib.parse import unquote

import logging
from logger import Logger
logger = Logger(name="file_utils.py", level=logging.DEBUG)
class FileUtils:
    """Utilities for file and directory management."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

    def _validate_path(self, path, base_dir=None):
        """Validate that a path is within the allowed base directory.
        Uses realpath to resolve symlinks, preventing traversal attacks.
        Returns the resolved absolute path, or raises ValueError."""
        if base_dir is None:
            base_dir = self.shared_data.current_dir
        resolved_base = os.path.realpath(base_dir)
        resolved_path = os.path.realpath(path)
        if not resolved_path.startswith(resolved_base + os.sep) and resolved_path != resolved_base:
            raise ValueError(f"Access denied: path is outside the allowed directory")
        return resolved_path

    def list_files(self, directory, depth=0, max_depth=3):
        """List files and directories recursively."""
        files = []
        if depth > max_depth:
            return files
        for entry in os.scandir(directory):
            if entry.is_dir():
                files.append({
                    "name": entry.name,
                    "is_directory": True,
                    "children": self.list_files(entry.path, depth+1, max_depth)
                })
            else:
                try:
                    fsize = entry.stat().st_size
                except OSError:
                    fsize = 0
                files.append({
                    "name": entry.name,
                    "is_directory": False,
                    "path": entry.path,
                    "size": fsize
                })
        return files

    def list_files_endpoint(self, handler):
        """HTTP endpoint to list files."""
        try:
            from http import HTTPStatus
            files = self.list_files(self.shared_data.bjorn_user_dir)
            payload = json.dumps(files).encode("utf-8")

            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()

            try:
                handler.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                return
        except Exception as e:
            error_payload = json.dumps({"status": "error", "message": str(e)}).encode("utf-8")
            handler.send_response(500)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(error_payload)))
            handler.end_headers()
            try:
                handler.wfile.write(error_payload)
            except (BrokenPipeError, ConnectionResetError):
                return

    def loot_directories(self, handler):
        """List all loot directories and their contents recursively."""
        try:
            def scan_dir(directory):
                items = []
                for entry in os.scandir(directory):
                    item = {
                        "name": entry.name,
                        "path": entry.path.replace(self.shared_data.data_stolen_dir + '/', '')
                    }
                    
                    if entry.is_dir():
                        item["type"] = "directory"
                        item["children"] = scan_dir(entry.path)
                        item["subdirs"] = len([c for c in item["children"] if c["type"] == "directory"])
                        item["total_files"] = sum(1 for c in item["children"] if c["type"] == "file")
                        for child in item["children"]:
                            if child["type"] == "directory":
                                item["total_files"] += child.get("total_files", 0)
                    else:
                        item["type"] = "file"
                        item["size"] = entry.stat().st_size
                    items.append(item)
                return items

            root_contents = scan_dir(self.shared_data.data_stolen_dir)
            response = {"status": "success", "data": root_contents}
            
            handler.send_response(200)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps(response).encode('utf-8'))

        except Exception as e:
            self.logger.error(f"Error listing directories: {e}")
            handler.send_response(500)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({
                "status": "error",
                "message": str(e)
            }).encode('utf-8'))

    def loot_download(self, handler):
        """Handle loot file download requests."""
        try:
            query = handler.path.split('?')[1]
            file_path = unquote(query.split('=')[1])
            full_path = os.path.join(self.shared_data.data_stolen_dir, file_path)
            self._validate_path(full_path, self.shared_data.data_stolen_dir)

            if not os.path.isfile(full_path):
                raise FileNotFoundError(f"File not found: {file_path}")
            
            file_name = os.path.basename(file_path)
            file_size = os.path.getsize(full_path)
            
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/octet-stream')
            handler.send_header('Content-Disposition', f'attachment; filename="{file_name}"')
            handler.send_header('Content-Length', file_size)
            handler.end_headers()
            
            with open(full_path, 'rb') as f:
                shutil.copyfileobj(f, handler.wfile)
                
        except Exception as e:
            self.logger.error(f"Error downloading file: {e}")
            handler.send_response(500)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({
                "status": "error", 
                "message": str(e)
            }).encode('utf-8'))

    def download_file(self, handler):
        """Download a file from current directory."""
        try:
            query = unquote(handler.path.split('?path=')[1])
            file_path = os.path.join(self.shared_data.current_dir, query)
            self._validate_path(file_path)
            if os.path.isfile(file_path):
                handler.send_response(200)
                handler.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(file_path)}"')
                handler.end_headers()
                with open(file_path, 'rb') as file:
                    shutil.copyfileobj(file, handler.wfile)
            else:
                handler.send_response(404)
                handler.end_headers()
        except Exception as e:
            handler.send_response(500)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

    def create_folder(self, data):
        """Create a new folder."""
        try:
            folder_path = os.path.join(self.shared_data.current_dir, data['folder_path'])
            self._validate_path(folder_path)
            os.makedirs(folder_path, exist_ok=True)
            return {'status': 'success', 'message': 'Folder created successfully'}
        except ValueError as e:
            return {'status': 'error', 'message': str(e)}
        except Exception as e:
            self.logger.error(f"Error creating folder: {e}")
            return {'status': 'error', 'message': str(e)}

    def handle_file_upload(self, handler):
        """Handle file upload with directory structure preservation."""
        try:
            import re
            content_type = handler.headers['Content-Type']
            boundary = content_type.split('=')[1].encode()
            content_length = int(handler.headers['Content-Length'])
            body = handler.rfile.read(content_length)
            parts = body.split(b'--' + boundary)

            current_path = []
            for part in parts:
                if b'Content-Disposition' in part and b'name="currentPath"' in part:
                    headers, data = part.split(b'\r\n\r\n', 1)
                    current_path = json.loads(data.decode().strip())
                    break

            target_dir = os.path.join(self.shared_data.current_dir, *current_path)
            os.makedirs(target_dir, exist_ok=True)

            uploaded_files = []
            for part in parts:
                if b'Content-Disposition' in part and b'filename=' in part:
                    try:
                        headers, file_data = part.split(b'\r\n\r\n', 1)
                        headers = headers.decode()
                        match = re.search(r'filename="(.+?)"', headers)
                        if match:
                            relative_path = match.group(1)
                            relative_path = os.path.normpath(relative_path)
                            full_path = os.path.join(target_dir, relative_path)
                            parent_dir = os.path.dirname(full_path)
                            
                            if not os.path.abspath(full_path).startswith(os.path.abspath(self.shared_data.current_dir)):
                                raise PermissionError(f"Access denied: {relative_path} is outside allowed directory")
                            
                            if parent_dir:
                                os.makedirs(parent_dir, exist_ok=True)

                            with open(full_path, 'wb') as f:
                                f.write(file_data.strip(b'\r\n--'))

                            uploaded_files.append(full_path)
                            self.logger.info(f"File uploaded: {full_path}")
                    except Exception as e:
                        self.logger.error(f"Error processing file: {str(e)}")
                        continue

            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({
                "status": "success",
                "message": f"Files uploaded successfully to {target_dir}",
                "files": uploaded_files
            }).encode('utf-8'))

        except Exception as e:
            self.logger.error(f"Upload error: {str(e)}")
            handler.send_response(500)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({
                "status": "error",
                "message": str(e)
            }).encode('utf-8'))

    def delete_file(self, data):
        """Delete file or directory."""
        try:
            file_path = data.get('file_path')
            if not file_path:
                return {"status": "error", "message": "No file path provided"}

            abs_file_path = self._validate_path(file_path)
            self.logger.info(f"Deleting: {abs_file_path}")

            if not os.path.exists(abs_file_path):
                return {"status": "error", "message": f"Path not found: {file_path}"}

            if os.path.isdir(abs_file_path):
                shutil.rmtree(abs_file_path)
            else:
                os.remove(abs_file_path)

            if os.path.exists(abs_file_path):
                return {"status": "error", "message": f"Failed to delete {abs_file_path} - file still exists"}

            return {
                "status": "success",
                "message": f"Successfully deleted {'directory' if os.path.isdir(abs_file_path) else 'file'}: {file_path}"
            }
        except Exception as e:
            self.logger.error(f"Error deleting file: {str(e)}")
            return {"status": "error", "message": str(e)}

    def rename_file(self, data):
        """Rename file or directory."""
        try:
            old_path = os.path.join(self.shared_data.current_dir, data['old_path'])
            new_path = os.path.join(self.shared_data.current_dir, data['new_path'])
            self._validate_path(old_path)
            self._validate_path(new_path)

            os.rename(old_path, new_path)
            return {
                "status": "success",
                "message": f"Successfully renamed {old_path} to {new_path}"
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            self.logger.error(f"Error renaming file: {str(e)}")
            return {"status": "error", "message": str(e)}

    def duplicate_file(self, data):
        """Duplicate file or directory."""
        try:
            source_path = os.path.join(self.shared_data.current_dir, data['source_path'])
            target_path = os.path.join(self.shared_data.current_dir, data['target_path'])
            self._validate_path(source_path)
            self._validate_path(target_path)

            if os.path.isdir(source_path):
                shutil.copytree(source_path, target_path)
            else:
                shutil.copy2(source_path, target_path)

            return {
                "status": "success",
                "message": f"Successfully duplicated {source_path} to {target_path}"
            }
        except Exception as e:
            self.logger.error(f"Error duplicating file: {str(e)}")
            return {"status": "error", "message": str(e)}

    def move_file(self, data):
        """Move file or directory."""
        try:
            source_path = os.path.join(self.shared_data.current_dir, data['source_path'])
            target_path = os.path.join(self.shared_data.current_dir, data['target_path'])
            self._validate_path(source_path)
            self._validate_path(target_path)

            target_dir = os.path.dirname(target_path)
            if not os.path.exists(target_dir):
                os.makedirs(target_dir, exist_ok=True)

            if os.path.exists(target_path):
                base, ext = os.path.splitext(target_path)
                counter = 1
                while os.path.exists(f"{base} ({counter}){ext}"):
                    counter += 1
                target_path = f"{base} ({counter}){ext}"

            shutil.move(source_path, target_path)
            return {"status": "success", "message": "Item moved successfully"}
        except Exception as e:
            self.logger.error(f"Error moving file: {str(e)}")
            return {"status": "error", "message": str(e)}

    def list_directories(self, handler):
        """List directory structure."""
        try:
            def get_directory_structure(path):
                items = []
                for entry in os.scandir(path):
                    if entry.is_dir():
                        items.append({
                            "name": entry.name,
                            "path": os.path.relpath(entry.path, self.shared_data.current_dir),
                            "is_directory": True,
                            "children": get_directory_structure(entry.path)
                        })
                return items

            directory_structure = get_directory_structure(self.shared_data.current_dir)
            
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps(directory_structure).encode())
        except Exception as e:
            handler.send_response(500)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({
                "status": "error",
                "message": str(e)
            }).encode())

    def clear_output_folder(self, data=None):
        """Remove all content inside output directory except first-level subfolders."""
        try:
            self.logger.info("Starting clear_output_folder operation...")
            base_dir = self.shared_data.output_dir
            self.logger.info(f"Base directory: {base_dir}")

            if not os.path.exists(base_dir):
                self.logger.warning(f"Output directory does not exist: {base_dir}")
                return {"status": "success", "message": "Output directory does not exist"}

            for root, dirs, files in os.walk(base_dir, topdown=True):
                try:
                    current_depth = root.rstrip(os.path.sep).count(os.path.sep) - base_dir.rstrip(os.path.sep).count(os.path.sep)
                    self.logger.debug(f"Processing directory at depth {current_depth}: {root}")

                    if current_depth == 0:
                        for dir_name in dirs:
                            try:
                                dir_path = os.path.join(root, dir_name)
                                self.logger.debug(f"Clearing contents of first-level subfolder: {dir_path}")
                                for sub_root, sub_dirs, sub_files in os.walk(dir_path):
                                    for sub_file in sub_files:
                                        try:
                                            file_path = os.path.join(sub_root, sub_file)
                                            self.logger.debug(f"Removing file: {file_path}")
                                            os.remove(file_path)
                                        except Exception as file_e:
                                            self.logger.warning(f"Failed to remove file {file_path}: {str(file_e)}")
                                            continue

                                    for sub_dir in sub_dirs:
                                        try:
                                            dir_to_remove = os.path.join(sub_root, sub_dir)
                                            self.logger.debug(f"Removing directory: {dir_to_remove}")
                                            shutil.rmtree(dir_to_remove)
                                        except Exception as dir_e:
                                            self.logger.warning(f"Failed to remove directory {dir_to_remove}: {str(dir_e)}")
                                            continue
                            except Exception as e:
                                self.logger.warning(f"Error processing directory {dir_name}: {str(e)}")
                                continue
                        dirs.clear()

                    elif current_depth > 0:
                        for name in dirs + files:
                            try:
                                path = os.path.join(root, name)
                                if os.path.isdir(path):
                                    shutil.rmtree(path)
                                else:
                                    os.remove(path)
                            except Exception as e:
                                self.logger.warning(f"Failed to remove {path}: {str(e)}")
                                continue

                except Exception as level_e:
                    self.logger.warning(f"Error processing depth level {current_depth}: {str(level_e)}")
                    continue
            
            self.logger.info("Output folder cleared successfully")
            return {
                "status": "success",
                "message": "Output folder cleared, keeping only first-level subfolders"
            }
        except Exception as e:
            self.logger.error(f"Error clearing output folder: {str(e)}")
            return {"status": "error", "message": f"Error clearing output folder: {str(e)}"}