"""backup_utils.py - System backups, GitHub updates, and restore operations."""
from __future__ import annotations
import os
import json
import tarfile
import zipfile
import subprocess
import shutil
import stat
from datetime import datetime
from typing import Any, Dict, Optional

import logging
from logger import Logger
logger = Logger(name="backup_utils.py", level=logging.DEBUG)

class BackupUtils:
    """Utilities for backup and restore operations."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

    def create_backup(self, data):
        """Create a backup of the Bjorn directory in tar.gz or zip format."""
        self.logger.debug("Starting backup process...")
        backup_dir = self.shared_data.backup_dir
        os.makedirs(backup_dir, exist_ok=True)

        backup_description = data.get('description', 'No description')
        backup_format = data.get('format', 'tar.gz')
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        if backup_format == 'zip':
            backup_filename = f"backup_{timestamp}.zip"
            backup_path = os.path.join(backup_dir, backup_filename)
            try:
                with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as backup_zip:
                    for foldername, subfolders, filenames in os.walk(self.shared_data.current_dir):
                        for filename in filenames:
                            file_path = os.path.join(foldername, filename)
                            rel_path = os.path.relpath(file_path, self.shared_data.current_dir)
                            backup_zip.write(file_path, rel_path)

                self.shared_data.db.add_backup(
                    filename=backup_filename,
                    description=backup_description,
                    date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    type_="User Backup",
                    is_default=False,
                    is_restore=False,
                    is_github=False
                )
                self.logger.debug(f"Backup created successfully: {backup_path}")
                return {"status": "success", "message": "Backup created successfully in ZIP format."}
            except Exception as e:
                self.logger.error(f"Failed to create ZIP backup: {e}")
                return {"status": "error", "message": "Internal server error"}

        elif backup_format == 'tar.gz':
            backup_filename = f"backup_{timestamp}.tar.gz"
            backup_path = os.path.join(backup_dir, backup_filename)
            try:
                with tarfile.open(backup_path, "w:gz") as backup_tar:
                    for item in os.listdir(self.shared_data.current_dir):
                        item_path = os.path.join(self.shared_data.current_dir, item)
                        backup_tar.add(item_path, arcname=item)

                self.shared_data.db.add_backup(
                    filename=backup_filename,
                    description=backup_description,
                    date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    type_="User Backup",
                    is_default=False,
                    is_restore=False,
                    is_github=False
                )
                self.logger.debug(f"Backup created successfully: {backup_path}")
                return {"status": "success", "message": "Backup created successfully in tar.gz format."}
            except Exception as e:
                self.logger.error(f"Failed to create tar.gz backup: {e}")
                return {"status": "error", "message": "Internal server error"}
        else:
            self.logger.error(f"Unsupported backup format: {backup_format}")
            return {"status": "error", "message": "Unsupported backup format."}

    def list_backups(self, data=None):
        """List all backups from DB."""
        self.logger.debug("Listing backups...")
        try:
            backups = self.shared_data.db.list_backups()
            return {"status": "success", "backups": backups}
        except Exception as e:
            self.logger.error(f"Failed to list backups: {e}")
            return {"status": "error", "message": "Internal server error"}

    def remove_named_pipes(self, directory):
        """Recursively remove named pipes in the specified directory."""
        self.logger.debug(f"Scanning for named pipes in {directory}...")
        for root, dirs, files in os.walk(directory):
            for name in files:
                file_path = os.path.join(root, name)
                try:
                    if stat.S_ISFIFO(os.stat(file_path).st_mode):
                        os.remove(file_path)
                        self.logger.debug(f"Removed named pipe: {file_path}")
                except Exception as e:
                    self.logger.error(f"Failed to remove named pipe {file_path}: {e}")

    def restore_backup(self, data):
        """Restore a backup with options to keep certain folders."""
        backup_filename = data.get('filename')
        mode = data.get('mode')  # 'full_restore' or 'selective_restore'
        keeps = data.get('keeps', [])

        if not backup_filename:
            return {"status": "error", "message": "Filename not provided"}

        backup_path = os.path.join(self.shared_data.backup_dir, backup_filename)
        original_dir = self.shared_data.current_dir
        temp_dir = f"{original_dir}_temp"

        try:
            if not os.path.exists(backup_path):
                self.logger.error(f"Backup file does not exist: {backup_path}")
                return {"status": "error", "message": "Backup file not found"}

            # Clean up old temp_dir if exists
            if os.path.exists(temp_dir):
                self.logger.debug(f"Removing existing temp directory: {temp_dir}")
                self.remove_named_pipes(temp_dir)
                shutil.rmtree(temp_dir)

            # Create backup of current state
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            from_restore_filename = f"FROM_RESTORE_{timestamp}.tar.gz"
            from_restore_path = os.path.join(self.shared_data.backup_dir, from_restore_filename)

            self.logger.debug("Creating backup of current directory before restoring...")
            with tarfile.open(from_restore_path, "w:gz") as backup_tar:
                for item in os.listdir(original_dir):
                    item_path = os.path.join(original_dir, item)
                    backup_tar.add(item_path, arcname=item)
            self.logger.debug(f"Backup of current directory created: {from_restore_path}")

            self.shared_data.db.add_backup(
                filename=from_restore_filename,
                description='AUTO Backup created during restoration',
                date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                type_="Restore Backup",
                is_default=False,
                is_restore=True,
                is_github=False
            )

            # Rename current directory to temp
            if os.path.exists(original_dir):
                os.rename(original_dir, temp_dir)
            else:
                self.logger.warning(f"Original directory does not exist: {original_dir}")

            # Recreate target directory
            os.makedirs(original_dir, exist_ok=True)

            # Extract backup
            self.logger.debug(f"Extracting backup into {original_dir}...")
            if backup_filename.endswith('.zip'):
                with zipfile.ZipFile(backup_path, 'r') as backup_zip:
                    backup_zip.extractall(original_dir)
            elif backup_filename.endswith('.tar.gz'):
                with tarfile.open(backup_path, 'r:gz') as backup_tar:
                    backup_tar.extractall(original_dir)
            else:
                if os.path.exists(temp_dir):
                    os.rename(temp_dir, original_dir)
                return {"status": "error", "message": "Unsupported backup file format"}

            # Selective restore
            if mode == 'selective_restore' and keeps:
                self.logger.debug("Selective restore: preserving specified folders...")
                for folder in keeps:
                    src = os.path.join(temp_dir, folder)
                    dest = os.path.join(original_dir, folder)
                    if os.path.exists(src):
                        if os.path.exists(dest):
                            self.remove_named_pipes(dest)
                            shutil.rmtree(dest)
                        shutil.copytree(src, dest)

            # Clean up temp_dir
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)

            # Restart Bjorn service
            self.logger.debug("Restarting Bjorn service after restoration...")
            try:
                subprocess.Popen(
                    ["sudo", "systemctl", "restart", "bjorn.service"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except Exception as e:
                self.logger.error(f"Failed to issue restart command: {e}")
                return {"status": "error", "message": "Failed to restart the service."}

            return {"status": "success", "message": "Backup restored successfully."}

        except (tarfile.TarError, zipfile.BadZipFile) as e:
            self.logger.error(f"Failed to extract backup: {e}")
            if os.path.exists(temp_dir):
                os.rename(temp_dir, original_dir)
            return {"status": "error", "message": "Failed to extract backup"}
        except Exception as e:
            self.logger.error(f"Failed to restore backup: {e}")
            if os.path.exists(temp_dir):
                os.rename(temp_dir, original_dir)
            return {"status": "error", "message": "Internal server error"}

    def set_default_backup(self, data):
        """Set a backup as default."""
        try:
            filename = data.get('filename')
            if not filename:
                return {"status": "error", "message": "No filename provided"}

            self.shared_data.db.set_default_backup(filename)
            return {"status": "success"}
        except Exception as e:
            self.logger.error(f"Error setting default backup: {e}")
            return {"status": "error", "message": "Internal server error"}

    def delete_backup(self, data):
        """Delete a backup file and its DB metadata."""
        filename = data.get('filename')
        if not filename:
            return {"status": "error", "message": "Filename not provided"}

        backup_path = os.path.join(self.shared_data.backup_dir, filename)

        try:
            if os.path.exists(backup_path):
                os.remove(backup_path)
                self.logger.debug(f"Deleted backup file: {backup_path}")

            self.shared_data.db.delete_backup(filename)
            return {"status": "success", "message": "Backup deleted successfully."}
        except Exception as e:
            self.logger.error(f"Failed to delete backup: {e}")
            return {"status": "error", "message": "Internal server error"}

    def update_application(self, data):
        """Update application from GitHub with options to keep certain folders."""
        mode = data.get('mode')  # 'fresh_start' or 'upgrade'
        keeps = data.get('keeps', [])

        original_dir = self.shared_data.current_dir
        temp_dir = f"{original_dir}_temp"
        github_zip_url = "https://codeload.github.com/infinition/Bjorn/zip/refs/heads/main"
        downloaded_zip = "/tmp/bjorn_update.zip"
        extract_dir = "/tmp/bjorn_extract"

        try:
            # Preliminary cleanup
            for cleanup_dir in [temp_dir, extract_dir]:
                if os.path.exists(cleanup_dir):
                    self.logger.debug(f"Removing existing directory: {cleanup_dir}")
                    self.remove_named_pipes(cleanup_dir)
                    shutil.rmtree(cleanup_dir, ignore_errors=True)

            # Create backup before update
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            from_update_filename = f"FROM_UPDATE_{timestamp}.tar.gz"
            from_update_path = os.path.join(self.shared_data.backup_dir, from_update_filename)
            os.makedirs(self.shared_data.backup_dir, exist_ok=True)

            self.logger.debug("Creating backup before update...")
            with tarfile.open(from_update_path, "w:gz") as backup_tar:
                for item in os.listdir(original_dir):
                    item_path = os.path.join(original_dir, item)
                    backup_tar.add(item_path, arcname=item)

            self.shared_data.db.add_backup(
                filename=from_update_filename,
                description='AUTO Backup created during GitHub update',
                date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                type_="GitHub Update Backup",
                is_default=False,
                is_restore=False,
                is_github=True
            )

            # Download ZIP from GitHub
            self.logger.debug("Downloading latest version from GitHub...")
            download_command = [
                'curl', '-L', '-o', downloaded_zip,
                '--connect-timeout', '10',
                '--max-time', '60',
                github_zip_url
            ]
            subprocess.run(download_command, check=True)

            if not os.path.exists(downloaded_zip):
                raise Exception("Failed to download update file")

            # Prepare original directory
            if os.path.exists(original_dir):
                os.rename(original_dir, temp_dir)
            os.makedirs(original_dir, exist_ok=True)

            # Extract new version
            self.logger.debug("Extracting new version...")
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(downloaded_zip, 'r') as zip_ref:
                contents = zip_ref.namelist()
                if not contents:
                    raise Exception("ZIP file is empty")
                root_dir = contents[0].split('/')[0]
                zip_ref.extractall(extract_dir)
                extracted_dir = os.path.join(extract_dir, root_dir)
                if not os.path.exists(extracted_dir):
                    raise Exception(f"Expected directory {extracted_dir} not found after extraction")

                for item in os.listdir(extracted_dir):
                    source = os.path.join(extracted_dir, item)
                    destination = os.path.join(original_dir, item)
                    shutil.move(source, destination)

            # If upgrade: restore kept folders
            if mode == 'upgrade' and keeps:
                self.logger.debug("Restoring kept folders...")
                for folder in keeps:
                    src = os.path.join(temp_dir, folder)
                    dest = os.path.join(original_dir, folder)
                    if os.path.exists(src):
                        if os.path.exists(dest):
                            shutil.rmtree(dest, ignore_errors=True)
                        shutil.copytree(src, dest)
                    else:
                        self.logger.warning(f"Source folder not found: {src}")

            # Cleanup
            for path in [temp_dir, extract_dir, downloaded_zip]:
                if os.path.exists(path):
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        try:
                            os.remove(path)
                        except Exception:
                            pass

            # Restart service
            self.logger.debug("Restarting Bjorn service...")
            subprocess.Popen(
                ["sudo", "systemctl", "restart", "bjorn.service"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            return {"status": "success", "message": "Application updated successfully"}

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to download update: {e}")
            if os.path.exists(temp_dir):
                os.rename(temp_dir, original_dir)
            return {"status": "error", "message": "Failed to download update"}
        except Exception as e:
            self.logger.error(f"Update failed: {e}")
            if os.path.exists(temp_dir):
                os.rename(temp_dir, original_dir)
            return {"status": "error", "message": "Internal server error"}
        finally:
            for path in [downloaded_zip, extract_dir]:
                if os.path.exists(path):
                    try:
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            os.remove(path)
                    except Exception as ee:
                        self.logger.error(f"Failed to clean up {path}: {ee}")

    def check_update(self, handler):
        """Check for updates from GitHub."""
        try:
            import requests
            github_raw_url = self.shared_data.github_version_url
            response = requests.get(github_raw_url, timeout=10)
            if response.status_code != 200:
                raise Exception(f"Failed to fetch version from GitHub. Status code: {response.status_code}")

            latest_version_line = response.text.splitlines()[0].strip()
            latest_version = latest_version_line

            with open(self.shared_data.version_file, 'r') as vf:
                current_version_line = vf.readline().strip()
                current_version = current_version_line

            update_available = latest_version != current_version
            self.logger.debug(f"Current version: {current_version}, Latest version: {latest_version}, Update available: {update_available}")

            response_data = {
                'latest_version': latest_version,
                'current_version': current_version,
                'update_available': update_available
            }

            handler.send_response(200)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps(response_data).encode('utf-8'))

        except Exception as e:
            self.logger.error(f"Error checking update: {e}")
            handler.send_response(500)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({
                "status": "error",
                "message": "Failed to check for updates."
            }).encode('utf-8'))



    def download_backup(self, handler, filename):
        """Download a backup file."""
        backup_path = os.path.join(self.shared_data.backup_dir, filename)
        if not os.path.exists(backup_path):
            handler.send_response(404)
            handler.end_headers()
            handler.wfile.write(b"Backup file not found")
            return

        try:
            file_size = os.path.getsize(backup_path)
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/octet-stream')
            handler.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            handler.send_header('Content-Length', str(file_size))
            handler.end_headers()
            with open(backup_path, 'rb') as f:
                shutil.copyfileobj(f, handler.wfile)
        except Exception as e:
            self.logger.error(f"Error downloading backup: {e}")
            handler.send_response(500)
            handler.end_headers()
            handler.wfile.write(b"Internal server error")
