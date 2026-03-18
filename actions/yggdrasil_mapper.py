#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yggdrasil_mapper.py - Traceroute-based network topology mapping to JSON.

Uses scapy ICMP (fallback: subprocess) and merges results across runs.
"""

import json
import logging
import os
import socket
import time
from datetime import datetime

from typing import Any, Dict, List, Optional, Tuple

from logger import Logger
from actions.bruteforce_common import ProgressTracker

logger = Logger(name="yggdrasil_mapper.py", level=logging.DEBUG)

# Silence scapy logging before import
logging.getLogger("scapy.runtime").setLevel(logging.ERROR)
logging.getLogger("scapy.interactive").setLevel(logging.ERROR)
logging.getLogger("scapy.loading").setLevel(logging.ERROR)

_SCAPY_AVAILABLE = False
try:
    from scapy.all import IP, ICMP, sr1, conf as scapy_conf
    scapy_conf.verb = 0
    _SCAPY_AVAILABLE = True
except ImportError:
    logger.warning("scapy not available; falling back to subprocess traceroute")
except Exception as exc:
    logger.warning(f"scapy import error ({exc}); falling back to subprocess traceroute")

# -------------------- Action metadata (AST-friendly) --------------------
b_class       = "YggdrasilMapper"
b_module      = "yggdrasil_mapper"
b_status      = "yggdrasil_mapper"
b_port        = None
b_service     = '[]'
b_trigger     = "on_host_alive"
b_parent      = None
b_action      = "normal"
b_requires    = '{"action":"NetworkScanner","status":"success","scope":"global"}'
b_priority    = 10
b_cooldown    = 3600
b_rate_limit  = "3/86400"
b_timeout     = 300
b_max_retries = 2
b_stealth_level = 6
b_risk_level  = "low"
b_enabled     = 1
b_tags        = ["topology", "network", "recon", "mapping"]
b_category    = "recon"
b_name        = "Yggdrasil Mapper"
b_description = (
    "Network topology mapper that discovers routing paths via traceroute, enriches "
    "nodes with service data from the DB, and saves a merged JSON topology graph. "
    "Lightweight -- no matplotlib or networkx required."
)
b_author      = "Bjorn Team"
b_version     = "2.0.0"
b_icon        = "YggdrasilMapper.png"

b_args = {
    "max_depth": {
        "type": "slider",
        "label": "Max trace depth (hops)",
        "min": 5,
        "max": 30,
        "step": 1,
        "default": 15,
        "help": "Maximum number of hops for traceroute probes.",
    },
    "probe_timeout": {
        "type": "slider",
        "label": "Probe timeout (s)",
        "min": 1,
        "max": 5,
        "step": 1,
        "default": 2,
        "help": "Timeout in seconds for each ICMP / TCP probe.",
    },
}

b_examples = [
    {"max_depth": 15, "probe_timeout": 2},
    {"max_depth": 10, "probe_timeout": 1},
    {"max_depth": 30, "probe_timeout": 3},
]

b_docs_url = "docs/actions/YggdrasilMapper.md"

# -------------------- Constants --------------------
_DATA_DIR   = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUTPUT_DIR  = os.path.join(_DATA_DIR, "output", "topology")

# Ports to verify during service enrichment (small set to stay Pi Zero friendly).
_VERIFY_PORTS = [22, 80, 443, 445, 3389, 8080]


# -------------------- Helpers --------------------

def _generate_mermaid_topology(topology: Dict[str, Any]) -> str:
    """Generate a Mermaid.js diagram string from topology data."""
    lines = ["graph TD"]
    
    # Define styles
    lines.append("  classDef target fill:#f96,stroke:#333,stroke-width:2px;")
    lines.append("  classDef router fill:#69f,stroke:#333,stroke-width:1px;")
    lines.append("  classDef unknown fill:#ccc,stroke:#333,stroke-dasharray: 5 5;")
    
    nodes = topology.get("nodes", {})
    for node_id, node in nodes.items():
        label = node.get("hostname") or node.get("ip")
        node_type = node.get("type", "unknown")
        
        # Sanitize label for Mermaid
        safe_label = str(label).replace(" ", "_").replace(".", "_").replace("-", "_")
        safe_id = node_id.replace(".", "_").replace("*", "unknown").replace("-", "_")
        
        lines.append(f'  {safe_id}["{label}"]')
        
        if node_type == "target":
            lines.append(f"  class {safe_id} target")
        elif node_type == "router":
            lines.append(f"  class {safe_id} router")
        else:
            lines.append(f"  class {safe_id} unknown")

    edges = topology.get("edges", [])
    for edge in edges:
        src = str(edge.get("source", "")).replace(".", "_").replace("*", "unknown").replace("-", "_")
        dst = str(edge.get("target", "")).replace(".", "_").replace("*", "unknown").replace("-", "_")
        if src and dst:
            rtt = edge.get("rtt_ms", 0)
            if rtt > 0:
                lines.append(f"  {src} -- {rtt}ms --> {dst}")
            else:
                lines.append(f"  {src} --> {dst}")
                
    return "\n".join(lines)


def _reverse_dns(ip: str) -> str:
    """Best-effort reverse DNS lookup. Returns hostname or empty string."""
    try:
        hostname, _, _ = socket.gethostbyaddr(ip)
        return hostname or ""
    except Exception:
        return ""


def _tcp_probe(ip: str, port: int, timeout_s: float) -> Tuple[bool, int]:
    """
    Quick TCP connect probe. Returns (is_open, rtt_ms).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    t0 = time.time()
    try:
        rc = s.connect_ex((ip, int(port)))
        rtt_ms = int((time.time() - t0) * 1000)
        return (rc == 0), rtt_ms
    except Exception:
        return False, 0
    finally:
        try:
            s.close()
        except Exception:
            pass


