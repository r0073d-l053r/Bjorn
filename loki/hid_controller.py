"""hid_controller.py - Low-level USB HID controller for Loki.

Writes keyboard and mouse reports to /dev/hidg0 and /dev/hidg1.
"""
import os
import struct
import time
import random
import logging
import select
from threading import Event

from logger import Logger
from loki.layouts import load as load_layout

logger = Logger(name="loki.hid_controller", level=logging.DEBUG)

# ── HID Keycodes ──────────────────────────────────────────────
# USB HID Usage Tables - Keyboard/Keypad Page (0x07)

KEY_NONE = 0x00
KEY_A = 0x04
KEY_B = 0x05
KEY_C = 0x06
KEY_D = 0x07
KEY_E = 0x08
KEY_F = 0x09
KEY_G = 0x0A
KEY_H = 0x0B
KEY_I = 0x0C
KEY_J = 0x0D
KEY_K = 0x0E
KEY_L = 0x0F
KEY_M = 0x10
KEY_N = 0x11
KEY_O = 0x12
KEY_P = 0x13
KEY_Q = 0x14
KEY_R = 0x15
KEY_S = 0x16
KEY_T = 0x17
KEY_U = 0x18
KEY_V = 0x19
KEY_W = 0x1A
KEY_X = 0x1B
KEY_Y = 0x1C
KEY_Z = 0x1D
KEY_1 = 0x1E
KEY_2 = 0x1F
KEY_3 = 0x20
KEY_4 = 0x21
KEY_5 = 0x22
KEY_6 = 0x23
KEY_7 = 0x24
KEY_8 = 0x25
KEY_9 = 0x26
KEY_0 = 0x27
KEY_ENTER = 0x28
KEY_ESC = 0x29
KEY_BACKSPACE = 0x2A
KEY_TAB = 0x2B
KEY_SPACE = 0x2C
KEY_MINUS = 0x2D
KEY_EQUAL = 0x2E
KEY_LEFTBRACE = 0x2F
KEY_RIGHTBRACE = 0x30
KEY_BACKSLASH = 0x31
KEY_SEMICOLON = 0x33
KEY_APOSTROPHE = 0x34
KEY_GRAVE = 0x35
KEY_COMMA = 0x36
KEY_DOT = 0x37
KEY_SLASH = 0x38
KEY_CAPSLOCK = 0x39
KEY_F1 = 0x3A
KEY_F2 = 0x3B
KEY_F3 = 0x3C
KEY_F4 = 0x3D
KEY_F5 = 0x3E
KEY_F6 = 0x3F
KEY_F7 = 0x40
KEY_F8 = 0x41
KEY_F9 = 0x42
KEY_F10 = 0x43
KEY_F11 = 0x44
KEY_F12 = 0x45
KEY_PRINTSCREEN = 0x46
KEY_SCROLLLOCK = 0x47
KEY_PAUSE = 0x48
KEY_INSERT = 0x49
KEY_HOME = 0x4A
KEY_PAGEUP = 0x4B
KEY_DELETE = 0x4C
KEY_END = 0x4D
KEY_PAGEDOWN = 0x4E
KEY_RIGHT = 0x4F
KEY_LEFT = 0x50
KEY_DOWN = 0x51
KEY_UP = 0x52
KEY_NUMLOCK = 0x53

# ── Modifier bitmasks ─────────────────────────────────────────
MOD_NONE = 0x00
MOD_LEFT_CONTROL = 0x01
MOD_LEFT_SHIFT = 0x02
MOD_LEFT_ALT = 0x04
MOD_LEFT_GUI = 0x08
MOD_RIGHT_CONTROL = 0x10
MOD_RIGHT_SHIFT = 0x20
MOD_RIGHT_ALT = 0x40
MOD_RIGHT_GUI = 0x80

