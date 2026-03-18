#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""berserker_force.py - Rate-limited service stress testing with degradation analysis.

Measures baseline response times, applies light load (max 50 req/s), then reports per-port degradation.
"""

import json
import logging
import os
import random
import socket
import ssl
import statistics
import time
import threading
from datetime import datetime, timezone

from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError

from logger import Logger
from actions.bruteforce_common import ProgressTracker

logger = Logger(name="berserker_force.py", level=logging.DEBUG)

# -------------------- Scapy (optional) ----------------------------------------
_HAS_SCAPY = False
try:
    from scapy.all import IP, TCP, sr1, conf as scapy_conf  # type: ignore
    _HAS_SCAPY = True
except ImportError:
    logger.info("scapy not available -- SYN probe mode will fall back to TCP connect")

# -------------------- Action metadata (AST-friendly) --------------------------
b_class       = "BerserkerForce"
b_module      = "berserker_force"
b_status      = "berserker_force"
b_port        = None
b_parent      = None
b_service     = '[]'
b_trigger     = "on_port_change"
b_action      = "aggressive"
b_requires    = '{"action":"NetworkScanner","status":"success","scope":"global"}'
b_priority    = 15
b_cooldown    = 7200
b_rate_limit  = "2/86400"
b_timeout     = 300
b_max_retries = 1
b_stealth_level = 1
b_risk_level  = "high"
b_enabled     = 1

b_category    = "stress"
b_name        = "Berserker Force"
b_description = (
    "Service resilience and stress-testing action.  Measures baseline response "
    "times, applies controlled TCP/SYN/HTTP load, then re-measures to quantify "
    "degradation.  Rate-limited to 50 req/s max (RPi-safe).  No actual DoS -- "
    "just measured probing with structured JSON reporting."
)
b_author      = "Bjorn Community"
b_version     = "2.0.0"
b_icon        = "BerserkerForce.png"

b_tags        = ["stress", "availability", "resilience"]

b_args = {
    "mode": {
        "type":    "select",
        "label":   "Probe mode",
        "choices": ["tcp", "syn", "http", "mixed"],
        "default": "tcp",
        "help":    "tcp = connect probe, syn = SYN via scapy (needs root), "
                   "http = urllib GET for web ports, mixed = random pick per probe.",
    },
    "duration": {
        "type":    "slider",
        "label":   "Stress duration (s)",
        "min":     10,
        "max":     120,
        "step":    5,
        "default": 30,
        "help":    "How long the stress phase runs in seconds.",
    },
    "rate": {
        "type":    "slider",
        "label":   "Probes per second",
        "min":     1,
        "max":     50,
        "step":    1,
        "default": 20,
        "help":    "Max probes per second (clamped to 50 for RPi safety).",
    },
}

b_examples = [
    {"mode": "tcp",   "duration": 30, "rate": 20},
    {"mode": "mixed", "duration": 60, "rate": 40},
    {"mode": "syn",   "duration": 20, "rate": 10},
]

b_docs_url = "docs/actions/BerserkerForce.md"

# -------------------- Constants -----------------------------------------------
_DATA_DIR   = None  # Resolved at runtime via shared_data.data_dir
OUTPUT_DIR  = None  # Resolved at runtime via shared_data.data_dir

_BASELINE_SAMPLES   = 3       # TCP connect samples per port for baseline
_CONNECT_TIMEOUT_S  = 2.0     # socket connect timeout
_HTTP_TIMEOUT_S     = 3.0     # urllib timeout
_MAX_RATE           = 50      # hard ceiling probes/s (RPi guard)
_WEB_PORTS          = {80, 443, 8080, 8443, 8000, 8888, 9443, 3000, 5000}

# -------------------- Helpers -------------------------------------------------

def _tcp_connect_time(ip: str, port: int, timeout_s: float = _CONNECT_TIMEOUT_S) -> Optional[float]:
    """Return round-trip TCP connect time in seconds, or None on failure."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        t0 = time.monotonic()
        err = sock.connect_ex((ip, int(port)))
        elapsed = time.monotonic() - t0
        return elapsed if err == 0 else None
    except Exception:
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _syn_probe_time(ip: str, port: int, timeout_s: float = _CONNECT_TIMEOUT_S) -> Optional[float]:
    """Send a SYN via scapy and measure SYN-ACK time.  Falls back to TCP connect."""
    if not _HAS_SCAPY:
        return _tcp_connect_time(ip, port, timeout_s)
    try:
        pkt = IP(dst=ip) / TCP(dport=int(port), flags="S", seq=random.randint(0, 0xFFFFFFFF))
        t0 = time.monotonic()
        resp = sr1(pkt, timeout=timeout_s, verbose=0)
        elapsed = time.monotonic() - t0
        if resp and resp.haslayer(TCP):
            flags = resp[TCP].flags
            # SYN-ACK (0x12) or RST (0x14) both count as "responded"
            if flags in (0x12, 0x14, "SA", "RA"):
                # Send RST to be polite
                try:
                    from scapy.all import send as scapy_send  # type: ignore
                    rst = IP(dst=ip) / TCP(dport=int(port), flags="R", seq=resp[TCP].ack)
                    scapy_send(rst, verbose=0)
                except Exception:
                    pass
                return elapsed
        return None
    except Exception:
        return _tcp_connect_time(ip, port, timeout_s)


