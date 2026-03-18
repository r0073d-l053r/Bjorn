"""valkyrie_scout.py - Web app scanner for hidden paths and directory enumeration."""

import os
import json
import requests
import argparse
from datetime import datetime
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
import re
from bs4 import BeautifulSoup


b_class       = "ValkyrieScout"
b_module      = "valkyrie_scout"
b_enabled    = 0

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Default settings
DEFAULT_OUTPUT_DIR = "/home/bjorn/Bjorn/data/output/webscan"
DEFAULT_SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
SETTINGS_FILE = os.path.join(DEFAULT_SETTINGS_DIR, "valkyrie_scout_settings.json")

# Common web vulnerabilities to check
VULNERABILITY_PATTERNS = {
    'sql_injection': [
        "error in your SQL syntax",
        "mysql_fetch_array",
        "ORA-",
        "PostgreSQL",
    ],
    'xss': [
        "<script>alert(1)</script>",
        "javascript:alert(1)",
    ],
    'lfi': [
        "include(",
        "require(",
        "include_once(",
        "require_once(",
    ]
}

class ValkyieScout:
    def __init__(self, url, wordlist=None, output_dir=DEFAULT_OUTPUT_DIR, threads=10, delay=0.1):
        self.base_url = url.rstrip('/')
        self.wordlist = wordlist
        self.output_dir = output_dir
        self.threads = threads
        self.delay = delay
        
        self.discovered_paths = set()
        self.vulnerabilities = []
        self.forms = []
        
        self.session = requests.Session()
        self.session.headers = {
            'User-Agent': 'Valkyrie Scout Web Scanner',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
        
        self.lock = threading.Lock()

    def load_wordlist(self):
        """Load directory wordlist."""
        if self.wordlist and os.path.exists(self.wordlist):
            with open(self.wordlist, 'r') as f:
                return [line.strip() for line in f if line.strip()]
        return [
            'admin', 'wp-admin', 'administrator', 'login', 'wp-login.php',
            'upload', 'uploads', 'backup', 'backups', 'config', 'configuration',
            'dev', 'development', 'test', 'testing', 'staging', 'prod',
            'api', 'v1', 'v2', 'beta', 'debug', 'console', 'phpmyadmin',
            'mysql', 'database', 'db', 'wp-content', 'includes', 'tmp', 'temp'
        ]

    def scan_path(self, path):
        """Scan a single path for existence and vulnerabilities."""
        url = urljoin(self.base_url, path)
        try:
            response = self.session.get(url, allow_redirects=False)
            
            if response.status_code in [200, 301, 302, 403]:
                with self.lock:
                    self.discovered_paths.add({
                        'path': path,
                        'url': url,
                        'status_code': response.status_code,
                        'content_length': len(response.content),
                        'timestamp': datetime.now().isoformat()
                    })
                
                # Scan for vulnerabilities
                self.check_vulnerabilities(url, response)
                
                # Extract and analyze forms
                self.analyze_forms(url, response)
                
        except Exception as e:
            logging.error(f"Error scanning {url}: {e}")

    def check_vulnerabilities(self, url, response):
        """Check for common vulnerabilities in the response."""
        try:
            content = response.text.lower()
            
            for vuln_type, patterns in VULNERABILITY_PATTERNS.items():
                for pattern in patterns:
                    if pattern.lower() in content:
                        with self.lock:
                            self.vulnerabilities.append({
                                'type': vuln_type,
                                'url': url,
                                'pattern': pattern,
                                'timestamp': datetime.now().isoformat()
                            })
                            
            # Additional checks
            self.check_security_headers(url, response)
            self.check_information_disclosure(url, response)
            
        except Exception as e:
            logging.error(f"Error checking vulnerabilities for {url}: {e}")

    def analyze_forms(self, url, response):
        """Analyze HTML forms for potential vulnerabilities."""
        try:
            soup = BeautifulSoup(response.text, 'html.parser')
            forms = soup.find_all('form')
            
            for form in forms:
                form_data = {
                    'url': url,
                    'method': form.get('method', 'get').lower(),
                    'action': urljoin(url, form.get('action', '')),
                    'inputs': [],
                    'timestamp': datetime.now().isoformat()
                }
                
                # Analyze form inputs
                for input_field in form.find_all(['input', 'textarea']):
                    input_data = {
                        'type': input_field.get('type', 'text'),
                        'name': input_field.get('name', ''),
                        'id': input_field.get('id', ''),
                        'required': input_field.get('required') is not None
                    }
                    form_data['inputs'].append(input_data)
                
                with self.lock:
                    self.forms.append(form_data)
                    
        except Exception as e:
            logging.error(f"Error analyzing forms in {url}: {e}")

    def check_security_headers(self, url, response):
        """Check for missing or misconfigured security headers."""
        security_headers = {
            'X-Frame-Options': 'Missing X-Frame-Options header',
            'X-XSS-Protection': 'Missing X-XSS-Protection header',
            'X-Content-Type-Options': 'Missing X-Content-Type-Options header',
            'Strict-Transport-Security': 'Missing HSTS header',
            'Content-Security-Policy': 'Missing Content-Security-Policy'
        }
        
        for header, message in security_headers.items():
            if header not in response.headers:
                with self.lock:
                    self.vulnerabilities.append({
                        'type': 'missing_security_header',
                        'url': url,
                        'detail': message,
                        'timestamp': datetime.now().isoformat()
                    })

    def check_information_disclosure(self, url, response):
        """Check for information disclosure in response."""
        patterns = {
            'email': r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
            'internal_ip': r'\b(?:192\.168|10\.|172\.(?:1[6-9]|2[0-9]|3[01]))\.\d{1,3}\.\d{1,3}\b',
            'debug_info': r'(?:stack trace|debug|error|exception)',
            'version_info': r'(?:version|powered by|built with)'
        }
        
        content = response.text.lower()
        for info_type, pattern in patterns.items():
            matches = re.findall(pattern, content, re.IGNORECASE)
            if matches:
                with self.lock:
                    self.vulnerabilities.append({
                        'type': 'information_disclosure',
                        'url': url,
                        'info_type': info_type,
                        'findings': matches,
                        'timestamp': datetime.now().isoformat()
                    })

    def save_results(self):
        """Save scan results to JSON files."""
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            
            # Save discovered paths
            if self.discovered_paths:
                paths_file = os.path.join(self.output_dir, f"paths_{timestamp}.json")
                with open(paths_file, 'w') as f:
                    json.dump(list(self.discovered_paths), f, indent=4)
            
            # Save vulnerabilities
            if self.vulnerabilities:
                vulns_file = os.path.join(self.output_dir, f"vulnerabilities_{timestamp}.json")
                with open(vulns_file, 'w') as f:
                    json.dump(self.vulnerabilities, f, indent=4)
            
            # Save form analysis
            if self.forms:
                forms_file = os.path.join(self.output_dir, f"forms_{timestamp}.json")
                with open(forms_file, 'w') as f:
                    json.dump(self.forms, f, indent=4)
            
            logging.info(f"Results saved to {self.output_dir}")
            
        except Exception as e:
            logging.error(f"Failed to save results: {e}")

    def execute(self):
        """Execute the web application scan."""
        try:
            logging.info(f"Starting web scan on {self.base_url}")
            paths = self.load_wordlist()
            
            with ThreadPoolExecutor(max_workers=self.threads) as executor:
                executor.map(self.scan_path, paths)
            
            self.save_results()
            
        except Exception as e:
            logging.error(f"Scan error: {e}")
        finally:
            self.session.close()

def save_settings(url, wordlist, output_dir, threads, delay):
    """Save settings to JSON file."""
    try:
        os.makedirs(DEFAULT_SETTINGS_DIR, exist_ok=True)
        settings = {
            "url": url,
            "wordlist": wordlist,
            "output_dir": output_dir,
            "threads": threads,
            "delay": delay
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
    parser = argparse.ArgumentParser(description="Web application vulnerability scanner")
    parser.add_argument("-u", "--url", help="Target URL to scan")
    parser.add_argument("-w", "--wordlist", help="Path to directory wordlist")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR, help="Output directory")
    parser.add_argument("-t", "--threads", type=int, default=10, help="Number of threads")
    parser.add_argument("-d", "--delay", type=float, default=0.1, help="Delay between requests")
    args = parser.parse_args()

    settings = load_settings()
    url = args.url or settings.get("url")
    wordlist = args.wordlist or settings.get("wordlist")
    output_dir = args.output or settings.get("output_dir")
    threads = args.threads or settings.get("threads")
    delay = args.delay or settings.get("delay")

    if not url:
        logging.error("URL is required. Use -u or save it in settings")
        return

    save_settings(url, wordlist, output_dir, threads, delay)

    scanner = ValkyieScout(
        url=url,
        wordlist=wordlist,
        output_dir=output_dir,
        threads=threads,
        delay=delay
    )
    scanner.execute()

if __name__ == "__main__":
    main()