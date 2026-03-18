"""llm_utils.py - HTTP endpoints for LLM chat, bridge config, and MCP server config."""
import json
import uuid
from typing import Any, Dict

from logger import Logger

logger = Logger(name="llm_utils.py", level=20)

_ALLOWED_TOOLS = [
    "get_hosts", "get_vulnerabilities", "get_credentials",
    "get_action_history", "get_status", "run_action", "query_db",
]


def _send_json(handler, data: Any, status: int = 200) -> None:
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class LLMUtils:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    # ------------------------------------------------------------------
    # GET  /api/llm/status
    # ------------------------------------------------------------------
    def get_llm_status(self, handler) -> None:
        """Return current LLM bridge status."""
        try:
            from llm_bridge import LLMBridge
            status = LLMBridge().status()
        except Exception as e:
            status = {"error": str(e), "enabled": False}
        _send_json(handler, status)

    # ------------------------------------------------------------------
    # POST /api/llm/chat   {"message": "...", "session_id": "..."}
    # ------------------------------------------------------------------
    def handle_chat(self, data: Dict) -> Dict:
        """Process a chat message and return the LLM response."""
        message = (data.get("message") or "").strip()
        if not message:
            return {"status": "error", "message": "Empty message"}

        session_id = data.get("session_id") or "default"

        try:
            from llm_bridge import LLMBridge
            response = LLMBridge().chat(message, session_id=session_id)
            return {"status": "ok", "response": response or "(no response)", "session_id": session_id}
        except Exception as e:
            logger.error(f"Chat error: {e}")
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------
    # POST /api/llm/clear_history   {"session_id": "..."}
    # ------------------------------------------------------------------
    def clear_chat_history(self, data: Dict) -> Dict:
        session_id = data.get("session_id") or "default"
        try:
            from llm_bridge import LLMBridge
            LLMBridge().clear_history(session_id)
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------
    # GET  /api/mcp/status
    # ------------------------------------------------------------------
    def get_mcp_status(self, handler) -> None:
        """Return current MCP server status."""
        try:
            import mcp_server
            status = mcp_server.server_status()
        except Exception as e:
            status = {"error": str(e), "enabled": False, "running": False}
        _send_json(handler, status)

    # ------------------------------------------------------------------
    # POST /api/mcp/toggle   {"enabled": true/false}
    # ------------------------------------------------------------------
    def toggle_mcp(self, data: Dict) -> Dict:
        """Enable or disable the MCP server."""
        enabled = bool(data.get("enabled", False))
        try:
            self.shared_data.config["mcp_enabled"] = enabled
            setattr(self.shared_data, "mcp_enabled", enabled)
            self.shared_data.save_config()

            import mcp_server
            if enabled and not mcp_server.is_running():
                started = mcp_server.start()
                return {"status": "ok", "enabled": True, "started": started}
            elif not enabled:
                mcp_server.stop()
                return {"status": "ok", "enabled": False}
            return {"status": "ok", "enabled": enabled}
        except Exception as e:
            logger.error(f"MCP toggle error: {e}")
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------
    # POST /api/mcp/config   {"allowed_tools": [...], "port": 8765, ...}
    # ------------------------------------------------------------------
    def save_mcp_config(self, data: Dict) -> Dict:
        """Save MCP server configuration."""
        try:
            cfg = self.shared_data.config

            if "allowed_tools" in data:
                tools = [t for t in data["allowed_tools"] if t in _ALLOWED_TOOLS]
                cfg["mcp_allowed_tools"] = tools

            if "port" in data:
                port = int(data["port"])
                if 1024 <= port <= 65535:
                    cfg["mcp_port"] = port

            if "transport" in data and data["transport"] in ("http", "stdio"):
                cfg["mcp_transport"] = data["transport"]

            self.shared_data.save_config()
            return {"status": "ok", "config": {
                "mcp_enabled": cfg.get("mcp_enabled", False),
                "mcp_port": cfg.get("mcp_port", 8765),
                "mcp_transport": cfg.get("mcp_transport", "http"),
                "mcp_allowed_tools": cfg.get("mcp_allowed_tools", []),
            }}
        except Exception as e:
            logger.error(f"MCP config save error: {e}")
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------
    # POST /api/llm/config   {all llm_* keys}
    # ------------------------------------------------------------------
    def save_llm_config(self, data: Dict) -> Dict:
        """Save LLM bridge configuration."""
        _llm_keys = {
            "llm_enabled", "llm_comments_enabled", "llm_comments_log", "llm_chat_enabled",
            "llm_backend", "llm_laruche_discovery", "llm_laruche_url", "llm_laruche_model",
            "llm_ollama_url", "llm_ollama_model",
            "llm_api_provider", "llm_api_key", "llm_api_model", "llm_api_base_url",
            "llm_timeout_s", "llm_max_tokens", "llm_comment_max_tokens",
            "llm_chat_history_size", "llm_chat_tools_enabled",
            # Orchestrator keys
            "llm_orchestrator_mode", "llm_orchestrator_interval_s",
            "llm_orchestrator_max_actions", "llm_orchestrator_allowed_actions",
            "llm_orchestrator_skip_if_no_change", "llm_orchestrator_log_reasoning",
            "llm_orchestrator_skip_scheduler",
            # Personality & prompt keys
            "llm_system_prompt_chat", "llm_system_prompt_comment",
            "llm_user_name", "llm_user_bio",
            # EPD
            "epd_buttons_enabled",
        }
        _int_keys = {
            "llm_timeout_s", "llm_max_tokens", "llm_comment_max_tokens",
            "llm_chat_history_size", "llm_orchestrator_interval_s",
            "llm_orchestrator_max_actions",
        }
        _bool_keys = {
            "llm_enabled", "llm_comments_enabled", "llm_comments_log", "llm_chat_enabled",
            "llm_laruche_discovery", "llm_chat_tools_enabled",
            "llm_orchestrator_skip_if_no_change", "llm_orchestrator_log_reasoning",
            "llm_orchestrator_skip_scheduler", "epd_buttons_enabled",
        }
        try:
            cfg = self.shared_data.config
            for key in _llm_keys:
                if key in data:
                    value = data[key]
                    if key in _int_keys:
                        value = int(value)
                    elif key in _bool_keys:
                        value = bool(value)
                    cfg[key] = value
                    setattr(self.shared_data, key, value)

            self.shared_data.save_config()
            self.shared_data.invalidate_config_cache()

            # Restart discovery if URL/toggle changed
            if "llm_laruche_url" in data or "llm_laruche_discovery" in data:
                try:
                    from llm_bridge import LLMBridge
                    bridge = LLMBridge()
                    bridge._laruche_url = cfg.get("llm_laruche_url") or None
                    if cfg.get("llm_laruche_discovery", True) and not bridge._discovery_active:
                        bridge._start_laruche_discovery()
                except Exception:
                    pass

            # Notify orchestrator of mode change
            if "llm_orchestrator_mode" in data:
                try:
                    from orchestrator import Orchestrator
                    orch = getattr(self.shared_data, '_orchestrator_ref', None)
                    if orch and hasattr(orch, 'llm_orchestrator'):
                        orch.llm_orchestrator.restart_if_mode_changed()
                except Exception:
                    pass

            return {"status": "ok"}
        except Exception as e:
            logger.error(f"LLM config save error: {e}")
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------
    # GET  /api/llm/models?backend=laruche|ollama
    # Returns available models from the specified backend.
    # ------------------------------------------------------------------
    def get_llm_models(self, handler, params: Dict = None) -> None:
        """Return available models from LaRuche or Ollama."""
        backend = (params or {}).get("backend", "laruche")
        models = []
        laruche_default = None
        try:
            if backend == "laruche":
                import land_protocol
                # Get LaRuche URL from bridge discovery or config
                url = self.shared_data.config.get("llm_laruche_url", "")
                if not url:
                    try:
                        from llm_bridge import LLMBridge
                        bridge = LLMBridge()
                        with bridge._laruche_lock:
                            url = bridge._laruche_url or ""
                    except Exception:
                        pass
                if url:
                    result_data = land_protocol.list_models(url, timeout=10)
                    raw = result_data.get("models", []) if isinstance(result_data, dict) else result_data
                    for m in raw:
                        if isinstance(m, dict):
                            models.append({
                                "name": m.get("name", m.get("model", "?")),
                                "size": m.get("size", 0),
                                "modified": m.get("modified_at", ""),
                            })
                        elif isinstance(m, str):
                            models.append({"name": m, "size": 0})
                    # Extract default model from the same /models response
                    if isinstance(result_data, dict):
                        laruche_default = result_data.get("default_model")
            elif backend == "ollama":
                base = self.shared_data.config.get("llm_ollama_url", "http://127.0.0.1:11434").rstrip("/")
                import urllib.request
                req = urllib.request.Request(f"{base}/api/tags", method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json.loads(resp.read().decode())
                for m in body.get("models", []):
                    models.append({
                        "name": m.get("name", "?"),
                        "size": m.get("size", 0),
                        "modified": m.get("modified_at", ""),
                    })
        except Exception as e:
            _send_json(handler, {"status": "error", "message": str(e), "models": []})
            return

        result = {"status": "ok", "backend": backend, "models": models}
        if laruche_default:
            result["default_model"] = laruche_default

        _send_json(handler, result)

    # ------------------------------------------------------------------
    # GET  /api/llm/reasoning
    # Returns the llm_orchestrator chat session (reasoning log).
    # ------------------------------------------------------------------
    def get_llm_reasoning(self, handler) -> None:
        """Return the LLM orchestrator reasoning session history."""
        try:
            from llm_bridge import LLMBridge
            bridge = LLMBridge()
            with bridge._hist_lock:
                hist = list(bridge._chat_histories.get("llm_orchestrator", []))
            _send_json(handler, {"status": "ok", "messages": hist, "count": len(hist)})
        except Exception as e:
            _send_json(handler, {"status": "error", "message": str(e), "messages": [], "count": 0})

    # ------------------------------------------------------------------
    # GET  /api/llm/config
    # ------------------------------------------------------------------
    def get_llm_config(self, handler) -> None:
        """Return current LLM config (api_key redacted) + live discovery state."""
        cfg = self.shared_data.config
        result = {k: cfg.get(k) for k in (
            "llm_enabled", "llm_comments_enabled", "llm_comments_log", "llm_chat_enabled",
            "llm_backend", "llm_laruche_discovery", "llm_laruche_url", "llm_laruche_model",
            "llm_ollama_url", "llm_ollama_model",
            "llm_api_provider", "llm_api_model", "llm_api_base_url",
            "llm_timeout_s", "llm_max_tokens", "llm_comment_max_tokens",
            "llm_chat_history_size", "llm_chat_tools_enabled",
            # Orchestrator
            "llm_orchestrator_mode", "llm_orchestrator_interval_s",
            "llm_orchestrator_max_actions", "llm_orchestrator_skip_if_no_change",
            "llm_orchestrator_log_reasoning", "llm_orchestrator_skip_scheduler",
            # EPD
            "epd_buttons_enabled",
            # Personality & prompts
            "llm_system_prompt_chat", "llm_system_prompt_comment",
            "llm_user_name", "llm_user_bio",
        )}
        result["llm_api_key_set"] = bool(cfg.get("llm_api_key", ""))

        # Default prompts for placeholder display in the UI
        result["llm_default_prompt_chat"] = (
            "You are Bjorn, an autonomous network security AI assistant running on a Raspberry Pi. "
            "Current state: {hosts} hosts discovered, {vulns} vulnerabilities, {creds} credentials captured. "
            "Operation mode: {mode}. Current action: {status}. "
            "Answer security questions concisely and technically. "
            "You can discuss network topology, vulnerabilities, and suggest next steps. "
            "Use brief Norse references occasionally. Never break character."
        )
        result["llm_default_prompt_comment"] = (
            "You are Bjorn, a terse Norse-themed autonomous security AI. "
            "Reply with ONE sentence of at most 12 words as a status comment. "
            "Be cryptic, dark, and technical. No punctuation at the end."
        )

        # Inject live mDNS discovery state so the UI can show it
        try:
            from llm_bridge import LLMBridge
            bridge = LLMBridge()
            with bridge._laruche_lock:
                result["laruche_discovered_url"] = bridge._laruche_url or ""
            result["laruche_discovery_active"] = bridge._discovery_active
        except Exception:
            result["laruche_discovered_url"] = ""
            result["laruche_discovery_active"] = False

        _send_json(handler, result)
