"""odin_eye.py - Dynamic network interface detection and monitoring."""

# --- Dynamic interface detection ---
import os
try:
    import psutil
except Exception:
    psutil = None


def _list_net_ifaces() -> list[str]:
    names = set()
    # 1) psutil if available
    if psutil:
        try:
            names.update(ifname for ifname in psutil.net_if_addrs().keys() if ifname != "lo")
        except Exception:
            pass
    # 2) kernel fallback
    try:
        for n in os.listdir("/sys/class/net"):
            if n and n != "lo":
                names.add(n)
    except Exception:
        pass
    out = ["auto"] + sorted(names)
    # deduplicate
    seen, unique = set(), []
    for x in out:
        if x not in seen:
            unique.append(x); seen.add(x)
    return unique


# Hook called by the backend before UI display / DB sync
def compute_dynamic_b_args(base: dict) -> dict:
    """
    Compute dynamic arguments at runtime.
    Called by the web interface to populate dropdowns, etc.
    """
    d = dict(base or {})
    
    # Example: Dynamic interface list
    if "interface" in d:
        import psutil
        interfaces = ["auto"]
        try:
            for ifname in psutil.net_if_addrs().keys():
                if ifname != "lo":
                    interfaces.append(ifname)
        except:
            interfaces.extend(["wlan0", "eth0"])
        
        d["interface"]["choices"] = interfaces
    
    return d

# --- Additional UI metadata ---
# Example arguments (frontend display; also persisted in DB via sync_actions)
b_examples = [
    {"interface": "auto", "filter": "http or ftp", "timeout": 120, "max_packets": 5000, "save_credentials": True},
    {"interface": "wlan0", "filter": "(http or smtp) and not broadcast", "timeout": 300, "max_packets": 10000},
]

# Docs link (local path served by frontend, or http(s))
b_docs_url = "docs/actions/OdinEye.md"


# --- Action metadata (consumed by shared.generate_actions_json) ---
b_class       = "OdinEye"
b_module      = "odin_eye"
b_enabled    = 0
b_action      = "normal"
b_category    = "recon"
b_name        = "Odin Eye"
b_description = (
    "Network traffic analyzer for capturing and analyzing data patterns and credentials.\n"
    "Requires: tshark (sudo apt install tshark) + pyshark (pip install pyshark)."
)
b_author      = "Fabien / Cyberviking"
b_version     = "1.0.0"
b_icon        = "OdinEye.png"

# UI argument schema (key == flag name without '--')
b_args = {
    "interface": {
        "type": "select", "label": "Network Interface",
        "choices": [],  # Populated dynamically by compute_dynamic_b_args()
        "default": "auto",
        "help": "Interface to listen on. 'auto' tries to detect the default interface."    },
    "filter":      {"type": "text",   "label": "BPF Filter",   "default": "(http or ftp or smtp or pop3 or imap or telnet) and not broadcast"},
    "output":      {"type": "text",   "label": "Output dir",   "default": "/home/bjorn/Bjorn/data/output/packets"},
    "timeout":     {"type": "number", "label": "Timeout (s)",  "min": 10, "max": 36000, "step": 1, "default": 300},
    "max_packets": {"type": "number", "label": "Max packets",  "min": 100, "max": 2000000, "step": 100, "default": 10000},
}

# --- Traffic analysis code ---
import os, json, pyshark, argparse, logging, re, threading, signal
from datetime import datetime
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

DEFAULT_OUTPUT_DIR = "/home/bjorn/Bjorn/data/output/packets"
DEFAULT_SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
SETTINGS_FILE = os.path.join(DEFAULT_SETTINGS_DIR, "odin_eye_settings.json")
DEFAULT_FILTER = "(http or ftp or smtp or pop3 or imap or telnet) and not broadcast"

CREDENTIAL_PATTERNS = {
    'http': {
        'username': [r'username=([^&]+)', r'user=([^&]+)', r'login=([^&]+)'],
        'password': [r'password=([^&]+)', r'pass=([^&]+)']
    },
    'ftp': {
        'username': [r'USER\s+(.+)', r'USERNAME\s+(.+)'],
        'password': [r'PASS\s+(.+)']
    },
    'smtp': {
        'auth': [r'AUTH\s+PLAIN\s+(.+)', r'AUTH\s+LOGIN\s+(.+)']
    }
}

