"""__init__.py - Keyboard layout loader for Loki HID subsystem.

Caches loaded layouts in memory.
"""
import json
import os
import logging

from logger import Logger

logger = Logger(name="loki.layouts", level=logging.DEBUG)

_LAYOUT_DIR = os.path.dirname(os.path.abspath(__file__))
_cache = {}


def load(name: str = "us") -> dict:
    """Load a keyboard layout by name. Returns char → (modifier, keycode) map."""
    name = name.lower()
    if name in _cache:
        return _cache[name]

    path = os.path.join(_LAYOUT_DIR, f"{name}.json")
    if not os.path.isfile(path):
        logger.warning("Layout '%s' not found, falling back to 'us'", name)
        path = os.path.join(_LAYOUT_DIR, "us.json")
        name = "us"
        if name in _cache:
            return _cache[name]

    with open(path, "r") as f:
        data = json.load(f)

    _cache[name] = data
    logger.debug("Loaded keyboard layout '%s' (%d chars)", name, len(data))
    return data


def available() -> list:
    """List available layout names."""
    layouts = []
    for f in os.listdir(_LAYOUT_DIR):
        if f.endswith(".json"):
            layouts.append(f[:-5])
    return sorted(layouts)
