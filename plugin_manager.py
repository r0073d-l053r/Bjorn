"""plugin_manager.py - Plugin discovery, lifecycle, hook dispatch, and config management."""

import gc
import importlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import weakref
import zipfile
from typing import Any, Dict, List, Optional, Set

from bjorn_plugin import BjornPlugin
from logger import Logger

logger = Logger(name="plugin_manager", level=logging.DEBUG)

# Supported hooks (must match BjornPlugin method names)
KNOWN_HOOKS = frozenset({
    "on_host_discovered",
    "on_credential_found",
    "on_vulnerability_found",
    "on_action_complete",
    "on_scan_complete",
})

# Required fields in plugin.json
_REQUIRED_MANIFEST_FIELDS = {"id", "name", "version", "type", "main", "class"}

# Valid plugin types
_VALID_TYPES = {"action", "notifier", "enricher", "exporter", "ui_widget"}

# Max loaded plugins (RAM safety on Pi Zero 2)
_MAX_PLUGINS = 30

# Max error entries to retain (prevents unbounded growth)
_MAX_ERRORS = 50


class PluginManager:
    """Manages plugin discovery, lifecycle, hook dispatch, and configuration."""

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.plugins_dir = getattr(shared_data, 'plugins_dir', None)
        if not self.plugins_dir:
            self.plugins_dir = os.path.join(shared_data.current_dir, 'plugins')
        os.makedirs(self.plugins_dir, exist_ok=True)

        self._instances: Dict[str, BjornPlugin] = {}  # plugin_id -> instance
        self._meta: Dict[str, dict] = {}               # plugin_id -> parsed plugin.json
        self._hook_map: Dict[str, Set[str]] = {h: set() for h in KNOWN_HOOKS}  # sets, not lists
        self._lock = threading.Lock()
        self._errors: Dict[str, str] = {}               # plugin_id -> error message (bounded)

        # Track original DB methods for clean unhook
        self._original_db_methods: Dict[str, Any] = {}

    # ── Discovery ────────────────────────────────────────────────────

    def discover_plugins(self) -> List[dict]:
        """Scan plugins_dir, parse each plugin.json, return list of valid metadata dicts."""
        results = []
        if not os.path.isdir(self.plugins_dir):
            return results

        for entry in os.listdir(self.plugins_dir):
            plugin_dir = os.path.join(self.plugins_dir, entry)
            if not os.path.isdir(plugin_dir):
                continue

            manifest_path = os.path.join(plugin_dir, "plugin.json")
            if not os.path.isfile(manifest_path):
                logger.debug(f"Skipping {entry}: no plugin.json")
                continue

            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception as e:
                logger.warning(f"Invalid plugin.json in {entry}: {e}")
                continue

            # Validate required fields
            missing = _REQUIRED_MANIFEST_FIELDS - set(meta.keys())
            if missing:
                logger.warning(f"Plugin {entry} missing fields: {missing}")
                continue

            if meta["type"] not in _VALID_TYPES:
                logger.warning(f"Plugin {entry} has invalid type: {meta['type']}")
                continue

            # Ensure main file exists
            main_path = os.path.join(plugin_dir, meta["main"])
            if not os.path.isfile(main_path):
                logger.warning(f"Plugin {entry}: main file {meta['main']} not found")
                continue

            meta["_dir"] = plugin_dir
            meta["_main_path"] = main_path
            results.append(meta)

        logger.info(f"Discovered {len(results)} plugin(s)")
        return results

    # ── Loading ──────────────────────────────────────────────────────

    def load_plugin(self, plugin_id: str) -> bool:
        """Load a single plugin: import module, instantiate class, call setup()."""
        # Quick check under lock (no I/O here)
        with self._lock:
            if plugin_id in self._instances:
                logger.debug(f"Plugin {plugin_id} already loaded")
                return True

            if len(self._instances) >= _MAX_PLUGINS:
                logger.warning(f"Max plugins reached ({_MAX_PLUGINS}), cannot load {plugin_id}")
                return False

        # Read manifest OUTSIDE the lock (I/O)
        plugin_dir = os.path.join(self.plugins_dir, plugin_id)
        manifest_path = os.path.join(plugin_dir, "plugin.json")
        if not os.path.isfile(manifest_path):
            self._set_error(plugin_id, "plugin.json not found")
            return False

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as e:
            self._set_error(plugin_id, f"Invalid plugin.json: {e}")
            return False

        meta["_dir"] = plugin_dir
        meta["_main_path"] = os.path.join(plugin_dir, meta.get("main", ""))

        # Load config from DB (merged with schema defaults)
        config = self._get_merged_config(plugin_id, meta)

        # Import module from file (OUTSIDE the lock — slow I/O)
        mod_name = f"bjorn_plugin_{plugin_id}"
        try:
            main_path = meta["_main_path"]
            spec = importlib.util.spec_from_file_location(mod_name, main_path)
            if spec is None or spec.loader is None:
                self._set_error(plugin_id, f"Cannot create module spec for {main_path}")
                return False

            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            cls_name = meta.get("class", "")
            cls = getattr(mod, cls_name, None)
            if cls is None:
                self._set_error(plugin_id, f"Class {cls_name} not found in {meta['main']}")
                return False

            # Instantiate
            instance = cls(self.shared_data, meta, config)

            # Call setup
            instance.setup()

        except Exception as e:
            self._set_error(plugin_id, f"Load failed: {e}")
            logger.error(f"Failed to load plugin {plugin_id}: {e}")
            # Clean up module from sys.modules on failure
            sys.modules.pop(mod_name, None)
            return False

        # Register UNDER the lock (fast, no I/O)
        with self._lock:
            self._instances[plugin_id] = instance
            self._meta[plugin_id] = meta
            self._errors.pop(plugin_id, None)

            # Register hooks (set — no duplicates possible)
            declared_hooks = meta.get("hooks", [])
            for hook_name in declared_hooks:
                if hook_name in KNOWN_HOOKS:
                    self._hook_map[hook_name].add(plugin_id)

        # Persist hooks to DB (outside lock)
        try:
            valid_hooks = [h for h in declared_hooks if h in KNOWN_HOOKS]
            self.shared_data.db.set_plugin_hooks(plugin_id, valid_hooks)
        except Exception as e:
            logger.debug(f"Could not persist hooks for {plugin_id}: {e}")

        logger.info(f"Plugin loaded: {plugin_id} (type={meta.get('type')})")
        return True

    def unload_plugin(self, plugin_id: str) -> None:
        """Call teardown() and remove from instances/hooks. Cleans up module from sys.modules."""
        with self._lock:
            instance = self._instances.pop(plugin_id, None)
            self._meta.pop(plugin_id, None)

            # Remove from all hook sets
            for hook_set in self._hook_map.values():
                hook_set.discard(plugin_id)

        if instance:
            try:
                instance.teardown()
            except Exception as e:
                logger.warning(f"Teardown error for {plugin_id}: {e}")
            # Break references to help GC
            instance.shared_data = None
            instance.db = None
            instance.config = None
            instance.meta = None

        # Remove module from sys.modules to free bytecode memory
        mod_name = f"bjorn_plugin_{plugin_id}"
        sys.modules.pop(mod_name, None)

        logger.info(f"Plugin unloaded: {plugin_id}")

    def load_all(self) -> None:
        """Load all enabled plugins. Called at startup."""
        discovered = self.discover_plugins()

        for meta in discovered:
            plugin_id = meta["id"]

            # Ensure DB record exists with defaults
            db_record = self.shared_data.db.get_plugin_config(plugin_id)
            if db_record is None:
                # First time: insert with schema defaults
                default_config = self._extract_defaults(meta)
                self.shared_data.db.upsert_plugin(plugin_id, 1, default_config, meta)
                db_record = self.shared_data.db.get_plugin_config(plugin_id)

            # Only load if enabled
            if db_record and db_record.get("enabled", 1):
                self.load_plugin(plugin_id)
            else:
                logger.debug(f"Plugin {plugin_id} is disabled, skipping load")

    def stop_all(self) -> None:
        """Teardown all loaded plugins. Called at shutdown."""
        with self._lock:
            plugin_ids = list(self._instances.keys())

        for pid in plugin_ids:
            self.unload_plugin(pid)

        # Restore original DB methods (remove monkey-patches)
        self._uninstall_db_hooks()

        # Clear all references
        self._errors.clear()
        self._meta.clear()

        gc.collect()
        logger.info("All plugins stopped")

    # ── Hook Dispatch ────────────────────────────────────────────────

    def dispatch(self, hook_name: str, **kwargs) -> None:
        """
        Fire a hook to all subscribed plugins.
        Synchronous, catches exceptions per-plugin to isolate failures.
        """
        if hook_name not in KNOWN_HOOKS:
            return

        # Copy subscriber set under lock (fast), then call outside lock
        with self._lock:
            subscribers = list(self._hook_map.get(hook_name, set()))

        for plugin_id in subscribers:
            instance = self._instances.get(plugin_id)
            if instance is None:
                continue
            try:
                method = getattr(instance, hook_name, None)
                if method:
                    method(**kwargs)
            except Exception as e:
                logger.error(f"Hook {hook_name} failed in plugin {plugin_id}: {e}")

    # ── DB Hook Wrappers ─────────────────────────────────────────────

    def install_db_hooks(self) -> None:
        """
        Monkey-patch DB facade methods to dispatch hooks on data mutations.
        Uses weakref to avoid reference cycles between PluginManager and DB.
        """
        db = self.shared_data.db
        manager_ref = weakref.ref(self)

        # Wrap insert_cred
        if hasattr(db, 'insert_cred'):
            original = db.insert_cred
            self._original_db_methods['insert_cred'] = original

            def hooked_insert_cred(*args, **kwargs):
                result = original(*args, **kwargs)
                try:
                    mgr = manager_ref()
                    if mgr:
                        mgr.dispatch("on_credential_found", cred={
                            "service": kwargs.get("service", args[0] if args else ""),
                            "mac": kwargs.get("mac", args[1] if len(args) > 1 else ""),
                            "ip": kwargs.get("ip", args[2] if len(args) > 2 else ""),
                            "user": kwargs.get("user", args[4] if len(args) > 4 else ""),
                            "port": kwargs.get("port", args[6] if len(args) > 6 else ""),
                        })
                except Exception as e:
                    logger.debug(f"Hook dispatch error (on_credential_found): {e}")
                return result

            db.insert_cred = hooked_insert_cred

        # Wrap insert_vulnerability if it exists
        if hasattr(db, 'insert_vulnerability'):
            original = db.insert_vulnerability
            self._original_db_methods['insert_vulnerability'] = original

            def hooked_insert_vuln(*args, **kwargs):
                result = original(*args, **kwargs)
                try:
                    mgr = manager_ref()
                    if mgr:
                        mgr.dispatch("on_vulnerability_found", vuln=kwargs or {})
                except Exception as e:
                    logger.debug(f"Hook dispatch error (on_vulnerability_found): {e}")
                return result

            db.insert_vulnerability = hooked_insert_vuln

        logger.debug("DB hook wrappers installed (weakref)")

    def _uninstall_db_hooks(self) -> None:
        """Restore original DB methods, removing monkey-patches."""
        db = getattr(self.shared_data, 'db', None)
        if not db:
            return
        for method_name, original in self._original_db_methods.items():
            try:
                setattr(db, method_name, original)
            except Exception:
                pass
        self._original_db_methods.clear()
        logger.debug("DB hook wrappers removed")

    # ── Action Registration ──────────────────────────────────────────

    def get_action_registrations(self) -> List[dict]:
        """
        Return action-metadata dicts for plugins of type='action'.
        These get merged into sync_actions_to_database() alongside regular actions.
        """
        registrations = []

        for meta in self._meta.values():
            if meta.get("type") != "action":
                continue

            action_meta = meta.get("action", {})
            plugin_id = meta["id"]

            reg = {
                "b_class": meta.get("class", plugin_id),
                "b_module": f"plugins/{plugin_id}",
                "b_action": "plugin",
                "b_name": meta.get("name", plugin_id),
                "b_description": meta.get("description", ""),
                "b_author": meta.get("author", ""),
                "b_version": meta.get("version", "0.0.0"),
                "b_icon": meta.get("icon", ""),
                "b_enabled": 1,
                "b_port": action_meta.get("port"),
                "b_service": json.dumps(action_meta.get("service", [])),
                "b_trigger": action_meta.get("trigger"),
                "b_priority": action_meta.get("priority", 50),
                "b_cooldown": action_meta.get("cooldown", 0),
                "b_timeout": action_meta.get("timeout", 300),
                "b_max_retries": action_meta.get("max_retries", 1),
                "b_stealth_level": action_meta.get("stealth_level", 5),
                "b_risk_level": action_meta.get("risk_level", "medium"),
                "b_tags": json.dumps(meta.get("tags", [])),
                "b_args": json.dumps(meta.get("config_schema", {})),
            }
            registrations.append(reg)

        return registrations

    # ── Config Management ────────────────────────────────────────────

    def get_config(self, plugin_id: str) -> dict:
        """Return merged config: schema defaults + DB overrides."""
        return self._get_merged_config(plugin_id, self._meta.get(plugin_id))

    def save_config(self, plugin_id: str, values: dict) -> None:
        """Validate against schema, persist to DB, hot-reload into instance."""
        meta = self._meta.get(plugin_id)
        if not meta:
            raise ValueError(f"Plugin {plugin_id} not found")

        schema = meta.get("config_schema", {})
        validated = {}

        for key, spec in schema.items():
            if key in values:
                validated[key] = self._coerce_value(values[key], spec)
            else:
                validated[key] = spec.get("default")

        self.shared_data.db.save_plugin_config(plugin_id, validated)

        # Hot-reload config into running instance
        instance = self._instances.get(plugin_id)
        if instance:
            instance.config = validated

        logger.info(f"Config saved for plugin {plugin_id}")

    # ── Install / Uninstall ──────────────────────────────────────────

    def install_from_zip(self, zip_bytes: bytes) -> dict:
        """
        Extract zip to plugins/<id>/, validate plugin.json, register in DB.
        Returns {"status": "ok", "plugin_id": ...} or {"status": "error", ...}.
        """
        tmp_dir = None
        try:
            # Extract to temp dir
            tmp_dir = tempfile.mkdtemp(prefix="bjorn_plugin_")
            zip_path = os.path.join(tmp_dir, "plugin.zip")
            with open(zip_path, "wb") as f:
                f.write(zip_bytes)

            with zipfile.ZipFile(zip_path, "r") as zf:
                # Security: check for path traversal in zip
                for name in zf.namelist():
                    if name.startswith("/") or ".." in name:
                        return {"status": "error", "message": f"Unsafe path in zip: {name}"}
                zf.extractall(tmp_dir)

            # Find plugin.json (may be in root or in a subdirectory)
            manifest_path = None
            for walk_root, dirs, files in os.walk(tmp_dir):
                if "plugin.json" in files:
                    manifest_path = os.path.join(walk_root, "plugin.json")
                    break

            if not manifest_path:
                return {"status": "error", "message": "No plugin.json found in archive"}

            with open(manifest_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            missing = _REQUIRED_MANIFEST_FIELDS - set(meta.keys())
            if missing:
                return {"status": "error", "message": f"Missing manifest fields: {missing}"}

            plugin_id = meta["id"]
            plugin_source_dir = os.path.dirname(manifest_path)
            target_dir = os.path.join(self.plugins_dir, plugin_id)

            # Check if already installed
            if os.path.isdir(target_dir):
                # Allow upgrade: remove old version
                self.unload_plugin(plugin_id)
                shutil.rmtree(target_dir)

            # Move to plugins dir
            shutil.copytree(plugin_source_dir, target_dir)

            # Register in DB
            default_config = self._extract_defaults(meta)
            self.shared_data.db.upsert_plugin(plugin_id, 0, default_config, meta)

            # Check dependencies
            dep_check = self.check_dependencies(meta)
            if not dep_check["ok"]:
                logger.warning(f"Plugin {plugin_id} has missing deps: {dep_check['missing']}")

            logger.info(f"Plugin installed: {plugin_id}")
            return {
                "status": "ok",
                "plugin_id": plugin_id,
                "name": meta.get("name", plugin_id),
                "dependencies": dep_check,
            }

        except Exception as e:
            logger.error(f"Plugin install failed: {e}")
            return {"status": "error", "message": "Plugin installation failed"}
        finally:
            # Always clean up temp dir
            if tmp_dir:
                try:
                    shutil.rmtree(tmp_dir)
                except Exception as cleanup_err:
                    logger.warning(f"Temp dir cleanup failed ({tmp_dir}): {cleanup_err}")

    def uninstall(self, plugin_id: str) -> dict:
        """Unload plugin, remove DB entries, delete directory."""
        try:
            self.unload_plugin(plugin_id)

            # Remove from DB
            self.shared_data.db.delete_plugin(plugin_id)

            # Remove action entry if it was an action-type plugin
            try:
                self.shared_data.db.delete_action(f"plugins/{plugin_id}")
            except Exception:
                pass

            # Delete directory
            target_dir = os.path.join(self.plugins_dir, plugin_id)
            if os.path.isdir(target_dir):
                shutil.rmtree(target_dir)

            # Clear any cached error
            self._errors.pop(plugin_id, None)

            logger.info(f"Plugin uninstalled: {plugin_id}")
            return {"status": "ok", "plugin_id": plugin_id}

        except Exception as e:
            logger.error(f"Plugin uninstall failed for {plugin_id}: {e}")
            return {"status": "error", "message": "Uninstall failed"}

    def toggle_plugin(self, plugin_id: str, enabled: bool) -> None:
        """Enable/disable a plugin. Update DB, load/unload accordingly."""
        self.shared_data.db.set_plugin_enabled(plugin_id, enabled)

        if enabled:
            self.load_plugin(plugin_id)
        else:
            self.unload_plugin(plugin_id)

        logger.info(f"Plugin {plugin_id} {'enabled' if enabled else 'disabled'}")

    # ── Dependency Checking ──────────────────────────────────────────

    def check_dependencies(self, meta: dict) -> dict:
        """Check pip and system dependencies. Returns {"ok": bool, "missing": [...]}."""
        requires = meta.get("requires", {})
        missing = []

        # Check pip packages
        for pkg in requires.get("pip", []):
            pkg_name = pkg.split(">=")[0].split("==")[0].split("<")[0].strip()
            if importlib.util.find_spec(pkg_name) is None:
                missing.append(f"pip:{pkg}")

        # Check system commands
        for cmd in requires.get("system", []):
            if shutil.which(cmd) is None:
                missing.append(f"system:{cmd}")

        return {"ok": len(missing) == 0, "missing": missing}

    # ── Status ───────────────────────────────────────────────────────

    def get_plugin_status(self, plugin_id: str) -> str:
        """Return status string: 'loaded', 'disabled', 'error', 'not_installed'."""
        if plugin_id in self._instances:
            return "loaded"
        if plugin_id in self._errors:
            return "error"
        db_rec = self.shared_data.db.get_plugin_config(plugin_id)
        if db_rec:
            return "disabled" if not db_rec.get("enabled", 1) else "error"
        return "not_installed"

    def get_all_status(self) -> List[dict]:
        """Return status for all known plugins (discovered + DB)."""
        result = []
        db_plugins = {p["plugin_id"]: p for p in self.shared_data.db.list_plugins_db()}

        # Include discovered plugins
        discovered = self.discover_plugins()
        seen = set()

        for meta in discovered:
            pid = meta["id"]
            seen.add(pid)
            db_rec = db_plugins.get(pid, {})
            result.append({
                "id": pid,
                "name": meta.get("name", pid),
                "description": meta.get("description", ""),
                "version": meta.get("version", "?"),
                "author": meta.get("author", ""),
                "type": meta.get("type", "unknown"),
                "enabled": bool(db_rec.get("enabled", 1)),
                "status": self.get_plugin_status(pid),
                "hooks": meta.get("hooks", []),
                "has_config": bool(meta.get("config_schema")),
                "error": self._errors.get(pid),
                "dependencies": self.check_dependencies(meta),
            })

        # Include DB-only entries (installed but directory removed?)
        for pid, db_rec in db_plugins.items():
            if pid not in seen:
                meta = db_rec.get("meta", {})
                result.append({
                    "id": pid,
                    "name": meta.get("name", pid),
                    "description": meta.get("description", ""),
                    "version": meta.get("version", "?"),
                    "author": meta.get("author", ""),
                    "type": meta.get("type", "unknown"),
                    "enabled": bool(db_rec.get("enabled", 0)),
                    "status": "missing",
                    "hooks": [],
                    "has_config": False,
                    "error": "Plugin directory not found",
                })

        return result

    # ── Private Helpers ──────────────────────────────────────────────

    def _set_error(self, plugin_id: str, message: str) -> None:
        """Set an error for a plugin, with bounded error dict size."""
        if len(self._errors) >= _MAX_ERRORS:
            # Evict oldest entry (arbitrary, just keep bounded)
            try:
                oldest_key = next(iter(self._errors))
                del self._errors[oldest_key]
            except StopIteration:
                pass
        self._errors[plugin_id] = message

    def _get_merged_config(self, plugin_id: str, meta: Optional[dict]) -> dict:
        """Merge schema defaults with DB-stored user config."""
        schema = (meta or {}).get("config_schema", {})
        defaults = self._extract_defaults(meta or {})

        db_rec = self.shared_data.db.get_plugin_config(plugin_id)
        if db_rec and db_rec.get("config"):
            merged = dict(defaults)
            merged.update(db_rec["config"])
            return merged

        return defaults

    @staticmethod
    def _extract_defaults(meta: dict) -> dict:
        """Extract default values from config_schema."""
        schema = meta.get("config_schema", {})
        return {k: spec.get("default") for k, spec in schema.items()}

    @staticmethod
    def _coerce_value(value: Any, spec: dict) -> Any:
        """Coerce a config value to the type declared in the schema."""
        vtype = spec.get("type", "string")
        try:
            if vtype == "int" or vtype == "number":
                return int(value)
            elif vtype == "float":
                return float(value)
            elif vtype in ("bool", "boolean"):
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes", "on")
                return bool(value)
            elif vtype == "select":
                choices = spec.get("choices", [])
                return value if value in choices else spec.get("default")
            elif vtype == "multiselect":
                if isinstance(value, list):
                    return value
                return spec.get("default", [])
            else:
                return str(value) if value is not None else spec.get("default", "")
        except (ValueError, TypeError):
            return spec.get("default")
