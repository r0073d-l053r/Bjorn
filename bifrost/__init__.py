"""__init__.py - Bifrost, pwnagotchi-compatible WiFi recon engine for Bjorn.

Runs as a daemon thread alongside MANUAL/AUTO/AI modes.
"""
import os
import time
import subprocess
import threading
import logging

from logger import Logger

logger = Logger(name="bifrost", level=logging.DEBUG)


class BifrostEngine:
    """Main Bifrost lifecycle manager.

    Manages the bettercap subprocess and BifrostAgent daemon loop.
    Pattern follows SentinelEngine (sentinel.py).
    """

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self._thread = None
        self._stop_event = threading.Event()
        self._running = False
        self._bettercap_proc = None
        self._monitor_torn_down = False
        self._monitor_failed = False
        self.agent = None

    @property
    def enabled(self):
        return bool(self.shared_data.config.get('bifrost_enabled', False))

    def start(self):
        """Start the Bifrost engine (bettercap + agent loop)."""
        if self._running:
            logger.warning("Bifrost already running")
            return

        # Wait for any previous thread to finish before re-starting
        if self._thread and self._thread.is_alive():
            logger.warning("Previous Bifrost thread still running - waiting ...")
            self._stop_event.set()
            self._thread.join(timeout=15)

        logger.info("Starting Bifrost engine ...")
        self._stop_event.clear()
        self._running = True
        self._monitor_failed = False
        self._monitor_torn_down = False

        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="BifrostEngine"
        )
        self._thread.start()

    def stop(self):
        """Stop the Bifrost engine gracefully.

        Signals the daemon loop to exit, then waits for it to finish.
        The loop's finally block handles bettercap shutdown and monitor teardown.
        """
        if not self._running:
            return

        logger.info("Stopping Bifrost engine ...")
        self._stop_event.set()
        self._running = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=15)
        self._thread = None
        self.agent = None

        # Safety net: teardown is idempotent, so this is a no-op if
        # _loop()'s finally already ran it.
        self._stop_bettercap()
        self._teardown_monitor_mode()
        logger.info("Bifrost engine stopped")

    def _loop(self):
        """Main daemon loop - setup monitor mode, start bettercap, create agent, run recon cycle."""
        try:
            # Install compatibility shim for pwnagotchi plugins
            from bifrost import plugins as bfplugins
            from bifrost.compat import install_shim
            install_shim(self.shared_data, bfplugins)

            # Setup monitor mode on the WiFi interface
            self._setup_monitor_mode()

            if self._monitor_failed:
                logger.error(
                    "Monitor mode setup failed - Bifrost cannot operate without monitor "
                    "mode. For Broadcom chips (Pi Zero W/2W), install nexmon: "
                    "https://github.com/seemoo-lab/nexmon - "
                    "Or use an external USB WiFi adapter with monitor mode support.")
                # Teardown first (restores network services) BEFORE switching mode,
                # so the orchestrator doesn't start scanning on a dead network.
                self._teardown_monitor_mode()
                self._running = False
                # Now switch mode back to AUTO - the network should be restored.
                # We set the flag directly FIRST (bypass setter to avoid re-stopping),
                # then ensure manual_mode/ai_mode are cleared so getter returns AUTO.
                try:
                    self.shared_data.config["bifrost_enabled"] = False
                    self.shared_data.config["manual_mode"] = False
                    self.shared_data.config["ai_mode"] = False
                    self.shared_data.manual_mode = False
                    self.shared_data.ai_mode = False
                    self.shared_data.invalidate_config_cache()
                    logger.info("Bifrost auto-disabled due to monitor mode failure - mode: AUTO")
                except Exception:
                    pass
                return

            # Start bettercap
            self._start_bettercap()
            self._stop_event.wait(3)  # Give bettercap time to initialize
            if self._stop_event.is_set():
                return

            # Create agent (pass stop_event so its threads exit cleanly)
            from bifrost.agent import BifrostAgent
            self.agent = BifrostAgent(self.shared_data, stop_event=self._stop_event)

            # Load plugins
            bfplugins.load(self.shared_data.config)

            # Initialize agent
            self.agent.start()

            logger.info("Bifrost agent started - entering recon cycle")

            # Main recon loop (port of do_auto_mode from pwnagotchi)
            while not self._stop_event.is_set():
                try:
                    # Full spectrum scan
                    self.agent.recon()

                    if self._stop_event.is_set():
                        break

                    # Get APs grouped by channel
                    channels = self.agent.get_access_points_by_channel()

                    # For each channel
                    for ch, aps in channels:
                        if self._stop_event.is_set():
                            break

                        self.agent.set_channel(ch)

                        # For each AP on this channel
                        for ap in aps:
                            if self._stop_event.is_set():
                                break

                            # Send association frame for PMKID
                            self.agent.associate(ap)

                            # Deauth all clients for full handshake
                            for sta in ap.get('clients', []):
                                if self._stop_event.is_set():
                                    break
                                self.agent.deauth(ap, sta)

                    if not self._stop_event.is_set():
                        self.agent.next_epoch()

                except Exception as e:
                    if 'wifi.interface not set' in str(e):
                        logger.error("WiFi interface lost: %s", e)
                        self._stop_event.wait(60)
                        if not self._stop_event.is_set():
                            self.agent.next_epoch()
                    else:
                        logger.error("Recon loop error: %s", e)
                        self._stop_event.wait(5)

        except Exception as e:
            logger.error("Bifrost engine fatal error: %s", e)
        finally:
            from bifrost import plugins as bfplugins
            bfplugins.shutdown()
            self._stop_bettercap()
            self._teardown_monitor_mode()
            self._running = False

    # ── Monitor mode management ─────────────────────────

    # ── Nexmon helpers ────────────────────────────────────

    @staticmethod
    def _has_nexmon():
        """Check if nexmon firmware patches are installed."""
        import shutil
        if not shutil.which('nexutil'):
            return False
        # Verify patched firmware via dmesg
        try:
            r = subprocess.run(
                ['dmesg'], capture_output=True, text=True, timeout=5)
            if 'nexmon' in r.stdout.lower():
                return True
        except Exception:
            pass
        # nexutil exists - assume usable even without dmesg confirmation
        return True

    @staticmethod
    def _is_brcmfmac(iface):
        """Check if the interface uses the brcmfmac driver (Broadcom)."""
        driver_path = '/sys/class/net/%s/device/driver' % iface
        try:
            real = os.path.realpath(driver_path)
            return 'brcmfmac' in real
        except Exception:
            return False

    def _detect_phy(self, iface):
        """Detect the phy name for a given interface (e.g. 'phy0')."""
        try:
            r = subprocess.run(
                ['iw', 'dev', iface, 'info'],
                capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if 'wiphy' in line:
                    idx = line.strip().split()[-1]
                    return 'phy%s' % idx
        except Exception:
            pass
        return 'phy0'

    def _setup_monitor_mode(self):
        """Put the WiFi interface into monitor mode.

        Strategy order:
        1. Nexmon - for Broadcom brcmfmac chips (Pi Zero W / Pi Zero 2 W)
           Uses: iw phy <phy> interface add mon0 type monitor + nexutil -m2
        2. airmon-ng - for chipsets with proper driver support (Atheros, Realtek, etc.)
        3. iw - direct fallback for other drivers
        """
        self._monitor_torn_down = False
        self._nexmon_used = False
        cfg = self.shared_data.config
        iface = cfg.get('bifrost_iface', 'wlan0mon')

        # If configured iface already ends with 'mon', derive the base name
        if iface.endswith('mon'):
            base_iface = iface[:-3]  # e.g. 'wlan0mon' -> 'wlan0'
        else:
            base_iface = iface

        # Store original interface name for teardown
        self._base_iface = base_iface
        self._mon_iface = iface

        # Check if a monitor interface already exists
        if iface != base_iface and self._iface_exists(iface):
            logger.info("Monitor interface %s already exists", iface)
            return

        # ── Strategy 1: Nexmon (Broadcom brcmfmac) ────────────────
        if self._is_brcmfmac(base_iface):
            logger.info("Broadcom brcmfmac chip detected on %s", base_iface)
            if self._has_nexmon():
                if self._setup_nexmon(base_iface, cfg):
                    return
                # nexmon setup failed - don't try other strategies, they won't work either
                self._monitor_failed = True
                return
            else:
                logger.error(
                    "Broadcom brcmfmac chip requires nexmon firmware patches for "
                    "monitor mode. Install nexmon manually using install_nexmon.sh "
                    "or visit: https://github.com/seemoo-lab/nexmon")
                self._monitor_failed = True
                return

        # ── Strategy 2: airmon-ng (Atheros, Realtek, etc.) ────────
        airmon_ok = False
        try:
            logger.info("Killing interfering processes ...")
            subprocess.run(
                ['airmon-ng', 'check', 'kill'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15,
            )
            logger.info("Starting monitor mode: airmon-ng start %s", base_iface)
            result = subprocess.run(
                ['airmon-ng', 'start', base_iface],
                capture_output=True, text=True, timeout=30,
            )
            combined = (result.stdout + result.stderr).strip()
            logger.info("airmon-ng output: %s", combined)

            if 'Operation not supported' in combined or 'command failed' in combined:
                logger.warning("airmon-ng failed: %s", combined)
            else:
                # airmon-ng may rename the interface (wlan0 -> wlan0mon)
                if self._iface_exists(iface):
                    logger.info("Monitor mode active: %s", iface)
                    airmon_ok = True
                elif self._iface_exists(base_iface):
                    logger.info("Interface %s is now in monitor mode (no rename)", base_iface)
                    cfg['bifrost_iface'] = base_iface
                    self._mon_iface = base_iface
                    airmon_ok = True

            if airmon_ok:
                return
        except FileNotFoundError:
            logger.warning("airmon-ng not found, trying iw fallback ...")
        except Exception as e:
            logger.warning("airmon-ng failed: %s, trying iw fallback ...", e)

        # ── Strategy 3: iw (direct fallback) ──────────────────────
        try:
            subprocess.run(
                ['ip', 'link', 'set', base_iface, 'down'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
            result = subprocess.run(
                ['iw', 'dev', base_iface, 'set', 'type', 'monitor'],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                err = result.stderr.strip()
                logger.error("iw set monitor failed (rc=%d): %s", result.returncode, err)
                self._monitor_failed = True
                subprocess.run(
                    ['ip', 'link', 'set', base_iface, 'up'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                )
                return
            subprocess.run(
                ['ip', 'link', 'set', base_iface, 'up'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
            logger.info("Monitor mode set via iw on %s", base_iface)
            cfg['bifrost_iface'] = base_iface
            self._mon_iface = base_iface
        except Exception as e:
            logger.error("Failed to set monitor mode: %s", e)
            self._monitor_failed = True

    def _setup_nexmon(self, base_iface, cfg):
        """Enable monitor mode using nexmon (for Broadcom brcmfmac chips).

        Creates a separate monitor interface (mon0) so wlan0 can potentially
        remain usable for management traffic (like pwnagotchi does).

        Returns True on success, False on failure.
        """
        mon_iface = 'mon0'
        phy = self._detect_phy(base_iface)
        logger.info("Nexmon: setting up monitor mode on %s (phy=%s)", base_iface, phy)

        try:
            # Kill interfering services (same as pwnagotchi)
            for svc in ('wpa_supplicant', 'NetworkManager', 'dhcpcd'):
                subprocess.run(
                    ['systemctl', 'stop', svc],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                )

            # Remove old mon0 if it exists
            if self._iface_exists(mon_iface):
                subprocess.run(
                    ['iw', 'dev', mon_iface, 'del'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                )

            # Create monitor interface via iw phy
            result = subprocess.run(
                ['iw', 'phy', phy, 'interface', 'add', mon_iface, 'type', 'monitor'],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                logger.error("Failed to create %s: %s", mon_iface, result.stderr.strip())
                return False

            # Bring monitor interface up
            subprocess.run(
                ['ifconfig', mon_iface, 'up'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )

            # Enable monitor mode with radiotap headers via nexutil
            result = subprocess.run(
                ['nexutil', '-m2'],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                logger.warning("nexutil -m2 returned rc=%d: %s", result.returncode, result.stderr.strip())

            # Verify
            verify = subprocess.run(
                ['nexutil', '-m'],
                capture_output=True, text=True, timeout=5,
            )
            mode_val = verify.stdout.strip()
            logger.info("nexutil -m reports: %s", mode_val)

            if not self._iface_exists(mon_iface):
                logger.error("Monitor interface %s not created", mon_iface)
                return False

            # Success - update config to use mon0
            cfg['bifrost_iface'] = mon_iface
            self._mon_iface = mon_iface
            self._nexmon_used = True
            logger.info("Nexmon monitor mode active on %s (phy=%s)", mon_iface, phy)
            return True

        except FileNotFoundError as e:
            logger.error("Required tool not found: %s", e)
            return False
        except Exception as e:
            logger.error("Nexmon setup error: %s", e)
            return False

    def _teardown_monitor_mode(self):
        """Restore the WiFi interface to managed mode (idempotent)."""
        if self._monitor_torn_down:
            return
        base_iface = getattr(self, '_base_iface', None)
        mon_iface = getattr(self, '_mon_iface', None)
        if not base_iface:
            return
        self._monitor_torn_down = True

        logger.info("Restoring managed mode for %s ...", base_iface)

        if getattr(self, '_nexmon_used', False):
            # ── Nexmon teardown ──
            try:
                subprocess.run(
                    ['nexutil', '-m0'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                )
                logger.info("Nexmon monitor mode disabled (nexutil -m0)")
            except Exception:
                pass
            # Remove the mon0 interface
            if mon_iface and mon_iface != base_iface and self._iface_exists(mon_iface):
                try:
                    subprocess.run(
                        ['iw', 'dev', mon_iface, 'del'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                    )
                    logger.info("Removed monitor interface %s", mon_iface)
                except Exception:
                    pass
        else:
            # ── airmon-ng / iw teardown ──
            try:
                iface_to_stop = mon_iface or base_iface
                subprocess.run(
                    ['airmon-ng', 'stop', iface_to_stop],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15,
                )
                logger.info("Monitor mode stopped via airmon-ng")
            except FileNotFoundError:
                try:
                    subprocess.run(
                        ['ip', 'link', 'set', base_iface, 'down'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    )
                    subprocess.run(
                        ['iw', 'dev', base_iface, 'set', 'type', 'managed'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    )
                    subprocess.run(
                        ['ip', 'link', 'set', base_iface, 'up'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    )
                    logger.info("Managed mode restored via iw on %s", base_iface)
                except Exception as e:
                    logger.error("Failed to restore managed mode: %s", e)
            except Exception as e:
                logger.warning("airmon-ng stop failed: %s", e)

        # Restart network services that were killed
        restarted = False
        for svc in ('wpa_supplicant', 'dhcpcd', 'NetworkManager'):
            try:
                subprocess.run(
                    ['systemctl', 'start', svc],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                )
                restarted = True
            except Exception:
                pass

        # Wait for network services to actually reconnect before handing
        # control back so the orchestrator doesn't scan a dead interface.
        if restarted:
            logger.info("Waiting for network services to reconnect ...")
            time.sleep(5)

    @staticmethod
    def _iface_exists(iface_name):
        """Check if a network interface exists."""
        return os.path.isdir('/sys/class/net/%s' % iface_name)

    # ── Bettercap subprocess management ────────────────

    def _start_bettercap(self):
        """Spawn bettercap subprocess with REST API."""
        cfg = self.shared_data.config
        iface = cfg.get('bifrost_iface', 'wlan0mon')
        host = cfg.get('bifrost_bettercap_host', '127.0.0.1')
        port = str(cfg.get('bifrost_bettercap_port', 8081))
        user = cfg.get('bifrost_bettercap_user', 'user')
        password = cfg.get('bifrost_bettercap_pass', 'pass')

        cmd = [
            'bettercap', '-iface', iface, '-no-colors',
            '-eval', 'set api.rest.address %s' % host,
            '-eval', 'set api.rest.port %s' % port,
            '-eval', 'set api.rest.username %s' % user,
            '-eval', 'set api.rest.password %s' % password,
            '-eval', 'api.rest on',
        ]

        logger.info("Starting bettercap: %s", ' '.join(cmd))
        try:
            self._bettercap_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("bettercap PID: %d", self._bettercap_proc.pid)
        except FileNotFoundError:
            logger.error("bettercap not found! Install with: apt install bettercap")
            raise
        except Exception as e:
            logger.error("Failed to start bettercap: %s", e)
            raise

    def _stop_bettercap(self):
        """Kill the bettercap subprocess."""
        if self._bettercap_proc:
            try:
                self._bettercap_proc.terminate()
                self._bettercap_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._bettercap_proc.kill()
            except Exception:
                pass
            self._bettercap_proc = None
            logger.info("bettercap stopped")

    # ── Status for web API ────────────────────────────────

    def get_status(self):
        """Return full engine status for web API."""
        base = {
            'enabled': self.enabled,
            'running': self._running,
            'monitor_failed': self._monitor_failed,
        }
        if self.agent and self._running:
            base.update(self.agent.get_status())
        else:
            base.update({
                'mood': 'sleeping',
                'face': '(-.-) zzZ',
                'voice': '',
                'channel': 0,
                'num_aps': 0,
                'num_handshakes': 0,
                'uptime': 0,
                'epoch': 0,
                'mode': 'auto',
                'last_pwnd': '',
                'reward': 0,
            })
        return base
