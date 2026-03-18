"""logger.py - Rotating file + console logger with custom SUCCESS level."""

import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler

SUCCESS_LEVEL_NUM = 25
logging.addLevelName(SUCCESS_LEVEL_NUM, "SUCCESS")


def success(self, message, *args, **kwargs):
    if self.isEnabledFor(SUCCESS_LEVEL_NUM):
        self._log(SUCCESS_LEVEL_NUM, message, args, **kwargs)


logging.Logger.success = success


class VerticalFilter(logging.Filter):
    def filter(self, record):
        return "Vertical" not in record.getMessage()


class Logger:
    LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs")
    LOG_FILE = os.path.join(LOGS_DIR, "Bjorn.log")

    _HANDLERS_LOCK = threading.Lock()
    _SHARED_CONSOLE_HANDLER = None
    _SHARED_FILE_HANDLER = None

    @classmethod
    def _ensure_shared_handlers(cls, enable_file_logging: bool):
        """
        Create shared handlers once.

        Why: every action instantiates Logger(name=...), which used to create a new
        RotatingFileHandler per logger name, leaking file descriptors (Bjorn.log opened N times).
        """
        with cls._HANDLERS_LOCK:
            if cls._SHARED_CONSOLE_HANDLER is None:
                h = logging.StreamHandler()
                # Do not filter by handler level; per-logger level controls output.
                h.setLevel(logging.NOTSET)
                h.setFormatter(
                    logging.Formatter(
                        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                    )
                )
                h.addFilter(VerticalFilter())
                cls._SHARED_CONSOLE_HANDLER = h

            if enable_file_logging and cls._SHARED_FILE_HANDLER is None:
                os.makedirs(cls.LOGS_DIR, exist_ok=True)
                h = RotatingFileHandler(
                    cls.LOG_FILE,
                    maxBytes=5 * 1024 * 1024,
                    backupCount=2,
                )
                h.setLevel(logging.NOTSET)
                h.setFormatter(
                    logging.Formatter(
                        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                    )
                )
                h.addFilter(VerticalFilter())
                cls._SHARED_FILE_HANDLER = h

            handlers = [cls._SHARED_CONSOLE_HANDLER]
            if enable_file_logging and cls._SHARED_FILE_HANDLER is not None:
                handlers.append(cls._SHARED_FILE_HANDLER)
            return handlers

    # Max entries before automatic purge of stale throttle keys
    _THROTTLE_MAX_KEYS = 200
    _THROTTLE_PURGE_AGE = 600.0  # Remove keys older than 10 minutes

    def __init__(self, name="Logger", level=logging.DEBUG, enable_file_logging=True):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        self.logger.propagate = False
        self.enable_file_logging = enable_file_logging
        self._throttle_lock = threading.Lock()
        self._throttle_state = {}
        self._throttle_last_purge = 0.0

        # Attach shared handlers (singleton) to avoid leaking file descriptors.
        for h in self._ensure_shared_handlers(self.enable_file_logging):
            if h not in self.logger.handlers:
                self.logger.addHandler(h)

    def set_level(self, level):
        self.logger.setLevel(level)
        for handler in self.logger.handlers:
            handler.setLevel(level)

    def debug(self, msg, *args, **kwargs):
        self.logger.debug(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self.logger.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self.logger.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self.logger.error(msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self.logger.critical(msg, *args, **kwargs)

    def success(self, msg, *args, **kwargs):
        self.logger.success(msg, *args, **kwargs)

    def info_throttled(self, msg, key=None, interval_s=60.0):
        self._log_throttled(logging.INFO, msg, key=key, interval_s=interval_s)

    def warning_throttled(self, msg, key=None, interval_s=60.0):
        self._log_throttled(logging.WARNING, msg, key=key, interval_s=interval_s)

    def error_throttled(self, msg, key=None, interval_s=60.0):
        self._log_throttled(logging.ERROR, msg, key=key, interval_s=interval_s)

    def _log_throttled(self, level, msg, key=None, interval_s=60.0):
        throttle_key = key or f"{level}:{msg}"
        now = time.monotonic()
        with self._throttle_lock:
            last = self._throttle_state.get(throttle_key, 0.0)
            if (now - last) < max(0.0, float(interval_s)):
                return
            self._throttle_state[throttle_key] = now
            # Periodic purge of stale keys to prevent unbounded growth
            if len(self._throttle_state) > self._THROTTLE_MAX_KEYS and (now - self._throttle_last_purge) > 60.0:
                self._throttle_last_purge = now
                stale = [k for k, v in self._throttle_state.items() if (now - v) > self._THROTTLE_PURGE_AGE]
                for k in stale:
                    del self._throttle_state[k]
        self.logger.log(level, msg)

    def disable_logging(self):
        logging.disable(logging.CRITICAL)


if __name__ == "__main__":
    log = Logger(name="MyLogger", level=logging.DEBUG, enable_file_logging=False)
    log.debug("This is a debug message")
    log.info("This is an info message")
    log.warning("This is a warning message")
    log.error("This is an error message")
    log.critical("This is a critical message")
    log.success("This is a success message")

    log.set_level(logging.WARNING)
    log.debug("This debug message should not appear")
    log.info("This info message should not appear")
    log.warning("This warning message should appear")

    log.disable_logging()
    log.error("This error message should not appear")
