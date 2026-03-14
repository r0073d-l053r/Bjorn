"""
Bifrost — Pwnagotchi compatibility shim.
Registers `pwnagotchi` in sys.modules so existing plugins can
`import pwnagotchi` and get Bifrost-backed implementations.
"""
import sys
import time
import types
import os


def install_shim(shared_data, bifrost_plugins_module):
    """Install the pwnagotchi namespace shim into sys.modules.

    Call this BEFORE loading any pwnagotchi plugins so their
    `import pwnagotchi` resolves to our shim.
    """
    _start_time = time.time()

    # Create the fake pwnagotchi module
    pwn = types.ModuleType('pwnagotchi')
    pwn.__version__ = '2.0.0-bifrost'
    pwn.__file__ = __file__
    pwn.config = _build_compat_config(shared_data)

    def _name():
        return shared_data.config.get('bjorn_name', 'bifrost')

    def _set_name(n):
        pass  # no-op, name comes from Bjorn config

    def _uptime():
        return time.time() - _start_time

    def _cpu_load():
        try:
            return os.getloadavg()[0]
        except (OSError, AttributeError):
            return 0.0

    def _mem_usage():
        try:
            with open('/proc/meminfo', 'r') as f:
                lines = f.readlines()
            total = int(lines[0].split()[1])
            available = int(lines[2].split()[1])
            return (total - available) / total if total else 0.0
        except Exception:
            return 0.0

    def _temperature():
        try:
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                return int(f.read().strip()) / 1000.0
        except Exception:
            return 0.0

    def _reboot():
        pass  # no-op in Bifrost — we don't auto-reboot

    pwn.name = _name
    pwn.set_name = _set_name
    pwn.uptime = _uptime
    pwn.cpu_load = _cpu_load
    pwn.mem_usage = _mem_usage
    pwn.temperature = _temperature
    pwn.reboot = _reboot

    # Register modules
    sys.modules['pwnagotchi'] = pwn
    sys.modules['pwnagotchi.plugins'] = bifrost_plugins_module
    sys.modules['pwnagotchi.utils'] = _build_utils_shim(shared_data)


def _build_compat_config(shared_data):
    """Translate Bjorn's flat bifrost_* config to pwnagotchi's nested format."""
    cfg = shared_data.config
    return {
        'main': {
            'name': cfg.get('bjorn_name', 'bifrost'),
            'iface': cfg.get('bifrost_iface', 'wlan0mon'),
            'mon_start_cmd': '',
            'no_restart': False,
            'filter': cfg.get('bifrost_filter', ''),
            'whitelist': [
                w.strip() for w in
                str(cfg.get('bifrost_whitelist', '')).split(',') if w.strip()
            ],
            'plugins': cfg.get('bifrost_plugins', {}),
            'custom_plugins': cfg.get('bifrost_plugins_path', ''),
            'mon_max_blind_epochs': 50,
        },
        'personality': {
            'ap_ttl': cfg.get('bifrost_personality_ap_ttl', 120),
            'sta_ttl': cfg.get('bifrost_personality_sta_ttl', 300),
            'min_rssi': cfg.get('bifrost_personality_min_rssi', -200),
            'associate': cfg.get('bifrost_personality_associate', True),
            'deauth': cfg.get('bifrost_personality_deauth', True),
            'recon_time': cfg.get('bifrost_personality_recon_time', 30),
            'hop_recon_time': cfg.get('bifrost_personality_hop_recon_time', 10),
            'min_recon_time': cfg.get('bifrost_personality_min_recon_time', 5),
            'max_inactive_scale': 3,
            'recon_inactive_multiplier': 2,
            'max_interactions': cfg.get('bifrost_personality_max_interactions', 3),
            'max_misses_for_recon': cfg.get('bifrost_personality_max_misses', 8),
            'excited_num_epochs': cfg.get('bifrost_personality_excited_epochs', 10),
            'bored_num_epochs': cfg.get('bifrost_personality_bored_epochs', 15),
            'sad_num_epochs': cfg.get('bifrost_personality_sad_epochs', 25),
            'bond_encounters_factor': cfg.get('bifrost_personality_bond_factor', 20000),
            'channels': [
                int(c.strip()) for c in
                str(cfg.get('bifrost_channels', '')).split(',') if c.strip()
            ],
        },
        'bettercap': {
            'hostname': cfg.get('bifrost_bettercap_host', '127.0.0.1'),
            'scheme': 'http',
            'port': cfg.get('bifrost_bettercap_port', 8081),
            'username': cfg.get('bifrost_bettercap_user', 'user'),
            'password': cfg.get('bifrost_bettercap_pass', 'pass'),
            'handshakes': cfg.get('bifrost_bettercap_handshakes', '/root/bifrost/handshakes'),
            'silence': [
                'ble.device.new', 'ble.device.lost', 'ble.device.disconnected',
                'ble.device.connected', 'ble.device.service.discovered',
                'ble.device.characteristic.discovered',
                'mod.started', 'mod.stopped', 'update.available',
                'session.closing', 'session.started',
            ],
        },
        'ai': {
            'enabled': cfg.get('bifrost_ai_enabled', False),
            'path': '/root/bifrost/brain.json',
        },
        'ui': {
            'fps': 1.0,
            'web': {'enabled': False},
            'display': {'enabled': False},
        },
    }


def _build_utils_shim(shared_data):
    """Minimal pwnagotchi.utils shim."""
    mod = types.ModuleType('pwnagotchi.utils')

    def secs_to_hhmmss(secs):
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        s = int(secs % 60)
        return "%d:%02d:%02d" % (h, m, s)

    def iface_channels(iface):
        """Return available channels for interface."""
        try:
            import subprocess
            out = subprocess.check_output(
                ['iwlist', iface, 'channel'],
                stderr=subprocess.DEVNULL, timeout=5
            ).decode()
            channels = []
            for line in out.split('\n'):
                if 'Channel' in line and 'Current' not in line:
                    parts = line.strip().split()
                    for p in parts:
                        try:
                            ch = int(p)
                            if 1 <= ch <= 14:
                                channels.append(ch)
                        except ValueError:
                            continue
            return sorted(set(channels)) if channels else list(range(1, 15))
        except Exception:
            return list(range(1, 15))

    def total_unique_handshakes(path):
        """Count unique handshake files in directory."""
        import glob as _glob
        if not os.path.isdir(path):
            return 0
        return len(_glob.glob(os.path.join(path, '*.pcap')))

    mod.secs_to_hhmmss = secs_to_hhmmss
    mod.iface_channels = iface_channels
    mod.total_unique_handshakes = total_unique_handshakes
    return mod
