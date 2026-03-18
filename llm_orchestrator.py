"""llm_orchestrator.py - LLM-driven scheduling layer (advisor or autonomous mode)."""

import json
import threading
import time
from typing import Any, Dict, List, Optional

from logger import Logger

logger = Logger(name="llm_orchestrator.py", level=20)

# Priority levels (must stay above normal scheduler/queue to be useful)
_ADVISOR_PRIORITY    = 85  # advisor > MCP (80) > normal (50) > scheduler (40)
_AUTONOMOUS_PRIORITY = 82


class LLMOrchestrator:
    """
    LLM-based orchestration layer.

    advisor mode    - called from orchestrator background tasks; LLM suggests one action.
    autonomous mode - runs its own thread; LLM loops with full tool-calling.
    """

    def __init__(self, shared_data):
        self._sd = shared_data
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_fingerprint: Optional[tuple] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        mode = self._mode()
        if mode == "autonomous":
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._autonomous_loop, daemon=True, name="LLMOrchestrator"
            )
            self._thread.start()
            logger.info("LLM Orchestrator started (autonomous)")
        elif mode == "advisor":
            logger.info("LLM Orchestrator ready (advisor - called from background tasks)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=15)
        self._thread = None

    def restart_if_mode_changed(self) -> None:
        """
        Call from the orchestrator main loop to react to runtime config changes.
        Starts/stops the autonomous thread when the mode changes.
        """
        mode = self._mode()
        running = self._thread is not None and self._thread.is_alive()

        if mode == "autonomous" and not running and self._is_llm_enabled():
            self.start()
        elif mode != "autonomous" and running:
            self.stop()

    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _mode(self) -> str:
        return str(self._sd.config.get("llm_orchestrator_mode", "none"))

    def _is_llm_enabled(self) -> bool:
        return bool(self._sd.config.get("llm_enabled", False))

    def _allowed_actions(self) -> List[str]:
        """
        Bjorn action module names the LLM may queue via run_action.
        Falls back to all loaded action names if empty.
        NOTE: These are action MODULE names (e.g. 'NetworkScanner', 'SSHBruteforce'),
              NOT MCP tool names (get_hosts, run_action, etc.).
        """
        custom = self._sd.config.get("llm_orchestrator_allowed_actions", [])
        if custom:
            return list(custom)
        # Auto-discover from loaded actions
        try:
            loaded = getattr(self._sd, 'loaded_action_names', None)
            if loaded:
                return list(loaded)
        except Exception:
            pass
        # Fallback: ask the DB for known action names
        try:
            rows = self._sd.db.query(
                "SELECT DISTINCT action_name FROM action_queue ORDER BY action_name"
            )
            if rows:
                return [r["action_name"] for r in rows]
        except Exception:
            pass
        return []

    def _max_actions(self) -> int:
        return max(1, int(self._sd.config.get("llm_orchestrator_max_actions", 3)))

    def _interval(self) -> int:
        return max(30, int(self._sd.config.get("llm_orchestrator_interval_s", 60)))

    # ------------------------------------------------------------------
    # Advisor mode  (called externally from orchestrator background tasks)
    # ------------------------------------------------------------------

    def advise(self) -> Optional[str]:
        """
        Ask the LLM for ONE tactical action recommendation.
        Returns the action name if one was queued, else None.
        """
        if not self._is_llm_enabled() or self._mode() != "advisor":
            return None

        try:
            from llm_bridge import LLMBridge

            allowed = self._allowed_actions()
            if not allowed:
                return None

            snapshot = self._build_snapshot()
            real_ips = snapshot.get("VALID_TARGET_IPS", [])
            ip_list_str = ", ".join(real_ips) if real_ips else "(none)"

            system = (
                "You are Bjorn's tactical advisor. Review the current network state "
                "and suggest ONE action to queue, or nothing if the queue is sufficient. "
                "Reply ONLY with valid JSON - no markdown, no commentary.\n"
                'Format when action needed: {"action": "ActionName", "target_ip": "1.2.3.4", "reason": "brief"}\n'
                'Format when nothing needed: {"action": null}\n'
                "action must be exactly one of: " + ", ".join(allowed) + "\n"
                f"target_ip MUST be one of these exact IPs: {ip_list_str}\n"
                "NEVER use placeholder IPs. Only use IPs from the hosts_alive list."
            )
            prompt = (
                f"Current Bjorn state:\n{json.dumps(snapshot, indent=2)}\n\n"
                "Suggest one action or null."
            )

            raw = LLMBridge().complete(
                [{"role": "user", "content": prompt}],
                system=system,
                max_tokens=150,
                timeout=20,
            )
            if not raw:
                return None

            return self._apply_advisor_response(raw, allowed)

        except Exception as e:
            logger.debug(f"LLM advisor error: {e}")
            return None

    def _apply_advisor_response(self, raw: str, allowed: List[str]) -> Optional[str]:
        """Parse advisor JSON and queue the suggested action. Returns action name or None."""
        try:
            text = raw.strip()
            # Strip markdown fences if the model added them
            if "```" in text:
                parts = text.split("```")
                text = parts[1] if len(parts) > 1 else parts[0]
                if text.startswith("json"):
                    text = text[4:]

            data = json.loads(text.strip())
            action = data.get("action")
            if not action:
                logger.debug("LLM advisor: no action suggested this cycle")
                return None

            if action not in allowed:
                logger.warning(f"LLM advisor suggested disallowed action '{action}' - ignored")
                return None

            target_ip = str(data.get("target_ip", "")).strip()
            reason = str(data.get("reason", "llm_advisor"))[:120]

            mac = self._resolve_mac(target_ip)

            self._sd.db.queue_action(
                action_name=action,
                mac=mac,
                ip=target_ip,
                priority=_ADVISOR_PRIORITY,
                trigger="llm_advisor",
                metadata={
                    "decision_method": "llm_advisor",
                    "decision_origin": "llm",
                    "ai_reason": reason,
                },
            )
            try:
                self._sd.queue_event.set()
            except Exception:
                pass

            logger.info(f"[LLM_ADVISOR] → {action} @ {target_ip}: {reason}")
            return action

        except json.JSONDecodeError:
            logger.warning(f"LLM advisor: invalid JSON response: {raw[:200]}")
            return None
        except Exception as e:
            logger.debug(f"LLM advisor apply error: {e}")
            return None

    # ------------------------------------------------------------------
    # Autonomous mode  (own thread)
    # ------------------------------------------------------------------

    def _autonomous_loop(self) -> None:
        logger.info("LLM Orchestrator autonomous loop starting")
        while not self._stop.is_set():
            try:
                if self._is_llm_enabled() and self._mode() == "autonomous":
                    self._run_autonomous_cycle()
                else:
                    # Mode was switched off at runtime - stop thread
                    break
            except Exception as e:
                logger.error(f"LLM autonomous cycle error: {e}")

            self._stop.wait(self._interval())

        logger.info("LLM Orchestrator autonomous loop stopped")

    def _compute_fingerprint(self) -> tuple:
        """
        Compact state fingerprint: (hosts, vulns, creds, last_completed_queue_id).
        Only increases are meaningful - a host going offline is not an opportunity.
        """
        try:
            hosts = int(getattr(self._sd, "target_count", 0))
            vulns = int(getattr(self._sd, "vuln_count", 0))
            creds = int(getattr(self._sd, "cred_count", 0))
            row = self._sd.db.query_one(
                "SELECT MAX(id) AS mid FROM action_queue WHERE status IN ('success','failed')"
            )
            last_id = int(row["mid"]) if row and row["mid"] is not None else 0
            return (hosts, vulns, creds, last_id)
        except Exception:
            return (0, 0, 0, 0)

    def _has_actionable_change(self, fp: tuple) -> bool:
        """
        Return True only if something *increased* since the last cycle:
          - new host discovered        (hosts ↑)
          - new vulnerability found    (vulns ↑)
          - new credential captured    (creds ↑)
          - an action completed        (last_id ↑)
        A host going offline (hosts ↓) is not an actionable event.
        """
        if self._last_fingerprint is None:
            return True  # first cycle always runs
        return any(fp[i] > self._last_fingerprint[i] for i in range(len(fp)))

    def _run_autonomous_cycle(self) -> None:
        """
        One autonomous cycle.

        Two paths based on backend capability:
          A) API backend (Anthropic) → agentic tool-calling loop
          B) LaRuche / Ollama       → snapshot-based JSON prompt (no tool-calling)

        Path B injects the full network state into the prompt and asks the LLM
        to reply with a JSON array of actions.  This works with any text-only LLM.
        """
        # Skip if nothing actionable changed (save tokens)
        if self._sd.config.get("llm_orchestrator_skip_if_no_change", True):
            fp = self._compute_fingerprint()
            if not self._has_actionable_change(fp):
                logger.debug("LLM autonomous: no actionable change, skipping cycle (no tokens used)")
                return
            self._last_fingerprint = fp

        try:
            from llm_bridge import LLMBridge, _BJORN_TOOLS
        except ImportError as e:
            logger.warning(f"LLM Orchestrator: cannot import llm_bridge: {e}")
            return

        bridge = LLMBridge()
        allowed = self._allowed_actions()
        max_act = self._max_actions()

        # Detect if the active backend supports tool-calling
        backend = self._sd.config.get("llm_backend", "auto")
        supports_tools = (backend == "api") or (
            backend == "auto" and not bridge._laruche_url
            and not self._ollama_reachable()
        )

        if supports_tools:
            response = self._cycle_with_tools(bridge, allowed, max_act)
        else:
            response = self._cycle_without_tools(bridge, allowed, max_act)

        if response:
            log_reasoning = self._sd.config.get("llm_orchestrator_log_reasoning", False)
            prompt_desc = f"Autonomous cycle (tools={'yes' if supports_tools else 'no'})"
            if log_reasoning:
                logger.info(f"[LLM_ORCH_REASONING]\n{response}")
                self._push_to_chat(bridge, prompt_desc, response)
            else:
                logger.info(f"[LLM_AUTONOMOUS] {response[:300]}")

    def _ollama_reachable(self) -> bool:
        """Quick check if Ollama is up (for backend detection)."""
        try:
            base = self._sd.config.get("llm_ollama_url", "http://127.0.0.1:11434").rstrip("/")
            import urllib.request
            urllib.request.urlopen(f"{base}/api/tags", timeout=2)
            return True
        except Exception:
            return False

    # ------ Path A: agentic tool-calling (Anthropic API only) ------

    def _cycle_with_tools(self, bridge, allowed: List[str], max_act: int) -> Optional[str]:
        """Full agentic loop: LLM calls MCP tools and queues actions."""
        from llm_bridge import _BJORN_TOOLS

        read_only = {"get_hosts", "get_vulnerabilities", "get_credentials",
                     "get_action_history", "get_status", "query_db"}
        tools = [
            t for t in _BJORN_TOOLS
            if t["name"] in read_only or t["name"] == "run_action"
        ]

        system = self._build_autonomous_system_prompt(allowed, max_act)
        prompt = (
            "Start a new orchestration cycle. "
            "Use get_status and get_hosts to understand the current state. "
            f"Then queue up to {max_act} high-value action(s) via run_action. "
            "When done, summarise what you queued and why."
        )

        return bridge.complete(
            [{"role": "user", "content": prompt}],
            system=system,
            tools=tools,
            max_tokens=1000,
            timeout=90,
        )

    # ------ Path B: snapshot + JSON parsing (LaRuche / Ollama) ------

    def _cycle_without_tools(self, bridge, allowed: List[str], max_act: int) -> Optional[str]:
        """
        No tool-calling: inject state snapshot into prompt, ask LLM for JSON actions.
        Parse the response and queue actions ourselves.
        """
        snapshot = self._build_snapshot()
        allowed_str = ", ".join(allowed) if allowed else "none"

        # Extract the real IP list so we can stress it in the prompt
        real_ips = snapshot.get("VALID_TARGET_IPS", [])
        ip_list_str = ", ".join(real_ips) if real_ips else "(no hosts discovered yet)"

        # Short system prompt - small models forget long instructions
        system = (
            "You are a network security orchestrator. "
            "You receive network scan data and output a JSON array of actions. "
            "Output ONLY a JSON array. No explanations, no markdown, no commentary."
        )

        # Put the real instructions in the user message AFTER the data,
        # so the model sees them last (recency bias helps small models).
        prompt = (
            f"Network state:\n{json.dumps(snapshot, indent=2)}\n\n"
            "---\n"
            f"Pick up to {max_act} actions from: {allowed_str}\n"
            f"Target IPs MUST be from this list: {ip_list_str}\n"
            "Match actions to open ports. Skip hosts already in pending_queue.\n"
            "Output ONLY a JSON array like:\n"
            '[{"action":"ActionName","target_ip":"1.2.3.4","reason":"brief"}]\n'
            "or [] if nothing needed.\n"
            "JSON array:"
        )

        # Use an assistant prefix to force the model into JSON mode.
        # Many LLMs will continue from this prefix rather than describe.
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "["},
        ]

        raw = bridge.complete(
            messages,
            system=system,
            max_tokens=500,
            timeout=60,
        )

        # Prepend the '[' prefix we forced if the model didn't include it
        if raw and not raw.strip().startswith("["):
            raw = "[" + raw

        if not raw:
            return None

        # Parse and queue actions
        queued = self._parse_and_queue_actions(raw, allowed, max_act)

        summary = raw.strip()
        if queued:
            summary += f"\n\n[Orchestrator queued {len(queued)} action(s): {', '.join(queued)}]"
        else:
            summary += "\n\n[Orchestrator: no valid actions parsed from LLM response]"

        return summary

    @staticmethod
    def _is_valid_ip(ip: str) -> bool:
        """Check that ip is a real IPv4 address (no placeholders like 192.168.1.x)."""
        parts = ip.split(".")
        if len(parts) != 4:
            return False
        for p in parts:
            try:
                n = int(p)
                if n < 0 or n > 255:
                    return False
            except ValueError:
                return False  # catches 'x', 'xx', etc.
        return True

    def _parse_and_queue_actions(self, raw: str, allowed: List[str], max_act: int) -> List[str]:
        """Parse JSON array from LLM response and queue valid actions. Returns list of queued action names."""
        queued = []
        try:
            text = raw.strip()
            # Strip markdown fences
            if "```" in text:
                parts = text.split("```")
                text = parts[1] if len(parts) > 1 else parts[0]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            # Try to find JSON array in the text
            start = text.find("[")
            end = text.rfind("]")
            if start == -1 or end == -1:
                # Check if the model wrote a text description instead of JSON
                if any(text.lower().startswith(w) for w in ("this ", "here", "the ", "based", "from ", "i ")):
                    logger.warning(
                        "LLM autonomous: model returned a text description instead of JSON array. "
                        "The model may not support structured output. First 120 chars: "
                        + text[:120]
                    )
                else:
                    logger.debug(f"LLM autonomous: no JSON array found in response: {text[:120]}")
                return []

            data = json.loads(text[start:end + 1])
            if not isinstance(data, list):
                data = [data]

            for item in data[:max_act]:
                if not isinstance(item, dict):
                    continue
                action = item.get("action", "").strip()
                target_ip = str(item.get("target_ip", "")).strip()
                reason = str(item.get("reason", "llm_autonomous"))[:120]

                if not action or action not in allowed:
                    logger.debug(f"LLM autonomous: skipping invalid/disallowed action '{action}'")
                    continue
                if not target_ip:
                    logger.debug(f"LLM autonomous: skipping '{action}' - no target_ip")
                    continue
                if not self._is_valid_ip(target_ip):
                    logger.warning(
                        f"LLM autonomous: skipping '{action}' - invalid/placeholder IP '{target_ip}' "
                        f"(LLM must use exact IPs from alive_hosts)"
                    )
                    continue

                mac = self._resolve_mac(target_ip)
                if not mac:
                    logger.warning(
                        f"LLM autonomous: skipping '{action}' @ {target_ip} - "
                        f"IP not found in hosts table (LLM used an IP not in alive_hosts)"
                    )
                    continue

                self._sd.db.queue_action(
                    action_name=action,
                    mac=mac,
                    ip=target_ip,
                    priority=_AUTONOMOUS_PRIORITY,
                    trigger="llm_autonomous",
                    metadata={
                        "decision_method": "llm_autonomous",
                        "decision_origin": "llm",
                        "ai_reason": reason,
                    },
                )
                queued.append(f"{action}@{target_ip}")
                logger.info(f"[LLM_AUTONOMOUS] → {action} @ {target_ip} (mac={mac}): {reason}")

            if queued:
                try:
                    self._sd.queue_event.set()
                except Exception:
                    pass

        except json.JSONDecodeError as e:
            logger.debug(f"LLM autonomous: JSON parse error: {e} - raw: {raw[:200]}")
        except Exception as e:
            logger.debug(f"LLM autonomous: action queue error: {e}")

        return queued

    def _build_autonomous_system_prompt(self, allowed: List[str], max_act: int) -> str:
        try:
            hosts = getattr(self._sd, "target_count", "?")
            vulns = getattr(self._sd, "vuln_count", "?")
            creds = getattr(self._sd, "cred_count", "?")
            mode  = getattr(self._sd, "operation_mode", "?")
        except Exception:
            hosts = vulns = creds = mode = "?"

        allowed_str = ", ".join(allowed) if allowed else "none"

        lang = ""
        try:
            from llm_bridge import LLMBridge
            lang = LLMBridge()._lang_instruction()
        except Exception:
            pass

        return (
            "You are Bjorn's Cyberviking autonomous orchestrator, running on a Raspberry Pi network security tool. "
            f"Current state: {hosts} hosts discovered, {vulns} vulnerabilities, {creds} credentials. "
            f"Operation mode: {mode}. "
            "Your objective: observe the network state via tools, then queue the most valuable actions. "
            f"Hard limit: at most {max_act} run_action calls per cycle. "
            f"Only these action names may be queued: {allowed_str}. "
            "Strategy: prioritise unexplored services, hosts with high port counts, and hosts with no recent scans. "
            "Do not queue duplicate actions already pending or recently successful. "
            "Use Norse references occasionally. Be terse and tactical."
            + (f" {lang}" if lang else "")
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _push_to_chat(self, bridge, user_prompt: str, assistant_response: str) -> None:
        """
        Inject the LLM's reasoning into the 'llm_orchestrator' chat session
        so it can be reviewed in chat.html (load session 'llm_orchestrator').
        Keeps last 40 messages to avoid unbounded memory.
        """
        try:
            with bridge._hist_lock:
                hist = bridge._chat_histories.setdefault("llm_orchestrator", [])
                hist.append({"role": "user", "content": f"[Autonomous cycle]\n{user_prompt}"})
                hist.append({"role": "assistant", "content": assistant_response})
                if len(hist) > 40:
                    hist[:] = hist[-40:]
        except Exception as e:
            logger.debug(f"LLM reasoning push to chat failed: {e}")

    def _resolve_mac(self, ip: str) -> str:
        """Resolve IP → MAC from hosts table. Column is 'ips' (may hold multiple IPs)."""
        if not ip:
            return ""
        try:
            row = self._sd.db.query_one(
                "SELECT mac_address FROM hosts WHERE ips LIKE ? LIMIT 1", (f"%{ip}%",)
            )
            return row["mac_address"] if row else ""
        except Exception:
            return ""

    def _build_snapshot(self) -> Dict[str, Any]:
        """
        Rich state snapshot for advisor / autonomous prompts.

        Includes:
          - alive_hosts   : full host details (ip, mac, hostname, vendor, ports)
          - services      : identified services per host (port, service, product, version)
          - vulns_found   : active vulnerabilities per host
          - creds_found   : captured credentials per host/service
          - available_actions : what the LLM can queue (name, description, target port/service)
          - pending_queue : actions already queued
          - recent_actions: last completed actions (avoid repeats)
        """
        hosts, services, vulns, creds = [], [], [], []
        actions_catalog, pending, history = [], [], []

        # ── Alive hosts ──
        try:
            rows = self._sd.db.query(
                "SELECT mac_address, ips, hostnames, ports, vendor "
                "FROM hosts WHERE alive=1 LIMIT 30"
            )
            for r in (rows or []):
                ip = (r.get("ips") or "").split(";")[0].strip()
                if not ip:
                    continue
                hosts.append({
                    "ip": ip,
                    "mac": r.get("mac_address", ""),
                    "hostname": (r.get("hostnames") or "").split(";")[0].strip(),
                    "vendor": r.get("vendor", ""),
                    "ports": r.get("ports", ""),
                })
        except Exception:
            pass

        # ── Port services (identified services with product/version) ──
        try:
            rows = self._sd.db.query(
                "SELECT mac_address, ip, port, service, product, version "
                "FROM port_services WHERE is_current=1 AND state='open' "
                "ORDER BY mac_address, port LIMIT 100"
            )
            for r in (rows or []):
                svc = {"mac": r.get("mac_address", ""), "port": r.get("port")}
                if r.get("ip"):
                    svc["ip"] = r["ip"]
                if r.get("service"):
                    svc["service"] = r["service"]
                if r.get("product"):
                    svc["product"] = r["product"]
                if r.get("version"):
                    svc["version"] = r["version"]
                services.append(svc)
        except Exception:
            pass

        # ── Active vulnerabilities ──
        try:
            rows = self._sd.db.query(
                "SELECT ip, port, vuln_id, hostname "
                "FROM vulnerabilities WHERE is_active=1 LIMIT 30"
            )
            vulns = [{"ip": r.get("ip", ""), "port": r.get("port"),
                       "vuln_id": r.get("vuln_id", ""),
                       "hostname": r.get("hostname", "")}
                      for r in (rows or [])]
        except Exception:
            pass

        # ── Captured credentials ──
        try:
            rows = self._sd.db.query(
                "SELECT service, ip, hostname, port, \"user\" "
                "FROM creds LIMIT 30"
            )
            creds = [{"service": r.get("service", ""), "ip": r.get("ip", ""),
                       "hostname": r.get("hostname", ""), "port": r.get("port"),
                       "user": r.get("user", "")}
                      for r in (rows or [])]
        except Exception:
            pass

        # ── Available actions catalog (what the LLM can queue) ──
        allowed = self._allowed_actions()
        try:
            if allowed:
                placeholders = ",".join("?" * len(allowed))
                rows = self._sd.db.query(
                    f"SELECT b_class, b_description, b_port, b_service "
                    f"FROM actions WHERE b_class IN ({placeholders}) AND b_enabled=1",
                    tuple(allowed)
                )
                for r in (rows or []):
                    entry = {"name": r["b_class"]}
                    if r.get("b_description"):
                        entry["description"] = r["b_description"][:100]
                    if r.get("b_port"):
                        entry["target_port"] = r["b_port"]
                    if r.get("b_service"):
                        entry["target_service"] = r["b_service"]
                    actions_catalog.append(entry)
        except Exception:
            pass

        # ── Pending queue ──
        try:
            rows = self._sd.db.query(
                "SELECT action_name, ip, priority FROM action_queue "
                "WHERE status='pending' ORDER BY priority DESC LIMIT 15"
            )
            pending = [{"action": r["action_name"], "ip": r["ip"]} for r in (rows or [])]
        except Exception:
            pass

        # ── Recent action history ──
        try:
            rows = self._sd.db.query(
                "SELECT action_name, ip, status FROM action_queue "
                "WHERE status IN ('success','failed') ORDER BY completed_at DESC LIMIT 15"
            )
            history = [{"action": r["action_name"], "ip": r["ip"], "result": r["status"]}
                       for r in (rows or [])]
        except Exception:
            pass

        # Build explicit IP list for emphasis
        ip_list = [h["ip"] for h in hosts if h.get("ip")]

        result = {
            "VALID_TARGET_IPS": ip_list,
            "hosts_alive": hosts,
            "operation_mode": getattr(self._sd, "operation_mode", "?"),
        }
        if services:
            result["services_detected"] = services
        if vulns:
            result["vulnerabilities_found"] = vulns
        if creds:
            result["credentials_captured"] = creds
        if actions_catalog:
            result["available_actions"] = actions_catalog
        result["pending_queue"] = pending
        result["recent_actions"] = history
        result["summary"] = {
            "hosts_alive": len(ip_list),
            "vulns": getattr(self._sd, "vuln_count", 0),
            "creds": getattr(self._sd, "cred_count", 0),
        }

        return result