def _scapy_traceroute(target: str, max_depth: int, timeout_s: float) -> List[Dict[str, Any]]:
    """
    ICMP traceroute using scapy. Returns list of hop dicts:
    [{"hop": 1, "ip": "x.x.x.x", "rtt_ms": 12}, ...]
    """
    hops: List[Dict[str, Any]] = []
    for ttl in range(1, max_depth + 1):
        pkt = IP(dst=target, ttl=ttl) / ICMP()
        t0 = time.time()
        reply = sr1(pkt, timeout=timeout_s, verbose=0)
        rtt_ms = int((time.time() - t0) * 1000)

        if reply is None:
            hops.append({"hop": ttl, "ip": "*", "rtt_ms": 0})
            continue

        src = reply.src
        hops.append({"hop": ttl, "ip": src, "rtt_ms": rtt_ms})

        # Reached destination
        if src == target:
            break

    return hops


def _subprocess_traceroute(target: str, max_depth: int, timeout_s: float) -> List[Dict[str, Any]]:
    """
    Fallback traceroute using the system `traceroute` command.
    Works on Linux / macOS. On Windows falls back to `tracert`.
    """
    import subprocess
    import re

    hops: List[Dict[str, Any]] = []

    # Decide command based on platform
    if os.name == "nt":
        cmd = ["tracert", "-d", "-h", str(max_depth), "-w", str(int(timeout_s * 1000)), target]
    else:
        cmd = ["traceroute", "-n", "-m", str(max_depth), "-w", str(int(timeout_s)), target]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max_depth * timeout_s + 30,
        )
        output = proc.stdout or ""
    except FileNotFoundError:
        logger.error("traceroute/tracert command not found on this system")
        return hops
    except subprocess.TimeoutExpired:
        logger.warning(f"Subprocess traceroute to {target} timed out")
        return hops
    except Exception as exc:
        logger.error(f"Subprocess traceroute error: {exc}")
        return hops

    # Parse output lines
    ip_pattern = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})')
    rtt_pattern = re.compile(r'(\d+(?:\.\d+)?)\s*ms')
    hop_num = 0

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Skip header lines
        parts = stripped.split()
        if not parts:
            continue

        # Try to extract hop number from first token
        try:
            hop_candidate = int(parts[0])
        except (ValueError, IndexError):
            continue

        hop_num = hop_candidate
        ip_match = ip_pattern.search(stripped)
        rtt_match = rtt_pattern.search(stripped)

        hop_ip = ip_match.group(1) if ip_match else "*"
        hop_rtt = int(float(rtt_match.group(1))) if rtt_match else 0

        hops.append({"hop": hop_num, "ip": hop_ip, "rtt_ms": hop_rtt})

        # Stop if we reached the target
        if hop_ip == target:
            break

    return hops


