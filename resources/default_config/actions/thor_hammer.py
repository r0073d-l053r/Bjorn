"""thor_hammer.py - Service fingerprinting and version detection for vuln identification."""

import os
import json
import socket
import argparse
import threading
from datetime import datetime
import logging
from concurrent.futures import ThreadPoolExecutor
import subprocess





b_class       = "ThorHammer"
b_module      = "thor_hammer"
b_enabled    = 0

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Default settings
DEFAULT_OUTPUT_DIR = "/home/bjorn/Bjorn/data/output/services"
DEFAULT_SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
SETTINGS_FILE = os.path.join(DEFAULT_SETTINGS_DIR, "thor_hammer_settings.json")
DEFAULT_PORTS = [21, 22, 23, 25, 53, 80, 110, 115, 139, 143, 194, 443, 445, 1433, 3306, 3389, 5432, 5900, 8080]

# Service signature database
SERVICE_SIGNATURES = {
    21: {
        'name': 'FTP',
        'vulnerabilities': {
            'vsftpd 2.3.4': 'Backdoor command execution',
            'ProFTPD 1.3.3c': 'Remote code execution'
        }
    },
    22: {
        'name': 'SSH',
        'vulnerabilities': {
            'OpenSSH 5.3': 'Username enumeration',
            'OpenSSH 7.2p1': 'User enumeration timing attack'
        }
    },
    # Add more signatures as needed
}

class ThorHammer:
    def __init__(self, target, ports=None, output_dir=DEFAULT_OUTPUT_DIR, delay=1, verbose=False):
        self.target = target
        self.ports = ports or DEFAULT_PORTS
        self.output_dir = output_dir
        self.delay = delay
        self.verbose = verbose
        self.results = {
            'target': target,
            'timestamp': datetime.now().isoformat(),
            'services': {}
        }
        self.lock = threading.Lock()

    def probe_service(self, port):
        """Probe a specific port for service information."""
        try:
            # Initial connection test
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.delay)
            result = sock.connect_ex((self.target, port))
            
            if result == 0:
                service_info = {
                    'port': port,
                    'state': 'open',
                    'service': None,
                    'version': None,
                    'vulnerabilities': []
                }

                # Get service banner
                try:
                    banner = sock.recv(1024).decode('utf-8', errors='ignore').strip()
                    service_info['banner'] = banner
                except:
                    service_info['banner'] = None

                # Advanced service detection using nmap if available
                try:
                    nmap_output = subprocess.check_output(
                        ['nmap', '-sV', '-p', str(port), '-T4', self.target],
                        stderr=subprocess.DEVNULL
                    ).decode()
                    
                    # Parse nmap output
                    for line in nmap_output.split('\n'):
                        if str(port) in line and 'open' in line:
                            service_info['service'] = line.split()[2]
                            if len(line.split()) > 3:
                                service_info['version'] = ' '.join(line.split()[3:])
                except:
                    pass

                # Check for known vulnerabilities
                if port in SERVICE_SIGNATURES:
                    sig = SERVICE_SIGNATURES[port]
                    service_info['service'] = service_info['service'] or sig['name']
                    if service_info['version']:
                        for vuln_version, vuln_desc in sig['vulnerabilities'].items():
                            if vuln_version.lower() in service_info['version'].lower():
                                service_info['vulnerabilities'].append({
                                    'version': vuln_version,
                                    'description': vuln_desc
                                })

                with self.lock:
                    self.results['services'][port] = service_info
                    if self.verbose:
                        logging.info(f"Service detected on port {port}: {service_info['service']}")

            sock.close()

        except Exception as e:
            logging.error(f"Error probing port {port}: {e}")

    def save_results(self):
        """Save scan results to a JSON file."""
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            filename = os.path.join(self.output_dir, f"service_scan_{timestamp}.json")
            
            with open(filename, 'w') as f:
                json.dump(self.results, f, indent=4)
            logging.info(f"Results saved to {filename}")
        except Exception as e:
            logging.error(f"Failed to save results: {e}")

    def execute(self):
        """Execute the service scanning and fingerprinting process."""
        logging.info(f"Starting service scan on {self.target}")
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(self.probe_service, self.ports)
        
        self.save_results()
        return self.results

def save_settings(target, ports, output_dir, delay, verbose):
    """Save settings to JSON file."""
    try:
        os.makedirs(DEFAULT_SETTINGS_DIR, exist_ok=True)
        settings = {
            "target": target,
            "ports": ports,
            "output_dir": output_dir,
            "delay": delay,
            "verbose": verbose
        }
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f)
        logging.info(f"Settings saved to {SETTINGS_FILE}")
    except Exception as e:
        logging.error(f"Failed to save settings: {e}")

def load_settings():
    """Load settings from JSON file."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load settings: {e}")
    return {}

def main():
    parser = argparse.ArgumentParser(description="Service fingerprinting and vulnerability detection tool")
    parser.add_argument("-t", "--target", help="Target IP or hostname")
    parser.add_argument("-p", "--ports", help="Ports to scan (comma-separated)")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR, help="Output directory")
    parser.add_argument("-d", "--delay", type=float, default=1, help="Delay between probes")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    args = parser.parse_args()

    settings = load_settings()
    target = args.target or settings.get("target")
    ports = [int(p) for p in args.ports.split(',')] if args.ports else settings.get("ports", DEFAULT_PORTS)
    output_dir = args.output or settings.get("output_dir")
    delay = args.delay or settings.get("delay")
    verbose = args.verbose or settings.get("verbose")

    if not target:
        logging.error("Target is required. Use -t or save it in settings")
        return

    save_settings(target, ports, output_dir, delay, verbose)

    scanner = ThorHammer(
        target=target,
        ports=ports,
        output_dir=output_dir,
        delay=delay,
        verbose=verbose
    )
    scanner.execute()

if __name__ == "__main__":
    main()