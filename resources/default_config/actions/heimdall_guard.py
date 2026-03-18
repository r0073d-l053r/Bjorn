"""heimdall_guard.py - IDS/IPS evasion via timing jitter, fragmentation, and traffic shaping."""

import os
import json
import argparse
from datetime import datetime
import logging
import random
import time
import socket
import struct
import threading
from scapy.all import *
from collections import deque

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')





b_class       = "HeimdallGuard"
b_module      = "heimdall_guard"
b_enabled    = 0

# Default settings
DEFAULT_OUTPUT_DIR = "/home/bjorn/Bjorn/data/output/stealth"
DEFAULT_SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
SETTINGS_FILE = os.path.join(DEFAULT_SETTINGS_DIR, "heimdall_guard_settings.json")

class HeimdallGuard:
    def __init__(self, interface, mode='all', base_delay=1, random_factor=0.5, output_dir=DEFAULT_OUTPUT_DIR):
        self.interface = interface
        self.mode = mode
        self.base_delay = base_delay
        self.random_factor = random_factor
        self.output_dir = output_dir
        
        self.packet_queue = deque()
        self.active = False
        self.lock = threading.Lock()
        
        # Statistics
        self.stats = {
            'packets_processed': 0,
            'packets_fragmented': 0,
            'timing_adjustments': 0
        }

    def initialize_interface(self):
        """Configure network interface for stealth operations."""
        try:
            # Disable NIC offloading features that might interfere with packet manipulation
            commands = [
                f"ethtool -K {self.interface} tso off",  # TCP segmentation offload
                f"ethtool -K {self.interface} gso off",  # Generic segmentation offload
                f"ethtool -K {self.interface} gro off",  # Generic receive offload
                f"ethtool -K {self.interface} lro off"   # Large receive offload
            ]
            
            for cmd in commands:
                try:
                    subprocess.run(cmd.split(), check=True)
                except subprocess.CalledProcessError:
                    logging.warning(f"Failed to execute: {cmd}")
                    
            logging.info(f"Interface {self.interface} configured for stealth operations")
            return True
            
        except Exception as e:
            logging.error(f"Failed to initialize interface: {e}")
            return False

    def calculate_timing(self):
        """Calculate timing delays with randomization."""
        base = self.base_delay
        variation = self.random_factor * base
        return max(0, base + random.uniform(-variation, variation))

    def fragment_packet(self, packet, mtu=1500):
        """Fragment packets to avoid detection patterns."""
        try:
            if IP in packet:
                # Fragment IP packets
                frags = []
                payload = bytes(packet[IP].payload)
                header_length = len(packet) - len(payload)
                max_size = mtu - header_length
                
                # Create fragments
                offset = 0
                while offset < len(payload):
                    frag_size = min(max_size, len(payload) - offset)
                    frag_payload = payload[offset:offset + frag_size]
                    
                    # Create fragment packet
                    frag = packet.copy()
                    frag[IP].flags = 'MF' if offset + frag_size < len(payload) else 0
                    frag[IP].frag = offset // 8
                    frag[IP].payload = Raw(frag_payload)
                    
                    frags.append(frag)
                    offset += frag_size
                
                return frags
            return [packet]
            
        except Exception as e:
            logging.error(f"Error fragmenting packet: {e}")
            return [packet]

    def randomize_ttl(self, packet):
        """Randomize TTL values to avoid fingerprinting."""
        if IP in packet:
            ttl_values = [32, 64, 128, 255]  # Common TTL values
            packet[IP].ttl = random.choice(ttl_values)
        return packet

    def modify_tcp_options(self, packet):
        """Modify TCP options to avoid fingerprinting."""
        if TCP in packet:
            # Common window sizes
            window_sizes = [8192, 16384, 32768, 65535]
            packet[TCP].window = random.choice(window_sizes)
            
            # Randomize TCP options
            tcp_options = []
            
            # MSS option
            mss_values = [1400, 1460, 1440]
            tcp_options.append(('MSS', random.choice(mss_values)))
            
            # Window scale
            if random.random() < 0.5:
                tcp_options.append(('WScale', random.randint(0, 14)))
            
            # SACK permitted
            if random.random() < 0.5:
                tcp_options.append(('SAckOK', ''))
            
            packet[TCP].options = tcp_options
            
        return packet

    def process_packet(self, packet):
        """Process a packet according to stealth settings."""
        processed_packets = []
        
        try:
            if self.mode in ['all', 'fragmented']:
                fragments = self.fragment_packet(packet)
                processed_packets.extend(fragments)
                self.stats['packets_fragmented'] += len(fragments) - 1
            else:
                processed_packets.append(packet)
            
            # Apply additional stealth techniques
            final_packets = []
            for pkt in processed_packets:
                pkt = self.randomize_ttl(pkt)
                pkt = self.modify_tcp_options(pkt)
                final_packets.append(pkt)
            
            self.stats['packets_processed'] += len(final_packets)
            return final_packets
            
        except Exception as e:
            logging.error(f"Error processing packet: {e}")
            return [packet]

    def send_packet(self, packet):
        """Send packet with timing adjustments."""
        try:
            if self.mode in ['all', 'timing']:
                delay = self.calculate_timing()
                time.sleep(delay)
                self.stats['timing_adjustments'] += 1
            
            send(packet, iface=self.interface, verbose=False)
            
        except Exception as e:
            logging.error(f"Error sending packet: {e}")

    def packet_processor_thread(self):
        """Process packets from the queue."""
        while self.active:
            try:
                if self.packet_queue:
                    packet = self.packet_queue.popleft()
                    processed_packets = self.process_packet(packet)
                    
                    for processed in processed_packets:
                        self.send_packet(processed)
                else:
                    time.sleep(0.1)
                    
            except Exception as e:
                logging.error(f"Error in packet processor thread: {e}")

    def start(self):
        """Start stealth operations."""
        if not self.initialize_interface():
            return False
        
        self.active = True
        self.processor_thread = threading.Thread(target=self.packet_processor_thread)
        self.processor_thread.start()
        return True

    def stop(self):
        """Stop stealth operations."""
        self.active = False
        if hasattr(self, 'processor_thread'):
            self.processor_thread.join()
        self.save_stats()

    def queue_packet(self, packet):
        """Queue a packet for processing."""
        self.packet_queue.append(packet)

    def save_stats(self):
        """Save operation statistics."""
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            
            stats_file = os.path.join(self.output_dir, f"stealth_stats_{timestamp}.json")
            
            with open(stats_file, 'w') as f:
                json.dump({
                    'timestamp': datetime.now().isoformat(),
                    'interface': self.interface,
                    'mode': self.mode,
                    'stats': self.stats
                }, f, indent=4)
                
            logging.info(f"Statistics saved to {stats_file}")
            
        except Exception as e:
            logging.error(f"Failed to save statistics: {e}")

