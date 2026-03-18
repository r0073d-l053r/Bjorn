"""rune_cracker.py - Threaded hash cracker with wordlist + mutation rules (MD5/SHA/NTLM)."""

import os
import json
import hashlib
import argparse
from datetime import datetime
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
import itertools
import re


b_class       = "RuneCracker"
b_module      = "rune_cracker"
b_enabled    = 0

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Default settings
DEFAULT_OUTPUT_DIR = "/home/bjorn/Bjorn/data/output/hashes"
DEFAULT_SETTINGS_DIR = "/home/bjorn/.settings_bjorn"
SETTINGS_FILE = os.path.join(DEFAULT_SETTINGS_DIR, "rune_cracker_settings.json")

# Supported hash types and their patterns
HASH_PATTERNS = {
    'md5': r'^[a-fA-F0-9]{32}$',
    'sha1': r'^[a-fA-F0-9]{40}$',
    'sha256': r'^[a-fA-F0-9]{64}$',
    'sha512': r'^[a-fA-F0-9]{128}$',
    'ntlm': r'^[a-fA-F0-9]{32}$'
}

class RuneCracker:
    def __init__(self, input_file, wordlist=None, rules=None, hash_type=None, output_dir=DEFAULT_OUTPUT_DIR):
        self.input_file = input_file
        self.wordlist = wordlist
        self.rules = rules
        self.hash_type = hash_type
        self.output_dir = output_dir
        
        self.hashes = set()
        self.cracked = {}
        self.lock = threading.Lock()
        
        # Load mutation rules
        self.mutation_rules = self.load_rules()

    def load_hashes(self):
        """Load hashes from input file and validate format."""
        try:
            with open(self.input_file, 'r') as f:
                for line in f:
                    hash_value = line.strip()
                    if self.hash_type:
                        if re.match(HASH_PATTERNS[self.hash_type], hash_value):
                            self.hashes.add(hash_value)
                    else:
                        # Try to auto-detect hash type
                        for h_type, pattern in HASH_PATTERNS.items():
                            if re.match(pattern, hash_value):
                                self.hashes.add(hash_value)
                                break
            
            logging.info(f"Loaded {len(self.hashes)} valid hashes")
            
        except Exception as e:
            logging.error(f"Error loading hashes: {e}")

    def load_wordlist(self):
        """Load password wordlist."""
        if self.wordlist and os.path.exists(self.wordlist):
            with open(self.wordlist, 'r', errors='ignore') as f:
                return [line.strip() for line in f if line.strip()]
        return ['password', 'admin', '123456', 'qwerty', 'letmein']

    def load_rules(self):
        """Load mutation rules."""
        if self.rules and os.path.exists(self.rules):
            with open(self.rules, 'r') as f:
                return [line.strip() for line in f if line.strip() and not line.startswith('#')]
        return [
            'capitalize',
            'lowercase',
            'uppercase',
            'l33t',
            'append_numbers',
            'prepend_numbers',
            'toggle_case'
        ]

    def apply_mutations(self, word):
        """Apply various mutation rules to a word."""
        mutations = set([word])
        
        for rule in self.mutation_rules:
            if rule == 'capitalize':
                mutations.add(word.capitalize())
            elif rule == 'lowercase':
                mutations.add(word.lower())
            elif rule == 'uppercase':
                mutations.add(word.upper())
            elif rule == 'l33t':
                mutations.add(word.replace('a', '@').replace('e', '3').replace('i', '1')
                            .replace('o', '0').replace('s', '5'))
            elif rule == 'append_numbers':
                mutations.update(word + str(n) for n in range(100))
            elif rule == 'prepend_numbers':
                mutations.update(str(n) + word for n in range(100))
            elif rule == 'toggle_case':
                mutations.add(''.join(c.upper() if i % 2 else c.lower() 
                                   for i, c in enumerate(word)))

        return mutations

    def hash_password(self, password, hash_type):
        """Generate hash for a password using specified algorithm."""
        if hash_type == 'md5':
            return hashlib.md5(password.encode()).hexdigest()
        elif hash_type == 'sha1':
            return hashlib.sha1(password.encode()).hexdigest()
        elif hash_type == 'sha256':
            return hashlib.sha256(password.encode()).hexdigest()
        elif hash_type == 'sha512':
            return hashlib.sha512(password.encode()).hexdigest()
        elif hash_type == 'ntlm':
            return hashlib.new('md4', password.encode('utf-16le')).hexdigest()
        
        return None

    def crack_password(self, password):
        """Attempt to crack hashes using a single password and its mutations."""
        try:
            mutations = self.apply_mutations(password)
            
            for mutation in mutations:
                for hash_type in HASH_PATTERNS.keys():
                    if not self.hash_type or self.hash_type == hash_type:
                        hash_value = self.hash_password(mutation, hash_type)
                        
                        if hash_value in self.hashes:
                            with self.lock:
                                self.cracked[hash_value] = {
                                    'password': mutation,
                                    'hash_type': hash_type,
                                    'timestamp': datetime.now().isoformat()
                                }
                                logging.info(f"Cracked hash: {hash_value[:8]}... = {mutation}")
                                
        except Exception as e:
            logging.error(f"Error cracking with password {password}: {e}")

    def save_results(self):
        """Save cracked passwords to JSON file."""
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            
            results = {
                'timestamp': datetime.now().isoformat(),
                'total_hashes': len(self.hashes),
                'cracked_count': len(self.cracked),
                'cracked_hashes': self.cracked
            }
            
            output_file = os.path.join(self.output_dir, f"cracked_{timestamp}.json")
            with open(output_file, 'w') as f:
                json.dump(results, f, indent=4)
                
            logging.info(f"Results saved to {output_file}")
            
        except Exception as e:
            logging.error(f"Failed to save results: {e}")

    def execute(self):
        """Execute the password cracking process."""
        try:
            logging.info("Starting password cracking process")
            self.load_hashes()
            
            if not self.hashes:
                logging.error("No valid hashes loaded")
                return
            
            wordlist = self.load_wordlist()
            
            with ThreadPoolExecutor(max_workers=10) as executor:
                executor.map(self.crack_password, wordlist)
            
            self.save_results()
            
            logging.info(f"Cracking completed. Cracked {len(self.cracked)}/{len(self.hashes)} hashes")
            
        except Exception as e:
            logging.error(f"Error during execution: {e}")

