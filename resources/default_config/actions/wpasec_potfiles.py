"""wpasec_potfiles.py - Download, clean, import, or erase WiFi creds from WPAsec potfiles."""

import os
import json
import glob
import argparse
import requests
import subprocess
from datetime import datetime
import logging

# ── METADATA / UI FOR NEO LAUNCHER ────────────────────────────────────────────
b_class       = "WPAsecPotfileManager"
b_module      = "wpasec_potfiles"
b_enabled     = 1
b_action      = "normal"   # normal | aggressive | stealth
b_category    = "wifi"
b_name        = "WPAsec Potfile Manager"
b_description = (
    "Download, clean, import, or erase Wi-Fi networks from WPAsec potfiles. "
    "Options: download (default if API key is set), clean, import, erase."
)
b_author      = "Fabien / Cyberviking"
b_version     = "1.0.0"
b_icon        = f"/actions_icons/{b_class}.png"
b_docs_url    = "https://wpa-sec.stanev.org/?api"

b_args = {
    "key": {
        "type": "text",
        "label": "API key (WPAsec)",
        "placeholder": "wpa-sec api key",
        "secret": True,
        "help": "API key used to download the potfile. If empty, the saved key is reused."
    },
    "directory": {
        "type": "text",
        "label": "Potfiles directory",
        "default": "/home/bjorn/Bjorn/data/input/potfiles",
        "placeholder": "/path/to/potfiles",
        "help": "Directory containing/receiving .pot / .potfile files."
    },
    "clean": {
        "type": "checkbox",
        "label": "Clean potfiles directory",
        "default": False,
        "help": "Delete all files in the potfiles directory."
    },
    "import_potfiles": {
        "type": "checkbox",
        "label": "Import potfiles into NetworkManager",
        "default": False,
        "help": "Add Wi-Fi networks found in potfiles via nmcli (avoiding duplicates)."
    },
    "erase": {
        "type": "checkbox",
        "label": "Erase Wi-Fi connections from potfiles",
        "default": False,
        "help": "Delete via nmcli the Wi-Fi networks listed in potfiles (avoiding duplicates)."
    }
}

b_examples = [
    {"directory": "/home/bjorn/Bjorn/data/input/potfiles"},
    {"key": "YOUR_API_KEY_HERE", "directory": "/home/bjorn/Bjorn/data/input/potfiles"},
    {"directory": "/home/bjorn/Bjorn/data/input/potfiles", "clean": True},
    {"directory": "/home/bjorn/Bjorn/data/input/potfiles", "import_potfiles": True},
    {"directory": "/home/bjorn/Bjorn/data/input/potfiles", "erase": True},
    {"directory": "/home/bjorn/Bjorn/data/input/potfiles", "clean": True, "import_potfiles": True},
]


def compute_dynamic_b_args(base: dict) -> dict:
    """
    Enrich dynamic UI arguments:
    - Pre-fill the API key if previously saved.
    - Show info about the number of potfiles in the chosen directory.
    """
    d = dict(base or {})
    try:
        settings_path = os.path.join(
            os.path.expanduser("~"), ".settings_bjorn", "wpasec_settings.json"
        )
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            saved_key = (saved or {}).get("api_key")
            if saved_key and not d.get("key", {}).get("default"):
                d.setdefault("key", {}).setdefault("default", saved_key)
                d["key"]["help"] = (d["key"].get("help") or "") + " (auto-detected)"
    except Exception:
        pass

    try:
        directory = d.get("directory", {}).get("default") or "/home/bjorn/Bjorn/data/input/potfiles"
        exists = os.path.isdir(directory)
        count = 0
        if exists:
            count = len(glob.glob(os.path.join(directory, "*.pot"))) + \
                    len(glob.glob(os.path.join(directory, "*.potfile")))
        extra = f" | Found: {count} potfile(s)" if exists else " | (directory does not exist yet)"
        d["directory"]["help"] = (d["directory"].get("help") or "") + extra
    except Exception:
        pass

    return d