def _http_probe_time(ip: str, port: int, timeout_s: float = _HTTP_TIMEOUT_S) -> Optional[float]:
    """Send an HTTP HEAD/GET and measure response time via urllib."""
    scheme = "https" if int(port) in {443, 8443, 9443} else "http"
    url = f"{scheme}://{ip}:{port}/"
    ctx = None
    if scheme == "https":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        req = Request(url, method="HEAD", headers={"User-Agent": "BjornStress/2.0"})
        t0 = time.monotonic()
        resp = urlopen(req, timeout=timeout_s, context=ctx) if ctx else urlopen(req, timeout=timeout_s)
        elapsed = time.monotonic() - t0
        resp.close()
        return elapsed
    except Exception:
        # Fallback: even a refused connection or error page counts
        try:
            req2 = Request(url, method="GET", headers={"User-Agent": "BjornStress/2.0"})
            t0 = time.monotonic()
            resp2 = urlopen(req2, timeout=timeout_s, context=ctx) if ctx else urlopen(req2, timeout=timeout_s)
            elapsed = time.monotonic() - t0
            resp2.close()
            return elapsed
        except URLError:
            return None
        except Exception:
            return None


def _pick_probe_func(mode: str, port: int):
    """Return the probe function appropriate for the requested mode + port."""
    if mode == "tcp":
        return _tcp_connect_time
    elif mode == "syn":
        return _syn_probe_time
    elif mode == "http":
        if int(port) in _WEB_PORTS:
            return _http_probe_time
        return _tcp_connect_time  # non-web port falls back
    elif mode == "mixed":
        candidates = [_tcp_connect_time]
        if _HAS_SCAPY:
            candidates.append(_syn_probe_time)
        if int(port) in _WEB_PORTS:
            candidates.append(_http_probe_time)
        return random.choice(candidates)
    return _tcp_connect_time


def _safe_mean(values: List[float]) -> float:
    return statistics.mean(values) if values else 0.0


