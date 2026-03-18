"""runtime_state_updater.py - Background thread keeping display-facing state fresh."""

import logging
import os
import random
import subprocess
import threading
import time
import gc
from collections import OrderedDict
from typing import Dict, Optional, Tuple

import psutil

from comment import CommentAI
from logger import Logger

logger = Logger(name="runtime_state_updater.py", level=logging.DEBUG)


class RuntimeStateUpdater(threading.Thread):
    """
    Centralized runtime state updater.
    Keeps display-facing data fresh in background so display loop can stay render-only.
    """

    def __init__(self, shared_data):
        super().__init__(daemon=True, name="RuntimeStateUpdater")
        self.shared_data = shared_data
        self._stop_event = threading.Event()

        cfg = getattr(self.shared_data, "config", {}) or {}

        # Tight loops create allocator churn on Pi; keep these configurable.
        self._tick_s = max(0.2, float(cfg.get("runtime_tick_s", 1.0)))
        self._stats_interval_s = max(
            2.0,
            float(getattr(self.shared_data, "shared_update_interval", cfg.get("shared_update_interval", 10))),
        )
        self._system_interval_s = 4.0
        self._comment_poll_interval_s = max(1.0, float(cfg.get("runtime_comment_poll_interval_s", 2.0)))
        self._network_interval_s = 30.0
        self._connection_interval_s = 10.0
        self._data_count_interval_s = 60.0
        self._battery_interval_s = 10.0
        self._status_image_interval_s = max(1.0, float(cfg.get("runtime_status_image_interval_s", 2.0)))
        self._image_min_delay_s = max(0.5, float(getattr(self.shared_data, "image_display_delaymin", 2)))
        self._image_max_delay_s = max(
            self._image_min_delay_s,
            float(getattr(self.shared_data, "image_display_delaymax", 8)),
        )
        self._data_count_path = str(getattr(self.shared_data, "data_stolen_dir", ""))
        self._image_cache_limit = 12

        # Optional housekeeping (off by default)
        self._gc_interval_s = max(0.0, float(cfg.get("runtime_gc_interval_s", 0.0)))
        self._last_gc = 0.0

        self._last_stats = 0.0
        self._last_system = 0.0
        self._last_comment = 0.0
        self._last_network = 0.0
        self._last_connections = 0.0
        self._last_data_count = 0.0
        self._last_battery = 0.0
        self._last_status_image = 0.0
        self._next_anim = 0.0
        self._last_status_image_key = None
        self._image_cache: OrderedDict[str, object] = OrderedDict()
        self._image_cache_lock = threading.Lock()

        self.comment_ai = CommentAI()

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

        try:
            self._initialize_fast_defaults()
            self._warmup_once()

            while not self._stop_event.is_set() and not self.shared_data.should_exit:
                now = time.time()
                try:
                    if self._gc_interval_s and (now - self._last_gc) >= self._gc_interval_s:
                        # Helps long-running Pi processes reduce allocator fragmentation.
                        gc.collect()
                        self._last_gc = now

                    if now - self._last_stats >= self._stats_interval_s:
                        self._update_display_stats()
                        self._last_stats = now

                    if now - self._last_system >= self._system_interval_s:
                        self._update_system_metrics()
                        self._last_system = now

                    if now - self._last_comment >= self._comment_poll_interval_s:
                        self._update_comment()
                        self._last_comment = now

                    if now - self._last_network >= self._network_interval_s:
                        self._update_network_info()
                        self._last_network = now

                    if now - self._last_connections >= self._connection_interval_s:
                        self._update_connection_flags()
                        self._last_connections = now

                    if now - self._last_data_count >= self._data_count_interval_s:
                        self._update_data_count()
                        self._last_data_count = now

                    if now - self._last_battery >= self._battery_interval_s:
                        self._update_battery()
                        self._last_battery = now

                    if now - self._last_status_image >= self._status_image_interval_s:
                        self._update_status_image()
                        self._last_status_image = now

                    if now >= self._next_anim:
                        self._update_main_animation_image()
                        self._next_anim = now + random.uniform(self._image_min_delay_s, self._image_max_delay_s)

                except Exception as exc:
                    logger.error(f"RuntimeStateUpdater loop error: {exc}")

                self._stop_event.wait(self._tick_s)
        finally:
            self._close_image_cache()

    def _warmup_once(self):
        try:
            self._update_network_info()
            self._update_connection_flags()
            self._update_battery()
            self._update_display_stats()
            self._update_system_metrics()
            self._update_status_image()
            self._update_main_animation_image()
        except Exception as exc:
            logger.error(f"RuntimeStateUpdater warmup error: {exc}")

    def _initialize_fast_defaults(self):
        if not getattr(self.shared_data, "bjorn_status_image", None):
            self.shared_data.bjorn_status_image = getattr(self.shared_data, "attack", None)
        if not getattr(self.shared_data, "bjorn_character", None):
            self.shared_data.bjorn_character = getattr(self.shared_data, "bjorn1", None)
        if not hasattr(self.shared_data, "current_ip"):
            self.shared_data.current_ip = "No IP"
        if not hasattr(self.shared_data, "current_ssid"):
            self.shared_data.current_ssid = "No Wi-Fi"

    def _update_display_stats(self):
        stats = self.shared_data.db.get_display_stats()
        self.shared_data.port_count = int(stats.get("total_open_ports", 0))
        self.shared_data.target_count = int(stats.get("alive_hosts_count", 0))
        self.shared_data.network_kb_count = int(stats.get("all_known_hosts_count", 0))
        self.shared_data.vuln_count = int(stats.get("vulnerabilities_count", 0))
        self.shared_data.cred_count = int(stats.get("credentials_count", 0))
        self.shared_data.attacks_count = int(stats.get("actions_count", 0))
        self.shared_data.zombie_count = int(stats.get("zombie_count", 0))
        self.shared_data.update_stats()

    def _update_system_metrics(self):
        self.shared_data.system_cpu = int(psutil.cpu_percent(interval=None))
        vm = psutil.virtual_memory()
        self.shared_data.system_mem = int(vm.percent)
        self.shared_data.system_mem_used = int(vm.total - vm.available)
        self.shared_data.system_mem_total = int(vm.total)

    def _update_comment(self):
        status = getattr(self.shared_data, "bjorn_orch_status", "IDLE") or "IDLE"
        params = getattr(self.shared_data, "comment_params", {}) or {}
        comment = self.comment_ai.get_comment(status, params=params)
        if comment:
            self.shared_data.bjorn_says = comment
            self.shared_data.bjorn_status_text = status

    def _update_network_info(self):
        self.shared_data.current_ip = self._get_ip_address()
        self.shared_data.current_ssid = self._get_ssid()

    def _update_connection_flags(self):
        flags = self._check_all_connections()
        self.shared_data.wifi_connected = bool(flags.get("wifi"))
        self.shared_data.bluetooth_active = bool(flags.get("bluetooth"))
        self.shared_data.ethernet_active = bool(flags.get("ethernet"))
        self.shared_data.usb_active = bool(flags.get("usb"))

    def _update_data_count(self):
        try:
            # Guard: os.walk("") would traverse CWD (very expensive) if path is empty.
            if not self._data_count_path or not os.path.isdir(self._data_count_path):
                self.shared_data.data_count = 0
                return
            total = 0
            for _, _, files in os.walk(self._data_count_path):
                total += len(files)
            self.shared_data.data_count = total
        except Exception as exc:
            logger.error(f"Data count update failed: {exc}")

    def _update_battery(self):
        try:
            self.shared_data.update_battery_status()
        except Exception as exc:
            logger.warning_throttled(
                f"Battery update failed: {exc}",
                key="runtime_state_updater_battery",
                interval_s=120.0,
            )

    def _update_status_image(self):
        status = getattr(self.shared_data, "bjorn_orch_status", "IDLE") or "IDLE"
        if status == self._last_status_image_key and getattr(self.shared_data, "bjorn_status_image", None) is not None:
            return

        path = self.shared_data.main_status_paths.get(status)
        img = self._load_cached_image(path)
        if img is None:
            img = getattr(self.shared_data, "attack", None)
        self.shared_data.bjorn_status_image = img
        self.shared_data.bjorn_status_text = status
        self._last_status_image_key = status

    def _update_main_animation_image(self):
        status = getattr(self.shared_data, "bjorn_status_text", "IDLE") or "IDLE"
        paths = self.shared_data.image_series_paths.get(status)
        if not paths:
            paths = self.shared_data.image_series_paths.get("IDLE") or []
        if not paths:
            return

        selected = random.choice(paths)
        img = self._load_cached_image(selected)
        if img is not None:
            self.shared_data.bjorn_character = img

    def _load_cached_image(self, path: Optional[str]):
        if not path:
            return None
        try:
            with self._image_cache_lock:
                if path in self._image_cache:
                    img = self._image_cache.pop(path)
                    self._image_cache[path] = img
                    return img

            img = self.shared_data._load_image(path)
            if img is None:
                return None

            with self._image_cache_lock:
                self._image_cache[path] = img
                while len(self._image_cache) > self._image_cache_limit:
                    # Important: cached PIL images are also referenced by display/web threads.
                    # Closing here can invalidate an image still in use and trigger:
                    # ValueError: Operation on closed image
                    # We only drop our cache reference and let GC reclaim when no refs remain.
                    self._image_cache.popitem(last=False)
            return img
        except Exception as exc:
            logger.error(f"Image cache load failed for {path}: {exc}")
            return None

    def _close_image_cache(self):
        try:
            with self._image_cache_lock:
                # Drop references only; avoid closing shared PIL objects that may still be read
                # by other threads during shutdown sequencing.
                self._image_cache.clear()
        except Exception:
            pass

    def _get_ip_address(self) -> str:
        iface_list = self._as_list(
            getattr(self.shared_data, "ip_iface_priority", ["wlan0", "eth0"]),
            default=["wlan0", "eth0"],
        )
        for iface in iface_list:
            try:
                result = subprocess.run(
                    # Keep output small; we only need the IPv4 address.
                    ["ip", "-4", "-o", "addr", "show", "dev", iface],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=2,
                )
                if result.returncode != 0:
                    continue
                for line in result.stdout.split("\n"):
                    parts = line.split()
                    if "inet" not in parts:
                        continue
                    idx = parts.index("inet")
                    if idx + 1 < len(parts):
                        return parts[idx + 1].split("/")[0]
            except Exception:
                continue
        return "No IP"

    def _get_ssid(self) -> str:
        try:
            result = subprocess.run(
                ["iwgetid", "-r"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.strip() or "No Wi-Fi"
        except Exception:
            pass
        return "No Wi-Fi"

    def _check_all_connections(self) -> Dict[str, bool]:
        results = {"wifi": False, "bluetooth": False, "ethernet": False, "usb": False}
        try:
            ip_neigh = subprocess.run(
                ["ip", "neigh", "show"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
            neigh_output = ip_neigh.stdout if ip_neigh.returncode == 0 else ""

            iwgetid = subprocess.run(
                ["iwgetid", "-r"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1,
            )
            results["wifi"] = bool(iwgetid.returncode == 0 and iwgetid.stdout.strip())

            bt_ifaces = self._as_list(
                getattr(self.shared_data, "neigh_bluetooth_ifaces", ["pan0", "bnep0"]),
                default=["pan0", "bnep0"],
            )
            results["bluetooth"] = any(f"dev {iface}" in neigh_output for iface in bt_ifaces)

            eth_iface = self._as_str(
                getattr(self.shared_data, "neigh_ethernet_iface", "eth0"),
                "eth0",
            )
            results["ethernet"] = f"dev {eth_iface}" in neigh_output

            usb_iface = self._as_str(
                getattr(self.shared_data, "neigh_usb_iface", "usb0"),
                "usb0",
            )
            results["usb"] = f"dev {usb_iface}" in neigh_output
        except Exception as exc:
            logger.error(f"Connection check failed: {exc}")
        return results

    def _as_list(self, value, default=None):
        if default is None:
            default = []
        try:
            if isinstance(value, list):
                return value
            if isinstance(value, tuple):
                return list(value)
            if isinstance(value, str):
                return [x.strip() for x in value.split(",") if x.strip()]
            if value is None:
                return default
            return list(value)
        except Exception:
            return default

    def _as_str(self, value, default="") -> str:
        if isinstance(value, str):
            return value
        if value is None:
            return default
        try:
            return str(value)
        except Exception:
            return default
