"""
Bifrost — Plugin system.
Ported from pwnagotchi/plugins/__init__.py with ThreadPoolExecutor.
Compatible with existing pwnagotchi plugin files.
"""
import os
import glob
import threading
import importlib
import importlib.util
import logging
import concurrent.futures

from logger import Logger

logger = Logger(name="bifrost.plugins", level=logging.DEBUG)

default_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "plugins")
loaded = {}
database = {}
locks = {}

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="BifrostPlugin"
)


class Plugin:
    """Base class for Bifrost/Pwnagotchi plugins.

    Subclasses are auto-registered via __init_subclass__.
    """
    __author__ = 'unknown'
    __version__ = '0.0.0'
    __license__ = 'GPL3'
    __description__ = ''
    __name__ = ''
    __help__ = ''
    __dependencies__ = []
    __defaults__ = {}

    def __init__(self):
        self.options = {}

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        global loaded, locks

        plugin_name = cls.__module__.split('.')[0]
        plugin_instance = cls()
        logger.debug("loaded plugin %s as %s", plugin_name, plugin_instance)
        loaded[plugin_name] = plugin_instance

        for attr_name in dir(plugin_instance):
            if attr_name.startswith('on_'):
                cb = getattr(plugin_instance, attr_name, None)
                if cb is not None and callable(cb):
                    locks["%s::%s" % (plugin_name, attr_name)] = threading.Lock()


def toggle_plugin(name, enable=True):
    """Enable or disable a plugin at runtime. Returns True if state changed."""
    global loaded, database

    if not enable and name in loaded:
        try:
            if hasattr(loaded[name], 'on_unload'):
                loaded[name].on_unload()
        except Exception as e:
            logger.warning("Error unloading plugin %s: %s", name, e)
        del loaded[name]
        return True

    if enable and name in database and name not in loaded:
        try:
            load_from_file(database[name])
            if name in loaded:
                one(name, 'loaded')
            return True
        except Exception as e:
            logger.warning("Error loading plugin %s: %s", name, e)

    return False


def on(event_name, *args, **kwargs):
    """Dispatch event to ALL loaded plugins."""
    for plugin_name in list(loaded.keys()):
        one(plugin_name, event_name, *args, **kwargs)


def _locked_cb(lock_name, cb, *args, **kwargs):
    """Execute callback under its per-plugin lock."""
    global locks
    if lock_name not in locks:
        locks[lock_name] = threading.Lock()
    with locks[lock_name]:
        cb(*args, **kwargs)


def one(plugin_name, event_name, *args, **kwargs):
    """Dispatch event to a single plugin (thread-safe)."""
    global loaded
    if plugin_name in loaded:
        plugin = loaded[plugin_name]
        cb_name = 'on_%s' % event_name
        callback = getattr(plugin, cb_name, None)
        if callback is not None and callable(callback):
            try:
                lock_name = "%s::%s" % (plugin_name, cb_name)
                _executor.submit(_locked_cb, lock_name, callback, *args, **kwargs)
            except Exception as e:
                logger.error("error running %s.%s: %s", plugin_name, cb_name, e)


def load_from_file(filename):
    """Load a single plugin file."""
    logger.debug("loading %s", filename)
    plugin_name = os.path.basename(filename.replace(".py", ""))
    spec = importlib.util.spec_from_file_location(plugin_name, filename)
    instance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(instance)
    return plugin_name, instance


def load_from_path(path, enabled=()):
    """Scan a directory for plugins, load enabled ones."""
    global loaded, database
    if not path or not os.path.isdir(path):
        return loaded

    logger.debug("loading plugins from %s — enabled: %s", path, enabled)
    for filename in glob.glob(os.path.join(path, "*.py")):
        plugin_name = os.path.basename(filename.replace(".py", ""))
        database[plugin_name] = filename
        if plugin_name in enabled:
            try:
                load_from_file(filename)
            except Exception as e:
                logger.warning("error loading %s: %s", filename, e)

    return loaded


def load(config):
    """Load plugins from default + custom paths based on config."""
    plugins_cfg = config.get('bifrost_plugins', {})
    enabled = [
        name for name, opts in plugins_cfg.items()
        if isinstance(opts, dict) and opts.get('enabled', False)
    ]

    # Load from default path (bifrost/plugins/)
    if os.path.isdir(default_path):
        load_from_path(default_path, enabled=enabled)

    # Load from custom path
    custom_path = config.get('bifrost_plugins_path', '')
    if custom_path and os.path.isdir(custom_path):
        load_from_path(custom_path, enabled=enabled)

    # Propagate options
    for name, plugin in loaded.items():
        if name in plugins_cfg:
            plugin.options = plugins_cfg[name]

    on('loaded')
    on('config_changed', config)


def get_loaded_info():
    """Return list of loaded plugin info dicts for web API."""
    result = []
    for name, plugin in loaded.items():
        result.append({
            'name': name,
            'enabled': True,
            'author': getattr(plugin, '__author__', 'unknown'),
            'version': getattr(plugin, '__version__', '0.0.0'),
            'description': getattr(plugin, '__description__', ''),
        })
    # Also include known-but-not-loaded plugins
    for name, path in database.items():
        if name not in loaded:
            result.append({
                'name': name,
                'enabled': False,
                'author': '',
                'version': '',
                'description': '',
            })
    return result


def shutdown():
    """Clean shutdown of plugin system."""
    _executor.shutdown(wait=False)
