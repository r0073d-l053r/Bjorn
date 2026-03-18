"""dns_pillager.py - DNS recon and subdomain enumeration with threaded brute."""

import os
import json
import dns.resolver
import threading
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import logging


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


b_class       = "DNSPillager"
b_module      = "dns_pillager"
b_enabled    = 0

# Default settings
DEFAULT_OUTPUT_DIR = "/home/bjorn/Bjorn/data/output/dns"
DEFAULT_SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
SETTINGS_FILE = os.path.join(DEFAULT_SETTINGS_DIR, "dns_pillager_settings.json")
DEFAULT_RECORD_TYPES = ['A', 'AAAA', 'MX', 'NS', 'TXT', 'CNAME', 'SOA']

class DNSPillager:
    def __init__(self, domain, wordlist=None, output_dir=DEFAULT_OUTPUT_DIR, threads=10, recursive=False):
        self.domain = domain
        self.wordlist = wordlist
        self.output_dir = output_dir
        self.threads = threads
        self.recursive = recursive
        self.discovered_domains = set()
        self.lock = threading.Lock()
        self.resolver = dns.resolver.Resolver()
        self.resolver.timeout = 1
        self.resolver.lifetime = 1

    def save_results(self, results):
        """Save enumeration results to a JSON file."""
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            filename = os.path.join(self.output_dir, f"dns_enum_{timestamp}.json")
            
            with open(filename, 'w') as f:
                json.dump(results, f, indent=4)
            logging.info(f"Results saved to {filename}")
        except Exception as e:
            logging.error(f"Failed to save results: {e}")

    def query_domain(self, domain, record_type):
        """Query a domain for specific DNS record type."""
        try:
            answers = self.resolver.resolve(domain, record_type)
            return [str(answer) for answer in answers]
        except:
            return []

    def enumerate_domain(self, subdomain):
        """Enumerate a single subdomain for all record types."""
        full_domain = f"{subdomain}.{self.domain}" if subdomain else self.domain
        results = {'domain': full_domain, 'records': {}}

        for record_type in DEFAULT_RECORD_TYPES:
            records = self.query_domain(full_domain, record_type)
            if records:
                results['records'][record_type] = records
                with self.lock:
                    self.discovered_domains.add(full_domain)
                logging.info(f"Found {record_type} records for {full_domain}")

        return results if results['records'] else None

    def load_wordlist(self):
        """Load subdomain wordlist or use built-in list."""
        if self.wordlist and os.path.exists(self.wordlist):
            with open(self.wordlist, 'r') as f:
                return [line.strip() for line in f if line.strip()]
        return ['www', 'mail', 'remote', 'blog', 'webmail', 'server', 'ns1', 'ns2', 'smtp', 'secure']

    def execute(self):
        """Execute the DNS enumeration process."""
        results = {'timestamp': datetime.now().isoformat(), 'findings': []}
        subdomains = self.load_wordlist()
        
        logging.info(f"Starting DNS enumeration for {self.domain}")
        
        with ThreadPoolExecutor(max_workers=self.threads) as executor:
            enum_results = list(filter(None, executor.map(self.enumerate_domain, subdomains)))
            results['findings'].extend(enum_results)

        if self.recursive and self.discovered_domains:
            logging.info("Starting recursive enumeration")
            new_domains = set()
            for domain in self.discovered_domains:
                if domain != self.domain:
                    new_subdomains = [d.split('.')[0] for d in domain.split('.')[:-2]]
                    new_domains.update(new_subdomains)
            
            if new_domains:
                enum_results = list(filter(None, executor.map(self.enumerate_domain, new_domains)))
                results['findings'].extend(enum_results)

        self.save_results(results)
        return results

def save_settings(domain, wordlist, output_dir, threads, recursive):
    """Save settings to JSON file."""
    try:
        os.makedirs(DEFAULT_SETTINGS_DIR, exist_ok=True)
        settings = {
            "domain": domain,
            "wordlist": wordlist,
            "output_dir": output_dir,
            "threads": threads,
            "recursive": recursive
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
    parser = argparse.ArgumentParser(description="DNS Pillager for domain reconnaissance")
    parser.add_argument("-d", "--domain", help="Target domain for enumeration")
    parser.add_argument("-w", "--wordlist", help="Path to subdomain wordlist")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR, help="Output directory for results")
    parser.add_argument("-t", "--threads", type=int, default=10, help="Number of threads")
    parser.add_argument("-r", "--recursive", action="store_true", help="Enable recursive enumeration")
    args = parser.parse_args()

    settings = load_settings()
    domain = args.domain or settings.get("domain")
    wordlist = args.wordlist or settings.get("wordlist")
    output_dir = args.output or settings.get("output_dir")
    threads = args.threads or settings.get("threads")
    recursive = args.recursive or settings.get("recursive")

    if not domain:
        logging.error("Domain is required. Use -d or save it in settings")
        return

    save_settings(domain, wordlist, output_dir, threads, recursive)

    pillager = DNSPillager(
        domain=domain,
        wordlist=wordlist,
        output_dir=output_dir,
        threads=threads,
        recursive=recursive
    )
    pillager.execute()

if __name__ == "__main__":
    main()