class OdinEye:
    def __init__(self, interface, capture_filter=DEFAULT_FILTER, output_dir=DEFAULT_OUTPUT_DIR,
                 timeout=300, max_packets=10000):
        self.interface = interface
        self.capture_filter = capture_filter
        self.output_dir = output_dir
        self.timeout = timeout
        self.max_packets = max_packets
        self.capture = None
        self.stop_capture = threading.Event()

        self.statistics = defaultdict(int)
        self.credentials = []
        self.interesting_patterns = []

        self.lock = threading.Lock()

    def process_packet(self, packet):
        try:
            with self.lock:
                self.statistics['total_packets'] += 1
                if hasattr(packet, 'highest_layer'):
                    self.statistics[packet.highest_layer] += 1
            if hasattr(packet, 'tcp'):
                self.analyze_tcp_packet(packet)
        except Exception as e:
            logging.error(f"Error processing packet: {e}")

    def analyze_tcp_packet(self, packet):
        try:
            if hasattr(packet, 'http'):
                self.analyze_http_packet(packet)
            elif hasattr(packet, 'ftp'):
                self.analyze_ftp_packet(packet)
            elif hasattr(packet, 'smtp'):
                self.analyze_smtp_packet(packet)
            if hasattr(packet.tcp, 'payload'):
                self.analyze_payload(packet.tcp.payload)
        except Exception as e:
            logging.error(f"Error analyzing TCP packet: {e}")

    def analyze_http_packet(self, packet):
        try:
            if hasattr(packet.http, 'request_uri'):
                for field in ['username', 'password']:
                    for pattern in CREDENTIAL_PATTERNS['http'][field]:
                        matches = re.findall(pattern, packet.http.request_uri)
                        if matches:
                            with self.lock:
                                self.credentials.append({
                                    'protocol': 'HTTP',
                                    'type': field,
                                    'value': matches[0],
                                    'timestamp': datetime.now().isoformat(),
                                    'source': packet.ip.src if hasattr(packet, 'ip') else None
                                })
        except Exception as e:
            logging.error(f"Error analyzing HTTP packet: {e}")

    def analyze_ftp_packet(self, packet):
        try:
            if hasattr(packet.ftp, 'request_command'):
                cmd = packet.ftp.request_command.upper()
                if cmd in ['USER', 'PASS']:
                    with self.lock:
                        self.credentials.append({
                            'protocol': 'FTP',
                            'type': 'username' if cmd == 'USER' else 'password',
                            'value': packet.ftp.request_arg,
                            'timestamp': datetime.now().isoformat(),
                            'source': packet.ip.src if hasattr(packet, 'ip') else None
                        })
        except Exception as e:
            logging.error(f"Error analyzing FTP packet: {e}")

    def analyze_smtp_packet(self, packet):
        try:
            if hasattr(packet.smtp, 'command_line'):
                for pattern in CREDENTIAL_PATTERNS['smtp']['auth']:
                    matches = re.findall(pattern, packet.smtp.command_line)
                    if matches:
                        with self.lock:
                            self.credentials.append({
                                'protocol': 'SMTP',
                                'type': 'auth',
                                'value': matches[0],
                                'timestamp': datetime.now().isoformat(),
                                'source': packet.ip.src if hasattr(packet, 'ip') else None
                            })
        except Exception as e:
            logging.error(f"Error analyzing SMTP packet: {e}")

    def analyze_payload(self, payload):
        patterns = {
            'email': r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
            'credit_card': r'\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b',
            'ip_address': r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'
        }
        for name, pattern in patterns.items():
            matches = re.findall(pattern, payload)
            if matches:
                with self.lock:
                    self.interesting_patterns.append({
                        'type': name,
                        'value': matches[0],
                        'timestamp': datetime.now().isoformat()
                    })

    def save_results(self):
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            stats_file = os.path.join(self.output_dir, f"capture_stats_{timestamp}.json")
            with open(stats_file, 'w') as f:
                json.dump(dict(self.statistics), f, indent=4)
            if self.credentials:
                creds_file = os.path.join(self.output_dir, f"credentials_{timestamp}.json")
                with open(creds_file, 'w') as f:
                    json.dump(self.credentials, f, indent=4)
            if self.interesting_patterns:
                patterns_file = os.path.join(self.output_dir, f"patterns_{timestamp}.json")
                with open(patterns_file, 'w') as f:
                    json.dump(self.interesting_patterns, f, indent=4)
            logging.info(f"Results saved to {self.output_dir}")
        except Exception as e:
            logging.error(f"Failed to save results: {e}")

    def execute(self):
        try:
            # Timeout thread
            if self.timeout and self.timeout > 0:
                def _stop_after():
                    self.stop_capture.wait(self.timeout)
                    self.stop_capture.set()
                threading.Thread(target=_stop_after, daemon=True).start()

            logging.info(...)

            self.capture = pyshark.LiveCapture(interface=self.interface, bpf_filter=self.capture_filter)

            # Graceful interrupt - skip if running in importlib (threaded) mode
            if os.environ.get("BJORN_EMBEDDED") != "1":
                try:
                    signal.signal(signal.SIGINT, self.handle_interrupt)
                    signal.signal(signal.SIGTERM, self.handle_interrupt)
                except Exception:
                    # e.g. ValueError if not in main thread
                    pass

            for packet in self.capture.sniff_continuously():
                if self.stop_capture.is_set() or self.statistics['total_packets'] >= self.max_packets:
                    break
                self.process_packet(packet)
        except Exception as e:
            logging.error(f"Capture error: {e}")
        finally:
            self.cleanup()

    def handle_interrupt(self, signum, frame):
        self.stop_capture.set()

    def cleanup(self):
        if self.capture:
            self.capture.close()
        self.save_results()
        logging.info("Capture completed")

