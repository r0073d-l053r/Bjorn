"""yggdrasil_mapper.py - Network topology mapper with traceroute and graph visualization."""

import os
import json
import argparse
from datetime import datetime
import logging
import subprocess
import networkx as nx
import matplotlib.pyplot as plt
import nmap
import scapy.all as scapy
from scapy.layers.inet import IP, ICMP, TCP
import threading
import queue


b_class       = "YggdrasilMapper"
b_module      = "yggdrasil_mapper"
b_enabled    = 0

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Default settings
DEFAULT_OUTPUT_DIR = "/home/bjorn/Bjorn/data/output/topology"
DEFAULT_SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
SETTINGS_FILE = os.path.join(DEFAULT_SETTINGS_DIR, "yggdrasil_mapper_settings.json")

class YggdrasilMapper:
    def __init__(self, network_range, interface=None, max_depth=5, output_dir=DEFAULT_OUTPUT_DIR, timeout=2):
        self.network_range = network_range
        self.interface = interface or scapy.conf.iface
        self.max_depth = max_depth
        self.output_dir = output_dir
        self.timeout = timeout
        
        self.graph = nx.Graph()
        self.hosts = {}
        self.routes = {}
        self.lock = threading.Lock()
        
        # For parallel processing
        self.queue = queue.Queue()
        self.results = queue.Queue()

    def discover_hosts(self):
        """Discover live hosts in the network range."""
        try:
            logging.info(f"Discovering hosts in {self.network_range}")
            
            # ARP scan for local network
            arp_request = scapy.ARP(pdst=self.network_range)
            broadcast = scapy.Ether(dst="ff:ff:ff:ff:ff:ff")
            packets = broadcast/arp_request
            
            answered, _ = scapy.srp(packets, timeout=self.timeout, iface=self.interface, verbose=False)
            
            for sent, received in answered:
                ip = received.psrc
                mac = received.hwsrc
                self.hosts[ip] = {'mac': mac, 'status': 'up'}
                logging.info(f"Discovered host: {ip} ({mac})")
            
            # Additional Nmap scan for service discovery
            nm = nmap.PortScanner()
            nm.scan(hosts=self.network_range, arguments=f'-sn -T4')
            
            for host in nm.all_hosts():
                if host not in self.hosts:
                    self.hosts[host] = {'status': 'up'}
                    logging.info(f"Discovered host: {host}")
                    
        except Exception as e:
            logging.error(f"Error discovering hosts: {e}")

    def trace_route(self, target):
        """Perform traceroute to a target."""
        try:
            hops = []
            for ttl in range(1, self.max_depth + 1):
                pkt = IP(dst=target, ttl=ttl)/ICMP()
                reply = scapy.sr1(pkt, timeout=self.timeout, verbose=False)
                
                if reply is None:
                    continue
                
                if reply.src == target:
                    hops.append(reply.src)
                    break
                
                hops.append(reply.src)
                
            return hops
        except Exception as e:
            logging.error(f"Error tracing route to {target}: {e}")
            return []

    def scan_ports(self, ip):
        """Scan common ports on a host."""
        try:
            common_ports = [21, 22, 23, 25, 53, 80, 443, 445, 3389]
            open_ports = []
            
            for port in common_ports:
                tcp_connect = IP(dst=ip)/TCP(dport=port, flags="S")
                response = scapy.sr1(tcp_connect, timeout=self.timeout, verbose=False)
                
                if response and response.haslayer(TCP):
                    if response[TCP].flags == 0x12:  # SYN-ACK
                        open_ports.append(port)
                        # Send RST to close connection
                        rst = IP(dst=ip)/TCP(dport=port, flags="R")
                        scapy.send(rst, verbose=False)
            
            return open_ports
        except Exception as e:
            logging.error(f"Error scanning ports for {ip}: {e}")
            return []

    def worker(self):
        """Worker function for parallel processing."""
        while True:
            try:
                task = self.queue.get()
                if task is None:
                    break
                
                ip = task
                hops = self.trace_route(ip)
                ports = self.scan_ports(ip)
                
                self.results.queue.put({
                    'ip': ip,
                    'hops': hops,
                    'ports': ports
                })
                
                self.queue.task_done()
            except Exception as e:
                logging.error(f"Worker error: {e}")
                self.queue.task_done()

    def build_topology(self):
        """Build network topology by tracing routes and scanning hosts."""
        try:
            # Start worker threads
            workers = []
            for _ in range(5):  # Number of parallel workers
                t = threading.Thread(target=self.worker)
                t.start()
                workers.append(t)
            
            # Add tasks to queue
            for ip in self.hosts.keys():
                self.queue.put(ip)
            
            # Add None to queue to stop workers
            for _ in workers:
                self.queue.put(None)
            
            # Wait for all workers to complete
            for t in workers:
                t.join()
            
            # Process results
            while not self.results.empty():
                result = self.results.get()
                ip = result['ip']
                hops = result['hops']
                ports = result['ports']
                
                self.hosts[ip]['ports'] = ports
                if len(hops) > 1:
                    self.routes[ip] = hops
                
                # Add nodes and edges to graph
                self.graph.add_node(ip, **self.hosts[ip])
                for i in range(len(hops) - 1):
                    self.graph.add_edge(hops[i], hops[i + 1])
                    
        except Exception as e:
            logging.error(f"Error building topology: {e}")

    def generate_visualization(self):
        """Generate network topology visualization."""
        try:
            plt.figure(figsize=(12, 8))
            
            # Position nodes using spring layout
            pos = nx.spring_layout(self.graph)
            
            # Draw nodes
            nx.draw_networkx_nodes(self.graph, pos, node_size=500)
            
            # Draw edges
            nx.draw_networkx_edges(self.graph, pos)
            
            # Add labels
            labels = {}
            for node in self.graph.nodes():
                label = f"{node}\n"
                if 'ports' in self.hosts[node]:
                    label += f"Ports: {', '.join(map(str, self.hosts[node]['ports']))}"
                labels[node] = label
            
            nx.draw_networkx_labels(self.graph, pos, labels, font_size=8)
            
            # Save visualization
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            viz_path = os.path.join(self.output_dir, f"topology_{timestamp}.png")
            plt.savefig(viz_path)
            plt.close()
            
            logging.info(f"Visualization saved to {viz_path}")
            
        except Exception as e:
            logging.error(f"Error generating visualization: {e}")

    def save_results(self):
        """Save topology data to JSON file."""
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            
            results = {
                'timestamp': datetime.now().isoformat(),
                'network_range': self.network_range,
                'hosts': self.hosts,
                'routes': self.routes,
                'topology': {
                    'nodes': list(self.graph.nodes()),
                    'edges': list(self.graph.edges())
                }
            }
            
            output_file = os.path.join(self.output_dir, f"topology_{timestamp}.json")
            with open(output_file, 'w') as f:
                json.dump(results, f, indent=4)
                
            logging.info(f"Results saved to {output_file}")
            
        except Exception as e:
            logging.error(f"Failed to save results: {e}")

    def execute(self):
        """Execute the network mapping process."""
        try:
            logging.info(f"Starting network mapping of {self.network_range}")
            
            # Discovery phase
            self.discover_hosts()
            if not self.hosts:
                logging.error("No hosts discovered")
                return
            
            # Topology building phase
            self.build_topology()
            
            # Generate outputs
            self.generate_visualization()
            self.save_results()
            
            logging.info("Network mapping completed")
            
        except Exception as e:
            logging.error(f"Error during execution: {e}")