# ── CLASS IMPLEMENTATION ─────────────────────────────────────────────────────
class WPAsecPotfileManager:
    DEFAULT_SAVE_DIR = "/home/bjorn/Bjorn/data/input/potfiles"
    DEFAULT_SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
    SETTINGS_FILE = os.path.join(DEFAULT_SETTINGS_DIR, "wpasec_settings.json")
    DOWNLOAD_URL = "https://wpa-sec.stanev.org/?api&dl=1"

    def __init__(self, shared_data):
        """
        Orchestrator always passes shared_data.
        Even if unused here, we store it for compatibility.
        """
        self.shared_data = shared_data
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    # --- Orchestrator entry point ---
    def execute(self, ip=None, port=None, row=None, status_key=None):
        """
        Entry point for orchestrator.
        By default: download latest potfile if API key is available.
        """
        self.shared_data.bjorn_orch_status = "WPAsecPotfileManager"
        self.shared_data.comment_params = {"ip": ip, "port": port}

        api_key = self.load_api_key()
        if api_key:
            logging.info("WPAsecPotfileManager: downloading latest potfile (orchestrator trigger).")
            self.download_potfile(self.DEFAULT_SAVE_DIR, api_key)
            return "success"
        else:
            logging.warning("WPAsecPotfileManager: no API key found, nothing done.")
            return "failed"

    # --- API Key Handling ---
    def save_api_key(self, api_key: str):
        """Save the API key locally."""
        try:
            os.makedirs(self.DEFAULT_SETTINGS_DIR, exist_ok=True)
            settings = {"api_key": api_key}
            with open(self.SETTINGS_FILE, "w") as file:
                json.dump(settings, file)
            logging.info(f"API key saved to {self.SETTINGS_FILE}")
        except Exception as e:
            logging.error(f"Failed to save API key: {e}")

    def load_api_key(self):
        """Load the API key from local storage."""
        if os.path.exists(self.SETTINGS_FILE):
            try:
                with open(self.SETTINGS_FILE, "r") as file:
                    settings = json.load(file)
                    return settings.get("api_key")
            except Exception as e:
                logging.error(f"Failed to load API key: {e}")
        return None

    # --- Actions ---
    def download_potfile(self, save_dir, api_key):
        """Download the potfile from WPAsec."""
        try:
            cookies = {"key": api_key}
            logging.info(f"Downloading potfile from: {self.DOWNLOAD_URL}")
            response = requests.get(self.DOWNLOAD_URL, cookies=cookies, stream=True)
            response.raise_for_status()

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            filename = os.path.join(save_dir, f"potfile_{timestamp}.pot")

            os.makedirs(save_dir, exist_ok=True)
            with open(filename, "wb") as file:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)

            logging.info(f"Potfile saved to: {filename}")
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to download potfile: {e}")
        except Exception as e:
            logging.error(f"Unexpected error: {e}")

    def clean_directory(self, directory):
        """Delete all potfiles in the given directory."""
        try:
            if os.path.exists(directory):
                logging.info(f"Cleaning directory: {directory}")
                for file in os.listdir(directory):
                    file_path = os.path.join(directory, file)
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        logging.info(f"Deleted: {file_path}")
            else:
                logging.info(f"Directory does not exist: {directory}")
        except Exception as e:
            logging.error(f"Failed to clean directory {directory}: {e}")

    def import_potfiles(self, directory):
        """Import potfiles into NetworkManager using nmcli."""
        try:
            potfile_paths = glob.glob(os.path.join(directory, "*.pot")) + glob.glob(os.path.join(directory, "*.potfile"))
            processed_ssids = set()
            networks_added = []
            DEFAULT_PRIORITY = 5

            for path in potfile_paths:
                with open(path, "r") as potfile:
                    for line in potfile:
                        line = line.strip()
                        if ":" not in line:
                            continue
                        ssid, password = self._parse_potfile_line(line)
                        if not ssid or not password or ssid in processed_ssids:
                            continue

                        try:
                            subprocess.run(
                                ["sudo", "nmcli", "connection", "add", "type", "wifi",
                                 "con-name", ssid, "ifname", "*", "ssid", ssid,
                                 "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
                                 "connection.autoconnect", "yes",
                                 "connection.autoconnect-priority", str(DEFAULT_PRIORITY)],
                                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                            )
                            processed_ssids.add(ssid)
                            networks_added.append(ssid)
                            logging.info(f"Imported network {ssid}")
                        except subprocess.CalledProcessError as e:
                            logging.error(f"Failed to import {ssid}: {e.stderr.strip()}")

            logging.info(f"Total imported: {networks_added}")
        except Exception as e:
            logging.error(f"Unexpected error while importing: {e}")

    def erase_networks(self, directory):
        """Erase Wi-Fi connections listed in potfiles using nmcli."""
        try:
            potfile_paths = glob.glob(os.path.join(directory, "*.pot")) + glob.glob(os.path.join(directory, "*.potfile"))
            processed_ssids = set()
            networks_removed = []

            for path in potfile_paths:
                with open(path, "r") as potfile:
                    for line in potfile:
                        line = line.strip()
                        if ":" not in line:
                            continue
                        ssid, _ = self._parse_potfile_line(line)
                        if not ssid or ssid in processed_ssids:
                            continue

                        try:
                            subprocess.run(
                                ["sudo", "nmcli", "connection", "delete", "id", ssid],
                                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                            )
                            processed_ssids.add(ssid)
                            networks_removed.append(ssid)
                            logging.info(f"Deleted network {ssid}")
                        except subprocess.CalledProcessError as e:
                            logging.warning(f"Failed to delete {ssid}: {e.stderr.strip()}")

            logging.info(f"Total deleted: {networks_removed}")
        except Exception as e:
            logging.error(f"Unexpected error while erasing: {e}")

    # --- Helpers ---
    def _parse_potfile_line(self, line: str):
        """Parse a potfile line into (ssid, password)."""
        ssid, password = None, None
        if line.startswith("$WPAPSK$") and "#" in line:
            try:
                ssid_hash, password = line.split(":", 1)
                ssid = ssid_hash.split("#")[0].replace("$WPAPSK$", "")
            except ValueError:
                return None, None
        elif len(line.split(":")) == 4:
            try:
                _, _, ssid, password = line.split(":")
            except ValueError:
                return None, None
        return ssid, password

    # --- CLI ---
    def run(self, argv=None):
        parser = argparse.ArgumentParser(description="Manage WPAsec potfiles (download, clean, import, erase).")
        parser.add_argument("-k", "--key", help="API key for WPAsec (saved locally after first use).")
        parser.add_argument("-d", "--directory", default=self.DEFAULT_SAVE_DIR, help="Directory for potfiles.")
        parser.add_argument("-c", "--clean", action="store_true", help="Clean the potfiles directory.")
        parser.add_argument("-a", "--import-potfiles", action="store_true", help="Import potfiles into NetworkManager.")
        parser.add_argument("-e", "--erase", action="store_true", help="Erase Wi-Fi connections from potfiles.")
        args = parser.parse_args(argv)

        api_key = args.key
        if api_key:
            self.save_api_key(api_key)
        else:
            api_key = self.load_api_key()

        if args.clean:
            self.clean_directory(args.directory)
        if args.import_potfiles:
            self.import_potfiles(args.directory)
        if args.erase:
            self.erase_networks(args.directory)
        if api_key and not args.clean and not args.import_potfiles and not args.erase:
            self.download_potfile(args.directory, api_key)


if __name__ == "__main__":
    WPAsecPotfileManager(shared_data=None).run()