def save_settings(interface, capture_filter, output_dir, timeout, max_packets):
    try:
        os.makedirs(DEFAULT_SETTINGS_DIR, exist_ok=True)
        settings = {
            "interface": interface,
            "capture_filter": capture_filter,
            "output_dir": output_dir,
            "timeout": timeout,
            "max_packets": max_packets
        }
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f)
        logging.info(f"Settings saved to {SETTINGS_FILE}")
    except Exception as e:
        logging.error(f"Failed to save settings: {e}")

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load settings: {e}")
    return {}

def main():
    parser = argparse.ArgumentParser(description="OdinEye: network traffic analyzer & credential hunter")
    parser.add_argument("-i", "--interface", required=False, help="Network interface to monitor")
    parser.add_argument("-f", "--filter", default=DEFAULT_FILTER, help="BPF capture filter")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR, help="Output directory")
    parser.add_argument("-t", "--timeout", type=int, default=300, help="Capture timeout in seconds")
    parser.add_argument("-m", "--max-packets", type=int, default=10000, help="Maximum packets to capture")
    args = parser.parse_args()

    settings = load_settings()
    interface = args.interface or settings.get("interface")
    capture_filter = args.filter or settings.get("capture_filter", DEFAULT_FILTER)
    output_dir = args.output or settings.get("output_dir", DEFAULT_OUTPUT_DIR)
    timeout = args.timeout or settings.get("timeout", 300)
    max_packets = args.max_packets or settings.get("max_packets", 10000)

    if not interface:
        logging.error("Interface is required. Use -i or set it in settings")
        return

    save_settings(interface, capture_filter, output_dir, timeout, max_packets)
    analyzer = OdinEye(interface, capture_filter, output_dir, timeout, max_packets)
    analyzer.execute()

if __name__ == "__main__":
    main()




"""
# action_template.py
# Example template for a Bjorn action with Neo launcher support

# UI Metadata
b_class = "MyAction"
b_module = "my_action"
b_enabled = 1
b_action = "normal"  # normal, aggressive, stealth
b_description = "Description of what this action does"

# Arguments schema for UI
b_args = {
    "target": {
        "type": "text",
        "label": "Target IP/Host",
        "default": "192.168.1.1",
        "placeholder": "Enter target",
        "help": "The target to scan"
    },
    "port": {
        "type": "number",
        "label": "Port",
        "default": 80,
        "min": 1,
        "max": 65535
    },
    "protocol": {
        "type": "select",
        "label": "Protocol",
        "choices": ["tcp", "udp"],
        "default": "tcp"
    },
    "verbose": {
        "type": "checkbox",
        "label": "Verbose output",
        "default": False
    },
    "timeout": {
        "type": "slider",
        "label": "Timeout (seconds)",
        "min": 10,
        "max": 300,
        "step": 10,
        "default": 60
    }
}

def compute_dynamic_b_args(base: dict) -> dict:
    # Compute dynamic values at runtime
    return base

import argparse
import sys

def main():
    parser = argparse.ArgumentParser(description=b_description)
    parser.add_argument('--target', default=b_args['target']['default'])
    parser.add_argument('--port', type=int, default=b_args['port']['default'])
    parser.add_argument('--protocol', choices=b_args['protocol']['choices'], 
                       default=b_args['protocol']['default'])
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--timeout', type=int, default=b_args['timeout']['default'])
    
    args = parser.parse_args()
    
    # Your action logic here
    print(f"Starting action with target: {args.target}")
    # ...
    
if __name__ == "__main__":
    main()
"""