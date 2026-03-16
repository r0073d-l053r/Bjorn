# Bjorn.py
# Main entry point and supervisor for the Bjorn project
# Manages lifecycle of threads, health monitoring, and crash protection.
# OPTIMIZED FOR PI ZERO 2: Low CPU overhead, aggressive RAM management.

import logging
import os
import signal
import subprocess
import sys
import threading
import time
import gc 
import tracemalloc
import atexit

from comment import Commentaireia
from display import Display, handle_exit_display
from init_shared import shared_data
from logger import Logger
from orchestrator import Orchestrator
from runtime_state_updater import RuntimeStateUpdater
from webapp import web_thread

logger = Logger(name="Bjorn.py", level=logging.DEBUG)
_shutdown_lock = threading.Lock()
_shutdown_started = False
_instance_lock_fd = None
_instance_lock_path = "/tmp/bjorn_160226.lock"

try:
    import fcntl
except Exception:
    fcntl = None


def _release_instance_lock():
    global _instance_lock_fd
    if _instance_lock_fd is None:
        return
    try:
        if fcntl is not None:
            try:
                fcntl.flock(_instance_lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        _instance_lock_fd.close()
    except Exception:
        pass
    _instance_lock_fd = None


def _acquire_instance_lock() -> bool:
    """Ensure only one Bjorn_160226 process can run at once."""
    global _instance_lock_fd
    if _instance_lock_fd is not None:
        return True

    try:
        fd = open(_instance_lock_path, "a+", encoding="utf-8")
    except Exception as exc:
        logger.error(f"Unable to open instance lock file {_instance_lock_path}: {exc}")
        return True

    if fcntl is None:
        _instance_lock_fd = fd
        return True

    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
    except OSError:
        try:
            fd.seek(0)
            owner_pid = fd.read().strip() or "unknown"
        except Exception:
            owner_pid = "unknown"
        logger.critical(f"Another Bjorn instance is already running (pid={owner_pid}).")
        try:
            fd.close()
        except Exception:
            pass
        return False

    _instance_lock_fd = fd
    return True


class HealthMonitor(threading.Thread):
    """Periodic runtime health logger (threads/fd/rss/queue/epd metrics)."""

    def __init__(self, shared_data_, interval_s: int = 60):
        super().__init__(daemon=True, name="HealthMonitor")
        self.shared_data = shared_data_
        self.interval_s = max(10, int(interval_s))
        self._stop_event = threading.Event()
        self._tm_prev_snapshot = None
        self._tm_last_report = 0.0

    def stop(self):
        self._stop_event.set()

    def _fd_count(self) -> int:
        try:
            return len(os.listdir("/proc/self/fd"))
        except Exception:
            return -1

    def _rss_kb(self) -> int:
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1])
        except Exception:
            pass
        return -1

    def _queue_counts(self):
        pending = running = scheduled = -1
        try:
            # Using query_one safe method from database
            row = self.shared_data.db.query_one(
                """
                SELECT
                    SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status='scheduled' THEN 1 ELSE 0 END) AS scheduled
                FROM action_queue
                """
            )
            if row:
                pending = int(row.get("pending") or 0)
                running = int(row.get("running") or 0)
                scheduled = int(row.get("scheduled") or 0)
        except Exception as exc:
            logger.error_throttled(
                f"Health monitor queue count query failed: {exc}",
                key="health_queue_counts",
                interval_s=120,
            )
        return pending, running, scheduled

    def run(self):
        while not self._stop_event.wait(self.interval_s):
            try:
                threads = threading.enumerate()
                thread_count = len(threads)
                top_threads = ",".join(t.name for t in threads[:8])
                fd_count = self._fd_count()
                rss_kb = self._rss_kb()
                pending, running, scheduled = self._queue_counts()

                # Lock to safely read shared metrics without race conditions
                with self.shared_data.health_lock:
                    display_metrics = dict(getattr(self.shared_data, "display_runtime_metrics", {}) or {})

                epd_enabled = int(display_metrics.get("epd_enabled", 0))
                epd_failures = int(display_metrics.get("failed_updates", 0))
                epd_reinit = int(display_metrics.get("reinit_attempts", 0))
                epd_headless = int(display_metrics.get("headless", 0))
                epd_last_success = display_metrics.get("last_success_epoch", 0)

                logger.info(
                    "health "
                    f"thread_count={thread_count} "
                    f"rss_kb={rss_kb} "
                    f"queue_pending={pending} "
                    f"epd_failures={epd_failures} "
                    f"epd_reinit={epd_reinit} "
                )

                # Optional: tracemalloc report (only if enabled via PYTHONTRACEMALLOC or tracemalloc.start()).
                try:
                    if tracemalloc.is_tracing():
                        now = time.monotonic()
                        tm_interval = float(self.shared_data.config.get("tracemalloc_report_interval_s", 300) or 300)
                        if tm_interval > 0 and (now - self._tm_last_report) >= tm_interval:
                            self._tm_last_report = now
                            top_n = int(self.shared_data.config.get("tracemalloc_top_n", 10) or 10)
                            top_n = max(3, min(top_n, 25))

                            snap = tracemalloc.take_snapshot()
                            if self._tm_prev_snapshot is not None:
                                stats = snap.compare_to(self._tm_prev_snapshot, "lineno")[:top_n]
                                logger.info(f"mem_top (tracemalloc diff, top_n={top_n})")
                                for st in stats:
                                    logger.info(f"mem_top {st}")
                            else:
                                stats = snap.statistics("lineno")[:top_n]
                                logger.info(f"mem_top (tracemalloc, top_n={top_n})")
                                for st in stats:
                                    logger.info(f"mem_top {st}")
                            self._tm_prev_snapshot = snap
                except Exception as exc:
                    logger.error_throttled(
                        f"Health monitor tracemalloc failure: {exc}",
                        key="health_tracemalloc_error",
                        interval_s=300,
                    )
            except Exception as exc:
                logger.error_throttled(
                    f"Health monitor loop failure: {exc}",
                    key="health_loop_error",
                    interval_s=120,
                )


