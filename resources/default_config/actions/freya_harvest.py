"""freya_harvest.py - Aggregates findings from other modules into JSON/HTML/MD reports."""

import os
import json
import argparse
from datetime import datetime
import logging
import time
import shutil
import glob
import watchdog.observers
import watchdog.events
import markdown
import jinja2
from collections import defaultdict


b_class       = "FreyaHarvest"
b_module      = "freya_harvest"
b_enabled    = 0

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Default settings
DEFAULT_INPUT_DIR = "/home/bjorn/Bjorn/data/output"
DEFAULT_OUTPUT_DIR = "/home/bjorn/Bjorn/data/reports"
DEFAULT_SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
SETTINGS_FILE = os.path.join(DEFAULT_SETTINGS_DIR, "freya_harvest_settings.json")

# HTML template for reports
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Bjorn Reconnaissance Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .section { margin: 20px 0; padding: 10px; border: 1px solid #ddd; }
        .vuln-high { background-color: #ffebee; }
        .vuln-medium { background-color: #fff3e0; }
        .vuln-low { background-color: #f1f8e9; }
        table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #f5f5f5; }
        h1, h2, h3 { color: #333; }
        .metadata { color: #666; font-style: italic; }
        .timestamp { font-weight: bold; }
    </style>
</head>
<body>
    <h1>Bjorn Reconnaissance Report</h1>
    <div class="metadata">
        <p class="timestamp">Generated: {{ timestamp }}</p>
    </div>
    {% for section in sections %}
    <div class="section">
        <h2>{{ section.title }}</h2>
        {{ section.content }}
    </div>
    {% endfor %}
</body>
</html>
"""

class FreyaHarvest:
    def __init__(self, input_dir=DEFAULT_INPUT_DIR, output_dir=DEFAULT_OUTPUT_DIR, 
                 formats=None, watch_mode=False, clean=False):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.formats = formats or ['json', 'html', 'md']
        self.watch_mode = watch_mode
        self.clean = clean
        
        self.data = defaultdict(list)
        self.observer = None

    def clean_directories(self):
        """Clean output directory if requested."""
        if self.clean and os.path.exists(self.output_dir):
            shutil.rmtree(self.output_dir)
            os.makedirs(self.output_dir)
            logging.info(f"Cleaned output directory: {self.output_dir}")

    def collect_wifi_data(self):
        """Collect WiFi-related findings."""
        try:
            wifi_dir = os.path.join(self.input_dir, "wifi")
            if os.path.exists(wifi_dir):
                for file in glob.glob(os.path.join(wifi_dir, "*.json")):
                    with open(file, 'r') as f:
                        data = json.load(f)
                        self.data['wifi'].append(data)
        except Exception as e:
            logging.error(f"Error collecting WiFi data: {e}")

    def collect_network_data(self):
        """Collect network topology and host findings."""
        try:
            network_dir = os.path.join(self.input_dir, "topology")
            if os.path.exists(network_dir):
                for file in glob.glob(os.path.join(network_dir, "*.json")):
                    with open(file, 'r') as f:
                        data = json.load(f)
                        self.data['network'].append(data)
        except Exception as e:
            logging.error(f"Error collecting network data: {e}")

    def collect_vulnerability_data(self):
        """Collect vulnerability findings."""
        try:
            vuln_dir = os.path.join(self.input_dir, "webscan")
            if os.path.exists(vuln_dir):
                for file in glob.glob(os.path.join(vuln_dir, "*.json")):
                    with open(file, 'r') as f:
                        data = json.load(f)
                        self.data['vulnerabilities'].append(data)
        except Exception as e:
            logging.error(f"Error collecting vulnerability data: {e}")

    def collect_credential_data(self):
        """Collect credential findings."""
        try:
            cred_dir = os.path.join(self.input_dir, "packets")
            if os.path.exists(cred_dir):
                for file in glob.glob(os.path.join(cred_dir, "*.json")):
                    with open(file, 'r') as f:
                        data = json.load(f)
                        self.data['credentials'].append(data)
        except Exception as e:
            logging.error(f"Error collecting credential data: {e}")

    def collect_data(self):
        """Collect all data from various sources."""
        self.data.clear()  # Reset data before collecting
        self.collect_wifi_data()
        self.collect_network_data()
        self.collect_vulnerability_data()
        self.collect_credential_data()
        logging.info("Data collection completed")

    def generate_json_report(self):
        """Generate JSON format report."""
        try:
            report = {
                'timestamp': datetime.now().isoformat(),
                'findings': dict(self.data)
            }
            
            os.makedirs(self.output_dir, exist_ok=True)
            output_file = os.path.join(self.output_dir, 
                                     f"report_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json")
            
            with open(output_file, 'w') as f:
                json.dump(report, f, indent=4)
                
            logging.info(f"JSON report saved to {output_file}")
            
        except Exception as e:
            logging.error(f"Error generating JSON report: {e}")

    def generate_html_report(self):
        """Generate HTML format report."""
        try:
            template = jinja2.Template(HTML_TEMPLATE)
            sections = []
            
            # Network Section
            if self.data['network']:
                content = "<h3>Network Topology</h3>"
                for topology in self.data['network']:
                    content += f"<p>Hosts discovered: {len(topology.get('hosts', []))}</p>"
                    content += "<table><tr><th>IP</th><th>MAC</th><th>Open Ports</th><th>Status</th></tr>"
                    for ip, data in topology.get('hosts', {}).items():
                        ports = data.get('ports', [])
                        mac = data.get('mac', 'Unknown')
                        status = data.get('status', 'Unknown')
                        content += f"<tr><td>{ip}</td><td>{mac}</td><td>{', '.join(map(str, ports))}</td><td>{status}</td></tr>"
                    content += "</table>"
                sections.append({"title": "Network Information", "content": content})
            
            # WiFi Section
            if self.data['wifi']:
                content = "<h3>WiFi Findings</h3>"
                for wifi_data in self.data['wifi']:
                    content += "<table><tr><th>SSID</th><th>BSSID</th><th>Security</th><th>Signal</th><th>Channel</th></tr>"
                    for network in wifi_data.get('networks', []):
                        content += f"<tr><td>{network.get('ssid', 'Unknown')}</td>"
                        content += f"<td>{network.get('bssid', 'Unknown')}</td>"
                        content += f"<td>{network.get('security', 'Unknown')}</td>"
                        content += f"<td>{network.get('signal_strength', 'Unknown')}</td>"
                        content += f"<td>{network.get('channel', 'Unknown')}</td></tr>"
                    content += "</table>"
                sections.append({"title": "WiFi Networks", "content": content})
            
            # Vulnerabilities Section
            if self.data['vulnerabilities']:
                content = "<h3>Discovered Vulnerabilities</h3>"
                for vuln_data in self.data['vulnerabilities']:
                    content += "<table><tr><th>Type</th><th>Severity</th><th>Target</th><th>Description</th><th>Recommendation</th></tr>"
                    for vuln in vuln_data.get('findings', []):
                        severity_class = f"vuln-{vuln.get('severity', 'low').lower()}"
                        content += f"<tr class='{severity_class}'>"
                        content += f"<td>{vuln.get('type', 'Unknown')}</td>"
                        content += f"<td>{vuln.get('severity', 'Unknown')}</td>"
                        content += f"<td>{vuln.get('target', 'Unknown')}</td>"
                        content += f"<td>{vuln.get('description', 'No description')}</td>"
                        content += f"<td>{vuln.get('recommendation', 'No recommendation')}</td></tr>"
                    content += "</table>"
                sections.append({"title": "Vulnerabilities", "content": content})
            
            # Credentials Section
            if self.data['credentials']:
                content = "<h3>Discovered Credentials</h3>"
                content += "<table><tr><th>Type</th><th>Source</th><th>Service</th><th>Username</th><th>Timestamp</th></tr>"
                for cred_data in self.data['credentials']:
                    for cred in cred_data.get('credentials', []):
                        content += f"<tr><td>{cred.get('type', 'Unknown')}</td>"
                        content += f"<td>{cred.get('source', 'Unknown')}</td>"
                        content += f"<td>{cred.get('service', 'Unknown')}</td>"
                        content += f"<td>{cred.get('username', 'Unknown')}</td>"
                        content += f"<td>{cred.get('timestamp', 'Unknown')}</td></tr>"
                content += "</table>"
                sections.append({"title": "Credentials", "content": content})
            
            # Generate HTML
            os.makedirs(self.output_dir, exist_ok=True)
            html = template.render(
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                sections=sections
            )
            
            output_file = os.path.join(self.output_dir, 
                                     f"report_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.html")
            
            with open(output_file, 'w') as f:
                f.write(html)
                
            logging.info(f"HTML report saved to {output_file}")
            
        except Exception as e:
            logging.error(f"Error generating HTML report: {e}")

    def generate_markdown_report(self):
            """Generate Markdown format report."""
            try:
                md_content = [
                    "# Bjorn Reconnaissance Report",
                    f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                ]
                
                # Network Section
                if self.data['network']:
                    md_content.append("## Network Information")
                    for topology in self.data['network']:
                        md_content.append(f"\nHosts discovered: {len(topology.get('hosts', []))}")
                        md_content.append("\n| IP | MAC | Open Ports | Status |")
                        md_content.append("|-------|-------|------------|---------|")
                        for ip, data in topology.get('hosts', {}).items():
                            ports = data.get('ports', [])
                            mac = data.get('mac', 'Unknown')
                            status = data.get('status', 'Unknown')
                            md_content.append(f"| {ip} | {mac} | {', '.join(map(str, ports))} | {status} |")
                
                # WiFi Section
                if self.data['wifi']:
                    md_content.append("\n## WiFi Networks")
                    md_content.append("\n| SSID | BSSID | Security | Signal | Channel |")
                    md_content.append("|------|--------|-----------|---------|----------|")
                    for wifi_data in self.data['wifi']:
                        for network in wifi_data.get('networks', []):
                            md_content.append(
                                f"| {network.get('ssid', 'Unknown')} | "
                                f"{network.get('bssid', 'Unknown')} | "
                                f"{network.get('security', 'Unknown')} | "
                                f"{network.get('signal_strength', 'Unknown')} | "
                                f"{network.get('channel', 'Unknown')} |"
                            )
                
                # Vulnerabilities Section
                if self.data['vulnerabilities']:
                    md_content.append("\n## Vulnerabilities")
                    md_content.append("\n| Type | Severity | Target | Description | Recommendation |")
                    md_content.append("|------|-----------|--------|-------------|----------------|")
                    for vuln_data in self.data['vulnerabilities']:
                        for vuln in vuln_data.get('findings', []):
                            md_content.append(
                                f"| {vuln.get('type', 'Unknown')} | "
                                f"{vuln.get('severity', 'Unknown')} | "
                                f"{vuln.get('target', 'Unknown')} | "
                                f"{vuln.get('description', 'No description')} | "
                                f"{vuln.get('recommendation', 'No recommendation')} |"
                            )
                
                # Credentials Section
                if self.data['credentials']:
                    md_content.append("\n## Discovered Credentials")
                    md_content.append("\n| Type | Source | Service | Username | Timestamp |")
                    md_content.append("|------|---------|----------|-----------|------------|")
                    for cred_data in self.data['credentials']:
                        for cred in cred_data.get('credentials', []):
                            md_content.append(
                                f"| {cred.get('type', 'Unknown')} | "
                                f"{cred.get('source', 'Unknown')} | "
                                f"{cred.get('service', 'Unknown')} | "
                                f"{cred.get('username', 'Unknown')} | "
                                f"{cred.get('timestamp', 'Unknown')} |"
                            )
                
                os.makedirs(self.output_dir, exist_ok=True)
                output_file = os.path.join(self.output_dir, 
                                        f"report_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.md")
                
                with open(output_file, 'w') as f:
                    f.write('\n'.join(md_content))
                    
                logging.info(f"Markdown report saved to {output_file}")
                
            except Exception as e:
                logging.error(f"Error generating Markdown report: {e}")


    def generate_reports(self):
        """Generate reports in all specified formats."""
        os.makedirs(self.output_dir, exist_ok=True)
        
        if 'json' in self.formats:
            self.generate_json_report()
        if 'html' in self.formats:
            self.generate_html_report()
        if 'md' in self.formats:
            self.generate_markdown_report()

    def start_watching(self):
        """Start watching for new data files."""
        class FileHandler(watchdog.events.FileSystemEventHandler):
            def __init__(self, harvester):
                self.harvester = harvester
            
            def on_created(self, event):
                if event.is_directory:
                    return
                if event.src_path.endswith('.json'):
                    logging.info(f"New data file detected: {event.src_path}")
                    self.harvester.collect_data()
                    self.harvester.generate_reports()

        self.observer = watchdog.observers.Observer()
        self.observer.schedule(FileHandler(self), self.input_dir, recursive=True)
        self.observer.start()
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.observer.stop()
        self.observer.join()

    def execute(self):
        """Execute the data collection and reporting process."""
        try:
            logging.info("Starting data collection")
            
            if self.clean:
                self.clean_directories()
            
            # Initial data collection and report generation
            self.collect_data()
            self.generate_reports()
            
            # Start watch mode if enabled
            if self.watch_mode:
                logging.info("Starting watch mode for new data")
                try:
                    self.start_watching()
                except KeyboardInterrupt:
                    logging.info("Watch mode stopped by user")
                finally:
                    if self.observer:
                        self.observer.stop()
                        self.observer.join()
            
            logging.info("Data collection and reporting completed")
            
        except Exception as e:
            logging.error(f"Error during execution: {e}")
            raise
        finally:
            # Ensure observer is stopped if watch mode was active
            if self.observer and self.observer.is_alive():
                self.observer.stop()
                self.observer.join()

def save_settings(input_dir, output_dir, formats, watch_mode, clean):
    """Save settings to JSON file."""
    try:
        os.makedirs(DEFAULT_SETTINGS_DIR, exist_ok=True)
        settings = {
            "input_dir": input_dir,
            "output_dir": output_dir,
            "formats": formats,
            "watch_mode": watch_mode,
            "clean": clean
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
    parser = argparse.ArgumentParser(description="Data collection and organization tool")
    parser.add_argument("-i", "--input", default=DEFAULT_INPUT_DIR, help="Input directory to monitor")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR, help="Output directory for reports")
    parser.add_argument("-f", "--format", choices=['json', 'html', 'md', 'all'], default='all', 
                        help="Output format")
    parser.add_argument("-w", "--watch", action="store_true", help="Watch for new findings")
    parser.add_argument("-c", "--clean", action="store_true", help="Clean old data before processing")
    args = parser.parse_args()

    settings = load_settings()
    input_dir = args.input or settings.get("input_dir")
    output_dir = args.output or settings.get("output_dir")
    formats = ['json', 'html', 'md'] if args.format == 'all' else [args.format]
    watch_mode = args.watch or settings.get("watch_mode", False)
    clean = args.clean or settings.get("clean", False)

    save_settings(input_dir, output_dir, formats, watch_mode, clean)

    harvester = FreyaHarvest(
        input_dir=input_dir,
        output_dir=output_dir,
        formats=formats,
        watch_mode=watch_mode,
        clean=clean
    )
    harvester.execute()

if __name__ == "__main__":
    main()