# land_protocol.py
# Python client for the LAND Protocol (Local AI Network Discovery).
# https://github.com/infinition/land-protocol
#
# Replace this file to update LAND protocol compatibility.
# Imported by llm_bridge.py — no other Bjorn code touches this.
#
# Protocol summary:
#   Discovery : mDNS service type  _ai-inference._tcp.local.  (port 5353)
#   Transport : TCP HTTP on port 8419 by default
#   Infer     : POST /infer  {"prompt": str, "capability": "llm", "max_tokens": int}
#   Response  : {"response": str}  or  {"text": str}

import json
import threading
import time
import urllib.request
import urllib.error
from typing import Optional, Callable

# mDNS service type broadcast by all LAND-compatible nodes (LaRuche, etc.)
LAND_SERVICE_TYPE = "_ai-inference._tcp.local."

# Default inference port
LAND_DEFAULT_PORT = 8419


def discover_node(
    on_found: Callable[[str], None],
    stop_event: threading.Event,
    logger=None,
) -> None:
    """
    Background mDNS listener for LAND nodes.

    Calls on_found(url) whenever a new node is discovered.
    Runs until stop_event is set.

    Requires: pip install zeroconf
    """
    try:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
    except ImportError:
        if logger:
            logger.warning(
                "zeroconf not installed — LAND mDNS discovery disabled. "
                "Run:  pip install zeroconf"
            )
        else:
            print("[LAND] zeroconf not installed — mDNS discovery disabled")
        return

    class _Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:  # type: ignore[override]
            info = zc.get_service_info(type_, name)
            if not info:
                return
            addresses = info.parsed_scoped_addresses()
            if not addresses:
                return
            port = info.port or LAND_DEFAULT_PORT
            url = f"http://{addresses[0]}:{port}"
            on_found(url)

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:  # type: ignore[override]
            pass

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:  # type: ignore[override]
            self.add_service(zc, type_, name)

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, LAND_SERVICE_TYPE, _Listener())
        if logger:
            logger.info(f"LAND: mDNS discovery active ({LAND_SERVICE_TYPE})")
        while not stop_event.is_set():
            time.sleep(5)
    finally:
        zc.close()


def infer(
    base_url: str,
    prompt: str,
    max_tokens: int = 500,
    capability: str = "llm",
    model: Optional[str] = None,
    timeout: int = 30,
) -> Optional[str]:
    """
    Send an inference request to a LAND node.

    POST {base_url}/infer
    Body: {"prompt": str, "capability": str, "max_tokens": int, "model": str|null}

    If model is None, the node uses its default model.
    Returns the response text, or None on failure.
    """
    payload = {
        "prompt": prompt,
        "capability": capability,
        "max_tokens": max_tokens,
    }
    if model:
        payload["model"] = model
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/infer",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    # LAND response may use "response" or "text" key
    return body.get("response") or body.get("text") or None


def get_default_model(base_url: str, timeout: int = 10) -> Optional[str]:
    """
    Get the current default model from a LAND node.

    GET {base_url}/config/default_model
    Returns the model name string, or None on failure.
    """
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/config/default_model",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        return body.get("default_model") or None
    except Exception:
        return None


def list_models(base_url: str, timeout: int = 10) -> dict:
    """
    List available models on a LAND node.

    GET {base_url}/models
    Returns a dict with:
      - "models": list of model dicts
      - "default_model": str or None (the node's current default model)

    Example: {"models": [{"name": "mistral:latest", ...}], "default_model": "mistral:latest"}
    Returns {"models": [], "default_model": None} on failure.
    """
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/models",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        # LaRuche returns {"models": [...], "default_model": "..."} or a flat list
        if isinstance(body, list):
            return {"models": body, "default_model": None}
        if isinstance(body, dict):
            return {
                "models": body.get("models", []),
                "default_model": body.get("default_model") or None,
            }
        return {"models": [], "default_model": None}
    except Exception:
        return {"models": [], "default_model": None}