def _safe_stdev(values: List[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def _degradation_pct(baseline_mean: float, post_mean: float) -> float:
    """Percentage increase from baseline to post-stress.  Positive = slower."""
    if baseline_mean <= 0:
        return 0.0
    return round(((post_mean - baseline_mean) / baseline_mean) * 100.0, 2)


# -------------------- Main class ----------------------------------------------

class BerserkerForce:
    """Service resilience tester -- orchestrator-compatible Bjorn action."""

    def __init__(self, shared_data):
        self.shared_data = shared_data

    # ------------------------------------------------------------------ #
    #  Phase helpers                                                       #
    # ------------------------------------------------------------------ #

    def _resolve_ports(self, ip: str, port, row: Dict) -> List[int]:
        """Gather target ports from the port argument, row data, or DB hosts table."""
        ports: List[int] = []

        # 1) Explicit port argument
        try:
            p = int(port) if str(port).strip() else None
            if p:
                ports.append(p)
        except Exception:
            pass

        # 2) Row data (Ports column, semicolon-separated)
        if not ports:
            ports_txt = str(row.get("Ports") or row.get("ports") or "")
            for tok in ports_txt.replace(",", ";").split(";"):
                tok = tok.strip().split("/")[0]  # handle "80/tcp"
                if tok.isdigit():
                    ports.append(int(tok))

        # 3) DB lookup via MAC
        if not ports:
            mac = (row.get("MAC Address") or row.get("mac_address") or row.get("mac") or "").strip()
            if mac:
                try:
                    rows = self.shared_data.db.query(
                        "SELECT ports FROM hosts WHERE mac_address=? LIMIT 1", (mac,)
                    )
                    if rows and rows[0].get("ports"):
                        for tok in rows[0]["ports"].replace(",", ";").split(";"):
                            tok = tok.strip().split("/")[0]
                            if tok.isdigit():
                                ports.append(int(tok))
                except Exception as exc:
                    logger.debug(f"DB port lookup failed: {exc}")

        # De-duplicate, cap at 20 ports (Pi Zero guard)
        seen = set()
        unique: List[int] = []
        for p in ports:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return unique[:20]

    def _measure_baseline(self, ip: str, ports: List[int], samples: int = _BASELINE_SAMPLES) -> Dict[int, List[float]]:
        """Phase 1 / 3: TCP connect baseline measurement (always TCP for consistency)."""
        baselines: Dict[int, List[float]] = {}
        for p in ports:
            times: List[float] = []
            for _ in range(samples):
                if self.shared_data.orchestrator_should_exit:
                    break
                rt = _tcp_connect_time(ip, p)
                if rt is not None:
                    times.append(rt)
                time.sleep(0.05)  # gentle spacing
            baselines[p] = times
        return baselines

    def _run_stress(
        self,
        ip: str,
        ports: List[int],
        mode: str,
        duration_s: int,
        rate: int,
        progress: ProgressTracker,
        stress_progress_start: int,
        stress_progress_span: int,
    ) -> Dict[int, Dict[str, Any]]:
        """Phase 2: Controlled stress test with rate limiting."""
        rate = max(1, min(rate, _MAX_RATE))
        interval = 1.0 / rate
        deadline = time.monotonic() + duration_s

        # Per-port accumulators
        results: Dict[int, Dict[str, Any]] = {}
        for p in ports:
            results[p] = {"sent": 0, "success": 0, "fail": 0, "times": []}

        total_probes_est = rate * duration_s
        probes_done = 0
        port_idx = 0

        while time.monotonic() < deadline:
            if self.shared_data.orchestrator_should_exit:
                break

            p = ports[port_idx % len(ports)]
            port_idx += 1

            probe_fn = _pick_probe_func(mode, p)
            rt = probe_fn(ip, p)
            results[p]["sent"] += 1
            if rt is not None:
                results[p]["success"] += 1
                results[p]["times"].append(rt)
            else:
                results[p]["fail"] += 1

            probes_done += 1

            # Update progress (map probes_done onto the stress progress range)
            if total_probes_est > 0:
                frac = min(1.0, probes_done / total_probes_est)
                pct = stress_progress_start + int(frac * stress_progress_span)
                self.shared_data.bjorn_progress = f"{min(pct, stress_progress_start + stress_progress_span)}%"

            # Rate limit
            time.sleep(interval)

        return results

    def _analyze(
        self,
        pre_baseline: Dict[int, List[float]],
        post_baseline: Dict[int, List[float]],
        stress_results: Dict[int, Dict[str, Any]],
        ports: List[int],
    ) -> Dict[str, Any]:
        """Phase 4: Build the analysis report dict."""
        per_port: List[Dict[str, Any]] = []
        for p in ports:
            pre = pre_baseline.get(p, [])
            post = post_baseline.get(p, [])
            sr = stress_results.get(p, {"sent": 0, "success": 0, "fail": 0, "times": []})

            pre_mean = _safe_mean(pre)
            post_mean = _safe_mean(post)
            degradation = _degradation_pct(pre_mean, post_mean)

            per_port.append({
                "port": p,
                "pre_baseline": {
                    "samples": len(pre),
                    "mean_s": round(pre_mean, 6),
                    "stdev_s": round(_safe_stdev(pre), 6),
                    "values_s": [round(v, 6) for v in pre],
                },
                "stress": {
                    "probes_sent": sr["sent"],
                    "probes_ok": sr["success"],
                    "probes_fail": sr["fail"],
                    "mean_rt_s": round(_safe_mean(sr["times"]), 6),
                    "stdev_rt_s": round(_safe_stdev(sr["times"]), 6),
                    "min_rt_s": round(min(sr["times"]), 6) if sr["times"] else None,
                    "max_rt_s": round(max(sr["times"]), 6) if sr["times"] else None,
                },
                "post_baseline": {
                    "samples": len(post),
                    "mean_s": round(post_mean, 6),
                    "stdev_s": round(_safe_stdev(post), 6),
                    "values_s": [round(v, 6) for v in post],
                },
                "degradation_pct": degradation,
            })

        # Overall summary
        total_sent = sum(sr.get("sent", 0) for sr in stress_results.values())
        total_ok = sum(sr.get("success", 0) for sr in stress_results.values())
        total_fail = sum(sr.get("fail", 0) for sr in stress_results.values())
        avg_degradation = (
            round(statistics.mean([pp["degradation_pct"] for pp in per_port]), 2)
            if per_port else 0.0
        )

        return {
            "summary": {
                "ports_tested": len(ports),
                "total_probes_sent": total_sent,
                "total_probes_ok": total_ok,
                "total_probes_fail": total_fail,
                "avg_degradation_pct": avg_degradation,
            },
            "per_port": per_port,
        }

    def _save_report(self, ip: str, mode: str, duration_s: int, rate: int, analysis: Dict) -> str:
        """Write the JSON report and return the file path."""
        output_dir = os.path.join(self.shared_data.data_dir, "output", "stress")
        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as exc:
            logger.warning(f"Could not create output dir {output_dir}: {exc}")

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        safe_ip = ip.replace(":", "_").replace(".", "_")
        filename = f"{safe_ip}_{ts}.json"
        filepath = os.path.join(output_dir, filename)

        report = {
            "tool": "berserker_force",
            "version": b_version,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "target": ip,
            "config": {
                "mode": mode,
                "duration_s": duration_s,
                "rate_per_s": rate,
                "scapy_available": _HAS_SCAPY,
            },
            "analysis": analysis,
        }

        try:
            with open(filepath, "w") as fh:
                json.dump(report, fh, indent=2, default=str)
            logger.info(f"Report saved to {filepath}")
        except Exception as exc:
            logger.error(f"Failed to write report {filepath}: {exc}")

        return filepath

    # ------------------------------------------------------------------ #
    #  Orchestrator entry point                                            #
    # ------------------------------------------------------------------ #

    def execute(self, ip: str, port, row: Dict, status_key: str) -> str:
        """
        Main entry point called by the Bjorn orchestrator.

        Returns 'success', 'failed', or 'interrupted'.
        """
        if self.shared_data.orchestrator_should_exit:
            return "interrupted"

        # --- Identity cache from row -----------------------------------------
        mac = (row.get("MAC Address") or row.get("mac_address") or row.get("mac") or "").strip()
        hostname = (row.get("Hostname") or row.get("hostname") or "").strip()
        if ";" in hostname:
            hostname = hostname.split(";", 1)[0].strip()

        # --- Resolve target ports --------------------------------------------
        ports = self._resolve_ports(ip, port, row)
        if not ports:
            logger.warning(f"BerserkerForce: no ports resolved for {ip}")
            return "failed"

        # --- Read runtime config from shared_data ----------------------------
        mode = str(getattr(self.shared_data, "berserker_mode", "tcp") or "tcp").lower()
        if mode not in ("tcp", "syn", "http", "mixed"):
            mode = "tcp"
        duration_s = max(10, min(int(getattr(self.shared_data, "berserker_duration", 30) or 30), 120))
        rate = max(1, min(int(getattr(self.shared_data, "berserker_rate", 20) or 20), _MAX_RATE))

        # --- EPD / UI updates ------------------------------------------------
        self.shared_data.bjorn_orch_status = "berserker_force"
        self.shared_data.bjorn_status_text2 = f"{ip} ({len(ports)} ports)"
        self.shared_data.comment_params = {"ip": ip, "ports": str(len(ports)), "mode": mode}

        # Total units for progress: baseline(15) + stress(70) + post-baseline(10) + analysis(5)
        self.shared_data.bjorn_progress = "0%"

        try:
            # ============================================================== #
            # Phase 1: Pre-stress baseline  (0 - 15%)                        #
            # ============================================================== #
            logger.info(f"Phase 1/4: pre-stress baseline for {ip} on {len(ports)} ports")
            self.shared_data.comment_params = {"ip": ip, "phase": "baseline"}
            self.shared_data.log_milestone(b_class, "BaselineStart", f"Measuring {len(ports)} ports")

            pre_baseline = self._measure_baseline(ip, ports)
            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            self.shared_data.bjorn_progress = "15%"

            # ============================================================== #
            # Phase 2: Stress test  (15 - 85%)                               #
            # ============================================================== #
            logger.info(f"Phase 2/4: stress test ({mode}, {duration_s}s, {rate} req/s)")
            self.shared_data.comment_params = {
                "ip": ip,
                "phase": "stress",
                "mode": mode,
                "rate": str(rate),
            }
            self.shared_data.log_milestone(b_class, "StressActive", f"Mode: {mode} | Duration: {duration_s}s")

            # Build a dummy ProgressTracker just for internal bookkeeping;
            # we do fine-grained progress updates ourselves.
            progress = ProgressTracker(self.shared_data, 100)

            stress_results = self._run_stress(
                ip=ip,
                ports=ports,
                mode=mode,
                duration_s=duration_s,
                rate=rate,
                progress=progress,
                stress_progress_start=15,
                stress_progress_span=70,
            )

            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            self.shared_data.bjorn_progress = "85%"

            # ============================================================== #
            # Phase 3: Post-stress baseline  (85 - 95%)                      #
            # ============================================================== #
            logger.info(f"Phase 3/4: post-stress baseline for {ip}")
            self.shared_data.comment_params = {"ip": ip, "phase": "post-baseline"}
            self.shared_data.log_milestone(b_class, "RecoveryMeasure", f"Checking {ip} after stress")

            post_baseline = self._measure_baseline(ip, ports)
            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            self.shared_data.bjorn_progress = "95%"

            # ============================================================== #
            # Phase 4: Analysis & report  (95 - 100%)                        #
            # ============================================================== #
            logger.info("Phase 4/4: analyzing results")
            self.shared_data.comment_params = {"ip": ip, "phase": "analysis"}

            analysis = self._analyze(pre_baseline, post_baseline, stress_results, ports)
            report_path = self._save_report(ip, mode, duration_s, rate, analysis)

            self.shared_data.bjorn_progress = "100%"

            # Final UI update
            avg_deg = analysis.get("summary", {}).get("avg_degradation_pct", 0.0)
            self.shared_data.log_milestone(b_class, "Complete", f"Avg Degradation: {avg_deg}% | Report: {os.path.basename(report_path)}")
            return "success"

        except Exception as exc:
            logger.error(f"BerserkerForce failed for {ip}: {exc}", exc_info=True)
            return "failed"

        finally:
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}
            self.shared_data.bjorn_status_text2 = ""


# -------------------- Optional CLI (debug / manual) ---------------------------
if __name__ == "__main__":
    import argparse
    from shared import SharedData

    parser = argparse.ArgumentParser(description="BerserkerForce (service resilience tester)")
    parser.add_argument("--ip", required=True, help="Target IP address")
    parser.add_argument("--port", default="", help="Specific port (optional; uses row/DB otherwise)")
    parser.add_argument("--mode", default="tcp", choices=["tcp", "syn", "http", "mixed"])
    parser.add_argument("--duration", type=int, default=30, help="Stress duration in seconds")
    parser.add_argument("--rate", type=int, default=20, help="Probes per second (max 50)")
    args = parser.parse_args()

    sd = SharedData()
    # Push CLI args into shared_data so the action reads them
    sd.berserker_mode = args.mode
    sd.berserker_duration = args.duration
    sd.berserker_rate = args.rate

    act = BerserkerForce(sd)

    row = {
        "MAC Address": getattr(sd, "get_raspberry_mac", lambda: "__GLOBAL__")() or "__GLOBAL__",
        "Hostname": "",
        "Ports": args.port,
    }

    result = act.execute(args.ip, args.port, row, "berserker_force")
    print(f"Result: {result}")
