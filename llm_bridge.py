"""llm_bridge.py - LLM backend cascade: LAND/LaRuche -> Ollama -> external API -> fallback."""

import json
import socket
import threading
import time
import urllib.request
import urllib.error
from typing import Optional, List, Dict, Any

from logger import Logger
import land_protocol

logger = Logger(name="llm_bridge.py", level=20)  # INFO

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic Messages API format).
# Mirrors the tools exposed by mcp_server.py - add new tools here too.
# ---------------------------------------------------------------------------
_BJORN_TOOLS: List[Dict] = [
    {
        "name": "get_hosts",
        "description": "Return all network hosts discovered by Bjorn's scanner.",
        "input_schema": {
            "type": "object",
            "properties": {
                "alive_only": {"type": "boolean", "description": "Only return alive hosts. Default: true."},
            },
        },
    },
    {
        "name": "get_vulnerabilities",
        "description": "Return discovered vulnerabilities, optionally filtered by host IP.",
        "input_schema": {
            "type": "object",
            "properties": {
                "host_ip": {"type": "string", "description": "Filter by IP address. Empty = all hosts."},
                "limit": {"type": "integer", "description": "Max results. Default: 100."},
            },
        },
    },
    {
        "name": "get_credentials",
        "description": "Return captured credentials, optionally filtered by service name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service filter (ssh, ftp, smb…). Empty = all."},
                "limit": {"type": "integer", "description": "Max results. Default: 100."},
            },
        },
    },
    {
        "name": "get_action_history",
        "description": "Return the history of executed Bjorn actions, most recent first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max results. Default: 50."},
                "action_name": {"type": "string", "description": "Filter by action name. Empty = all."},
            },
        },
    },
    {
        "name": "get_status",
        "description": "Return Bjorn's current operational status, scan counters, and active action.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_action",
        "description": "Queue a Bjorn action (e.g. port_scan, ssh_bruteforce) against a target IP address.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_name": {"type": "string", "description": "Action module name (e.g. port_scan)."},
                "target_ip": {"type": "string", "description": "Target IP address."},
                "target_mac": {"type": "string", "description": "Target MAC address (optional)."},
            },
            "required": ["action_name", "target_ip"],
        },
    },
    {
        "name": "query_db",
        "description": "Run a read-only SELECT query against Bjorn's SQLite database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SELECT SQL statement."},
                "params": {"type": "array", "items": {"type": "string"}, "description": "Bind parameters."},
            },
            "required": ["sql"],
        },
    },
]