def save_settings(interface, mode, base_delay, random_factor, output_dir):
    """Save settings to JSON file."""
    try:
        os.makedirs(DEFAULT_SETTINGS_DIR, exist_ok=True)
        settings = {
            "interface": interface,
            "mode": mode,
            "base_delay": base_delay,
            "random_factor": random_factor,
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
    parser = argparse.ArgumentParser(description="Stealth operations module")
    parser.add_argument("-i", "--interface", help="Network interface to use")
    parser.add_argument("-m", "--mode", choices=['timing', 'random', 'fragmented', 'all'], 
                        default='all', help="Operating mode")
    parser.add_argument("-d", "--delay", type=float, default=1, help="Base delay between operations")
    parser.add_argument("-r", "--randomize", type=float, default=0.5, help="Randomization factor")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR, help="Output directory")
    args = parser.parse_args()

    settings = load_settings()
    interface = args.interface or settings.get("interface")
    mode = args.mode or settings.get("mode")
    base_delay = args.delay or settings.get("base_delay")
    random_factor = args.randomize or settings.get("random_factor")
    output_dir = args.output or settings.get("output_dir")

    if not interface:
        interface = conf.iface
        logging.info(f"Using default interface: {interface}")

    save_settings(interface, mode, base_delay, random_factor, output_dir)

    guard = HeimdallGuard(
        interface=interface,
        mode=mode,
        base_delay=base_delay,
        random_factor=random_factor,
        output_dir=output_dir
    )

    try:
        if guard.start():
            logging.info("Heimdall Guard started. Press Ctrl+C to stop.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Stopping Heimdall Guard...")
        guard.stop()

if __name__ == "__main__":
    main()