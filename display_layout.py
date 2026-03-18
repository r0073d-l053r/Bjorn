"""display_layout.py - Data-driven layout definitions for multi-size e-paper displays."""

import json
import os
import logging
from logger import Logger

logger = Logger(name="display_layout.py", level=logging.DEBUG)

# Default layout for 122x250 (epd2in13 reference)
DEFAULT_LAYOUT = {
    "meta": {
        "name": "epd2in13_default",
        "ref_width": 122,
        "ref_height": 250,
        "description": "Default layout for 2.13 inch e-paper display"
    },
    "elements": {
        "title": {"x": 37, "y": 5, "w": 80, "h": 14},
        "wifi_icon": {"x": 3, "y": 3, "w": 12, "h": 12},
        "bt_icon": {"x": 18, "y": 3, "w": 12, "h": 12},
        "usb_icon": {"x": 33, "y": 4, "w": 12, "h": 12},
        "eth_icon": {"x": 48, "y": 4, "w": 12, "h": 12},
        "battery_icon": {"x": 110, "y": 3, "w": 12, "h": 12},
        "stats_row": {"x": 2, "y": 22, "w": 118, "h": 16},
        "status_image": {"x": 3, "y": 52, "w": 15, "h": 15},
        "progress_bar": {"x": 35, "y": 75, "w": 55, "h": 5},
        "ip_text": {"x": 35, "y": 52, "w": 85, "h": 10},
        "status_line1": {"x": 35, "y": 55, "w": 85, "h": 10},
        "status_line2": {"x": 35, "y": 66, "w": 85, "h": 10},
        "comment_area": {"x": 1, "y": 86, "w": 120, "h": 73},
        "main_character": {"x": 25, "y": 100, "w": 70, "h": 65},
        "lvl_box": {"x": 2, "y": 172, "w": 18, "h": 26},
        "cpu_histogram": {"x": 2, "y": 204, "w": 8, "h": 33},
        "mem_histogram": {"x": 12, "y": 204, "w": 8, "h": 33},
        "network_kb": {"x": 101, "y": 170, "w": 20, "h": 26},
        "attacks_count": {"x": 101, "y": 200, "w": 20, "h": 26},
        "frise": {"x": 0, "y": 160, "w": 122, "h": 10},
        "line_top_bar": {"y": 20},
        "line_mid_section": {"y": 51},
        "line_comment_top": {"y": 85},
        "line_bottom_section": {"y": 170}
    },
    "fonts": {
        "title_size": 11,
        "stats_size": 8,
        "status_size": 8,
        "comment_size": 8,
        "lvl_size": 10
    }
}

# Layout for 176x264 (epd2in7)
LAYOUT_EPD2IN7 = {
    "meta": {
        "name": "epd2in7_default",
        "ref_width": 176,
        "ref_height": 264,
        "description": "Default layout for 2.7 inch e-paper display"
    },
    "elements": {
        "title": {"x": 50, "y": 5, "w": 120, "h": 16},
        "wifi_icon": {"x": 4, "y": 3, "w": 14, "h": 14},
        "bt_icon": {"x": 22, "y": 3, "w": 14, "h": 14},
        "usb_icon": {"x": 40, "y": 4, "w": 14, "h": 14},
        "eth_icon": {"x": 58, "y": 4, "w": 14, "h": 14},
        "battery_icon": {"x": 158, "y": 3, "w": 14, "h": 14},
        "stats_row": {"x": 2, "y": 24, "w": 172, "h": 18},
        "status_image": {"x": 4, "y": 55, "w": 18, "h": 18},
        "progress_bar": {"x": 45, "y": 80, "w": 80, "h": 6},
        "ip_text": {"x": 45, "y": 55, "w": 125, "h": 12},
        "status_line1": {"x": 45, "y": 58, "w": 125, "h": 12},
        "status_line2": {"x": 45, "y": 72, "w": 125, "h": 12},
        "comment_area": {"x": 2, "y": 92, "w": 172, "h": 78},
        "main_character": {"x": 35, "y": 105, "w": 100, "h": 70},
        "lvl_box": {"x": 2, "y": 178, "w": 22, "h": 30},
        "cpu_histogram": {"x": 2, "y": 215, "w": 10, "h": 38},
        "mem_histogram": {"x": 14, "y": 215, "w": 10, "h": 38},
        "network_kb": {"x": 148, "y": 178, "w": 26, "h": 30},
        "attacks_count": {"x": 148, "y": 215, "w": 26, "h": 30},
        "frise": {"x": 50, "y": 170, "w": 90, "h": 10},
        "line_top_bar": {"y": 22},
        "line_mid_section": {"y": 53},
        "line_comment_top": {"y": 90},
        "line_bottom_section": {"y": 176}
    },
    "fonts": {
        "title_size": 13,
        "stats_size": 9,
        "status_size": 9,
        "comment_size": 9,
        "lvl_size": 12
    }
}