# ── Combo name → (modifier_mask, keycode) ─────────────────────
_COMBO_MAP = {
    # Modifiers (used standalone or in combos)
    "CTRL": (MOD_LEFT_CONTROL, KEY_NONE),
    "CONTROL": (MOD_LEFT_CONTROL, KEY_NONE),
    "SHIFT": (MOD_LEFT_SHIFT, KEY_NONE),
    "ALT": (MOD_LEFT_ALT, KEY_NONE),
    "GUI": (MOD_LEFT_GUI, KEY_NONE),
    "WIN": (MOD_LEFT_GUI, KEY_NONE),
    "WINDOWS": (MOD_LEFT_GUI, KEY_NONE),
    "COMMAND": (MOD_LEFT_GUI, KEY_NONE),
    "META": (MOD_LEFT_GUI, KEY_NONE),
    "RCTRL": (MOD_RIGHT_CONTROL, KEY_NONE),
    "RSHIFT": (MOD_RIGHT_SHIFT, KEY_NONE),
    "RALT": (MOD_RIGHT_ALT, KEY_NONE),
    "RGUI": (MOD_RIGHT_GUI, KEY_NONE),
    # Special keys
    "ENTER": (MOD_NONE, KEY_ENTER),
    "RETURN": (MOD_NONE, KEY_ENTER),
    "ESC": (MOD_NONE, KEY_ESC),
    "ESCAPE": (MOD_NONE, KEY_ESC),
    "BACKSPACE": (MOD_NONE, KEY_BACKSPACE),
    "TAB": (MOD_NONE, KEY_TAB),
    "SPACE": (MOD_NONE, KEY_SPACE),
    "CAPSLOCK": (MOD_NONE, KEY_CAPSLOCK),
    "DELETE": (MOD_NONE, KEY_DELETE),
    "INSERT": (MOD_NONE, KEY_INSERT),
    "HOME": (MOD_NONE, KEY_HOME),
    "END": (MOD_NONE, KEY_END),
    "PAGEUP": (MOD_NONE, KEY_PAGEUP),
    "PAGEDOWN": (MOD_NONE, KEY_PAGEDOWN),
    "UP": (MOD_NONE, KEY_UP),
    "DOWN": (MOD_NONE, KEY_DOWN),
    "LEFT": (MOD_NONE, KEY_LEFT),
    "RIGHT": (MOD_NONE, KEY_RIGHT),
    "PRINTSCREEN": (MOD_NONE, KEY_PRINTSCREEN),
    "SCROLLLOCK": (MOD_NONE, KEY_SCROLLLOCK),
    "PAUSE": (MOD_NONE, KEY_PAUSE),
    "NUMLOCK": (MOD_NONE, KEY_NUMLOCK),
    # F keys
    "F1": (MOD_NONE, KEY_F1), "F2": (MOD_NONE, KEY_F2),
    "F3": (MOD_NONE, KEY_F3), "F4": (MOD_NONE, KEY_F4),
    "F5": (MOD_NONE, KEY_F5), "F6": (MOD_NONE, KEY_F6),
    "F7": (MOD_NONE, KEY_F7), "F8": (MOD_NONE, KEY_F8),
    "F9": (MOD_NONE, KEY_F9), "F10": (MOD_NONE, KEY_F10),
    "F11": (MOD_NONE, KEY_F11), "F12": (MOD_NONE, KEY_F12),
    # Letters (for combo usage like "GUI r")
    "A": (MOD_NONE, KEY_A), "B": (MOD_NONE, KEY_B),
    "C": (MOD_NONE, KEY_C), "D": (MOD_NONE, KEY_D),
    "E": (MOD_NONE, KEY_E), "F": (MOD_NONE, KEY_F),
    "G": (MOD_NONE, KEY_G), "H": (MOD_NONE, KEY_H),
    "I": (MOD_NONE, KEY_I), "J": (MOD_NONE, KEY_J),
    "K": (MOD_NONE, KEY_K), "L": (MOD_NONE, KEY_L),
    "M": (MOD_NONE, KEY_M), "N": (MOD_NONE, KEY_N),
    "O": (MOD_NONE, KEY_O), "P": (MOD_NONE, KEY_P),
    "Q": (MOD_NONE, KEY_Q), "R": (MOD_NONE, KEY_R),
    "S": (MOD_NONE, KEY_S), "T": (MOD_NONE, KEY_T),
    "U": (MOD_NONE, KEY_U), "V": (MOD_NONE, KEY_V),
    "W": (MOD_NONE, KEY_W), "X": (MOD_NONE, KEY_X),
    "Y": (MOD_NONE, KEY_Y), "Z": (MOD_NONE, KEY_Z),
}

