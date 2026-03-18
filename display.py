"""display.py - E-paper display renderer and web screenshot generator."""

import math
import threading
import time
import os
import signal
import logging
import sys
import traceback
from typing import Dict, List, Optional, Any, Tuple
from PIL import Image, ImageDraw, ImageFont
from init_shared import shared_data
from logger import Logger
from display_layout import DisplayLayout

logger = Logger(name="display.py", level=logging.DEBUG)


class DisplayUpdateController:
    """
    Single-writer EPD update queue. 
    Ensures only one thread accesses the SPI bus at a time.
    Drops older frames if the display is busy (Frame Skipping) to prevent lag.
    """

    def __init__(self, update_fn):
        self.update_fn = update_fn
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._latest_frame: Optional[Image.Image] = None
        self._metrics = {
            "queue_dropped": 0,
            "queue_submitted": 0,
            "processed": 0,
            "failures": 0,
            "last_duration_s": 0.0,
            "last_error": "",
            "busy_since": 0.0,
            "last_update_epoch": 0.0,
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker_loop, 
            daemon=True, 
            name="DisplayUpdateController"
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0):
        self._stop.set()
        self._event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        # Close any residual pending frame
        residual = self._pop_latest_frame()
        if residual is not None:
            try:
                residual.close()
            except Exception:
                pass
        return not bool(self._thread and self._thread.is_alive())

    def submit(self, frame: Image.Image):
        """Submit a new frame. If busy, drop the previous pending frame (Latest-Win strategy)."""
        with self._lock:
            old_frame = self._latest_frame
            if old_frame is not None:
                self._metrics["queue_dropped"] += 1
            self._latest_frame = frame
            self._metrics["queue_submitted"] += 1
        # Close the dropped frame outside the lock to avoid holding it while doing I/O
        if old_frame is not None:
            try:
                old_frame.close()
            except Exception:
                pass
        self._event.set()

    def get_metrics(self) -> Dict[str, Any]:
        with self._lock:
            metrics = dict(self._metrics)
        busy_since = float(metrics.get("busy_since") or 0.0)
        metrics["busy_for_s"] = (time.monotonic() - busy_since) if busy_since else 0.0
        metrics["thread_alive"] = bool(self._thread and self._thread.is_alive())
        return metrics

    def _pop_latest_frame(self) -> Optional[Image.Image]:
        with self._lock:
            frame = self._latest_frame
            self._latest_frame = None
        return frame

    def _set_busy(self, busy: bool):
        with self._lock:
            self._metrics["busy_since"] = time.monotonic() if busy else 0.0

    def _mark_success(self, duration_s: float):
        with self._lock:
            self._metrics["processed"] += 1
            self._metrics["last_duration_s"] = duration_s
            self._metrics["last_update_epoch"] = time.time()
            self._metrics["last_error"] = ""

    def _mark_failure(self, duration_s: float, error: str):
        with self._lock:
            self._metrics["failures"] += 1
            self._metrics["last_duration_s"] = duration_s
            self._metrics["last_error"] = error

    def _worker_loop(self):
        while not self._stop.is_set():
            self._event.wait(timeout=0.5)
            self._event.clear()

            if self._stop.is_set():
                break

            frame = self._pop_latest_frame()
            if frame is None:
                continue

            started = time.monotonic()
            self._set_busy(True)
            try:
                # Execute the actual EPD write
                ok = bool(self.update_fn(frame))
                duration = time.monotonic() - started
                if ok:
                    self._mark_success(duration)
                else:
                    self._mark_failure(duration, "update_fn returned False")
            except Exception as exc:
                duration = time.monotonic() - started
                self._mark_failure(duration, str(exc))
                logger.error(f"EPD update worker failure: {exc}")
            finally:
                self._set_busy(False)
                try:
                    frame.close()
                except Exception:
                    pass