# Registry of built-in layouts
BUILTIN_LAYOUTS = {
    "epd2in13": DEFAULT_LAYOUT,
    "epd2in13_V2": DEFAULT_LAYOUT,
    "epd2in13_V3": DEFAULT_LAYOUT,
    "epd2in13_V4": DEFAULT_LAYOUT,
    "epd2in7": LAYOUT_EPD2IN7,
}


class DisplayLayout:
    """Manages display layout definitions with per-element positioning."""

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self._layout = None
        self._custom_dir = os.path.join(
            getattr(shared_data, 'current_dir', '.'),
            'resources', 'layouts'
        )
        self.load()

    def load(self):
        """Load layout for current EPD type. Custom file overrides built-in."""
        epd_type = getattr(self.shared_data, 'epd_type',
                           self.shared_data.config.get('epd_type', 'epd2in13_V4')
                           if hasattr(self.shared_data, 'config') else 'epd2in13_V4')

        # Try custom layout file first
        custom_path = os.path.join(self._custom_dir, f'{epd_type}.json')
        if os.path.isfile(custom_path):
            try:
                with open(custom_path, 'r') as f:
                    self._layout = json.load(f)
                logger.info(f"Loaded custom layout from {custom_path}")
                return
            except Exception as e:
                logger.error(f"Failed to load custom layout {custom_path}: {e}")

        # Fallback to built-in
        base = epd_type.split('_')[0] if '_' in epd_type else epd_type
        self._layout = BUILTIN_LAYOUTS.get(epd_type) or BUILTIN_LAYOUTS.get(base) or DEFAULT_LAYOUT
        logger.info(f"Using built-in layout for {epd_type}: {self._layout['meta']['name']}")

    def get(self, element_name, prop=None):
        """Get element position dict or specific property.

        Returns: dict {x, y, w, h} or int value if prop specified.
        Falls back to (0,0) if element not found.
        """
        elem = self._layout.get('elements', {}).get(element_name, {})
        if prop:
            return elem.get(prop, 0)
        return elem

    def font_size(self, name):
        """Get font size by name."""
        return self._layout.get('fonts', {}).get(name, 8)

    def meta(self):
        """Get layout metadata."""
        return self._layout.get('meta', {})

    def ref_size(self):
        """Get reference dimensions (width, height)."""
        m = self.meta()
        return m.get('ref_width', 122), m.get('ref_height', 250)

    def all_elements(self):
        """Return all element definitions."""
        return dict(self._layout.get('elements', {}))

    def save_custom(self, layout_dict, epd_type=None):
        """Save a custom layout to disk."""
        if epd_type is None:
            epd_type = getattr(self.shared_data, 'epd_type',
                               self.shared_data.config.get('epd_type', 'epd2in13_V4')
                               if hasattr(self.shared_data, 'config') else 'epd2in13_V4')
        os.makedirs(self._custom_dir, exist_ok=True)
        path = os.path.join(self._custom_dir, f'{epd_type}.json')
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(layout_dict, f, indent=2)
        os.replace(tmp, path)
        self._layout = layout_dict
        logger.info(f"Saved custom layout to {path}")

    def reset_to_default(self, epd_type=None):
        """Delete custom layout, revert to built-in."""
        if epd_type is None:
            epd_type = getattr(self.shared_data, 'epd_type',
                               self.shared_data.config.get('epd_type', 'epd2in13_V4')
                               if hasattr(self.shared_data, 'config') else 'epd2in13_V4')
        custom_path = os.path.join(self._custom_dir, f'{epd_type}.json')
        if os.path.isfile(custom_path):
            os.remove(custom_path)
            logger.info(f"Removed custom layout {custom_path}")
        self.load()

    def to_dict(self):
        """Export current layout as dict (for API)."""
        return dict(self._layout) if self._layout else {}
