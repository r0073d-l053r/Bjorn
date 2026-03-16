# mcp_server.py
# Model Context Protocol server for Bjorn.
# Exposes Bjorn's database and actions as MCP tools consumable by any MCP client
# (Claude Desktop, custom agents, etc.).
#
# Transport: HTTP SSE (default, port configurable) or stdio.
# Requires: pip install mcp
# Gracefully no-ops if mcp is not installed.

import json
import threading
import time
from typing import Any, Dict, List, Optional

from logger import Logger

logger = Logger(name="mcp_server.py", level=20)

# ---------------------------------------------------------------------------
# Lazy shared_data import (avoids circular imports at module level)
# ---------------------------------------------------------------------------
_shared_data = None

def _sd():
    global _shared_data
    if _shared_data is None:
        from init_shared import shared_data
        _shared_data = shared_data
    return _shared_data


def _tool_allowed(name: str) -> bool:
    allowed = _sd().config.get("mcp_allowed_tools", [])
    return name in allowed


# ---------------------------------------------------------------------------
# Tool implementations (pure functions, no MCP deps)
# ---------------------------------------------------------------------------

def _impl_get_hosts(alive_only: bool = True) -> str:
    try:
        sql = "SELECT ip, mac, hostname, os, alive, ports_open FROM hosts"
        if alive_only:
            sql += " WHERE alive=1"
        sql += " ORDER BY ip"
        rows = _sd().db.query(sql, ())
        result = [dict(r) for r in rows] if rows else []
        return json.dumps(result, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _impl_get_vulnerabilities(host_ip: Optional[str] = None, limit: int = 100) -> str:
    try:
        if host_ip:
            sql = ("SELECT v.ip, v.port, v.cve_id, v.severity, v.description "
                   "FROM vulnerabilities v WHERE v.ip=? ORDER BY v.severity DESC LIMIT ?")
            rows = _sd().db.query(sql, (host_ip, limit))
        else:
            sql = ("SELECT v.ip, v.port, v.cve_id, v.severity, v.description "
                   "FROM vulnerabilities v ORDER BY v.severity DESC LIMIT ?")
            rows = _sd().db.query(sql, (limit,))
        return json.dumps([dict(r) for r in rows] if rows else [], default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _impl_get_credentials(service: Optional[str] = None, limit: int = 100) -> str:
    try:
        if service:
            sql = ("SELECT ip, port, service, username, password, found_at "
                   "FROM credentials WHERE service=? ORDER BY found_at DESC LIMIT ?")
            rows = _sd().db.query(sql, (service, limit))
        else:
            sql = ("SELECT ip, port, service, username, password, found_at "
                   "FROM credentials ORDER BY found_at DESC LIMIT ?")
            rows = _sd().db.query(sql, (limit,))
        return json.dumps([dict(r) for r in rows] if rows else [], default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _impl_get_action_history(limit: int = 50, action_name: Optional[str] = None) -> str:
    try:
        if action_name:
            sql = ("SELECT action_name, target_ip, status, result, started_at, finished_at "
                   "FROM action_history WHERE action_name=? ORDER BY started_at DESC LIMIT ?")
            rows = _sd().db.query(sql, (action_name, limit))
        else:
            sql = ("SELECT action_name, target_ip, status, result, started_at, finished_at "
                   "FROM action_history ORDER BY started_at DESC LIMIT ?")
            rows = _sd().db.query(sql, (limit,))
        return json.dumps([dict(r) for r in rows] if rows else [], default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _impl_get_status() -> str:
    try:
        sd = _sd()
        return json.dumps({
            "operation_mode": sd.operation_mode,
            "active_action": getattr(sd, "active_action", None),
            "bjorn_status": getattr(sd, "bjorn_status_text", "IDLE"),
            "bjorn_says": getattr(sd, "bjorn_says", ""),
            "hosts_discovered": getattr(sd, "target_count", 0),
            "vulnerabilities": getattr(sd, "vuln_count", 0),
            "credentials": getattr(sd, "cred_count", 0),
            "current_ip": getattr(sd, "current_ip", ""),
            "current_ssid": getattr(sd, "current_ssid", ""),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


_MCP_PRIORITY = 80  # Higher than scheduler default (40) and queue_action default (50)


def _impl_run_action(action_name: str, target_ip: str, target_mac: str = "") -> str:
    """Queue a Bjorn action with MCP priority boost. Returns queue confirmation."""
    try:
        sd = _sd()

        # Resolve MAC from IP if not supplied
        mac = target_mac or ""
        if not mac and target_ip:
            try:
                row = sd.db.query_one(
                    "SELECT mac_address FROM hosts WHERE ip=? LIMIT 1", (target_ip,)
                )
                if row:
                    mac = row["mac_address"]
            except Exception:
                pass

        sd.db.queue_action(
            action_name=action_name,
            mac=mac,
            ip=target_ip,
            priority=_MCP_PRIORITY,
            trigger="mcp",
            metadata={"decision_method": "mcp", "decision_origin": "mcp"},
        )

        # Wake the orchestrator immediately (it sleeps up to 5 s when idle)
        try:
            sd.queue_event.set()
        except Exception:
            pass

        return json.dumps({
            "status": "queued",
            "action": action_name,
            "target": target_ip,
            "priority": _MCP_PRIORITY,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _impl_query_db(sql: str, params: Optional[List] = None) -> str:
    """Run a read-only SELECT query. Non-SELECT statements are rejected."""
    try:
        stripped = sql.strip().upper()
        if not stripped.startswith("SELECT"):
            return json.dumps({"error": "Only SELECT queries are allowed."})
        rows = _sd().db.query(sql, tuple(params or []))
        return json.dumps([dict(r) for r in rows] if rows else [], default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# MCP Server setup (requires `pip install mcp`)
# ---------------------------------------------------------------------------

def _build_mcp_server():
    """Build and return a FastMCP server instance, or None if mcp not available."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        logger.warning("mcp package not installed — MCP server disabled. "
                       "Run: pip install mcp")
        return None

    mcp = FastMCP(
        name="bjorn",
        version="1.0.0",
        instructions=(
            "Bjorn is a Raspberry Pi network security tool. "
            "Use these tools to query discovered hosts, vulnerabilities, credentials, "
            "and action history, or to queue new actions."
        ),
    )

    # ---- Tool registrations ----------------------------------------

    @mcp.tool()
    def get_hosts(alive_only: bool = True) -> str:
        """Return all network hosts discovered by Bjorn's scanner.
        Set alive_only=false to include hosts that are currently offline."""
        if not _tool_allowed("get_hosts"):
            return json.dumps({"error": "Tool disabled in Bjorn MCP config."})
        return _impl_get_hosts(alive_only)

    @mcp.tool()
    def get_vulnerabilities(host_ip: str = "", limit: int = 100) -> str:
        """Return discovered vulnerabilities. Optionally filter by host_ip."""
        if not _tool_allowed("get_vulnerabilities"):
            return json.dumps({"error": "Tool disabled in Bjorn MCP config."})
        return _impl_get_vulnerabilities(host_ip or None, limit)

    @mcp.tool()
    def get_credentials(service: str = "", limit: int = 100) -> str:
        """Return captured credentials. Optionally filter by service (ssh, ftp, smb…)."""
        if not _tool_allowed("get_credentials"):
            return json.dumps({"error": "Tool disabled in Bjorn MCP config."})
        return _impl_get_credentials(service or None, limit)

    @mcp.tool()
    def get_action_history(limit: int = 50, action_name: str = "") -> str:
        """Return the history of executed actions, most recent first."""
        if not _tool_allowed("get_action_history"):
            return json.dumps({"error": "Tool disabled in Bjorn MCP config."})
        return _impl_get_action_history(limit, action_name or None)

    @mcp.tool()
    def get_status() -> str:
        """Return Bjorn's current operational status, counters, and active action."""
        if not _tool_allowed("get_status"):
            return json.dumps({"error": "Tool disabled in Bjorn MCP config."})
        return _impl_get_status()

    @mcp.tool()
    def run_action(action_name: str, target_ip: str, target_mac: str = "") -> str:
        """Queue a Bjorn action (e.g. ssh_bruteforce) against target_ip.
        The action will be executed by Bjorn's orchestrator."""
        if not _tool_allowed("run_action"):
            return json.dumps({"error": "Tool disabled in Bjorn MCP config."})
        return _impl_run_action(action_name, target_ip, target_mac)

    @mcp.tool()
    def query_db(sql: str, params: str = "[]") -> str:
        """Run a read-only SELECT query against Bjorn's SQLite database.
        params must be a JSON array of bind parameters."""
        if not _tool_allowed("query_db"):
            return json.dumps({"error": "Tool disabled in Bjorn MCP config."})
        try:
            p = json.loads(params)
        except Exception:
            p = []
        return _impl_query_db(sql, p)

    return mcp


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

_server_thread: Optional[threading.Thread] = None
_mcp_instance = None


def start(block: bool = False) -> bool:
    """
    Start the MCP server in a daemon thread.

    Args:
        block: If True, run in the calling thread (for stdio mode).

    Returns:
        True if started successfully, False otherwise.
    """
    global _server_thread, _mcp_instance

    sd = _sd()
    if not sd.config.get("mcp_enabled", False):
        logger.debug("MCP server disabled in config (mcp_enabled=False)")
        return False

    mcp = _build_mcp_server()
    if mcp is None:
        return False

    _mcp_instance = mcp
    transport = sd.config.get("mcp_transport", "http")
    port = int(sd.config.get("mcp_port", 8765))

    def _run():
        try:
            if transport == "stdio":
                logger.info("MCP server starting (stdio transport)")
                mcp.run(transport="stdio")
            else:
                logger.info(f"MCP server starting (HTTP SSE transport, port {port})")
                # FastMCP HTTP SSE — runs uvicorn internally
                mcp.run(transport="sse", port=port)
        except Exception as e:
            logger.error(f"MCP server error: {e}")

    if block:
        _run()
        return True

    _server_thread = threading.Thread(target=_run, daemon=True, name="MCPServer")
    _server_thread.start()
    logger.info(f"MCP server thread started (transport={transport})")
    return True


def stop() -> None:
    """Signal MCP server to stop (best-effort — FastMCP handles cleanup)."""
    global _server_thread
    if _server_thread and _server_thread.is_alive():
        logger.info("MCP server thread stopping (daemon — will exit with process)")
    _server_thread = None


def is_running() -> bool:
    return _server_thread is not None and _server_thread.is_alive()


def server_status() -> Dict[str, Any]:
    sd = _sd()
    return {
        "enabled": sd.config.get("mcp_enabled", False),
        "running": is_running(),
        "transport": sd.config.get("mcp_transport", "http"),
        "port": sd.config.get("mcp_port", 8765),
        "allowed_tools": sd.config.get("mcp_allowed_tools", []),
    }