# ── LED bitmasks (host → device output report) ────────────────
LED_NUM = 0x01
LED_CAPS = 0x02
LED_SCROLL = 0x04
LED_ANY = 0xFF


class HIDController:
    """Low-level USB HID report writer."""

    def __init__(self):
        self._kbd_fd = None    # /dev/hidg0
        self._mouse_fd = None  # /dev/hidg1
        self._layout = load_layout("us")
        self._speed_min = 0    # ms between keystrokes (0 = instant)
        self._speed_max = 0

    # ── Lifecycle ──────────────────────────────────────────────

    def open(self):
        """Open HID gadget device files."""
        try:
            self._kbd_fd = os.open("/dev/hidg0", os.O_RDWR | os.O_NONBLOCK)
            logger.info("Opened /dev/hidg0 (keyboard)")
        except OSError as e:
            logger.error("Cannot open /dev/hidg0: %s", e)
            raise

        try:
            self._mouse_fd = os.open("/dev/hidg1", os.O_RDWR | os.O_NONBLOCK)
            logger.info("Opened /dev/hidg1 (mouse)")
        except OSError as e:
            logger.warning("Cannot open /dev/hidg1 (mouse disabled): %s", e)
            self._mouse_fd = None

    def close(self):
        """Close HID device files."""
        self.release_all()
        if self._kbd_fd is not None:
            try:
                os.close(self._kbd_fd)
            except OSError:
                pass
            self._kbd_fd = None
        if self._mouse_fd is not None:
            try:
                os.close(self._mouse_fd)
            except OSError:
                pass
            self._mouse_fd = None
        logger.debug("HID devices closed")

    @property
    def is_open(self) -> bool:
        return self._kbd_fd is not None

    # ── Layout ─────────────────────────────────────────────────

    def set_layout(self, name: str):
        """Switch keyboard layout."""
        self._layout = load_layout(name)
        logger.debug("Layout switched to '%s'", name)

    def set_typing_speed(self, min_ms: int, max_ms: int):
        """Set random delay range between keystrokes (ms)."""
        self._speed_min = max(0, min_ms)
        self._speed_max = max(self._speed_min, max_ms)

    # ── Keyboard Reports ───────────────────────────────────────

    def send_key_report(self, modifiers: int, keys: list):
        """Send an 8-byte keyboard report: [mod, 0x00, key1..key6]."""
        if self._kbd_fd is None:
            return
        report = bytearray(8)
        report[0] = modifiers & 0xFF
        for i, k in enumerate(keys[:6]):
            report[2 + i] = k & 0xFF
        os.write(self._kbd_fd, bytes(report))

    def release_all(self):
        """Send empty keyboard + mouse reports (release everything)."""
        if self._kbd_fd is not None:
            try:
                os.write(self._kbd_fd, bytes(8))
            except OSError:
                pass
        if self._mouse_fd is not None:
            try:
                os.write(self._mouse_fd, bytes([0x01, 0, 0, 0, 0, 0]))
            except OSError:
                pass

    def press_combo(self, combo_str: str):
        """Press a key combination like 'GUI r', 'CTRL ALT DELETE'.

        Keys are separated by spaces. All are pressed simultaneously, then released.
        """
        parts = combo_str.strip().split()
        mod_mask = 0
        keycodes = []

        for part in parts:
            upper = part.upper()
            if upper in _COMBO_MAP:
                m, k = _COMBO_MAP[upper]
                mod_mask |= m
                if k != KEY_NONE:
                    keycodes.append(k)
            else:
                # Try single char via layout
                if len(part) == 1 and part in self._layout:
                    char_mod, char_key = self._layout[part]
                    mod_mask |= char_mod
                    keycodes.append(char_key)
                else:
                    logger.warning("Unknown combo key: '%s'", part)

        if keycodes or mod_mask:
            self.send_key_report(mod_mask, keycodes)
            time.sleep(0.02)
            self.send_key_report(0, [])  # release

    def type_string(self, text: str, stop_event: Event = None):
        """Type a string character by character using the current layout."""
        for ch in text:
            if stop_event and stop_event.is_set():
                return
            if ch in self._layout:
                mod, key = self._layout[ch]
                self.send_key_report(mod, [key])
                time.sleep(0.01)
                self.send_key_report(0, [])  # release
            else:
                logger.warning("Unmapped char: %r", ch)
                continue

            # Inter-keystroke delay
            if self._speed_max > 0:
                delay = random.randint(self._speed_min, self._speed_max) / 1000.0
                if stop_event:
                    stop_event.wait(delay)
                else:
                    time.sleep(delay)
            else:
                time.sleep(0.005)  # tiny default gap for reliability

    # ── LED State ──────────────────────────────────────────────

    def read_led_state(self) -> int:
        """Read current LED state from host (non-blocking). Returns bitmask."""
        if self._kbd_fd is None:
            return 0
        try:
            r, _, _ = select.select([self._kbd_fd], [], [], 0)
            if r:
                data = os.read(self._kbd_fd, 1)
                if data:
                    return data[0]
        except OSError:
            pass
        return 0

    def wait_led(self, mask: int, stop_event: Event = None, timeout: float = 0):
        """Block until host LED state matches mask.

        mask=LED_ANY matches any LED change.
        Returns True if matched, False if stopped/timed out.
        """
        start = time.monotonic()
        initial = self.read_led_state()
        while True:
            if stop_event and stop_event.is_set():
                return False
            if timeout > 0 and (time.monotonic() - start) > timeout:
                return False
            current = self.read_led_state()
            if mask == LED_ANY:
                if current != initial:
                    return True
            else:
                if current & mask:
                    return True
            time.sleep(0.05)

    def wait_led_repeat(self, mask: int, count: int, stop_event: Event = None):
        """Wait for LED to toggle count times."""
        for _ in range(count):
            if not self.wait_led(mask, stop_event):
                return False
        return True

    # ── Mouse Reports ──────────────────────────────────────────
    # P4wnP1 mouse descriptor uses Report ID 1 for relative mode.
    # Report format: [0x01, buttons, X, Y, 0x00, 0x00] = 6 bytes

    def send_mouse_report(self, buttons: int, x: int, y: int, wheel: int = 0):
        """Send a 6-byte relative mouse report with Report ID 1.

        Format: [report_id=1, buttons, X, Y, pad, pad]
        """
        if self._mouse_fd is None:
            return
        # Clamp to signed byte range
        x = max(-127, min(127, x))
        y = max(-127, min(127, y))
        report = struct.pack("BBbbBB", 0x01, buttons & 0xFF, x, y, 0, 0)
        os.write(self._mouse_fd, report)

    def mouse_move(self, x: int, y: int):
        """Move mouse by (x, y) relative pixels."""
        self.send_mouse_report(0, x, y)

    def mouse_move_stepped(self, x: int, y: int, step: int = 10):
        """Move mouse in small increments for better tracking."""
        while x != 0 or y != 0:
            dx = max(-step, min(step, x))
            dy = max(-step, min(step, y))
            self.send_mouse_report(0, dx, dy)
            x -= dx
            y -= dy
            time.sleep(0.005)

    def mouse_click(self, button: int = 1):
        """Click a mouse button (1=left, 2=right, 4=middle)."""
        self.send_mouse_report(button, 0, 0)
        time.sleep(0.05)
        self.send_mouse_report(0, 0, 0)

    def mouse_double_click(self, button: int = 1):
        """Double-click a mouse button."""
        self.mouse_click(button)
        time.sleep(0.05)
        self.mouse_click(button)