class LLMBridge:
    """
    Unified LLM backend with automatic cascade:
      1. LaRuche node discovered via LAND protocol (mDNS _ai-inference._tcp.local.)
      2. Ollama running locally  (http://localhost:11434)
      3. External API           (Anthropic / OpenAI / OpenRouter)
      4. None → caller falls back to templates

    Singleton - one instance per process, thread-safe.
    """

    _instance: Optional["LLMBridge"] = None
    _init_lock = threading.Lock()

    def __new__(cls) -> "LLMBridge":
        with cls._init_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._ready = False
                cls._instance = inst
        return cls._instance

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            from init_shared import shared_data
            self._sd = shared_data
            self._laruche_url: Optional[str] = None
            self._laruche_lock = threading.Lock()
            self._discovery_active = False
            self._chat_histories: Dict[str, List[Dict]] = {}   # session_id → messages
            self._hist_lock = threading.Lock()
            self._ready = True

        # Always start mDNS discovery - even if LLM is disabled.
        # This way LaRuche URL is ready the moment the user enables LLM.
        if self._cfg("llm_laruche_discovery", True):
            self._start_laruche_discovery()

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default=None):
        return self._sd.config.get(key, getattr(self._sd, key, default))

    def _is_enabled(self) -> bool:
        return bool(self._cfg("llm_enabled", False))

    def _lang_instruction(self) -> str:
        """Return a prompt sentence that forces the LLM to reply in the configured language."""
        _LANG_NAMES = {
            "en": "English", "fr": "French", "es": "Spanish", "de": "German",
            "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
            "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
            "pl": "Polish", "sv": "Swedish", "no": "Norwegian", "da": "Danish",
            "fi": "Finnish", "cs": "Czech", "tr": "Turkish",
        }
        code = self._cfg("lang", "en")
        name = _LANG_NAMES.get(code, code)
        if code == "en":
            return ""  # No extra instruction needed for English (default)
        return f"Always respond in {name}."

    # ------------------------------------------------------------------
    # LaRuche / LAND discovery
    # ------------------------------------------------------------------

    def _start_laruche_discovery(self) -> None:
        """Launch background mDNS discovery for LaRuche/LAND nodes (non-blocking)."""
        manual_url = self._cfg("llm_laruche_url", "")
        if manual_url:
            with self._laruche_lock:
                self._laruche_url = manual_url.rstrip("/")
            logger.info(f"LaRuche: manual URL configured → {self._laruche_url}")
            return

        stop_event = threading.Event()
        self._discovery_stop = stop_event

        def _on_found(url: str) -> None:
            with self._laruche_lock:
                if self._laruche_url != url:
                    self._laruche_url = url
                    logger.info(f"LaRuche: discovered LAND node → {url}")
            self._discovery_active = True

        def _run() -> None:
            try:
                land_protocol.discover_node(_on_found, stop_event, logger=logger)
            except Exception as e:
                logger.warning(f"LAND discovery error: {e}")

        threading.Thread(target=_run, daemon=True, name="LANDDiscovery").start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def complete(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        system: Optional[str] = None,
        timeout: Optional[int] = None,
        tools: Optional[List[Dict]] = None,
    ) -> Optional[str]:
        """
        Send a chat completion request through the configured cascade.

        Args:
            messages:   List of {"role": "user"|"assistant", "content": "..."}
            max_tokens: Override llm_max_tokens config value
            system:     System prompt (prepended if supported by backend)
            timeout:    Override llm_timeout_s config value

        Returns:
            str response, or None if all backends fail / LLM disabled
        """
        if not self._is_enabled():
            return None

        max_tok = max_tokens or int(self._cfg("llm_max_tokens", 500))
        tout = timeout or int(self._cfg("llm_timeout_s", 30))
        backend = self._cfg("llm_backend", "auto")

        if backend == "auto":
            order = ["laruche", "ollama", "api"]
        else:
            order = [backend]

        for b in order:
            try:
                result = self._dispatch(b, messages, max_tok, tout, system, tools)
                if result:
                    logger.info(f"LLM response from [{b}] (len={len(result)})")
                    return result
                else:
                    logger.warning(f"LLM backend [{b}] returned empty response - skipping")
            except Exception as exc:
                logger.warning(f"LLM backend [{b}] failed: {exc}")

        logger.debug("All LLM backends failed - returning None (template fallback)")
        return None

    def generate_comment(
        self,
        status: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Generate a short EPD status comment (≤ ~12 words).
        Used by comment.py when llm_comments_enabled=True.
        """
        if not self._is_enabled():
            return None

        lang = self._lang_instruction()
        custom_comment = str(self._cfg("llm_system_prompt_comment", "") or "").strip()
        if custom_comment:
            system = custom_comment + (f" {lang}" if lang else "")
        else:
            system = (
                "You are Bjorn, a terse Norse-themed autonomous security AI. "
                "Reply with ONE sentence of at most 12 words as a status comment. "
                "Be cryptic, dark, and technical. No punctuation at the end."
                + (f" {lang}" if lang else "")
            )
        params_str = f" Context: {json.dumps(params)}" if params else ""
        prompt = f"Current status: {status}.{params_str} Write a brief status comment."

        return self.complete(
            [{"role": "user", "content": prompt}],
            max_tokens=int(self._cfg("llm_comment_max_tokens", 80)),
            system=system,
            timeout=8,   # Short timeout for EPD - fall back fast
        )

    def chat(
        self,
        user_message: str,
        session_id: str = "default",
        system: Optional[str] = None,
    ) -> Optional[str]:
        """
        Stateful chat with Bjorn - maintains conversation history per session.
        """
        if not self._is_enabled():
            return "LLM is disabled. Enable it in Settings → LLM Bridge."

        max_hist = int(self._cfg("llm_chat_history_size", 20))

        if system is None:
            system = self._build_system_prompt()

        with self._hist_lock:
            history = self._chat_histories.setdefault(session_id, [])
            history.append({"role": "user", "content": user_message})
            # Keep history bounded
            if len(history) > max_hist:
                history[:] = history[-max_hist:]
            messages = list(history)

        tools = _BJORN_TOOLS if self._cfg("llm_chat_tools_enabled", False) else None
        response = self.complete(messages, system=system, tools=tools)

        if response:
            with self._hist_lock:
                self._chat_histories[session_id].append(
                    {"role": "assistant", "content": response}
                )

        return response or "No LLM backend available. Check Settings → LLM Bridge."

    def clear_history(self, session_id: str = "default") -> None:
        with self._hist_lock:
            self._chat_histories.pop(session_id, None)

    def status(self) -> Dict[str, Any]:
        """Return current bridge status for the web UI."""
        with self._laruche_lock:
            laruche = self._laruche_url

        return {
            "enabled": self._is_enabled(),
            "backend": self._cfg("llm_backend", "auto"),
            "laruche_url": laruche,
            "laruche_discovery": self._discovery_active,
            "ollama_url": self._cfg("llm_ollama_url", "http://127.0.0.1:11434"),
            "ollama_model": self._cfg("llm_ollama_model", "phi3:mini"),
            "api_provider": self._cfg("llm_api_provider", "anthropic"),
            "api_model": self._cfg("llm_api_model", "claude-haiku-4-5-20251001"),
            "api_key_set": bool(self._cfg("llm_api_key", "")),
        }

    # ------------------------------------------------------------------
    # Backend dispatcher
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        backend: str,
        messages: List[Dict],
        max_tokens: int,
        timeout: int,
        system: Optional[str],
        tools: Optional[List[Dict]] = None,
    ) -> Optional[str]:
        if backend == "laruche":
            return self._call_laruche(messages, max_tokens, timeout, system)
        if backend == "ollama":
            return self._call_ollama(messages, max_tokens, timeout, system)
        if backend == "api":
            return self._call_api(messages, max_tokens, timeout, system, tools)
        return None

    # ------------------------------------------------------------------
    # LaRuche backend  (LAND /infer endpoint)
    # ------------------------------------------------------------------

    def _call_laruche(
        self,
        messages: List[Dict],
        max_tokens: int,
        timeout: int,
        system: Optional[str],
    ) -> Optional[str]:
        with self._laruche_lock:
            url = self._laruche_url
        if not url:
            return None

        # Build flat prompt string (LAND /infer expects a single prompt)
        prompt_parts = []
        if system:
            prompt_parts.append(f"[System]: {system}")
        for m in messages:
            role = m.get("role", "user").capitalize()
            prompt_parts.append(f"[{role}]: {m.get('content', '')}")
        prompt = "\n".join(prompt_parts)

        model = self._cfg("llm_laruche_model", "") or None
        return land_protocol.infer(url, prompt, max_tokens=max_tokens, capability="llm", model=model, timeout=timeout)

    # ------------------------------------------------------------------
    # Ollama backend  (/api/chat)
    # ------------------------------------------------------------------

    def _call_ollama(
        self,
        messages: List[Dict],
        max_tokens: int,
        timeout: int,
        system: Optional[str],
    ) -> Optional[str]:
        base = self._cfg("llm_ollama_url", "http://127.0.0.1:11434").rstrip("/")
        model = self._cfg("llm_ollama_model", "phi3:mini")

        # Ollama /api/chat supports system messages natively
        ollama_messages = []
        if system:
            ollama_messages.append({"role": "system", "content": system})
        ollama_messages.extend(messages)

        payload = {
            "model": model,
            "messages": ollama_messages,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{base}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read().decode()
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            logger.warning(f"Ollama network error: {e}")
            return None
        try:
            body = json.loads(raw_bytes)
        except json.JSONDecodeError as e:
            logger.warning(f"Ollama returned invalid JSON: {e}")
            return None
        return body.get("message", {}).get("content") or None

    # ------------------------------------------------------------------
    # External API backend  (Anthropic / OpenAI / OpenRouter)
    # ------------------------------------------------------------------

    def _call_api(
        self,
        messages: List[Dict],
        max_tokens: int,
        timeout: int,
        system: Optional[str],
        tools: Optional[List[Dict]] = None,
    ) -> Optional[str]:
        provider = self._cfg("llm_api_provider", "anthropic")
        api_key = self._cfg("llm_api_key", "")
        if not api_key:
            return None

        if provider == "anthropic":
            return self._call_anthropic(messages, max_tokens, timeout, system, api_key, tools)
        else:
            # OpenAI-compatible (openai / openrouter)
            return self._call_openai_compat(messages, max_tokens, timeout, system, api_key)

    def _call_anthropic(
        self,
        messages: List[Dict],
        max_tokens: int,
        timeout: int,
        system: Optional[str],
        api_key: str,
        tools: Optional[List[Dict]] = None,
    ) -> Optional[str]:
        """Call Anthropic Messages API with optional agentic tool-calling loop."""
        model = self._cfg("llm_api_model", "claude-haiku-4-5-20251001")
        base_url = self._cfg("llm_api_base_url", "") or "https://api.anthropic.com"
        api_url = f"{base_url.rstrip('/')}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }

        current_messages = list(messages)

        for _round in range(6):  # max 5 tool-call rounds + 1 final
            payload: Dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": current_messages,
            }
            if system:
                payload["system"] = system
            if tools:
                payload["tools"] = tools

            data = json.dumps(payload).encode()
            req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw_bytes = resp.read().decode()
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
                logger.warning(f"Anthropic network error: {e}")
                return None
            try:
                body = json.loads(raw_bytes)
            except json.JSONDecodeError as e:
                logger.warning(f"Anthropic returned invalid JSON: {e}")
                return None

            stop_reason = body.get("stop_reason")
            content = body.get("content", [])

            if stop_reason != "tool_use" or not tools:
                # Final text response
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block.get("text") or None
                return None

            # ---- tool_use round ----
            current_messages.append({"role": "assistant", "content": content})
            tool_results = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    result_text = self._execute_tool(block["name"], block.get("input", {}))
                    logger.debug(f"Tool [{block['name']}] → {result_text[:200]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": result_text,
                    })
            if not tool_results:
                break
            current_messages.append({"role": "user", "content": tool_results})

        return None

    def _execute_tool(self, name: str, inputs: Dict) -> str:
        """Execute a Bjorn tool by name and return a JSON string result."""
        try:
            import mcp_server
        except Exception as e:
            return json.dumps({"error": f"mcp_server unavailable: {e}"})

        allowed: List[str] = self._cfg("mcp_allowed_tools", [])
        if name not in allowed:
            return json.dumps({"error": f"Tool '{name}' is not enabled in Bjorn MCP config."})

        try:
            if name == "get_hosts":
                return mcp_server._impl_get_hosts(inputs.get("alive_only", True))
            if name == "get_vulnerabilities":
                return mcp_server._impl_get_vulnerabilities(
                    inputs.get("host_ip") or None, inputs.get("limit", 100)
                )
            if name == "get_credentials":
                return mcp_server._impl_get_credentials(
                    inputs.get("service") or None, inputs.get("limit", 100)
                )
            if name == "get_action_history":
                return mcp_server._impl_get_action_history(
                    inputs.get("limit", 50), inputs.get("action_name") or None
                )
            if name == "get_status":
                return mcp_server._impl_get_status()
            if name == "run_action":
                action_name = inputs.get("action_name")
                target_ip = inputs.get("target_ip")
                if not action_name or not target_ip:
                    return json.dumps({"error": "run_action requires 'action_name' and 'target_ip'"})
                return mcp_server._impl_run_action(
                    action_name, target_ip, inputs.get("target_mac", "")
                )
            if name == "query_db":
                sql = inputs.get("sql")
                if not sql:
                    return json.dumps({"error": "query_db requires 'sql'"})
                return mcp_server._impl_query_db(sql, inputs.get("params"))
            return json.dumps({"error": f"Unknown tool: {name}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _call_openai_compat(
        self,
        messages: List[Dict],
        max_tokens: int,
        timeout: int,
        system: Optional[str],
        api_key: str,
    ) -> Optional[str]:
        """Call OpenAI-compatible API (OpenAI / OpenRouter / local)."""
        model = self._cfg("llm_api_model", "gpt-4o-mini")
        base_url = (
            self._cfg("llm_api_base_url", "")
            or "https://api.openai.com"
        )

        oai_messages = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        oai_messages.extend(messages)

        payload = {
            "model": model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read().decode()
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            logger.warning(f"OpenAI-compat network error: {e}")
            return None
        try:
            body = json.loads(raw_bytes)
        except json.JSONDecodeError as e:
            logger.warning(f"OpenAI-compat returned invalid JSON: {e}")
            return None
        return body.get("choices", [{}])[0].get("message", {}).get("content") or None

    # ------------------------------------------------------------------
    # System prompt builder
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        try:
            hosts = self._sd.target_count
            vulns = self._sd.vuln_count
            creds = self._sd.cred_count
            mode = self._sd.operation_mode
            status = getattr(self._sd, "bjorn_status_text", "IDLE")
        except Exception:
            hosts, vulns, creds, mode, status = "?", "?", "?", "?", "IDLE"

        # Use custom prompt if configured, otherwise default
        custom = str(self._cfg("llm_system_prompt_chat", "") or "").strip()
        if custom:
            base = custom
        else:
            base = (
                f"You are Bjorn, an autonomous network security AI assistant running on a Raspberry Pi. "
                f"Current state: {hosts} hosts discovered, {vulns} vulnerabilities, {creds} credentials captured. "
                f"Operation mode: {mode}. Current action: {status}. "
                f"Answer security questions concisely and technically. "
                f"You can discuss network topology, vulnerabilities, and suggest next steps. "
                f"Use brief Norse references occasionally. Never break character."
            )

        # Inject user profile if set
        user_name = str(self._cfg("llm_user_name", "") or "").strip()
        user_bio = str(self._cfg("llm_user_bio", "") or "").strip()
        if user_name:
            base += f"\nThe operator's name is {user_name}."
            if user_bio:
                base += f" {user_bio}"

        lang = self._lang_instruction()
        return base + (f" {lang}" if lang else "")