class Display:
    """
    Optimized display manager with robust error handling and recovery.
    Decouples rendering (CPU) from displaying (SPI/IO) to ensure stability on Pi Zero 2.
    """

    RECOVERY_COOLDOWN = 60.0         # Min time between hard resets

    # Circuit breaker
    MAX_CONSECUTIVE_FAILURES = 6     # Disable EPD after N failures

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.config = self.shared_data.config
        self.epd_enabled = self.config.get("epd_enabled", True)

        # Initialize display layout engine
        self.layout = DisplayLayout(self.shared_data)
        self.shared_data.display_layout = self.layout

        self.epd = self.shared_data.epd if self.epd_enabled else None

        if self.config.get("epd_type") == "epd2in13_V2":
            self.shared_data.width = 120
        else:
            self.shared_data.width = self.shared_data.width

        # Recovery tracking
        self.last_successful_update = time.time()
        self.last_recovery_attempt = 0
        self.consecutive_failures = 0
        self.total_updates = 0
        self.failed_updates = 0
        self.retry_attempts = 0
        self.reinit_attempts = 0
        self.watchdog_stuck_count = 0
        self.headless_reason = ""

        # EPD runtime controls
        self.epd_watchdog_timeout = float(self.config.get("epd_watchdog_timeout", 45))
        self.RECOVERY_COOLDOWN = float(self.config.get("epd_recovery_cooldown", 60))
        self.epd_error_backoff = float(self.config.get("epd_error_backoff", 2))
        self._partial_mode_ready = False
        self._epd_mode_lock = threading.Lock()
        self._recovery_lock = threading.Lock()
        self._recovery_in_progress = False
        self._watchdog_last_log = 0.0
        self._last_full_refresh = time.time()
        
        # Asynchronous Controller
        self.display_controller = DisplayUpdateController(self._process_epd_frame)

        # Screen configuration
        self.screen_reversed = self.shared_data.screen_reversed
        self.web_screen_reversed = self.shared_data.web_screen_reversed

        # Display name
        self.bjorn_name = self.shared_data.bjorn_name
        self.previous_bjorn_name = None
        self.calculate_font_to_fit()

        # Full refresh settings
        self.fullrefresh_activated = self.shared_data.fullrefresh_activated
        self.fullrefresh_delay = self.shared_data.fullrefresh_delay

        # NEW: comment wrap/layout cache + throttle
        self._comment_layout_cache = {"key": None, "lines": [], "ts": 0.0}
        self._comment_layout_min_interval = max(0.8, float(self.shared_data.screen_delay))
        self._last_screenshot_time = 0
        self._screenshot_interval_s = max(1.0, float(self.config.get("web_screenshot_interval_s", 4.0)))

        # Initialize display
        try:
            if self.epd_enabled:
                self.shared_data.epd.init_full_update()
                self._partial_mode_ready = False
                logger.info("EPD display initialization complete")

                if self.shared_data.showstartupipssid:
                    ip_address = getattr(self.shared_data, "current_ip", "No IP")
                    ssid = getattr(self.shared_data, "current_ssid", "No Wi-Fi")
                    self.display_startup_ip(ip_address, ssid)
                    time.sleep(self.shared_data.startup_splash_duration)
            else:
                logger.info("EPD display disabled - running in web-only mode")

        except Exception as e:
            logger.error(f"Error during display initialization: {e}")
            if self.epd_enabled:
                # If EPD was supposed to be enabled but failed, raise to alert supervisor
                raise
            else:
                logger.warning("EPD initialization failed but continuing in web-only mode")

        self.shared_data.bjorn_status_text2 = "Awakening..."
        try:
            self.shared_data.update_battery_status()
        except Exception as e:
            logger.warning_throttled(
                f"Initial battery probe failed: {e}",
                key="display_initial_battery_probe",
                interval_s=120.0,
            )

        self.display_controller.start()

    # ---- Positioning helpers ----

    def px(self, x_ref: int) -> int:
        return int(x_ref * self.shared_data.width / self.shared_data.ref_width)

    def py(self, y_ref: int) -> int:
        return int(y_ref * self.shared_data.height / self.shared_data.ref_height)

    # ---- Font management ----

    def calculate_font_to_fit(self):
        default_font_size = 13
        default_font_path = self.shared_data.font_viking_path
        default_font = ImageFont.truetype(default_font_path, default_font_size)
        bbox = default_font.getbbox("BJORN")
        max_text_width = bbox[2] - bbox[0]

        self.font_to_use = self.get_font_to_fit(
            self.bjorn_name, default_font_path, max_text_width, default_font_size
        )

    def get_font_to_fit(self, text: str, font_path: str, max_width: int, max_font_size: int):
        font_size = max_font_size
        font = ImageFont.truetype(font_path, font_size)
        bbox = font.getbbox(text)
        text_width = bbox[2] - bbox[0]

        while text_width > max_width and font_size > 5:
            font_size -= 1
            font = ImageFont.truetype(font_path, font_size)
            bbox = font.getbbox(text)
            text_width = bbox[2] - bbox[0]

        return font

    def _pad_for_v2(self, img: Image.Image) -> Image.Image:
        if self.config.get("epd_type") == "epd2in13_V2" and img.size == (120, 250):
            padded = Image.new('1', (122, 250), 1)
            padded.paste(img, (1, 0))
            img.close()
            return padded
        return img

    def display_startup_ip(self, ip_address: str, ssid: str):
        if not self.epd_enabled:
            logger.debug("Skipping EPD startup display (EPD disabled)")
            return

        try:
            image = Image.new('1', (self.shared_data.width, self.shared_data.height), 255)
            draw = ImageDraw.Draw(image)

            title_pos = self.layout.get('title')
            draw.text((self.px(title_pos.get('x', 37)), self.py(title_pos.get('y', 5))), "BJORN", font=self.shared_data.font_viking, fill=0)

            message = f"Awakening...\nIP: {ip_address}"
            draw.text(
                (self.px(10), int(self.shared_data.height / 2)),
                message, font=self.shared_data.font_arial14, fill=0
            )

            draw.text(
                (self.px(10), int(self.shared_data.height / 2) + 40),
                f"SSID: {ssid}", font=self.shared_data.font_arial9, fill=0
            )

            draw.rectangle((0, 1, self.shared_data.width - 1, self.shared_data.height - 1), outline=0)

            if self.screen_reversed:
                rotated = image.transpose(Image.ROTATE_180)
                image.close()
                image = rotated

            image = self._pad_for_v2(image)

            self.shared_data.epd.display_partial(image)
            if self.shared_data.double_partial_refresh:
                self.shared_data.epd.display_partial(image)

            logger.info(f"Displayed startup IP: {ip_address}, SSID: {ssid}")

        except Exception as e:
            logger.error(f"Error displaying startup IP: {e}")
            if 'image' in locals() and image:
                try: image.close()
                except: pass
        finally:
            if 'image' in locals() and image:
                try: image.close()
                except: pass

    def _as_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except:
            return default

    def get_frise_position(self) -> Tuple[int, int]:
        frise = self.layout.get('frise')
        if frise:
            # Layout-driven frise position; shared_data overrides still honoured
            display_type = self.config.get("epd_type", "default")
            if display_type == "epd2in7":
                x = self._as_int(getattr(self.shared_data, "frise_epd2in7_x", frise.get('x', 50)), frise.get('x', 50))
                y = self._as_int(getattr(self.shared_data, "frise_epd2in7_y", frise.get('y', 160)), frise.get('y', 160))
            else:
                x = self._as_int(getattr(self.shared_data, "frise_default_x", frise.get('x', 0)), frise.get('x', 0))
                y = self._as_int(getattr(self.shared_data, "frise_default_y", frise.get('y', 160)), frise.get('y', 160))
        else:
            # Fallback to original hardcoded logic
            display_type = self.config.get("epd_type", "default")
            if display_type == "epd2in7":
                x = self._as_int(getattr(self.shared_data, "frise_epd2in7_x", 50), 50)
                y = self._as_int(getattr(self.shared_data, "frise_epd2in7_y", 160), 160)
            else:
                x = self._as_int(getattr(self.shared_data, "frise_default_x", 0), 0)
                y = self._as_int(getattr(self.shared_data, "frise_default_y", 160), 160)

        return self.px(x), self.py(y)

    def clear_screen(self):
        if self.epd_enabled:
            try:
                self.shared_data.epd.clear()
            except Exception as e:
                logger.error(f"Error clearing EPD: {e}")
        else:
            logger.debug("Skipping EPD clear (EPD disabled)")

    # ========================================================================
    # MAIN DISPLAY LOOP WITH ROBUST ERROR HANDLING
    # ========================================================================

    def run(self):
        """Main display loop. Rendering is decoupled from EPD writes."""
        local_error_backoff = 1.0

        try:
            while not self.shared_data.display_should_exit:
                try:
                    image = self._render_display()
                    rotated = None
                    try:
                        if self.screen_reversed:
                            rotated = image.transpose(Image.ROTATE_180)
                            image.close()
                            image = rotated
                            rotated = None
                        
                        image = self._pad_for_v2(image)

                        # Keep web screen responsive even when EPD is degraded.
                        self._save_screenshot(image)

                        if self.epd_enabled:
                            # Submit transfers ownership to DisplayUpdateController
                            self.display_controller.submit(image)
                            image = None # Prevent closure in finally
                        else:
                            image.close()
                            image = None
                    finally:
                        if image:
                            try: image.close()
                            except: pass
                        if rotated:
                            try: rotated.close()
                            except: pass

                    self._check_epd_watchdog()
                    self._publish_display_metrics()
                    local_error_backoff = 1.0

                    time.sleep(self.shared_data.screen_delay)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    logger.error(f"Unexpected error in display loop: {exc}")
                    logger.error(traceback.format_exc())
                    time.sleep(local_error_backoff)
                    local_error_backoff = min(local_error_backoff * 2.0, 10.0)
        finally:
            self._cleanup_display()

    def _process_epd_frame(self, image: Image.Image) -> bool:
        """Single-writer EPD update callback used by DisplayUpdateController."""
        if not self.epd_enabled:
            return True

        try:
            self._display_frame(image)
            self.last_successful_update = time.time()
            self.consecutive_failures = 0
            self.total_updates += 1
            return True
        except Exception as first_error:
            self.retry_attempts += 1
            logger.warning(f"EPD update failed, retrying once: {first_error}")
            time.sleep(min(self.epd_error_backoff, 5.0))

            try:
                self._display_frame(image)
                self.last_successful_update = time.time()
                self.consecutive_failures = 0
                self.total_updates += 1
                return True
            except Exception as second_error:
                return self._handle_epd_failure(second_error)

    def _display_frame(self, image: Image.Image):
        with self._epd_mode_lock:
            if self.fullrefresh_activated:
                now = time.time()
                if now - self._last_full_refresh >= self.fullrefresh_delay:
                    self.shared_data.epd.clear()
                    self._last_full_refresh = now
                    self._partial_mode_ready = False
                    logger.info("Display full refresh completed")

            if not self._partial_mode_ready:
                self.shared_data.epd.init_partial_update()
                self._partial_mode_ready = True

            self.shared_data.epd.display_partial(image)
            if self.shared_data.double_partial_refresh:
                # Keep this behavior intentionally for ghosting mitigation.
                self.shared_data.epd.display_partial(image)

    def _handle_epd_failure(self, error: Exception) -> bool:
        self.failed_updates += 1
        self.consecutive_failures += 1
        logger.error(f"EPD update failed after retry: {error}")

        reinit_ok = self._safe_reinit_epd()
        if reinit_ok:
            logger.warning("EPD reinitialized after update failure")
            return False

        if self.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            self._enter_headless_mode("too many consecutive EPD failures")

        return False

    def _safe_reinit_epd(self) -> bool:
        now = time.time()
        if (now - self.last_recovery_attempt) < self.RECOVERY_COOLDOWN:
            remaining = self.RECOVERY_COOLDOWN - (now - self.last_recovery_attempt)
            logger.warning_throttled(
                f"EPD recovery cooldown active ({remaining:.1f}s remaining)",
                key="display_epd_recovery_cooldown",
                interval_s=10.0,
            )
            return False

        with self._recovery_lock:
            now = time.time()
            if (now - self.last_recovery_attempt) < self.RECOVERY_COOLDOWN:
                return False

            self.last_recovery_attempt = now
            self.reinit_attempts += 1
            self._recovery_in_progress = True

            try:
                self.shared_data.epd.hard_reset()
                self.shared_data.epd.init_full_update()
                self._partial_mode_ready = False
                self.consecutive_failures = 0
                return True
            except Exception as recovery_error:
                logger.error(f"EPD reinit failed: {recovery_error}")
                return False
            finally:
                self._recovery_in_progress = False

    def _enter_headless_mode(self, reason: str):
        if not self.epd_enabled:
            return
        self.epd_enabled = False
        self.headless_reason = reason
        logger.critical(f"EPD disabled (headless mode): {reason}")

    def _check_epd_watchdog(self):
        if not self.epd_enabled:
            return

        metrics = self.display_controller.get_metrics()
        busy_for_s = float(metrics.get("busy_for_s") or 0.0)
        if busy_for_s <= self.epd_watchdog_timeout:
            return

        self.watchdog_stuck_count += 1
        logger.error_throttled(
            f"EPD watchdog: update busy for {busy_for_s:.1f}s (threshold={self.epd_watchdog_timeout}s)",
            key="display_epd_watchdog",
            interval_s=10.0,
        )
        self._attempt_watchdog_recovery()

    def _attempt_watchdog_recovery(self):
        now = time.time()
        if (now - self.last_recovery_attempt) < self.RECOVERY_COOLDOWN:
            return

        if self._recovery_in_progress:
            return

        self.last_recovery_attempt = now
        self._recovery_in_progress = True

        def _recover():
            try:
                # [infinition] Force reset to break any deadlocks if the main thread is stuck
                logger.warning("[infinition] EPD Watchdog: Freeze detected. Initiating FORCED RESET to break potential deadlocks.")
                self.shared_data.epd.hard_reset(force=True)
                self.shared_data.epd.init_full_update()
                self._partial_mode_ready = False
                self.consecutive_failures = 0
                logger.warning("EPD watchdog recovery completed")
            except Exception as exc:
                logger.error(f"EPD watchdog recovery failed: {exc}")
                self._enter_headless_mode("watchdog recovery failed")
            finally:
                self._recovery_in_progress = False

        recovery_thread = threading.Thread(target=_recover, daemon=True, name="EPDWatchdogRecovery")
        recovery_thread.start()
        recovery_thread.join(timeout=10.0)
        if recovery_thread.is_alive():
            self._recovery_in_progress = False
            self._enter_headless_mode("watchdog recovery timed out")

    def _publish_display_metrics(self):
        controller_metrics = self.display_controller.get_metrics()
        epd_manager_metrics = {}
        try:
            if hasattr(self.shared_data, "epd") and hasattr(self.shared_data.epd, "check_health"):
                epd_manager_metrics = self.shared_data.epd.check_health()
        except Exception as exc:
            epd_manager_metrics = {"error": str(exc)}

        metrics = {
            "epd_enabled": int(bool(self.epd_enabled)),
            "headless": int(not bool(self.epd_enabled)),
            "headless_reason": self.headless_reason,
            "total_updates": int(self.total_updates),
            "failed_updates": int(self.failed_updates),
            "consecutive_failures": int(self.consecutive_failures),
            "retry_attempts": int(self.retry_attempts),
            "reinit_attempts": int(self.reinit_attempts),
            "watchdog_stuck_count": int(self.watchdog_stuck_count),
            "last_success_epoch": float(self.last_successful_update),
            "controller": controller_metrics,
            "epd_manager": epd_manager_metrics,
        }
        with self.shared_data.health_lock:
            self.shared_data.display_runtime_metrics = metrics

    def _render_display(self) -> Image.Image:
        """Render complete display image"""
        self.bjorn_name = getattr(self.shared_data, "bjorn_name", self.bjorn_name)
        if self.bjorn_name != self.previous_bjorn_name:
            self.calculate_font_to_fit()
            self.previous_bjorn_name = self.bjorn_name

        image = Image.new('1', (self.shared_data.width, self.shared_data.height), 255)
        try:
            draw = ImageDraw.Draw(image)

            title_pos = self.layout.get('title')
            draw.text((self.px(title_pos.get('x', 37)), self.py(title_pos.get('y', 5))), self.bjorn_name, font=self.font_to_use, fill=0)

            self._draw_connection_icons(image)
            self._draw_battery_status(image)
            self._draw_statistics(image, draw)
            self._draw_system_histogram(image, draw)

            status_pos = self.layout.get('status_image')
            status_img = self.shared_data.bjorn_status_image or self.shared_data.attack
            if status_img is not None:
                image.paste(status_img, (self.px(status_pos.get('x', 3)), self.py(status_pos.get('y', 52))))

            self._draw_status_text(draw)
            self._draw_decorations(image, draw)
            self._draw_comment_text(draw)

            main_img = getattr(self.shared_data, "bjorn_character", None)
            if main_img is not None:
                image.paste(main_img, (self.shared_data.x_center1, self.shared_data.y_bottom1))

            return image
        except Exception:
            if image:
                image.close()
            raise

    def _draw_connection_icons(self, image: Image.Image):
        wifi_pos = self.layout.get('wifi_icon')
        wifi_width = self.px(16)
        bluetooth_width = self.px(9)
        usb_width = self.px(9)
        ethernet_width = self.px(12)

        start_x = self.px(wifi_pos.get('x', 3))
        spacing = self.px(6)

        active_icons = []
        if self.shared_data.wifi_connected:
            active_icons.append(('wifi', self.shared_data.wifi, wifi_width))
        if self.shared_data.bluetooth_active:
            active_icons.append(('bluetooth', self.shared_data.bluetooth, bluetooth_width))
        if self.shared_data.usb_active:
            active_icons.append(('usb', self.shared_data.usb, usb_width))
        if self.shared_data.ethernet_active:
            active_icons.append(('ethernet', self.shared_data.ethernet, ethernet_width))

        current_x = start_x
        for i, (name, icon, width) in enumerate(active_icons):
            if len(active_icons) == 4 and i == 3:
                image.paste(icon, (self.px(92), self.py(4)))
            else:
                y_pos = self.py(3) if name == 'wifi' else self.py(4)
                image.paste(icon, (int(current_x), y_pos))
                current_x += width + spacing

    def _draw_battery_status(self, image: Image.Image):
        bat = self.layout.get('battery_icon')
        battery_pos = (self.px(bat.get('x', 110)), self.py(bat.get('y', 3)))
        battery_status = self.shared_data.battery_status

        if battery_status == 101:
            image.paste(self.shared_data.battery_charging, battery_pos)
        else:
            battery_icons = {
                (0, 24): self.shared_data.battery0,
                (25, 49): self.shared_data.battery25,
                (50, 74): self.shared_data.battery50,
                (75, 89): self.shared_data.battery75,
                (90, 100): self.shared_data.battery100,
            }

            for (lower, upper), icon in battery_icons.items():
                if lower <= battery_status <= upper:
                    image.paste(icon, battery_pos)
                    break

    def _draw_system_histogram(self, image: Image.Image, draw: ImageDraw.Draw):
        # Vertical bars at the bottom-left - positions from layout
        mem_hist = self.layout.get('mem_histogram')
        cpu_hist = self.layout.get('cpu_histogram')

        # Memory bar: x from layout, width from layout
        mem_x = mem_hist.get('x', 2)
        mem_w = mem_hist.get('w', 8)
        mem_bar_y = mem_hist.get('y', 204)
        mem_bar_h = mem_hist.get('h', 33)

        # CPU bar: x from layout
        cpu_x = cpu_hist.get('x', 12)
        cpu_w = cpu_hist.get('w', 8)

        label_y = self.py(239)
        base_y = self.py(237) # 1px gap above label
        max_h = self.py(mem_bar_h)

        # RAM
        ram_pct = max(0, min(100, self.shared_data.system_mem))
        ram_h = int((ram_pct / 100.0) * max_h)
        draw.rectangle([self.px(mem_x), base_y - max_h, self.px(mem_x + mem_w), base_y], outline=0)
        draw.rectangle([self.px(mem_x), base_y - ram_h, self.px(mem_x + mem_w), base_y], fill=0)

        # Label 'M' - No Box, just text
        draw.text((self.px(mem_x + 1), label_y), "M", font=self.shared_data.font_arial9, fill=0)

        # CPU
        cpu_pct = max(0, min(100, self.shared_data.system_cpu))
        cpu_h = int((cpu_pct / 100.0) * max_h)
        draw.rectangle([self.px(cpu_x), base_y - max_h, self.px(cpu_x + cpu_w), base_y], outline=0)
        draw.rectangle([self.px(cpu_x), base_y - cpu_h, self.px(cpu_x + cpu_w), base_y], fill=0)

        # Label 'C' - No Box
        draw.text((self.px(cpu_x + 1), label_y), "C", font=self.shared_data.font_arial9, fill=0)

    def _format_count(self, val):
        try:
            v = int(val)
            if v >= 1000:
                return f"{v/1000:.1f}K".replace(".0K", "K")
            return str(v)
        except:
            return str(val)

    def _draw_statistics(self, image: Image.Image, draw: ImageDraw.Draw):
        stats_y = self.layout.get('stats_row', 'y') if isinstance(self.layout.get('stats_row'), dict) else 22
        if isinstance(stats_y, dict):
            stats_y = stats_y.get('y', 22)
        stats_row = self.layout.get('stats_row')
        sr_y = stats_row.get('y', 22) if stats_row else 22
        sr_text_y = sr_y + 17  # Text offset below icon row
        stats = [
            # Row 1 (Icons at stats_row y, Text at y+17)
            # Target
            (self.shared_data.target, (self.px(2), self.py(sr_y)),
             (self.px(2), self.py(sr_text_y)), self._format_count(self.shared_data.target_count)),
            # Port
            (self.shared_data.port, (self.px(22), self.py(sr_y)),
             (self.px(22), self.py(sr_text_y)), self._format_count(self.shared_data.port_count)),
            # Vuln
            (self.shared_data.vuln, (self.px(42), self.py(sr_y)),
             (self.px(42), self.py(sr_text_y)), self._format_count(self.shared_data.vuln_count)),
            # Cred
            (self.shared_data.cred, (self.px(62), self.py(sr_y)),
             (self.px(62), self.py(sr_text_y)), self._format_count(self.shared_data.cred_count)),
            # Zombie
            (self.shared_data.zombie, (self.px(82), self.py(sr_y)),
             (self.px(82), self.py(sr_text_y)), self._format_count(self.shared_data.zombie_count)),
            # Data
            (self.shared_data.data, (self.px(102), self.py(sr_y)),
             (self.px(102), self.py(sr_text_y)), self._format_count(self.shared_data.data_count)),
            
            # LVL Widget (Top-Left of bottom frame)
            # Frame Line at y=170. Gap 1px -> Start y=172. Left Gap 1px -> Start x=2.
            # Small Square for Value. 
            # I'll use a 18x18 box.
            
            # --- Network KB / Attacks WIDGET (Right)---
            # Moved to dedicated drawing logic below for box alignment
        ]

        for img, img_pos, text_pos, text in stats:
            if img is not None:
                image.paste(img, img_pos)
                # Dynamic centering
                try:
                    # Center text relative to image center
                    center_x = img_pos[0] + (img.width // 2)
                    text_w = draw.textlength(text, font=self.shared_data.font_arial9)
                    new_x = int(center_x - (text_w / 2))
                    draw.text((new_x, text_pos[1]), text, font=self.shared_data.font_arial9, fill=0)
                except Exception:
                    # Fallback
                    draw.text(text_pos, text, font=self.shared_data.font_arial9, fill=0)
            else:
                draw.text(text_pos, text, font=self.shared_data.font_arial9, fill=0)
        
        # Draw LVL Box manually to ensure perfect positioning
        lvl = self.layout.get('lvl_box')
        lvl_x = self.px(lvl.get('x', 2))
        lvl_y = self.py(lvl.get('y', 172))
        lvl_w = self.px(lvl.get('w', 18))
        lvl_h = self.py(lvl.get('h', 26))
        
        draw.rectangle([lvl_x, lvl_y, lvl_x + lvl_w, lvl_y + lvl_h], outline=0)
        
        # 1. "LVL" Label at top - centered
        label_txt = "LVL"
        # Font 7
        label_font = self.shared_data.font_arial7
        l_bbox = label_font.getbbox(label_txt)
        l_w = l_bbox[2] - l_bbox[0]
        l_x = lvl_x + (lvl_w - l_w) // 2
        l_y = lvl_y + 1 # Top padding
        draw.text((l_x, l_y), label_txt, font=label_font, fill=0)
        
        # 2. Value below label - centered
        lvl_val = str(self.shared_data.level_count)
        val_font = self.shared_data.font_arial9
        v_bbox = val_font.getbbox(lvl_val)
        v_w = v_bbox[2] - v_bbox[0]
        v_x = lvl_x + (lvl_w - v_w) // 2
        # Position below label (approx y+10)
        v_y = lvl_y + 10 
        draw.text((v_x, v_y), lvl_val, font=val_font, fill=0)

        # --- Right Side Widgets (Integrated with Frame) ---
        nkb = self.layout.get('network_kb')
        line_bottom = self.layout.get('line_bottom_section')

        col_x_start = self.px(nkb.get('x', 101))
        col_x_end = self.px(nkb.get('x', 101) + nkb.get('w', 20))
        col_w = self.px(nkb.get('w', 20))

        y_top = self.py(line_bottom.get('y', 170))
        y_bottom = self.py(249)
        
        # 1. Draw Left Vertical Divider
        draw.line([col_x_start, y_top, col_x_start, y_bottom], fill=0)
        
        # Section Heights
        # A/M: Small top section. 15px high.
        h_am = self.px(15)
        # Remaining: 79 - 15 = 64px. Split evenly: 32px each.
        h_net = self.px(32)
        h_att = self.py(32)
        
        # Separator Y positions
        y_sep1 = y_top + h_am
        y_sep2 = y_sep1 + h_net
        
        # Draw Horizontal Separators (inside the column)
        draw.line([col_x_start, y_sep1, col_x_end, y_sep1], fill=0)
        draw.line([col_x_start, y_sep2, col_x_end, y_sep2], fill=0)
        
        # --- Section 1: A/M (Top) ---
        # Center A/M text in y_top to y_sep1
        # --- Section 1: A/M/AI (Top) ---
        mode_str = self.shared_data.operation_mode
        # Map to display text: MANUAL -> M, AUTO -> A, AI -> AI
        if mode_str == "MANUAL":
            mode_txt = "M"
        elif mode_str == "AI":
            mode_txt = "AI"
        else:
            mode_txt = "A"

        # Use slightly smaller font for "AI" if needed, or keep same
        mode_font = self.shared_data.font_arial11
        m_bbox = mode_font.getbbox(mode_txt)
        
        m_w = m_bbox[2] - m_bbox[0] # Largeur visuelle exacte
        m_h = m_bbox[3] - m_bbox[1] # Hauteur visuelle exacte
        
        # MODIFICATION ICI (Horizontal) :
        m_x = col_x_start + (col_w - m_w) // 2 - m_bbox[0]
        
        # MODIFICATION ICI (Vertical) :
        m_y = y_top + (h_am - m_h) // 2 - m_bbox[1]
        
        draw.text((m_x, m_y), mode_txt, font=mode_font, fill=0)
        
        # --- Section 2: Network KB (Middle) ---
        # Center in y_sep1 to y_sep2 (32px high)
        net_y_start = y_sep1
        
        # Icon
        if self.shared_data.networkkb:
            icon = self.shared_data.networkkb
            ix = col_x_start + (col_w - icon.width) // 2
            # Center icon somewhat? Or fixed top padding?
            # 32px height. Icon ~15px. Text ~7px. Total content ~23px.
            # Margin = (32 - 23) / 2 = ~4px.
            iy = net_y_start + 3
            image.paste(icon, (ix, iy))
            text_y_start = iy + icon.height
        else:
            text_y_start = net_y_start + 9

        # Value
        net_val = self._format_count(self.shared_data.network_kb_count)
        n_font = self.shared_data.font_arial10
        n_bbox = n_font.getbbox(net_val)
        n_w = n_bbox[2] - n_bbox[0]
        nx = col_x_start + (col_w - n_w) // 2
        draw.text((nx, text_y_start), net_val, font=n_font, fill=0)
        
        # --- Section 3: Attacks (Bottom) ---
        # Center in y_sep2 to y_bottom (32px high)
        att_y_start = y_sep2
        
        # Icon
        if self.shared_data.attacks:
            icon = self.shared_data.attacks
            ix = col_x_start + (col_w - icon.width) // 2
            iy = att_y_start + 3 # Same padding as above
            image.paste(icon, (ix, iy))
            text_y_start = iy + icon.height
        else:
            text_y_start = att_y_start + 9
            
        # Value
        att_val = self._format_count(self.shared_data.attacks_count)
        a_bbox = n_font.getbbox(att_val)
        a_w = a_bbox[2] - a_bbox[0]
        ax = col_x_start + (col_w - a_w) // 2
        draw.text((ax, text_y_start), att_val, font=n_font, fill=0)


    def _draw_status_text(self, draw: ImageDraw.Draw):
        # Determine progress value (0-100)
        try:
            progress_str = self.shared_data.bjorn_progress.replace("%", "").strip()
            progress_val = int(progress_str)
        except:
            progress_val = 0

        # Layout lookups for status area
        pbar = self.layout.get('progress_bar')
        ip_pos = self.layout.get('ip_text')
        sl1 = self.layout.get('status_line1')
        sl2 = self.layout.get('status_line2')
        line_comment = self.layout.get('line_comment_top')

        # Draw Progress Bar
        bar_x = self.px(pbar.get('x', 35))
        bar_y = self.py(pbar.get('y', 75))
        bar_w = self.px(pbar.get('w', 55))
        bar_h = self.py(pbar.get('h', 5))

        if progress_val > 0:
            # Standard Progress Bar
            draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], outline=0)
            fill_w = int((progress_val / 100.0) * bar_w)
            if fill_w > 0:
                draw.rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h], fill=0)

            # Draw Percentage Text at the end
            text_x = bar_x + bar_w + self.px(4)
            text_y = bar_y - 2 # Align visually with bar
            draw.text((text_x, text_y), f"{progress_val}%", font=self.shared_data.font_arial9, fill=0)

        current_ip = getattr(self.shared_data, "current_ip", "No IP")
        action_target_ip = str(getattr(self.shared_data, "action_target_ip", "") or "").strip()
        orch_status = str(getattr(self.shared_data, "bjorn_orch_status", "IDLE") or "IDLE").upper()
        show_ip = bool(getattr(self.shared_data, "showiponscreen", False))
        comment_line_y = self.py(line_comment.get('y', 85))
        if show_ip:
            # Show local IP only while idle; during actions show target IP when available.
            if orch_status == "IDLE":
                ip_to_show = current_ip
            else:
                ip_to_show = action_target_ip or current_ip

            draw.text((self.px(ip_pos.get('x', 35)), self.py(ip_pos.get('y', 52))), ip_to_show,
                      font=self.shared_data.font_arial9, fill=0)
            draw.text((self.px(sl1.get('x', 35)), self.py(sl1.get('y', 55) + 6)), self.shared_data.bjorn_status_text,
                      font=self.shared_data.font_arial9, fill=0)
            draw.line((1, comment_line_y, self.shared_data.width - 1, comment_line_y), fill=0)
        else:
            draw.text((self.px(sl1.get('x', 35)), self.py(sl1.get('y', 55))), self.shared_data.bjorn_status_text,
                      font=self.shared_data.font_arial9, fill=0)
            draw.text((self.px(sl2.get('x', 35)), self.py(sl2.get('y', 66))), self.shared_data.bjorn_status_text2,
                      font=self.shared_data.font_arial9, fill=0)
            draw.line((1, comment_line_y, self.shared_data.width - 1, comment_line_y), fill=0)

    def _draw_decorations(self, image: Image.Image, draw: ImageDraw.Draw):
        line_top = self.layout.get('line_top_bar')
        line_mid = self.layout.get('line_mid_section')
        line_bottom = self.layout.get('line_bottom_section')
        frise_elem = self.layout.get('frise')

        show_ssid = bool(getattr(self.shared_data, "showssidonscreen", False))
        if show_ssid:
            # Center SSID
            ssid = getattr(self.shared_data, "current_ssid", "No Wi-Fi")
            ssid_w = draw.textlength(ssid, font=self.shared_data.font_arial9)
            center_x = self.shared_data.width // 2
            ssid_x = int(center_x - (ssid_w / 2))

            frise_y_val = frise_elem.get('y', 160) if frise_elem else 160
            draw.text((ssid_x, self.py(frise_y_val)), ssid,
                      font=self.shared_data.font_arial9, fill=0)
            draw.line((0, self.py(line_bottom.get('y', 170)), self.shared_data.width, self.py(line_bottom.get('y', 170))), fill=0)
        else:
            frise_x, frise_y = self.get_frise_position()
            if self.shared_data.frise is not None:
                image.paste(self.shared_data.frise, (frise_x, frise_y))

        draw.rectangle((0, 0, self.shared_data.width - 1, self.shared_data.height - 1), outline=0)
        draw.line((0, self.py(line_top.get('y', 20)), self.shared_data.width, self.py(line_top.get('y', 20))), fill=0)
        draw.line((0, self.py(line_mid.get('y', 51)), self.shared_data.width, self.py(line_mid.get('y', 51))), fill=0)

    def _draw_comment_text(self, draw: ImageDraw.Draw):
            # Cache key for the layout
            key = (self.shared_data.bjorn_says, self.shared_data.width, id(self.shared_data.font_arialbold))
            now = time.time()
            if (
                self._comment_layout_cache["key"] != key or
                (now - self._comment_layout_cache["ts"]) >= self._comment_layout_min_interval
            ):
                # Use (width - 2) since text hugs the edge
                lines = self.shared_data.wrap_text(
                    self.shared_data.bjorn_says,
                    self.shared_data.font_arialbold,
                    self.shared_data.width - 2 
                )
                self._comment_layout_cache = {"key": key, "lines": lines, "ts": now}
            else:
                lines = self._comment_layout_cache["lines"]

            comment = self.layout.get('comment_area')
            y_text = self.py(comment.get('y', 86))
            
            font = self.shared_data.font_arialbold
            bbox = font.getbbox('Aj')
            font_height = (bbox[3] - bbox[1]) if bbox else font.size

            for line in lines:
                # MODIFICATION ICI : self.px(1) au lieu de self.px(4)
                draw.text((self.px(1), y_text), line,
                        font=font, fill=0)
                y_text += font_height + self.shared_data.line_spacing

    def _save_screenshot(self, image: Image.Image):
            # 1. Throttling : Only capture every 4 seconds to save CPU/IO
            now = time.time()
            if not hasattr(self, "_last_screenshot_time"):
                self._last_screenshot_time = 0

            if now - self._last_screenshot_time < self._screenshot_interval_s:
                return
            self._last_screenshot_time = now

            rotated = None
            try:
                out_img = image
                if self.web_screen_reversed:
                    rotated = out_img.transpose(Image.ROTATE_180)
                    out_img = rotated

                screenshot_path = os.path.join(self.shared_data.web_dir, "screen.png")
                tmp_path = f"{screenshot_path}.tmp"

                # 2. Optimization : compress_level=1 (much faster on CPU)
                out_img.save(tmp_path, format="PNG", compress_level=1)
                os.replace(tmp_path, screenshot_path)

            except Exception as e:
                logger.error(f"Error saving screenshot: {e}")
            finally:
                if rotated is not None:
                    try:
                        rotated.close()
                    except Exception:
                        pass

    def _cleanup_display(self):
        worker_stopped = True
        try:
            worker_stopped = self.display_controller.stop(timeout=2.0)
            if not worker_stopped:
                logger.warning("EPD worker still alive during shutdown; skipping blocking EPD cleanup")
        except Exception as exc:
            worker_stopped = False
            logger.warning(f"Display controller stop failed during cleanup: {exc}")

        try:
            if self.epd_enabled and worker_stopped:
                self.shared_data.epd.init_full_update()
                blank_image = Image.new('1', (self.shared_data.width, self.shared_data.height), 255)
                blank_image = self._pad_for_v2(blank_image)
                self.shared_data.epd.display_partial(blank_image)
                if self.shared_data.double_partial_refresh:
                    self.shared_data.epd.display_partial(blank_image)
                blank_image.close()
                logger.info("EPD display cleared and device exited")
                try:
                    self.shared_data.epd.sleep()
                except Exception:
                    pass
            elif self.epd_enabled and not worker_stopped:
                logger.warning("EPD cleanup skipped because worker did not stop in time")
            else:
                logger.info("Display thread exited (EPD was disabled)")
        except Exception as e:
            logger.error(f"Error clearing display: {e}")


def handle_exit_display(signum, frame, display_thread=None):
    """Signal handler to cleanly exit display threads"""
    shared_data.display_should_exit = True
    logger.info(f"Exit signal {signum} received, shutting down display...")

    try:
        if display_thread:
            display_thread.join(timeout=10.0)
            if display_thread.is_alive():
                logger.warning("Display thread did not exit cleanly")
            else:
                logger.info("Display thread finished cleanly.")
    except Exception as e:
        logger.error(f"Error while closing the display: {e}")
