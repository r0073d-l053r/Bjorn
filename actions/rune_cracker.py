#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rune_cracker.py -- Advanced password cracker for BJORN.
Supports multiple hash formats and uses bruteforce_common for progress tracking.
Optimized for Pi Zero 2 (limited CPU/RAM).
"""

import os
import json
import hashlib
import re
import threading
import time
from datetime import datetime


from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Set

from logger import Logger
from actions.bruteforce_common import ProgressTracker, merged_password_plan

logger = Logger(name="rune_cracker.py")

# -------------------- Action metadata --------------------
b_class       = "RuneCracker"
b_module      = "rune_cracker"
b_status      = "rune_cracker"
b_port        = None
b_service     = "[]"
b_trigger     = "on_start"
b_parent      = None
b_action      = "normal"
b_priority    = 40
b_cooldown    = 0
b_rate_limit  = None
b_timeout     = 600
b_max_retries = 1
b_stealth_level = 10  # Local cracking is stealthy
b_risk_level  = "low"
b_enabled     = 1
b_tags        = ["crack", "hash", "bruteforce", "local"]
b_category    = "exploitation"
b_name        = "Rune Cracker"
b_description = "Advanced password cracker with mutation rules and progress tracking."
b_author      = "Bjorn Team"
b_version     = "2.1.0"
b_icon        = "RuneCracker.png"

# Supported hash types and their patterns
HASH_PATTERNS = {
    'md5': r'^[a-fA-F0-9]{32}$',
    'sha1': r'^[a-fA-F0-9]{40}$',
    'sha256': r'^[a-fA-F0-9]{64}$',
    'sha512': r'^[a-fA-F0-9]{128}$',
    'ntlm': r'^[a-fA-F0-9]{32}$'
}


class RuneCracker:
    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.hashes: Set[str] = set()
        self.cracked: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.hash_type: Optional[str] = None
        
        # Performance tuning for Pi Zero 2
        self.max_workers = int(getattr(shared_data, "rune_cracker_workers", 4))
        
    def _hash_password(self, password: str, h_type: str) -> Optional[str]:
        """Generate hash for a password using specified algorithm."""
        try:
            if h_type == 'md5':
                return hashlib.md5(password.encode()).hexdigest()
            elif h_type == 'sha1':
                return hashlib.sha1(password.encode()).hexdigest()
            elif h_type == 'sha256':
                return hashlib.sha256(password.encode()).hexdigest()
            elif h_type == 'sha512':
                return hashlib.sha512(password.encode()).hexdigest()
            elif h_type == 'ntlm':
                # NTLM is MD4(UTF-16LE(password))
                try:
                    return hashlib.new('md4', password.encode('utf-16le')).hexdigest()
                except ValueError:
                    # MD4 not available in this Python build (e.g., FIPS mode)
                    return None
        except Exception as e:
            logger.debug(f"Hashing error ({h_type}): {e}")
        return None

    def _crack_password_worker(self, password: str, progress: ProgressTracker):
        """Worker function for cracking passwords."""
        if self.shared_data.orchestrator_should_exit:
            return

        for h_type in HASH_PATTERNS.keys():
            if self.hash_type and self.hash_type != h_type:
                continue
                
            hv = self._hash_password(password, h_type)
            if hv and hv in self.hashes:
                with self.lock:
                    if hv not in self.cracked:
                        self.cracked[hv] = {
                            "password": password,
                            "type": h_type,
                            "cracked_at": datetime.now().isoformat()
                        }
                        logger.success(f"Cracked {h_type}: {hv[:8]}... -> {password}")
                        self.shared_data.log_milestone(b_class, "Cracked", f"{h_type} found!")
                        # EPD live status update
                        self.shared_data.comment_params = {"hashes": str(len(self.hashes)), "cracked": str(len(self.cracked))}

        progress.advance()

    def execute(self, ip, port, row, status_key) -> str:
        """Standard Orchestrator entry point."""
        input_file = str(getattr(self.shared_data, "rune_cracker_input", ""))
        wordlist_path = str(getattr(self.shared_data, "rune_cracker_wordlist", ""))
        self.hash_type = getattr(self.shared_data, "rune_cracker_type", None)
        _fallback_dir = os.path.join(getattr(self.shared_data, "data_dir", "/home/bjorn/Bjorn/data"), "output", "hashes")
        output_dir = getattr(self.shared_data, "rune_cracker_output", _fallback_dir)

        if not input_file or not os.path.exists(input_file):
            # Fallback: Check for latest odin_recon or other hashes if running in generic mode
            potential_input = os.path.join(self.shared_data.data_dir, "output", "packets", "latest_hashes.txt")
            if os.path.exists(potential_input):
                input_file = potential_input
                logger.info(f"RuneCracker: No input provided, using fallback: {input_file}")
            else:
                logger.error(f"Input file not found: {input_file}")
                return "failed"

        # Reset per-run state to prevent accumulation across reused instances
        self.cracked.clear()
        # Load hashes
        self.hashes.clear()
        try:
            with open(input_file, 'r', encoding="utf-8", errors="ignore") as f:
                for line in f:
                    hv = line.strip()
                    if not hv: continue
                    # Auto-detect or validate
                    for h_t, pat in HASH_PATTERNS.items():
                        if re.match(pat, hv):
                            if not self.hash_type or self.hash_type == h_t:
                                self.hashes.add(hv)
                                break
        except Exception as e:
            logger.error(f"Error loading hashes: {e}")
            return "failed"

        if not self.hashes:
            logger.warning("No valid hashes found in input file.")
            return "failed"

        logger.info(f"RuneCracker: Loaded {len(self.hashes)} hashes. Starting engine...")
        self.shared_data.log_milestone(b_class, "Initialization", f"Loaded {len(self.hashes)} hashes")
        # EPD live status
        self.shared_data.comment_params = {"hashes": str(len(self.hashes)), "cracked": "0"}

        # Prepare password plan
        dict_passwords = []
        if wordlist_path and os.path.exists(wordlist_path):
            with open(wordlist_path, 'r', encoding="utf-8", errors="ignore") as f:
                dict_passwords = [l.strip() for l in f if l.strip()]
        else:
            # Fallback tiny list
            dict_passwords = ['password', 'admin', '123456', 'qwerty', 'bjorn']

        dictionary, fallback = merged_password_plan(self.shared_data, dict_passwords)
        all_candidates = dictionary + fallback
        
        progress = ProgressTracker(self.shared_data, len(all_candidates))
        self.shared_data.log_milestone(b_class, "Bruteforce", f"Testing {len(all_candidates)} candidates")

        try:
            try:
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    for pwd in all_candidates:
                        if self.shared_data.orchestrator_should_exit:
                            executor.shutdown(wait=False, cancel_futures=True)
                            return "interrupted"
                        executor.submit(self._crack_password_worker, pwd, progress)
            except Exception as e:
                logger.error(f"Cracking engine error: {e}")
                return "failed"

            # Save results
            if self.cracked:
                os.makedirs(output_dir, exist_ok=True)
                out_file = os.path.join(output_dir, f"cracked_{int(time.time())}.json")
                with open(out_file, 'w', encoding="utf-8") as f:
                    json.dump({
                        "target_file": input_file,
                        "total_hashes": len(self.hashes),
                        "cracked_count": len(self.cracked),
                        "results": self.cracked
                    }, f, indent=4)
                logger.success(f"Cracked {len(self.cracked)} hashes! Results: {out_file}")
                self.shared_data.log_milestone(b_class, "Complete", f"Cracked {len(self.cracked)} hashes")
                return "success"

            logger.info("Cracking finished. No matches found.")
            self.shared_data.log_milestone(b_class, "Finished", "No passwords found")
            return "success" # Still success even if 0 cracked, as it finished the task
        finally:
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}

if __name__ == "__main__":
    # Minimal CLI for testing
    import sys
    from init_shared import shared_data
    if len(sys.argv) < 2:
        print("Usage: rune_cracker.py <hash_file>")
        sys.exit(1)
    
    shared_data.rune_cracker_input = sys.argv[1]
    cracker = RuneCracker(shared_data)
    cracker.execute("local", None, {}, "rune_cracker")