def save_settings(input_file, wordlist, rules, hash_type, output_dir):
    """Save settings to JSON file."""
    try:
        os.makedirs(DEFAULT_SETTINGS_DIR, exist_ok=True)
        settings = {
            "input_file": input_file,
            "wordlist": wordlist,
            "rules": rules,
            "hash_type": hash_type,
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
    parser = argparse.ArgumentParser(description="Advanced password cracker")
    parser.add_argument("-i", "--input", help="Input file containing hashes")
    parser.add_argument("-w", "--wordlist", help="Path to password wordlist")
    parser.add_argument("-r", "--rules", help="Path to rules file")
    parser.add_argument("-t", "--type", choices=list(HASH_PATTERNS.keys()), help="Hash type")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR, help="Output directory")
    args = parser.parse_args()

    settings = load_settings()
    input_file = args.input or settings.get("input_file")
    wordlist = args.wordlist or settings.get("wordlist")
    rules = args.rules or settings.get("rules")
    hash_type = args.type or settings.get("hash_type")
    output_dir = args.output or settings.get("output_dir")

    if not input_file:
        logging.error("Input file is required. Use -i or save it in settings")
        return

    save_settings(input_file, wordlist, rules, hash_type, output_dir)

    cracker = RuneCracker(
        input_file=input_file,
        wordlist=wordlist,
        rules=rules,
        hash_type=hash_type,
        output_dir=output_dir
    )
    cracker.execute()

if __name__ == "__main__":
    main()