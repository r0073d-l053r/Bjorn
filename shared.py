# shared.py
# Core component for managing shared resources and data for Bjorn project
# Handles initialization, configuration, logging, fonts, images, and database management
# OPTIMIZED FOR PI ZERO 2: Lazy Loading, Thread-Safety, and Low Memory Footprint.

import os
import re
import json
import importlib
import random
import time
import ast
import logging
import subprocess
import threading
import socket
import gc
import weakref
from datetime import datetime
from typing import Dict, List, Optional, Any
from PIL import Image, ImageFont 
from logger import Logger
from epd_manager import EPDManager
from database import BjornDatabase

logger = Logger(name="shared.py", level=logging.DEBUG)

class SharedData:
    """Centralized shared data manager for all Bjorn modules"""
    
    def __init__(self):
        # Initialize core paths first
        self.initialize_paths()
        
        # --- THREAD SAFETY LOCKS ---
        # RLock allows the same thread to acquire the lock multiple times (re-entrant)
        # essential for config loading/saving which might be called recursively.
        self.config_lock = threading.RLock() 
        self.scripts_lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.health_lock = threading.Lock()
        
        # Initialize status tracking (set prevents duplicates and unbounded growth)
        self.status_list = set()
        self.last_comment_time = time.time()
        self.curr_status = {"status": "Idle", "details": ""}
        self.status_lock = threading.Lock()
        
        # --- BI-DIRECTIONAL LINKS (WEAK) ---
        # Prevent circular references while allowing access to the supervisor
        self._bjorn_ref = None
        
        # --- CACHING ---
        self._config_json_cache = None
        self._config_json_ts = 0
        
        # Event for orchestrator wake-up (Avoids CPU busy-waiting)
        self.queue_event = threading.Event()
        
        # Load default configuration
        self.default_config = self.get_default_config()
        self.config = self.default_config.copy()
        
        # Initialize database (single source of truth)
        self.db = BjornDatabase()
        
        # Load existing configuration from database (Thread-safe)
        self.load_config()
        
        # Update security blacklists
        self.update_security_blacklists()
        
        # Setup environment and resources
        self.setup_environment()
        self.initialize_runtime_variables()
        self.initialize_statistics()
        self.load_fonts()
        
        # --- LAZY LOADING IMAGES ---
        # Indexes paths instead of loading pixels to RAM
        self.load_images()
        
        logger.info("SharedData initialization complete (Pi Zero 2 Optimized)")

    def initialize_paths(self):
        """Initialize all application paths and create necessary directories"""
        # Base directories
        self.bjorn_user_dir = '/home/bjorn/'
        self.current_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Main application directories
        self.data_dir = os.path.join(self.current_dir, 'data')
        self.actions_dir = os.path.join(self.current_dir, 'actions')
        self.web_dir = os.path.join(self.current_dir, 'web')
        self.resources_dir = os.path.join(self.current_dir, 'resources')
        
        # User directories
        self.backup_dir = '/home/bjorn/.backups_bjorn'
        self.settings_dir = '/home/bjorn/.settings_bjorn'
        
        # Data subdirectories
        self.logs_dir = os.path.join(self.data_dir, 'logs')
        self.output_dir = os.path.join(self.data_dir, 'output')
        self.input_dir = os.path.join(self.data_dir, 'input')
        
        # Output subdirectories
        self.data_stolen_dir = os.path.join(self.output_dir, 'data_stolen')
        
        # Resources subdirectories
        self.images_dir = os.path.join(self.resources_dir, 'images')
        self.fonts_dir = os.path.join(self.resources_dir, 'fonts')
        self.default_config_dir = os.path.join(self.resources_dir, 'default_config')
        self.default_comments_dir = os.path.join(self.default_config_dir, 'comments')

        # Default config subdirectories
        self.default_comments_file = os.path.join(self.default_comments_dir, 'comments.en.json')
        self.default_images_dir = os.path.join(self.default_config_dir, 'images')
        self.default_actions_dir = os.path.join(self.default_config_dir, 'actions')
        
        # Images subdirectories
        self.status_images_dir = os.path.join(self.images_dir, 'status')
        self.static_images_dir = os.path.join(self.images_dir, 'static')
        
        # Input subdirectories
        self.dictionary_dir = os.path.join(self.input_dir, "dictionary")
        self.potfiles_dir = os.path.join(self.input_dir, "potfiles")
        self.wordlists_dir = os.path.join(self.input_dir, "wordlists")
        self.nmap_prefixes_dir = os.path.join(self.input_dir, "prefixes")
        
        # Actions subdirectory
        self.actions_icons_dir = os.path.join(self.actions_dir, 'actions_icons')
        
        # Important files
        self.version_file = os.path.join(self.current_dir, 'version.txt')
        self.backups_json = os.path.join(self.backup_dir, 'backups.json')
        self.webapp_json = os.path.join(self.settings_dir, 'webapp.json')
        self.nmap_prefixes_file = os.path.join(self.nmap_prefixes_dir, "nmap-mac-prefixes.txt")
        self.common_wordlist = os.path.join(self.wordlists_dir, "common.txt")
        self.users_file = os.path.join(self.dictionary_dir, "users.txt")
        self.passwords_file = os.path.join(self.dictionary_dir, "passwords.txt")
        self.log_file = os.path.join(self.logs_dir, 'Bjorn.log')
        self.web_console_log = os.path.join(self.logs_dir, 'web_console_log.txt')
        
        # AI Models
        self.ai_models_dir = os.path.join(self.bjorn_user_dir, 'ai_models')
        self.ml_exports_dir = os.path.join(self.data_dir, 'ml_exports')
        
        # Create all necessary directories
        self._create_directories()

    def _create_directories(self):
        """Create all necessary directories if they don't exist"""
        directories = [
            self.data_dir, self.actions_dir, self.web_dir, self.resources_dir,
            self.logs_dir, self.output_dir, self.input_dir, 
            self.data_stolen_dir, self.images_dir, self.fonts_dir, 
            self.fonts_dir, self.default_config_dir, self.default_comments_dir,
            self.status_images_dir, self.static_images_dir, self.dictionary_dir,
            self.potfiles_dir, self.wordlists_dir, self.nmap_prefixes_dir,
            self.backup_dir, self.settings_dir,
            self.ai_models_dir, self.ml_exports_dir
        ]
        
        for directory in directories:
            try:
                os.makedirs(directory, exist_ok=True)
            except Exception as e:
                logger.error(f"Cannot create directory {directory}: {e}")

    def get_default_config(self) -> Dict[str, Any]:
        """Return default configuration settings"""
        return {
            # Core / identity
            "__title_Bjorn__": "Core Settings",
            "bjorn_name": "Bjorn",
            "current_character": "BJORN",
            "lang": "en",
            "lang_priority": ["en", "fr", "es"],
            "__tooltips_i18n__": {
                "manual_mode": "settings.tooltip.manual_mode",
                "ai_mode": "settings.tooltip.ai_mode",
                "learn_in_auto": "settings.tooltip.learn_in_auto",
                "debug_mode": "settings.tooltip.debug_mode",
                "websrv": "settings.tooltip.websrv",
                "webauth": "settings.tooltip.webauth",
                "bjorn_debug_enabled": "settings.tooltip.bjorn_debug_enabled",
                "retry_success_actions": "settings.tooltip.retry_success_actions",
                "retry_failed_actions": "settings.tooltip.retry_failed_actions",
                "ai_server_url": "settings.tooltip.ai_server_url",
                "ai_exploration_rate": "settings.tooltip.ai_exploration_rate",
                "ai_sync_interval": "settings.tooltip.ai_sync_interval",
                "ai_server_max_failures_before_auto": "settings.tooltip.ai_server_max_failures_before_auto",
                "ai_feature_selection_min_variance": "settings.tooltip.ai_feature_selection_min_variance",
                "ai_model_history_max": "settings.tooltip.ai_model_history_max",
                "ai_auto_rollback_window": "settings.tooltip.ai_auto_rollback_window",
                "ai_cold_start_bootstrap_weight": "settings.tooltip.ai_cold_start_bootstrap_weight",
                "circuit_breaker_threshold": "settings.tooltip.circuit_breaker_threshold",
                "manual_mode_auto_scan": "settings.tooltip.manual_mode_auto_scan",
                "manual_mode_scan_interval": "settings.tooltip.manual_mode_scan_interval",
                "startup_delay": "settings.tooltip.startup_delay",
                "web_delay": "settings.tooltip.web_delay",
                "screen_delay": "settings.tooltip.screen_delay",
                "livestatus_delay": "settings.tooltip.livestatus_delay",
                "epd_enabled": "settings.tooltip.epd_enabled",
                "showiponscreen": "settings.tooltip.showiponscreen",
                "shared_update_interval": "settings.tooltip.shared_update_interval",
                "vuln_update_interval": "settings.tooltip.vuln_update_interval",
                "semaphore_slots": "settings.tooltip.semaphore_slots",
                "runtime_tick_s": "settings.tooltip.runtime_tick_s",
                "runtime_gc_interval_s": "settings.tooltip.runtime_gc_interval_s",
                "default_network_interface": "settings.tooltip.default_network_interface",
                "use_custom_network": "settings.tooltip.use_custom_network",
                "custom_network": "settings.tooltip.custom_network",
                "portlist": "settings.tooltip.portlist",
                "portstart": "settings.tooltip.portstart",
                "portend": "settings.tooltip.portend",
                "scan_max_host_threads": "settings.tooltip.scan_max_host_threads",
                "scan_max_port_threads": "settings.tooltip.scan_max_port_threads",
                "mac_scan_blacklist": "settings.tooltip.mac_scan_blacklist",
                "ip_scan_blacklist": "settings.tooltip.ip_scan_blacklist",
                "hostname_scan_blacklist": "settings.tooltip.hostname_scan_blacklist",
                "vuln_fast": "settings.tooltip.vuln_fast",
                "nse_vulners": "settings.tooltip.nse_vulners",
                "vuln_max_ports": "settings.tooltip.vuln_max_ports",
                "use_actions_studio": "settings.tooltip.use_actions_studio",
                "bruteforce_exhaustive_enabled": "settings.tooltip.bruteforce_exhaustive_enabled",
                "bruteforce_exhaustive_max_candidates": "settings.tooltip.bruteforce_exhaustive_max_candidates",
            },

            # Operation modes
            "__title_modes__": "Operation Modes",
            "manual_mode": False,
            "ai_mode": True,
            "learn_in_auto": False,
            "debug_mode": True,

            # Web server / UI behavior
            "__title_web__": "Web Server",
            "websrv": True,
            "webauth": False,
            "consoleonwebstart": True,
            "web_logging_enabled": False,
            "bjorn_debug_enabled": False,
            "retry_success_actions": False,
            "retry_failed_actions": True,
            "blacklistcheck": True,

            # AI / RL
            "__title_ai__": "AI / RL",
            "ai_server_url": "http://192.168.1.40:8000",
            "ai_exploration_rate": 0.1,
            "ai_sync_interval": 60,
            "ai_training_min_samples": 5,
            "ai_confirm_threshold": 0.3,
            "ai_batch_size": 100,
            "ai_export_max_records": 1000,
            "ai_server_max_failures_before_auto": 3,
            "ai_upload_retry_backoff_s": 120,
            "ai_consolidation_max_batches": 2,
            "ai_feature_hosts_limit": 512,
            "ai_delete_export_after_upload": True,
            "ai_feature_selection_min_variance": 0.001,
            "ai_model_history_max": 3,
            "ai_auto_rollback_window": 50,
            "ai_cold_start_bootstrap_weight": 0.6,
            "rl_train_batch_size": 10,

            # Global timing / refresh
            "__title_timing__": "Timing",
            "startup_delay": 3,
            "web_delay": 3,
            "screen_delay": 3,
            "web_screenshot_interval_s": 4.0,
            "comment_delaymin": 15,
            "comment_delaymax": 30,
            "livestatus_delay": 8,

            # Display / UI
            "__title_display__": "Display",
            "epd_enabled": True,
            "screen_reversed": True,
            "web_screen_reversed": True,
            "showstartupipssid": False,
            "showiponscreen": True,
            "showssidonscreen": True,
            "shared_update_interval": 10,
            "vuln_update_interval": 20,
            "semaphore_slots": 5,
            "double_partial_refresh": True,
            "startup_splash_duration": 3,
            "fullrefresh_activated": True,
            "fullrefresh_delay": 600,
            "image_display_delaymin": 2,
            "image_display_delaymax": 8,
            "health_log_interval": 60,
            "epd_watchdog_timeout": 45,
            "epd_recovery_cooldown": 60,
            "epd_error_backoff": 2,

            # Runtime state updater
            "__title_runtime__": "Runtime Updater",
            "runtime_tick_s": 0.5,
            "runtime_gc_interval_s": 0.0,

            # Power management
            "__title_power__": "Power Management",
            "pisugar_enabled": True,
            "pisugar_socket_path": "/tmp/pisugar-server.sock",
            "pisugar_tcp_host": "127.0.0.1",
            "pisugar_tcp_port": 8423,
            "pisugar_timeout_s": 1.5,
            "battery_probe_failures_before_none": 4,
            "battery_probe_grace_seconds": 120,

            # EPD / fonts / positions
            "__title_epd__": "EPD & Fonts",
            "ref_width": 122,
            "ref_height": 250,
            "epd_type": "epd2in13_V4",
            "defaultfonttitle": "Viking.TTF",
            "defaultfont": "Arial.ttf",
            "line_spacing": 1,
            "frise_default_x": 0,
            "frise_default_y": 160,
            "frise_epd2in7_x": 50,
            "frise_epd2in7_y": 160,

            # Network interfaces
            "__title_interfaces__": "Network Interfaces",
            "ip_iface_priority": ["wlan0", "eth0"],
            "neigh_wifi_iface": "wlan0",
            "neigh_ethernet_iface": "eth0",
            "neigh_usb_iface": "usb0",
            "neigh_bluetooth_ifaces": ["pan0", "bnep0"],

            # Network scanning
            "__title_network__": "Network Scanning",
            "portlist": [20, 21, 22, 23, 25, 53, 69, 80, 110, 111, 135, 137, 139, 143, 
                        161, 162, 389, 443, 445, 512, 513, 514, 587, 636, 993, 995, 
                        1080, 1433, 1521, 2049, 3306, 3389, 5000, 5001, 5432, 5900, 
                        8080, 8443, 9090, 10000],
            "mac_scan_blacklist": [],
            "ip_scan_blacklist": [],
            "hostname_scan_blacklist": ["bjorn.home"],
            "nmap_scan_aggressivity": "-T2",
            "portstart": 1,
            "portend": 2,
            "use_custom_network": False,
            "custom_network": "192.168.1.0/24",
            "default_network_interface": "wlan0",
            "scan_max_host_threads": 3,
            "scan_max_port_threads": 8,
            "scan_port_timeout_s": 1.0,
            "scan_mac_retries": 2,
            "scan_mac_retry_delay_s": 0.6,
            "scan_arping_timeout_s": 1.5,
            "scan_nmap_discovery_timeout_s": 90,
            "scan_nmap_discovery_args": "-sn -PR --max-retries 1 --host-timeout 8s",

            # Lists
            "__title_lists__": "List Settings",
            "steal_file_names": ["ssh.csv", "hack.txt"],
            "steal_file_extensions": [".bjorn", ".hack", ".flag"],
            "ignored_smb_shares": ["print$", "ADMIN$", "IPC$"],

            # Vulnerability scanning
            "__title_vuln__": "Vulnerability Scanning",
            "vuln_fast": True,
            "nse_vulners": True,
            "vuln_max_ports": 25,
            "vuln_rescan_on_change_only": False,
            "vuln_rescan_ttl_seconds": 0,
            "vuln_batch_size": 2,
            "vuln_batch_pause_s": 0.5,
            "scan_cpe": True,
            "nvd_api_key": "",                 
            "exploitdb_repo_dir": "/home/bjorn/exploitdb",
            "exploitdb_enabled": True,
            "searchsploit_path": "/home/bjorn/exploitdb/searchsploit",   
            "exploitdb_root": "/home/bjorn/exploitdb",
            "kev_feed_url": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
            "epss_api": "https://api.first.org/data/v1/epss?cve=",

            # Actions studio
            "__title_actions_studio__": "Actions Studio",
            "use_actions_studio": True,

            # Action timings / probes
            "__title_timewaits__": "Action Timing Settings",
            "timewait_smb": 0,
            "timewait_ssh": 0,
            "timewait_telnet": 0,
            "timewait_ftp": 0,
            "timewait_sql": 0,
            "ssh_connect_timeout_s": 6.0,
            "ftp_connect_timeout_s": 3.0,
            "telnet_connect_timeout_s": 6.0,
            "sql_connect_timeout_s": 6.0,
            "smb_connect_timeout_s": 6.0,
            "web_probe_timeout_s": 4.0,
            "web_probe_user_agent": "BjornWebProfiler/1.0",
            "web_login_profiler_paths": [
                "/",
                "/login",
                "/signin",
                "/auth",
                "/admin",
                "/administrator",
                "/wp-login.php",
                "/user/login",
                "/robots.txt",
            ],
            "web_probe_max_bytes": 65536,
            "valkyrie_delay_s": 0.05,
            "valkyrie_scout_paths": [
                "/",
                "/robots.txt",
                "/login",
                "/signin",
                "/auth",
                "/admin",
                "/wp-login.php",
            ],
            "thor_connect_timeout_s": 1.5,
            "thor_banner_max_bytes": 1024,
            "thor_source": "thor_hammer",

            # Exhaustive bruteforce fallback
            "__title_bruteforce__": "Bruteforce Exhaustive",
            "bruteforce_exhaustive_enabled": False,
            "bruteforce_exhaustive_min_length": 1,
            "bruteforce_exhaustive_max_length": 4,
            "bruteforce_exhaustive_max_candidates": 2000,
            "bruteforce_exhaustive_lowercase": True,
            "bruteforce_exhaustive_uppercase": True,
            "bruteforce_exhaustive_digits": True,
            "bruteforce_exhaustive_symbols": False,
            "bruteforce_exhaustive_symbols_chars": "!@#$%^&*",
            "bruteforce_exhaustive_require_mix": False,

            # Orchestrator improvements
            "__title_orchestrator__": "Orchestrator",
            "circuit_breaker_threshold": 3,
            "manual_mode_auto_scan": True,
            "manual_mode_scan_interval": 180,

            "__title_sentinel__": "Sentinel Watchdog",
            "sentinel_enabled": False,
            "sentinel_interval": 30,
            "sentinel_discord_webhook": "",
            "sentinel_webhook_url": "",
            "sentinel_email_enabled": False,

            # Bifrost (Pwnagotchi Mode)
            "__title_bifrost__": "Bifrost (Pwnagotchi Mode)",
            "bifrost_enabled": False,
            "bifrost_iface": "wlan0mon",
            "bifrost_bettercap_host": "127.0.0.1",
            "bifrost_bettercap_port": 8081,
            "bifrost_bettercap_user": "user",
            "bifrost_bettercap_pass": "pass",
            "bifrost_bettercap_handshakes": "/root/bifrost/handshakes",
            "bifrost_whitelist": "",
            "bifrost_channels": "",
            "bifrost_filter": "",
            "bifrost_personality_deauth": True,
            "bifrost_personality_associate": True,
            "bifrost_personality_recon_time": 30,
            "bifrost_personality_hop_recon_time": 10,
            "bifrost_personality_min_recon_time": 5,
            "bifrost_personality_ap_ttl": 120,
            "bifrost_personality_sta_ttl": 300,
            "bifrost_personality_min_rssi": -200,
            "bifrost_personality_max_interactions": 3,
            "bifrost_personality_max_misses": 8,
            "bifrost_personality_excited_epochs": 10,
            "bifrost_personality_bored_epochs": 15,
            "bifrost_personality_sad_epochs": 25,
            "bifrost_personality_bond_factor": 20000,
            "bifrost_plugins_path": "/root/bifrost/plugins",
            "bifrost_ai_enabled": False,

            # Loki (HID Attack Mode)
            "__title_loki__": "Loki (HID Attack Mode)",
            "loki_enabled": False,
            "loki_default_layout": "us",
            "loki_typing_speed_min": 0,
            "loki_typing_speed_max": 0,
            "loki_scripts_path": "/root/loki/scripts",
            "loki_auto_run": "",
        }

    @property
    def operation_mode(self) -> str:
        """
        Get current operation mode: 'MANUAL', 'AUTO', 'AI', 'BIFROST', or 'LOKI'.
        Abstracts legacy manual_mode and ai_mode flags.
        LOKI is the 5th exclusive mode — USB HID attack, Pi acts as keyboard/mouse.
        BIFROST is the 4th exclusive mode — WiFi monitor mode recon.
        """
        if self.config.get("loki_enabled", False):
            return "LOKI"
        if self.config.get("bifrost_enabled", False):
            return "BIFROST"
        if getattr(self, "manual_mode", False):
            return "MANUAL"
        if getattr(self, "ai_mode", False):
            return "AI"
        return "AUTO"

    @property
    def bjorn_instance(self):
        """Access the supervisor Bjorn instance via weak reference."""
        return self._bjorn_ref() if self._bjorn_ref else None

    @bjorn_instance.setter
    def bjorn_instance(self, instance):
        if instance is None:
            self._bjorn_ref = None
        else:
            self._bjorn_ref = weakref.ref(instance)

    @property
    def config_json(self) -> str:
        """Get configuration as a JSON string (Cached for performance)."""
        with self.config_lock:
            # Re-serialize only if not cached. 
            # In a real app we'd check if self.config was modified, 
            # but for Pi Zero simplicity, we mostly rely on this for repeated web probes.
            if self._config_json_cache is None:
                self._config_json_cache = json.dumps(self.config)
            return self._config_json_cache

    def invalidate_config_cache(self):
        """Invalidate the JSON config cache after modifications."""
        self._config_json_cache = None

    @operation_mode.setter
    def operation_mode(self, mode: str):
        """
        Set operation mode: 'MANUAL', 'AUTO', 'AI', 'BIFROST', or 'LOKI'.
        Updates legacy flags for backward compatibility.
        LOKI mode: stops orchestrator, starts loki engine (USB HID attack).
        BIFROST mode: stops orchestrator, starts bifrost engine (monitor mode WiFi recon).
        """
        mode = str(mode or "").upper().strip()
        if mode not in ("MANUAL", "AUTO", "AI", "BIFROST", "LOKI"):
            return

        # No-op if already in this mode (prevents log spam and redundant work).
        try:
            if mode == self.operation_mode:
                return
        except Exception:
            pass

        # ── Leaving LOKI → stop engine, remove HID gadget ──
        was_loki = self.config.get("loki_enabled", False)
        if was_loki and mode != "LOKI":
            engine = getattr(self, 'loki_engine', None)
            if engine and hasattr(engine, 'stop'):
                try:
                    engine.stop()
                except Exception as e:
                    logger.warning("Loki stop error: %s", e)
            self.config["loki_enabled"] = False

        # ── Leaving BIFROST → stop engine, restore WiFi ──
        was_bifrost = self.config.get("bifrost_enabled", False)
        if was_bifrost and mode != "BIFROST":
            engine = getattr(self, 'bifrost_engine', None)
            if engine and hasattr(engine, 'stop'):
                try:
                    engine.stop()
                except Exception as e:
                    logger.warning("Bifrost stop error: %s", e)
            self.config["bifrost_enabled"] = False

        # ── Set new mode ──
        if mode == "LOKI":
            self.config["loki_enabled"] = True
            self.config["bifrost_enabled"] = False
            self.config["manual_mode"] = False
            self.config["ai_mode"] = False
            self.manual_mode = False
            self.ai_mode = False
            # Start Loki engine
            engine = getattr(self, 'loki_engine', None)
            if engine and hasattr(engine, 'start'):
                try:
                    engine.start()
                except Exception as e:
                    logger.warning("Loki start error: %s", e)
        elif mode == "BIFROST":
            self.config["bifrost_enabled"] = True
            self.config["loki_enabled"] = False
            self.config["manual_mode"] = False
            self.config["ai_mode"] = False
            self.manual_mode = False
            self.ai_mode = False
            # Start engine
            engine = getattr(self, 'bifrost_engine', None)
            if engine and hasattr(engine, 'start'):
                try:
                    engine.start()
                except Exception as e:
                    logger.warning("Bifrost start error: %s", e)
        elif mode == "MANUAL":
            self.config["loki_enabled"] = False
            self.config["manual_mode"] = True
            self.manual_mode = True
            self.ai_mode = False
        elif mode == "AI":
            self.config["loki_enabled"] = False
            self.config["manual_mode"] = False
            self.config["ai_mode"] = True
            self.manual_mode = False
            self.ai_mode = True
        elif mode == "AUTO":
            self.config["loki_enabled"] = False
            self.config["manual_mode"] = False
            self.config["ai_mode"] = False
            self.manual_mode = False
            self.ai_mode = False

        # Ensure config reflects attributes
        self.config["manual_mode"] = self.manual_mode
        self.config["ai_mode"] = getattr(self, "ai_mode", False)

        self.invalidate_config_cache()
        logger.info(f"Operation mode switched to: {mode}")

    def get_actions_config(self) -> List[Dict[str, Any]]:
        """Return actions configuration from database"""
        try:
            return self.db.list_actions()
        except Exception as e:
            logger.error(f"Failed to get actions config from DB: {e}")
            return []

    def update_security_blacklists(self):
        """Update MAC and hostname blacklists for security"""
        # Get local MAC address
        mac_address = self.get_raspberry_mac()
        if mac_address:
            self._add_to_blacklist('mac_scan_blacklist', mac_address, 'MAC address')
        else:
            logger.warning("Could not add local MAC to blacklist: MAC address not found")
        
        # Add local hostname to blacklist
        bjorn_hostname = "bjorn.home"
        self._add_to_blacklist('hostname_scan_blacklist', bjorn_hostname, 'hostname')

    def _add_to_blacklist(self, blacklist_key: str, value: str, value_type: str):
        """Add value to specified blacklist (Thread-safe)"""
        with self.config_lock:
            if blacklist_key not in self.config:
                self.config[blacklist_key] = []
            
            if value not in self.config[blacklist_key]:
                self.config[blacklist_key].append(value)
                logger.info(f"Added {value_type} {value} to blacklist")
            else:
                logger.info(f"{value_type} {value} already in blacklist")

    def get_raspberry_mac(self) -> Optional[str]:
        """Get MAC address of primary network interface"""
        try:
            for path in ("/sys/class/net/wlan0/address", "/sys/class/net/eth0/address"):
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        mac = fh.read().strip().lower()
                        if mac:
                            return mac
                except Exception as read_error:
                    logger.debug(f"Could not read {path}: {read_error}")
            
            logger.warning("Could not find MAC address for wlan0 or eth0")
            return None
            
        except Exception as e:
            logger.error(f"Error getting Raspberry Pi MAC address: {e}")
            return None

    def setup_environment(self):
        """Setup application environment"""
        os.system('cls' if os.name == 'nt' else 'clear')
        self.save_config()
        self.sync_actions_to_database()
        self.delete_web_console_log()
        self.initialize_database()
        self.initialize_epd_display()

    def initialize_epd_display(self):
        """Initialize e-paper display"""
        if not self.config.get("epd_enabled", True):
            self.epd = None
            self.width = int(self.config.get("ref_width", 122))
            self.height = int(self.config.get("ref_height", 250))
            self.ref_width = self.config.get('ref_width', 122)
            self.ref_height = self.config.get('ref_height', 250)
            self.scale_factor_x = self.width / self.ref_width
            self.scale_factor_y = self.height / self.ref_height
            logger.info("EPD disabled by config - running in headless mode")
            return

        try:
            logger.info("Initializing EPD display...")
            time.sleep(1)

            # Use Manager instead of Helper
            self.epd = EPDManager(self.config["epd_type"])

            # Config orientation
            epd_configs = {
                "epd2in7": (False, False),
                "epd2in13_V2": (True, True),
                "epd2in13_V3": (True, True),
                "epd2in13_V4": (True, True)
            }
            if self.config["epd_type"] in epd_configs:
                self.screen_reversed, self.web_screen_reversed = epd_configs[self.config["epd_type"]]
                logger.info(f"EPD type: {self.config['epd_type']} - reversed: {self.screen_reversed}")

            # Init hardware once
            self.epd.init_full_update()
            self.width, self.height = self.epd.epd.width, self.epd.epd.height

            # Scaling
            self.ref_width = self.config.get('ref_width', 122)
            self.ref_height = self.config.get('ref_height', 250)
            self.scale_factor_x = self.width / self.ref_width
            self.scale_factor_y = self.height / self.ref_height

            logger.info(f"EPD {self.config['epd_type']} initialized: {self.width}x{self.height}")

        except Exception as e:
            logger.error(f"Error initializing EPD display: {e}")
            self.epd = None
            self.config["epd_enabled"] = False
            self.width = int(self.config.get("ref_width", 122))
            self.height = int(self.config.get("ref_height", 250))
            self.ref_width = self.config.get('ref_width', 122)
            self.ref_height = self.config.get('ref_height', 250)
            self.scale_factor_x = self.width / self.ref_width
            self.scale_factor_y = self.height / self.ref_height
            logger.warning("Falling back to headless mode after EPD init failure")


    def initialize_runtime_variables(self):
        """Initialize runtime variables"""
        # System state flags
        self.should_exit = False
        self.display_should_exit = False
        self.display_layout = None  # Initialized by Display module
        self.orchestrator_should_exit = False
        self.webapp_should_exit = False
        
        # Instance tracking
        self.bjorn_instance = None
        
        # Network state
        self.wifi_connected = False
        self.wifi_changed = False
        self.bluetooth_active = False
        self.ethernet_active = False
        self.pan_connected = False
        self.usb_active = False
        self.current_ip = "No IP"
        self.action_target_ip = ""
        self.current_ssid = "No Wi-Fi"
        
        # Display state
        self.bjorn_character = None
        self.current_path = []
        self.comment_params = {}
        self.bjorn_says = "Hacking away..."
        self.bjorn_orch_status = "IDLE"
        self.bjorn_status_text = "IDLE"
        self.bjorn_status_text2 = "Awakening..."
        self.bjorn_progress = ""
        
        # --- NEW: AI / RL Real-Time Tracking ---
        self.active_action = None
        self.last_decision_method = "heuristic" # 'neural_network', 'heuristic', 'exploration'
        self.last_ai_decision = {} # Stores all_scores, input_vector, manifest
        self.ai_update_event = threading.Event()
        
        # UI positioning
        self.text_frame_top = int(88 * self.scale_factor_x)
        self.text_frame_bottom = int(159 * self.scale_factor_y)
        self.y_text = self.text_frame_top + 2
        
        # Statistics
        self.battery_status = 26
        self.battery_percent = 26
        self.battery_voltage = None
        self.battery_is_charging = False
        self.battery_present = False
        self.battery_source = "unknown"
        self.battery_last_update = 0.0
        self.battery_probe_failures = 0
        self.target_count = 0
        self.port_count = 0
        self.vuln_count = 0
        self.cred_count = 0
        self.data_count = 0
        self.zombie_count = 0
        self.coin_count = 0
        self.level_count = 0
        self.network_kb_count = 0
        self.attacks_count = 0
        
        # System Resources (Cached)
        self.system_cpu = 0
        self.system_mem = 0
        self.system_mem_used = 0
        self.system_mem_total = 0
        
        # Display control
        self.show_first_image = True
        
        # Threading Containers
        self.running_scripts = {}
        self.display_runtime_metrics = {}
        self.health_metrics = {}
        
        # URLs
        self.github_version_url = "https://raw.githubusercontent.com/infinition/Bjorn/main/version.txt"

    def initialize_statistics(self):
        """Initialize statistics in database"""
        try:
            self.db.ensure_stats_initialized()
            self.db.update_livestats(
                total_open_ports=0,
                alive_hosts_count=0,
                all_known_hosts_count=0,
                vulnerabilities_count=0
            )
            logger.info("Statistics initialized in database")
        except Exception as e:
            logger.error(f"Error initializing statistics: {e}")

    def delete_web_console_log(self):
        """Delete and recreate web console log file"""
        try:
            if os.path.exists(self.web_console_log):
                os.remove(self.web_console_log)
                logger.info(f"Deleted web console log: {self.web_console_log}")
            
            # Recreate empty file
            open(self.web_console_log, 'a').close()
            
        except Exception as e:
            logger.error(f"Error managing web console log: {e}")

    def sync_actions_to_database(self):
        """Sync action definitions from files to database (and keep actions_studio in sync non-destructively)."""
        actions_config = []

        try:
            for filename in os.listdir(self.actions_dir):
                if not filename.endswith(".py") or filename == "__init__.py":
                    continue

                meta = self._extract_action_metadata(os.path.join(self.actions_dir, filename))
                if not meta:
                    continue

                # Defaults
                meta.setdefault("b_action", "normal")
                meta.setdefault("b_priority", 50)
                meta.setdefault("b_timeout", 300)
                meta.setdefault("b_max_retries", 3)
                meta.setdefault("b_cooldown", 0)
                meta.setdefault("b_stealth_level", 5)
                meta.setdefault("b_risk_level", "medium")
                meta.setdefault("b_enabled", 1)

                actions_config.append(meta)

                # Status tracking
                self.status_list.add(meta["b_class"])

            if actions_config:
                self.db.sync_actions(actions_config)
                logger.info(f"Synchronized {len(actions_config)} actions to database")

            # Keep actions_studio aligned
            try:
                self.db._sync_actions_studio_schema_and_rows()
                logger.info("actions_studio schema/rows synced (non-destructive)")
            except Exception as e:
                logger.error(f"actions_studio sync failed: {e}")

        except Exception as e:
            logger.error(f"Error syncing actions to database: {e}")

    def _extract_action_metadata(self, filepath: str) -> Optional[Dict[str, Any]]:
        """Extract action metadata from Python file using AST parsing (Safe)"""
        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                tree = ast.parse(f.read(), filename=filepath)
            
            meta = {}
            for node in tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    if isinstance(node.targets[0], ast.Name):
                        key = node.targets[0].id
                        if key.startswith("b_"):
                            try:
                                val = ast.literal_eval(node.value)
                                meta[key] = val
                            except (ValueError, SyntaxError):
                                pass
            
            # Set default module name if not specified
            if "b_module" not in meta:
                meta["b_module"] = os.path.splitext(os.path.basename(filepath))[0]
            
            return meta if meta.get("b_class") else None
            
        except Exception as e:
            logger.error(f"Failed to parse {filepath}: {e}")
            return None

    def initialize_database(self):
        """Initialize database schema"""
        logger.info("Initializing database schema")
        try:
            self.db.ensure_schema()
            
            # Update status list from database if empty
            if not self.status_list:
                actions = self.db.list_actions()
                for action in actions:
                    if action.get("b_class"):
                        self.status_list.add(action["b_class"])
                        
        except Exception as e:
            logger.error(f"Error initializing database: {e}")

    def load_config(self):
        """Load configuration from DB (Thread-safe)"""
        with self.config_lock: 
            try:
                cfg = self.db.get_config()
                if not cfg:
                    self.db.save_config(self.default_config.copy())
                    cfg = self.db.get_config() or {}
                self.config.update(cfg)
                for key, value in self.config.items():
                    setattr(self, key, value)
            except Exception as e:
                logger.error(f"Error loading configuration: {e}")

    def save_config(self):
        """Save configuration to DB (Thread-safe)"""
        with self.config_lock:
            try:
                self.db.save_config(self.config)
                self.invalidate_config_cache()
            except Exception as e:
                logger.error(f"Error saving configuration: {e}")

    def load_fonts(self):
        """Load font resources"""
        try:
            logger.info("Loading fonts")
            
            # Font paths
            self.default_font_path = os.path.join(self.fonts_dir, self.defaultfont)
            self.default_font_title_path = os.path.join(self.fonts_dir, self.defaultfonttitle)
            
            # Load font sizes
            self.font_arial14 = self._load_font(self.default_font_path, 14)
            self.font_arial11 = self._load_font(self.default_font_path, 11)
            self.font_arial10 = self._load_font(self.default_font_path, 10)
            self.font_arial9 = self._load_font(self.default_font_path, 9)
            self.font_arial8 = self._load_font(self.default_font_path, 8)
            self.font_arial7 = self._load_font(self.default_font_path, 7)
            self.font_arialbold = self._load_font(self.default_font_path, 12)
            
            # Viking font for title
            self.font_viking_path = self.default_font_title_path
            self.font_viking = self._load_font(self.default_font_title_path, 13)
            
            logger.info("Fonts loaded successfully")
            
        except Exception as e:
            logger.error(f"Error loading fonts: {e}")
            raise

    def _load_font(self, font_path: str, size: int):
        """Load a single font with specified size"""
        try:
            return ImageFont.truetype(font_path, size)
        except Exception as e:
            logger.error(f"Error loading font {font_path}: {e}")
            return ImageFont.load_default()

    # =========================================================================
    # IMAGE MANAGEMENT (LAZY LOADING EDITION)
    # Optimizes RAM by indexing paths instead of loading all pixels at once
    # =========================================================================

    def load_images(self):
        """Initialize images: load static ones to RAM, index status paths for lazy loading"""
        try:
            logger.info("SharedData: Indexing images (Lazy Loading Mode)")
            self.bjorn_status_image = None
            
            # Load static images (keep in RAM, they are small and used constantly)
            self._load_static_images()
            
            # Set default character from static images
            self.bjorn_character = getattr(self, 'bjorn1', None)
            
            # Index status images (don't load pixels yet)
            self._index_status_images()
            
            # Calculate display positions
            self._calculate_image_positions()
            
            logger.info("Images indexed successfully")
            
        except Exception as e:
            logger.error(f"Error indexing images: {e}")
            raise

    def _load_static_images(self):
        """Load static UI images into RAM"""
        static_images = {
            'bjorn1': 'bjorn1.bmp',
            'port': 'port.bmp',
            'frise': 'frise.bmp',
            'target': 'target.bmp',
            'vuln': 'vuln.bmp',
            'connected': 'connected.bmp',
            'bluetooth': 'bluetooth.bmp',
            'wifi': 'wifi.bmp',
            'ethernet': 'ethernet.bmp',
            'usb': 'usb.bmp',
            'level': 'level.bmp',
            'cred': 'cred.bmp',
            'attack': 'attack.bmp',
            'attacks': 'attacks.bmp',
            'gold': 'gold.bmp',
            'networkkb': 'networkkb.bmp',
            'zombie': 'zombie.bmp',
            'data': 'data.bmp',
            'money': 'money.bmp',
            'zombie_status': 'zombie.bmp',
            'battery0': '0.bmp',
            'battery25': '25.bmp',
            'battery50': '50.bmp',
            'battery75': '75.bmp',
            'battery100': '100.bmp',
            'battery_charging': 'charging1.bmp'
        }
        
        for attr_name, filename in static_images.items():
            image_path = os.path.join(self.static_images_dir, filename)
            setattr(self, attr_name, self._load_image(image_path))

    def _index_status_images(self):
        """Index file paths for animations instead of loading them into RAM"""
        self.image_series_paths = {} 
        self.main_status_paths = {}
        
        try:
            # Load images from database actions
            actions = self.db.list_actions()
            for action in actions:
                b_class = action.get('b_class')
                if b_class:
                    # Index main status image path
                    status_dir = os.path.join(self.status_images_dir, b_class)
                    main_img_path = os.path.join(status_dir, f'{b_class}.bmp')
                    self.main_status_paths[b_class] = main_img_path
                    
                    self.status_list.add(b_class)
                    
                    # Index animation frames paths
                    self.image_series_paths[b_class] = []
                    if os.path.isdir(status_dir):
                        for image_name in os.listdir(status_dir):
                            if image_name.endswith('.bmp') and re.search(r'\d', image_name):
                                self.image_series_paths[b_class].append(os.path.join(status_dir, image_name))
                    else:
                        # Create missing directory safely
                        try:
                            os.makedirs(status_dir, exist_ok=True)
                        except: pass

            logger.info(f"Indexed {len(self.image_series_paths)} status categories")
                    
        except Exception as e:
            logger.error(f"Error indexing status images: {e}")

    def _load_image(self, image_path: str) -> Optional[Image.Image]:
        """Load a single image file safely and release file descriptor immediately"""
        try:
            if not os.path.exists(image_path):
                # Only warn if it's not a lazy-load check
                return None
            
            # Force pixel load and detach from file handle to avoid FD leaks.
            with Image.open(image_path) as img:
                loaded = img.copy()
            return loaded
        except Exception as e:
            logger.error(f"Error loading image {image_path}: {e}")
            return None

    def _calculate_image_positions(self):
        """Calculate image positions for display centering"""
        if hasattr(self, 'bjorn1') and self.bjorn1:
            self.x_center1 = (self.width - self.bjorn1.width) // 2
            self.y_bottom1 = self.height - self.bjorn1.height

    def update_bjorn_status(self):
        """Lazy Load the main status image when status changes"""
        try:
            # Try to load from indexed paths
            path = self.main_status_paths.get(self.bjorn_orch_status)
            
            if path and os.path.exists(path):
                self.bjorn_status_image = self._load_image(path)
            else:
                # Fallback to attack image
                logger.warning(f"Image for status {self.bjorn_orch_status} not found, using default")
                self.bjorn_status_image = self.attack
                
        except Exception:
            self.bjorn_status_image = self.attack
        
        self.bjorn_status_text = self.bjorn_orch_status

    def update_image_randomizer(self):
        """Select random image path and Lazy Load it"""
        try:
            status = self.bjorn_status_text
            
            # Get list of paths for current status
            paths = self.image_series_paths.get(status)
            
            # Fallback to IDLE if empty or non-existent
            if not paths and "IDLE" in self.image_series_paths:
                paths = self.image_series_paths["IDLE"]
            
            if not paths:
                self.imagegen = None
                return

            # Select random file path
            random_path = random.choice(paths)
            
            # Load specific frame
            self.imagegen = self._load_image(random_path)
            
            if self.imagegen:
                # Calculate centering
                self.x_center = (self.width - self.imagegen.width) // 2
                self.y_bottom = self.height - self.imagegen.height
            
        except Exception as e:
            logger.error(f"Error updating image randomizer: {e}")
            self.imagegen = None

    def wrap_text(self, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
        """Wrap text to fit within specified width — boucle infinie protégée."""
        try:
            lines = []
            words = text.split()
            if not words:
                return [""]

            while words:
                line = []
                # Toujours ajouter au moins 1 mot même s'il dépasse max_width
                # sinon si le mot seul > max_width → boucle infinie garantie
                line.append(words.pop(0))
                while words and font.getlength(' '.join(line + [words[0]])) <= max_width:
                    line.append(words.pop(0))
                lines.append(' '.join(line))

            return lines if lines else [text]

        except Exception as e:
            logger.error(f"Error wrapping text: {e}")
            return [text]

    def update_stats(self):
        """Update calculated statistics based on formulas"""
        self.coin_count = int(
            self.network_kb_count * 5 + 
            self.cred_count * 5 + 
            self.data_count * 5 + 
            self.zombie_count * 10 + 
            self.attacks_count * 5 + 
            self.vuln_count * 2
        )
        
        self.level_count = int(
            self.network_kb_count * 0.1 + 
            self.cred_count * 0.2 + 
            self.data_count * 0.1 + 
            self.zombie_count * 0.5 + 
            self.attacks_count + 
            self.vuln_count * 0.01
        )

    # =========================================================================
    # BATTERY MANAGEMENT (ROBUST PISUGAR/SYSFS LOGIC)
    # =========================================================================

    def _extract_first_float(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        try:
            # PiSugar responses may use either '.' or ',' as decimal separator.
            text_normalized = str(text).replace(",", ".")
            m = re.search(r"[-+]?\d+(?:\.\d+)?", text_normalized)
            if not m:
                return None
            return float(m.group(0))
        except Exception:
            return None

    def _parse_bool_reply(self, text: Optional[str]) -> Optional[bool]:
        if text is None:
            return None
        s = str(text).strip().lower()
        if "true" in s:
            return True
        if "false" in s:
            return False
        n = self._extract_first_float(s)
        if n is None:
            return None
        return bool(int(n))

    def _pisugar_send_command(self, command: str, timeout_s: float = 1.0) -> Optional[str]:
        if not self.config.get("pisugar_enabled", True):
            return None

        timeout_s = float(self.config.get("pisugar_timeout_s", timeout_s))
        payload = (command.strip() + "\n").encode("utf-8")

        sock_path = str(self.config.get("pisugar_socket_path", "/tmp/pisugar-server.sock"))
        try:
            if os.path.exists(sock_path):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(timeout_s)
                    s.connect(sock_path)
                    s.sendall(payload)
                    return s.recv(1024).decode("utf-8", errors="ignore").strip()
        except Exception:
            pass

        host = str(self.config.get("pisugar_tcp_host", "127.0.0.1"))
        port = int(self.config.get("pisugar_tcp_port", 8423))
        try:
            with socket.create_connection((host, port), timeout=timeout_s) as s:
                s.settimeout(timeout_s)
                s.sendall(payload)
                return s.recv(1024).decode("utf-8", errors="ignore").strip()
        except Exception:
            return None

    def _pisugar_battery_probe(self) -> Optional[Dict[str, Any]]:
        battery_raw = self._pisugar_send_command("get battery")
        if not battery_raw:
            return None

        level_float = self._extract_first_float(battery_raw)
        if level_float is None:
            return None
        level_pct = max(0, min(100, int(round(level_float))))

        voltage_raw = self._pisugar_send_command("get battery_v")
        plugged_raw = self._pisugar_send_command("get battery_power_plugged")
        allow_charging_raw = self._pisugar_send_command("get battery_allow_charging")
        charging_raw = self._pisugar_send_command("get battery_charging")

        charging = self._parse_bool_reply(charging_raw)
        if charging is None:
            plugged = self._parse_bool_reply(plugged_raw)
            allow_charging = self._parse_bool_reply(allow_charging_raw)
            if plugged is not None and allow_charging is not None:
                charging = plugged and allow_charging
            elif plugged is not None:
                charging = plugged
            else:
                charging = False

        voltage = self._extract_first_float(voltage_raw)

        return {
            "present": True,
            "level_pct": level_pct,
            "charging": bool(charging),
            "voltage": voltage,
            "source": "pisugar",
        }

    def _sysfs_battery_probe(self) -> Optional[Dict[str, Any]]:
        try:
            base = "/sys/class/power_supply"
            if not os.path.isdir(base):
                return None

            bat_dir = None
            for entry in os.listdir(base):
                if entry.startswith("BAT"):
                    bat_dir = os.path.join(base, entry)
                    break
            if not bat_dir:
                return None

            cap_path = os.path.join(bat_dir, "capacity")
            status_path = os.path.join(bat_dir, "status")
            volt_path = os.path.join(bat_dir, "voltage_now")

            level_pct = None
            if os.path.exists(cap_path):
                with open(cap_path, "r", encoding="utf-8") as f:
                    cap_txt = f.read().strip()
                    if cap_txt.isdigit():
                        level_pct = max(0, min(100, int(cap_txt)))
            if level_pct is None:
                return None

            charging = False
            if os.path.exists(status_path):
                with open(status_path, "r", encoding="utf-8") as f:
                    st = f.read().strip().lower()
                    charging = st.startswith("char") or st.startswith("full")

            voltage = None
            if os.path.exists(volt_path):
                with open(volt_path, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                    n = self._extract_first_float(raw)
                    if n is not None:
                        # Common sysfs format: microvolts
                        voltage = n / 1_000_000 if n > 1000 else n

            return {
                "present": True,
                "level_pct": level_pct,
                "charging": bool(charging),
                "voltage": voltage,
                "source": "sysfs",
            }
        except Exception:
            return None

    def update_battery_status(self) -> bool:
        """
        Refresh battery metrics from PiSugar (preferred) or sysfs fallback.
        battery_status convention:
          - 0..100 => discharge level
          - 101    => charging icon on EPD
        """
        now = time.time()
        failures_before_none = max(1, int(self.config.get("battery_probe_failures_before_none", 4)))
        grace_seconds = max(0.0, float(self.config.get("battery_probe_grace_seconds", 120)))
        
        data = self._pisugar_battery_probe() or self._sysfs_battery_probe()
        
        if not data:
            self.battery_probe_failures = int(getattr(self, "battery_probe_failures", 0)) + 1
            last_ok = float(getattr(self, "battery_last_update", 0.0))
            had_recent_sample = last_ok > 0 and (now - last_ok) <= grace_seconds
            
            if had_recent_sample and bool(getattr(self, "battery_present", False)):
                return False

            if self.battery_probe_failures >= failures_before_none:
                self.battery_present = False
                self.battery_is_charging = False
                self.battery_source = "none"
                self.battery_status = 0
                self.battery_last_update = now
            return False
            
        recovered_after_failures = self.battery_probe_failures > 0
        self.battery_probe_failures = 0

        level_pct = int(data.get("level_pct", self.battery_percent))
        charging = bool(data.get("charging", False))
        voltage = data.get("voltage")

        self.battery_present = bool(data.get("present", True))
        self.battery_percent = max(0, min(100, level_pct))
        self.battery_is_charging = charging
        self.battery_voltage = float(voltage) if voltage is not None else None
        self.battery_source = str(data.get("source", "unknown"))
        self.battery_last_update = now
        self.battery_status = 101 if charging else self.battery_percent
        
        if recovered_after_failures:
            logger.info(f"Battery probe recovered: source={self.battery_source}")

        return True

    def debug_print(self, message: str):
        """Print debug message if debug mode is enabled"""
        if self.config.get('debug_mode', False):
            logger.debug(message)

    def get_status(self) -> Dict[str, Any]:
        """Get current system status (Thread-safe)"""
        with self.status_lock:
            return self.curr_status.copy()

    def update_status(self, status: str, details: str = ""):
        """Update system status (Thread-safe)"""
        with self.status_lock:
            self.curr_status = {
                "status": status,
                "details": details,
                "timestamp": time.time()
            }

    def log_milestone(self, action_name: str, phase: str, details: str = ""):
        """
        Broadcasting real-time milestones to the web console and logs.
        Used for granular progress tracking in the UI.
        """
        milestone_data = {
            "action": action_name,
            "phase": phase,
            "details": details,
            "timestamp": datetime.now().strftime("%H:%M:%S")
        }
        logger.info(f"[MILESTONE] {json.dumps(milestone_data)}")
        
        # Also update internal state for immediate access
        self.active_action = action_name
        self.bjorn_status_text2 = f"{phase}: {details}" if details else phase