def save_settings(network_range, interface, max_depth, output_dir, timeout):
    """Save settings to JSON file."""
    try:
        os.makedirs(DEFAULT_SETTINGS_DIR, exist_ok=True)
        settings = {
            "network_range": network_range,
            "interface": interface,
            "max_depth": max_depth,
            "output_dir": output_dir,
            "timeout": timeout
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
    parser = argparse.ArgumentParser(description="Network topology mapping tool")
    parser.add_argument("-r", "--range", help="Network range to scan (CIDR)")
    parser.add_argument("-i", "--interface", help="Network interface to use")
    parser.add_argument("-d", "--depth", type=int, default=5, help="Maximum trace depth")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR, help="Output directory")
    parser.add_argument("-t", "--timeout", type=int, default=2, help="Timeout for probes")
    args = parser.parse_args()

    settings = load_settings()
    network_range = args.range or settings.get("network_range")
    interface = args.interface or settings.get("interface")
    max_depth = args.depth or settings.get("max_depth")
    output_dir = args.output or settings.get("output_dir")
    timeout = args.timeout or settings.get("timeout")

    if not network_range:
        logging.error("Network range is required. Use -r or save it in settings")
        return

    save_settings(network_range, interface, max_depth, output_dir, timeout)

    mapper = YggdrasilMapper(
        network_range=network_range,
        interface=interface,
        max_depth=max_depth,
        output_dir=output_dir,
        timeout=timeout
    )
    mapper.execute()

if __name__ == "__main__":
    main()