class Bjorn:
    """Main class for Bjorn. Manages orchestration lifecycle."""

    def __init__(self, shared_data_):
        self.shared_data = shared_data_
        self.commentaire_ia = Commentaireia()
        self.orchestrator_thread = None
        self.orchestrator = None
        self.network_connected = False
        self.wifi_connected = False
        self.previous_network_connected = None
        self._orch_lock = threading.Lock()
        self._last_net_check = 0  # Throttling for network scan
        self._last_orch_stop_attempt = 0.0

    def run(self):
        """Main loop for Bjorn. Waits for network and starts/stops Orchestrator based on mode."""
        if hasattr(self.shared_data, "startup_delay") and self.shared_data.startup_delay > 0:
            logger.info(f"Waiting for startup delay: {self.shared_data.startup_delay} seconds")
            time.sleep(self.shared_data.startup_delay)

        backoff_s = 1.0
        while not self.shared_data.should_exit:
            try:
                # Manual/Bifrost mode must stop orchestration.
                # BIFROST: WiFi is in monitor mode, no network available for scans.
                current_mode = self.shared_data.operation_mode
                if current_mode in ("MANUAL", "BIFROST", "LOKI"):
                    # Avoid spamming stop requests if already stopped.
                    if self.orchestrator_thread is not None and self.orchestrator_thread.is_alive():
                        self.stop_orchestrator()
                else:
                    self.check_and_start_orchestrator()

                time.sleep(5)
                backoff_s = 1.0  # Reset backoff on success

            except Exception as exc:
                logger.error(f"Bjorn main loop error: {exc}")
                logger.error_throttled(
                    "Bjorn main loop entering backoff due to repeated errors",
                    key="bjorn_main_loop_backoff",
                    interval_s=60,
                )
                time.sleep(backoff_s)
                backoff_s = min(backoff_s * 2.0, 30.0)

    def check_and_start_orchestrator(self):
        if self.shared_data.operation_mode in ("MANUAL", "BIFROST", "LOKI"):
            return
        if self.is_network_connected():
            self.wifi_connected = True
            if self.orchestrator_thread is None or not self.orchestrator_thread.is_alive():
                self.start_orchestrator()
        else:
            self.wifi_connected = False
            logger.info_throttled(
                "Waiting for network connection to start Orchestrator...",
                key="bjorn_wait_network",
                interval_s=30,
            )

    def start_orchestrator(self):
        with self._orch_lock:
            # Re-check network inside lock
            if not self.network_connected:
                return
            if self.orchestrator_thread is not None and self.orchestrator_thread.is_alive():
                logger.debug("Orchestrator thread is already running.")
                return

            logger.info("Starting Orchestrator thread...")
            self.shared_data.orchestrator_should_exit = False
            
            self.orchestrator = Orchestrator()
            self.orchestrator_thread = threading.Thread(
                target=self.orchestrator.run,
                daemon=True,
                name="OrchestratorMain",
            )
            self.orchestrator_thread.start()
            logger.info("Orchestrator thread started.")

    def stop_orchestrator(self):
        with self._orch_lock:
            thread = self.orchestrator_thread
            if thread is None or not thread.is_alive():
                self.orchestrator_thread = None
                self.orchestrator = None
                return

            # Keep MANUAL sticky so supervisor does not auto-restart orchestration,
            # but only if the current mode isn't already handling it.
            # - MANUAL/BIFROST: already non-AUTO, no need to change
            # - AUTO: let it be — orchestrator will restart naturally (e.g. after Bifrost auto-disable)
            try:
                current = self.shared_data.operation_mode
                if current == "AI":
                    self.shared_data.operation_mode = "MANUAL"
            except Exception:
                pass

            now = time.time()
            if now - self._last_orch_stop_attempt >= 10.0:
                logger.info("Stop requested: stopping Orchestrator")
                self._last_orch_stop_attempt = now
            self.shared_data.orchestrator_should_exit = True
            self.shared_data.queue_event.set() # Wake up thread
            thread.join(timeout=10.0)

            if thread.is_alive():
                logger.warning_throttled(
                    "Orchestrator thread did not stop gracefully",
                    key="orch_stop_not_graceful",
                    interval_s=20,
                )
                # Still reset status so UI doesn't stay stuck on the
                # last action while the thread finishes in the background.
            else:
                self.orchestrator_thread = None
                self.orchestrator = None

            # Always reset display state regardless of whether join succeeded.
            self.shared_data.bjorn_orch_status = "IDLE"
            self.shared_data.bjorn_status_text = "IDLE"
            self.shared_data.bjorn_status_text2 = ""
            self.shared_data.action_target_ip = ""
            self.shared_data.active_action = None
            self.shared_data.update_status("IDLE", "")

    def is_network_connected(self):
        """Checks for network connectivity with throttling and low-CPU checks."""
        now = time.time()
        # Throttling: Do not scan more than once every 10 seconds
        if now - self._last_net_check < 10:
            return self.network_connected
        
        self._last_net_check = now

        def interface_has_ip(interface_name):
            try:
                # OPTIMIZATION: Check /sys/class/net first to avoid spawning subprocess if interface doesn't exist
                if not os.path.exists(f"/sys/class/net/{interface_name}"):
                    return False
                
                # Check for IP address
                result = subprocess.run(
                    ["ip", "-4", "addr", "show", interface_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=2,
                )
                if result.returncode != 0:
                    return False
                return "inet " in result.stdout
            except Exception:
                return False

        eth_connected = interface_has_ip("eth0")
        wifi_connected = interface_has_ip("wlan0")

        self.network_connected = eth_connected or wifi_connected

        if self.network_connected != self.previous_network_connected:
            if self.network_connected:
                logger.info(f"Network status changed: Connected (eth0={eth_connected}, wlan0={wifi_connected})")
            else:
                logger.warning("Network status changed: Connection lost")
            self.previous_network_connected = self.network_connected

        return self.network_connected

    @staticmethod
    def start_display(old_display=None):
        # Ensure the previous Display's controller is fully stopped to release frames
        if old_display is not None:
            try:
                old_display.display_controller.stop(timeout=3.0)
            except Exception:
                pass

        display = Display(shared_data)
        display_thread = threading.Thread(
            target=display.run,
            daemon=True,
            name="DisplayMain",
        )
        display_thread.start()
        return display_thread, display


def _request_shutdown():
    """Signals all threads to stop."""
    shared_data.should_exit = True
    shared_data.orchestrator_should_exit = True
    shared_data.display_should_exit = True
    shared_data.webapp_should_exit = True
    shared_data.queue_event.set()


def handle_exit(
    sig,
    frame,
    display_thread,
    bjorn_thread,
    web_thread_obj,
    health_thread=None,
    runtime_state_thread=None,
    from_signal=False,
):
    global _shutdown_started

    with _shutdown_lock:
        if _shutdown_started:
            if from_signal:
                logger.warning("Forcing exit (SIGINT/SIGTERM received twice)")
                os._exit(130)
            return
        _shutdown_started = True

    logger.info(f"Shutdown signal received: {sig}")
    _request_shutdown()

    # 1. Stop Display (handles EPD cleanup)
    try:
        handle_exit_display(sig, frame, display_thread)
    except Exception:
        pass

    # 2. Stop Health Monitor
    try:
        if health_thread and hasattr(health_thread, "stop"):
            health_thread.stop()
    except Exception:
        pass

    # 2b. Stop Runtime State Updater
    try:
        if runtime_state_thread and hasattr(runtime_state_thread, "stop"):
            runtime_state_thread.stop()
    except Exception:
        pass

    # 2c. Stop Sentinel Watchdog
    try:
        engine = getattr(shared_data, 'sentinel_engine', None)
        if engine and hasattr(engine, 'stop'):
            engine.stop()
    except Exception:
        pass

    # 2d. Stop Bifrost Engine
    try:
        engine = getattr(shared_data, 'bifrost_engine', None)
        if engine and hasattr(engine, 'stop'):
            engine.stop()
    except Exception:
        pass

    # 3. Stop Web Server
    try:
        if web_thread_obj and hasattr(web_thread_obj, "shutdown"):
            web_thread_obj.shutdown()
    except Exception:
        pass

    # 4. Join all threads
    for thread in (display_thread, bjorn_thread, web_thread_obj, health_thread, runtime_state_thread):
        try:
            if thread and thread.is_alive():
                thread.join(timeout=5.0)
        except Exception:
            pass

    # 5. Close Database (Prevent corruption)
    try:
        if hasattr(shared_data, "db") and hasattr(shared_data.db, "close"):
            shared_data.db.close()
    except Exception as exc:
        logger.error(f"Database shutdown error: {exc}")

    logger.info("Bjorn stopped. Clean exit.")
    _release_instance_lock()
    if from_signal:
        sys.exit(0)


def _install_thread_excepthook():
    def _hook(args):
        logger.error(f"Unhandled thread exception: {args.thread.name} - {args.exc_type.__name__}: {args.exc_value}")
        # We don't force shutdown here to avoid killing the app on minor thread glitches, 
        # unless it's critical. The Crash Shield will handle restarts.
    threading.excepthook = _hook


if __name__ == "__main__":
    if not _acquire_instance_lock():
        sys.exit(1)
    atexit.register(_release_instance_lock)
    _install_thread_excepthook()

    display_thread = None
    display_instance = None
    bjorn_thread = None
    health_thread = None
    runtime_state_thread = None
    last_gc_time = time.time()

    try:
        logger.info("Bjorn Startup: Loading config...")
        shared_data.load_config()

        logger.info("Starting Runtime State Updater...")
        runtime_state_thread = RuntimeStateUpdater(shared_data)
        runtime_state_thread.start()

        logger.info("Starting Display...")
        shared_data.display_should_exit = False
        display_thread, display_instance = Bjorn.start_display()

        logger.info("Starting Bjorn Core...")
        bjorn = Bjorn(shared_data)
        shared_data.bjorn_instance = bjorn
        bjorn_thread = threading.Thread(target=bjorn.run, daemon=True, name="BjornMain")
        bjorn_thread.start()

        if shared_data.config.get("websrv", False):
            logger.info("Starting Web Server...")
            if not web_thread.is_alive():
                web_thread.start()

        health_interval = int(shared_data.config.get("health_log_interval", 60))
        health_thread = HealthMonitor(shared_data, interval_s=health_interval)
        health_thread.start()

        # Sentinel watchdog — start if enabled in config
        try:
            from sentinel import SentinelEngine
            sentinel_engine = SentinelEngine(shared_data)
            shared_data.sentinel_engine = sentinel_engine
            if shared_data.config.get("sentinel_enabled", False):
                sentinel_engine.start()
                logger.info("Sentinel watchdog started")
            else:
                logger.info("Sentinel watchdog loaded (disabled)")
        except Exception as e:
            logger.warning("Sentinel init skipped: %s", e)

        # Bifrost engine — start if enabled in config
        try:
            from bifrost import BifrostEngine
            bifrost_engine = BifrostEngine(shared_data)
            shared_data.bifrost_engine = bifrost_engine
            if shared_data.config.get("bifrost_enabled", False):
                bifrost_engine.start()
                logger.info("Bifrost engine started")
            else:
                logger.info("Bifrost engine loaded (disabled)")
        except Exception as e:
            logger.warning("Bifrost init skipped: %s", e)

        # Loki engine — start if enabled in config
        try:
            from loki import LokiEngine
            loki_engine = LokiEngine(shared_data)
            shared_data.loki_engine = loki_engine
            if shared_data.config.get("loki_enabled", False):
                loki_engine.start()
                logger.info("Loki engine started")
            else:
                logger.info("Loki engine loaded (disabled)")
        except Exception as e:
            logger.warning("Loki init skipped: %s", e)

        # LLM Bridge — warm up singleton (starts LaRuche mDNS discovery if enabled)
        try:
            from llm_bridge import LLMBridge
            LLMBridge()  # Initialise singleton, kicks off background discovery
            logger.info("LLM Bridge initialised")
        except Exception as e:
            logger.warning("LLM Bridge init skipped: %s", e)

        # MCP Server — start if enabled in config
        try:
            import mcp_server
            if shared_data.config.get("mcp_enabled", False):
                mcp_server.start()
                logger.info("MCP server started")
            else:
                logger.info("MCP server loaded (disabled — enable via Settings)")
        except Exception as e:
            logger.warning("MCP server init skipped: %s", e)

        # Signal Handlers
        exit_handler = lambda s, f: handle_exit(
            s,
            f,
            display_thread,
            bjorn_thread,
            web_thread,
            health_thread,
            runtime_state_thread,
            True,
        )
        signal.signal(signal.SIGINT, exit_handler)
        signal.signal(signal.SIGTERM, exit_handler)

        # --- SUPERVISOR LOOP (Crash Shield) ---
        restart_times = []
        max_restarts = 5
        restart_window_s = 300

        logger.info("Bjorn Supervisor running.")

        while not shared_data.should_exit:
            time.sleep(2) # CPU Friendly polling
            now = time.time()

            # --- OPTIMIZATION: Periodic Garbage Collection ---
            # Forces cleanup of circular references and free RAM every 2 mins
            if now - last_gc_time > 120:
                gc.collect()
                last_gc_time = now
                logger.debug("System: Forced Garbage Collection executed.")

            # --- CRASH SHIELD: Bjorn Thread ---
            if bjorn_thread and not bjorn_thread.is_alive() and not shared_data.should_exit:
                restart_times = [t for t in restart_times if (now - t) <= restart_window_s]
                restart_times.append(now)
                
                if len(restart_times) <= max_restarts:
                    logger.warning("Crash Shield: Restarting Bjorn Main Thread")
                    bjorn_thread = threading.Thread(target=bjorn.run, daemon=True, name="BjornMain")
                    bjorn_thread.start()
                else:
                    logger.critical("Crash Shield: Bjorn exceeded restart budget. Shutting down.")
                    _request_shutdown()
                    break

            # --- CRASH SHIELD: Display Thread ---
            if display_thread and not display_thread.is_alive() and not shared_data.should_exit:
                restart_times = [t for t in restart_times if (now - t) <= restart_window_s]
                restart_times.append(now)
                if len(restart_times) <= max_restarts:
                    logger.warning("Crash Shield: Restarting Display Thread")
                    display_thread, display_instance = Bjorn.start_display(old_display=display_instance)
                else:
                    logger.critical("Crash Shield: Display exceeded restart budget. Shutting down.")
                    _request_shutdown()
                    break

            # --- CRASH SHIELD: Runtime State Updater ---
            if runtime_state_thread and not runtime_state_thread.is_alive() and not shared_data.should_exit:
                restart_times = [t for t in restart_times if (now - t) <= restart_window_s]
                restart_times.append(now)
                if len(restart_times) <= max_restarts:
                    logger.warning("Crash Shield: Restarting Runtime State Updater")
                    runtime_state_thread = RuntimeStateUpdater(shared_data)
                    runtime_state_thread.start()
                else:
                    logger.critical("Crash Shield: Runtime State Updater exceeded restart budget. Shutting down.")
                    _request_shutdown()
                    break

        # Exit cleanup
        if health_thread:
            health_thread.stop()
        if runtime_state_thread:
            runtime_state_thread.stop()

        handle_exit(
            signal.SIGTERM,
            None,
            display_thread,
            bjorn_thread,
            web_thread,
            health_thread,
            runtime_state_thread,
            False,
        )

    except Exception as exc:
        logger.critical(f"Critical bootstrap failure: {exc}")
        _request_shutdown()
        # Try to clean up anyway
        try:
            handle_exit(
                signal.SIGTERM,
                None,
                display_thread,
                bjorn_thread,
                web_thread,
                health_thread,
                runtime_state_thread,
                False,
            )
        except:
            pass
        sys.exit(1)
