# utils.py (pattern lazy __getattr__)
import importlib
import threading

class WebUtils:
    _lock = threading.RLock()
    _registry = {
        "c2": ("web_utils.c2_utils", "C2Utils"),
        "index_utils": ("web_utils.index_utils", "IndexUtils"),
        "webenum_utils": ("web_utils.webenum_utils", "WebEnumUtils"),
        "network_utils": ("web_utils.network_utils", "NetworkUtils"),
        "file_utils": ("web_utils.file_utils", "FileUtils"),
        "backup_utils": ("web_utils.backup_utils", "BackupUtils"),
        "system_utils": ("web_utils.system_utils", "SystemUtils"),
        "bluetooth_utils": ("web_utils.bluetooth_utils", "BluetoothUtils"),
        "script_utils": ("web_utils.script_utils", "ScriptUtils"),
        "vuln_utils": ("web_utils.vuln_utils", "VulnUtils"),
        "netkb_utils": ("web_utils.netkb_utils", "NetKBUtils"),
        "orchestrator_utils": ("web_utils.orchestrator_utils", "OrchestratorUtils"),
        "studio_utils": ("web_utils.studio_utils", "StudioUtils"),
        "db_utils": ("web_utils.db_utils", "DBUtils"),
        "action_utils": ("web_utils.action_utils", "ActionUtils"),
        "rl": ("web_utils.rl_utils", "RLUtils"),
        "debug_utils": ("web_utils.debug_utils", "DebugUtils"),
        "sentinel": ("web_utils.sentinel_utils", "SentinelUtils"),
        "bifrost": ("web_utils.bifrost_utils", "BifrostUtils"),
        "loki": ("web_utils.loki_utils", "LokiUtils"),
        "llm_utils": ("web_utils.llm_utils", "LLMUtils"),
    }


    def __init__(self, shared_data):
        self.shared_data = shared_data

    def _make(self, name):
        module_path, class_name = self._registry[name]
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        # Try common constructor signatures
        try:
            return cls(self.shared_data)
        except TypeError:
            return cls()

    def __getattr__(self, name):
        if name in self._registry:
            with self._lock:
                if name not in self.__dict__:
                    self.__dict__[name] = self._make(name)
            return self.__dict__[name]
        raise AttributeError(f"{type(self).__name__} has no attribute {name}")