def _load_existing_topology(output_dir: str) -> Dict[str, Any]:
    """
    Load the most recent aggregated topology JSON from output_dir.
    Returns an empty topology skeleton if nothing exists yet.
    """
    skeleton: Dict[str, Any] = {
        "version": b_version,
        "nodes": {},
        "edges": [],
        "metadata": {
            "created": datetime.utcnow().isoformat() + "Z",
            "updated": datetime.utcnow().isoformat() + "Z",
            "run_count": 0,
        },
    }

    if not os.path.isdir(output_dir):
        return skeleton

    # Find the latest aggregated file
    candidates = []
    try:
        for fname in os.listdir(output_dir):
            if fname.startswith("topology_aggregate") and fname.endswith(".json"):
                fpath = os.path.join(output_dir, fname)
                candidates.append((os.path.getmtime(fpath), fpath))
    except Exception:
        return skeleton

    if not candidates:
        return skeleton

    candidates.sort(reverse=True)
    latest_path = candidates[0][1]

    try:
        with open(latest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and "nodes" in data:
            return data
    except Exception as exc:
        logger.warning(f"Failed to load existing topology ({latest_path}): {exc}")

    return skeleton


def _merge_node(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two node dicts, preferring newer / non-empty values."""
    merged = dict(existing)
    for key, val in new.items():
        if val is None or val == "" or val == []:
            continue
        if key == "open_ports":
            # Union of port lists
            old_ports = set(merged.get("open_ports") or [])
            old_ports.update(val if isinstance(val, list) else [])
            merged["open_ports"] = sorted(old_ports)
        elif key == "rtt_ms":
            # Keep lowest non-zero RTT
            old_rtt = merged.get("rtt_ms") or 0
            new_rtt = val or 0
            if old_rtt == 0:
                merged["rtt_ms"] = new_rtt
            elif new_rtt > 0:
                merged["rtt_ms"] = min(old_rtt, new_rtt)
        else:
            merged[key] = val
    merged["last_seen"] = datetime.utcnow().isoformat() + "Z"
    return merged


def _edge_key(src: str, dst: str) -> str:
    """Canonical edge key (sorted to avoid duplicates)."""
    a, b = sorted([src, dst])
    return f"{a}--{b}"


# -------------------- Main Action Class --------------------

class YggdrasilMapper:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    # ---- Phase 1: Traceroute ----
    def _phase_traceroute(
        self,
        ip: str,
        max_depth: int,
        probe_timeout: float,
        progress: ProgressTracker,
        total_steps: int,
    ) -> List[Dict[str, Any]]:
        """Run traceroute to target. Returns list of hop dicts."""
        logger.info(f"Phase 1: Traceroute to {ip} (max_depth={max_depth})")

        if _SCAPY_AVAILABLE:
            hops = _scapy_traceroute(ip, max_depth, probe_timeout)
        else:
            hops = _subprocess_traceroute(ip, max_depth, probe_timeout)

        # Progress: phase 1 is 0-30%  (weight = 30% of total_steps)
        phase1_steps = max(1, int(total_steps * 0.30))
        progress.advance(phase1_steps)

        logger.info(f"Traceroute to {ip}: {len(hops)} hop(s) discovered")
        return hops

    # ---- Phase 2: Service Enrichment ----
    def _phase_enrich(
        self,
        ip: str,
        mac: str,
        row: Dict[str, Any],
        probe_timeout: float,
        progress: ProgressTracker,
        total_steps: int,
    ) -> Dict[str, Any]:
        """
        Enrich the target node with port / service data from the DB and
        optional TCP connect probes.
        """
        logger.info(f"Phase 2: Service enrichment for {ip}")

        node_info: Dict[str, Any] = {
            "ip": ip,
            "mac": mac,
            "hostname": "",
            "open_ports": [],
            "verified_ports": {},
            "vendor": "",
        }

        # Read hostname
        hostname = (row.get("Hostname") or row.get("hostname") or row.get("hostnames") or "").strip()
        if ";" in hostname:
            hostname = hostname.split(";", 1)[0].strip()
        if not hostname:
            hostname = _reverse_dns(ip)
        node_info["hostname"] = hostname

        # Query DB for known ports to prioritize probing
        db_ports = []
        host_data = None
        try:
            host_data = self.shared_data.db.get_host_by_mac(mac)
            if host_data and host_data.get("ports"):
                # Normalize ports from DB string
                db_ports = [int(p) for p in str(host_data["ports"]).split(";") if p.strip().isdigit()]
        except Exception as e:
            logger.debug(f"Failed to query DB for host ports: {e}")

        # Fallback to defaults if DB is empty
        if not db_ports:
            # Read existing ports from DB row (compatibility)
            ports_txt = str(row.get("Ports") or row.get("ports") or "")
            for p in ports_txt.split(";"):
                p = p.strip()
                if p.isdigit():
                    db_ports.append(int(p))
        
        node_info["open_ports"] = sorted(set(db_ports))

        # Vendor and OS guessing
        vendor = str(row.get("Vendor") or row.get("vendor") or "").strip()
        if not vendor and host_data:
            vendor = host_data.get("vendor", "")
        node_info["vendor"] = vendor

        # Guess OS if missing (leveraging FeatureLogger patterns if we had access, but we'll do basic here)
        # For now, we'll just store what we have.
        
        # Verify a small set of key ports via TCP connect
        verified: Dict[str, Dict[str, Any]] = {}
        # Prioritize ports we found in DB + a few common ones
        probe_candidates = sorted(set(db_ports + _VERIFY_PORTS))[:10]
        
        for port in probe_candidates:
            if self.shared_data.orchestrator_should_exit:
                break
            is_open, rtt = _tcp_probe(ip, port, probe_timeout)
            if is_open:
                verified[str(port)] = {"open": is_open, "rtt_ms": rtt}
                # Update node_info open_ports if we found a new one
                if port not in node_info["open_ports"]:
                    node_info["open_ports"].append(port)
                    node_info["open_ports"].sort()

        node_info["verified_ports"] = verified

        # Progress: phase 2 is 30-60%
        phase2_steps = max(1, int(total_steps * 0.30))
        progress.advance(phase2_steps)
        self.shared_data.log_milestone(b_class, "Enrichment", f"Discovered {len(node_info['open_ports'])} ports for {ip}")
        return node_info

    # ---- Phase 3: Build Topology ----
    def _phase_build_topology(
        self,
        ip: str,
        hops: List[Dict[str, Any]],
        target_node: Dict[str, Any],
        progress: ProgressTracker,
        total_steps: int,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Build nodes dict and edges list from traceroute hops and target enrichment.
        """
        logger.info(f"Phase 3: Building topology graph for {ip}")

        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []

        # Add target node
        nodes[ip] = {
            "ip": ip,
            "type": "target",
            "hostname": target_node.get("hostname", ""),
            "mac": target_node.get("mac", ""),
            "vendor": target_node.get("vendor", ""),
            "open_ports": target_node.get("open_ports", []),
            "verified_ports": target_node.get("verified_ports", {}),
            "rtt_ms": 0,
            "first_seen": datetime.utcnow().isoformat() + "Z",
            "last_seen": datetime.utcnow().isoformat() + "Z",
        }

        # Add hop nodes and edges
        prev_ip: Optional[str] = None
        for hop in hops:
            hop_ip = hop.get("ip", "*")
            hop_rtt = hop.get("rtt_ms", 0)
            hop_num = hop.get("hop", 0)

            if hop_ip == "*":
                # Unknown hop -- still create a placeholder node
                placeholder = f"*_hop{hop_num}"
                nodes[placeholder] = {
                    "ip": placeholder,
                    "type": "unknown_hop",
                    "hostname": "",
                    "mac": "",
                    "vendor": "",
                    "open_ports": [],
                    "verified_ports": {},
                    "rtt_ms": 0,
                    "hop_number": hop_num,
                    "first_seen": datetime.utcnow().isoformat() + "Z",
                    "last_seen": datetime.utcnow().isoformat() + "Z",
                }
                if prev_ip is not None:
                    edges.append({
                        "source": prev_ip,
                        "target": placeholder,
                        "hop": hop_num,
                        "rtt_ms": hop_rtt,
                        "discovered": datetime.utcnow().isoformat() + "Z",
                    })
                prev_ip = placeholder
                continue

            # Real hop IP
            if hop_ip not in nodes:
                hop_hostname = _reverse_dns(hop_ip)
                nodes[hop_ip] = {
                    "ip": hop_ip,
                    "type": "router" if hop_ip != ip else "target",
                    "hostname": hop_hostname,
                    "mac": "",
                    "vendor": "",
                    "open_ports": [],
                    "verified_ports": {},
                    "rtt_ms": hop_rtt,
                    "hop_number": hop_num,
                    "first_seen": datetime.utcnow().isoformat() + "Z",
                    "last_seen": datetime.utcnow().isoformat() + "Z",
                }
            else:
                # Update RTT if this hop is lower
                existing_rtt = nodes[hop_ip].get("rtt_ms") or 0
                if existing_rtt == 0 or (hop_rtt > 0 and hop_rtt < existing_rtt):
                    nodes[hop_ip]["rtt_ms"] = hop_rtt

            if prev_ip is not None:
                edges.append({
                    "source": prev_ip,
                    "target": hop_ip,
                    "hop": hop_num,
                    "rtt_ms": hop_rtt,
                    "discovered": datetime.utcnow().isoformat() + "Z",
                })

            prev_ip = hop_ip

        # Progress: phase 3 is 60-80%  (weight = 20% of total_steps)
        phase3_steps = max(1, int(total_steps * 0.20))
        progress.advance(phase3_steps)

        logger.info(f"Topology for {ip}: {len(nodes)} node(s), {len(edges)} edge(s)")
        return nodes, edges

    # ---- Phase 4: Aggregate ----
    def _phase_aggregate(
        self,
        new_nodes: Dict[str, Dict[str, Any]],
        new_edges: List[Dict[str, Any]],
        progress: ProgressTracker,
        total_steps: int,
    ) -> Dict[str, Any]:
        """
        Merge new topology data with previous runs.
        """
        logger.info("Phase 4: Aggregating topology data")

        topology = _load_existing_topology(OUTPUT_DIR)

        # Merge nodes
        existing_nodes = topology.get("nodes") or {}
        if not isinstance(existing_nodes, dict):
            existing_nodes = {}

        for node_id, node_data in new_nodes.items():
            if node_id in existing_nodes:
                existing_nodes[node_id] = _merge_node(existing_nodes[node_id], node_data)
            else:
                existing_nodes[node_id] = node_data

        topology["nodes"] = existing_nodes

        # Merge edges (deduplicate by canonical key)
        existing_edges = topology.get("edges") or []
        if not isinstance(existing_edges, list):
            existing_edges = []

        seen_keys: set = set()
        merged_edges: List[Dict[str, Any]] = []

        for edge in existing_edges:
            ek = _edge_key(str(edge.get("source", "")), str(edge.get("target", "")))
            if ek not in seen_keys:
                seen_keys.add(ek)
                merged_edges.append(edge)

        for edge in new_edges:
            ek = _edge_key(str(edge.get("source", "")), str(edge.get("target", "")))
            if ek not in seen_keys:
                seen_keys.add(ek)
                merged_edges.append(edge)

        topology["edges"] = merged_edges

        # Update metadata
        meta = topology.get("metadata") or {}
        meta["updated"] = datetime.utcnow().isoformat() + "Z"
        meta["run_count"] = int(meta.get("run_count") or 0) + 1
        meta["node_count"] = len(existing_nodes)
        meta["edge_count"] = len(merged_edges)
        topology["metadata"] = meta
        topology["version"] = b_version

        # Progress: phase 4 is 80-95%  (weight = 15% of total_steps)
        phase4_steps = max(1, int(total_steps * 0.15))
        progress.advance(phase4_steps)

        logger.info(
            f"Aggregated topology: {meta['node_count']} node(s), "
            f"{meta['edge_count']} edge(s), run #{meta['run_count']}"
        )
        return topology

    # ---- Phase 5: Save ----
    def _phase_save(
        self,
        topology: Dict[str, Any],
        ip: str,
        progress: ProgressTracker,
        total_steps: int,
    ) -> str:
        """
        Save topology JSON to disk. Returns the file path written.
        """
        logger.info("Phase 5: Saving topology data")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")

        # Per-target snapshot
        snapshot_name = f"topology_{ip.replace('.', '_')}_{timestamp}.json"
        snapshot_path = os.path.join(OUTPUT_DIR, snapshot_name)

        # Aggregated file (single canonical file, overwritten each run)
        aggregate_name = f"topology_aggregate_{timestamp}.json"
        aggregate_path = os.path.join(OUTPUT_DIR, aggregate_name)

        try:
            with open(snapshot_path, "w", encoding="utf-8") as fh:
                json.dump(topology, fh, indent=2, ensure_ascii=True, default=str)
            logger.info(f"Snapshot saved: {snapshot_path}")
        except Exception as exc:
            logger.error(f"Failed to write snapshot {snapshot_path}: {exc}")

        try:
            with open(aggregate_path, "w", encoding="utf-8") as fh:
                json.dump(topology, fh, indent=2, ensure_ascii=True, default=str)
            logger.info(f"Aggregate saved: {aggregate_path}")
        except Exception as exc:
            logger.error(f"Failed to write aggregate {aggregate_path}: {exc}")

        # Save Mermaid diagram
        mermaid_path = os.path.join(OUTPUT_DIR, f"topology_{ip.replace('.', '_')}_{timestamp}.mermaid")
        try:
            mermaid_str = _generate_mermaid_topology(topology)
            with open(mermaid_path, "w", encoding="utf-8") as fh:
                fh.write(mermaid_str)
            logger.info(f"Mermaid topology saved: {mermaid_path}")
        except Exception as exc:
            logger.error(f"Failed to write Mermaid topology: {exc}")

        # Progress: phase 5 is 95-100%  (weight = 5% of total_steps)
        phase5_steps = max(1, int(total_steps * 0.05))
        progress.advance(phase5_steps)
        self.shared_data.log_milestone(b_class, "Save", f"Topology saved for {ip}")

        return aggregate_path

    # ---- Main execute ----
    def execute(self, ip, port, row, status_key) -> str:
        """
        Orchestrator entry point. Maps topology for a single target host.

        Returns:
            'success'  -- topology data written successfully.
            'failed'   -- an error prevented meaningful output.
            'interrupted' -- orchestrator requested early exit.
        """
        if self.shared_data.orchestrator_should_exit:
            return "interrupted"

        # --- Identity cache from DB row ---
        mac = (
            row.get("MAC Address")
            or row.get("mac_address")
            or row.get("mac")
            or ""
        ).strip()
        hostname = (
            row.get("Hostname")
            or row.get("hostname")
            or row.get("hostnames")
            or ""
        ).strip()
        if ";" in hostname:
            hostname = hostname.split(";", 1)[0].strip()

        # --- Configurable arguments ---
        max_depth = int(getattr(self.shared_data, "yggdrasil_max_depth", 15))
        probe_timeout = float(getattr(self.shared_data, "yggdrasil_probe_timeout", 2.0))

        # Clamp to sane ranges
        max_depth = max(5, min(max_depth, 30))
        probe_timeout = max(1.0, min(probe_timeout, 5.0))

        # --- UI status ---
        self.shared_data.bjorn_orch_status = "yggdrasil_mapper"
        self.shared_data.bjorn_status_text2 = f"{ip}"
        self.shared_data.comment_params = {"ip": ip, "mac": mac, "phase": "init"}

        # Total steps for progress (arbitrary units; phases will consume proportional slices)
        total_steps = 100
        progress = ProgressTracker(self.shared_data, total_steps)

        try:
            # ---- Phase 1: Traceroute (0-30%) ----
            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            self.shared_data.log_milestone(b_class, "Traceroute", f"Running trace to {ip}")
            hops = self._phase_traceroute(ip, max_depth, probe_timeout, progress, total_steps)

            # ---- Phase 2: Service Enrichment (30-60%) ----
            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            self.shared_data.comment_params = {"ip": ip, "phase": "enrich"}
            target_node = self._phase_enrich(ip, mac, row, probe_timeout, progress, total_steps)

            # ---- Phase 3: Build Topology (60-80%) ----
            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            self.shared_data.comment_params = {"ip": ip, "phase": "topology"}
            new_nodes, new_edges = self._phase_build_topology(
                ip, hops, target_node, progress, total_steps
            )

            # ---- Phase 4: Aggregate (80-95%) ----
            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            self.shared_data.comment_params = {"ip": ip, "phase": "aggregate"}
            topology = self._phase_aggregate(new_nodes, new_edges, progress, total_steps)

            # ---- Phase 5: Save (95-100%) ----
            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            self.shared_data.comment_params = {"ip": ip, "phase": "save"}
            saved_path = self._phase_save(topology, ip, progress, total_steps)

            # Final UI update
            node_count = len(topology.get("nodes") or {})
            edge_count = len(topology.get("edges") or [])
            hop_count = len([h for h in hops if h.get("ip") != "*"])

            self.shared_data.comment_params = {
                "ip": ip,
                "hops": str(hop_count),
                "nodes": str(node_count),
                "edges": str(edge_count),
                "file": os.path.basename(saved_path),
            }

            progress.set_complete()
            logger.info(
                f"YggdrasilMapper complete for {ip}: "
                f"{hop_count} hops, {node_count} nodes, {edge_count} edges"
            )
            return "success"

        except Exception as exc:
            logger.error(f"YggdrasilMapper failed for {ip}: {exc}", exc_info=True)
            self.shared_data.comment_params = {"ip": ip, "error": str(exc)[:120]}
            return "failed"

        finally:
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}
            self.shared_data.bjorn_status_text2 = ""


# -------------------- Optional CLI (debug / manual) --------------------
if __name__ == "__main__":
    import argparse
    from shared import SharedData

    parser = argparse.ArgumentParser(description="YggdrasilMapper (network topology mapper)")
    parser.add_argument("--ip", required=True, help="Target IP to trace")
    parser.add_argument("--max-depth", type=int, default=15, help="Max traceroute depth")
    parser.add_argument("--timeout", type=float, default=2.0, help="Probe timeout in seconds")
    args = parser.parse_args()

    sd = SharedData()

    # Push CLI args into shared_data so execute() picks them up
    sd.yggdrasil_max_depth = args.max_depth
    sd.yggdrasil_probe_timeout = args.timeout

    mapper = YggdrasilMapper(sd)
    row = {
        "MAC Address": getattr(sd, "get_raspberry_mac", lambda: "__GLOBAL__")() or "__GLOBAL__",
        "Hostname": "",
        "Ports": "",
    }
    result = mapper.execute(args.ip, None, row, "yggdrasil_mapper")
    print(f"Result: {result}")
