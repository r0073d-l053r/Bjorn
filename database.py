# database.py
# Main database facade - delegates to specialized modules in db_utils/
# Maintains backward compatibility with existing code

import os
from typing import Any, Dict, Iterable, List, Optional, Tuple
from contextlib import contextmanager
from threading import RLock
import sqlite3
import logging

from logger import Logger
from db_utils.base import DatabaseBase
from db_utils.config import ConfigOps
from db_utils.hosts import HostOps
from db_utils.actions import ActionOps
from db_utils.queue import QueueOps
from db_utils.vulnerabilities import VulnerabilityOps
from db_utils.software import SoftwareOps
from db_utils.credentials import CredentialOps
from db_utils.services import ServiceOps
from db_utils.scripts import ScriptOps
from db_utils.stats import StatsOps
from db_utils.backups import BackupOps
from db_utils.comments import CommentOps
from db_utils.agents import AgentOps
from db_utils.studio import StudioOps
from db_utils.webenum import WebEnumOps
from db_utils.sentinel import SentinelOps
from db_utils.bifrost import BifrostOps
from db_utils.loki import LokiOps

logger = Logger(name="database.py", level=logging.DEBUG)

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bjorn.db")


class BjornDatabase:
    """
    Main database facade that delegates operations to specialized modules.
    All existing method calls remain unchanged - they're automatically forwarded.
    """
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _DEFAULT_DB
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        # Initialize base connection manager
        self._base = DatabaseBase(self.db_path)
        
        # Initialize all operational modules (they share the base connection)
        self._config = ConfigOps(self._base)
        self._hosts = HostOps(self._base)
        self._actions = ActionOps(self._base)
        self._queue = QueueOps(self._base)
        self._vulnerabilities = VulnerabilityOps(self._base)
        self._software = SoftwareOps(self._base)
        self._credentials = CredentialOps(self._base)
        self._services = ServiceOps(self._base)
        self._scripts = ScriptOps(self._base)
        self._stats = StatsOps(self._base)
        self._backups = BackupOps(self._base)
        self._comments = CommentOps(self._base)
        self._agents = AgentOps(self._base)
        self._studio = StudioOps(self._base)
        self._webenum = WebEnumOps(self._base)
        self._sentinel = SentinelOps(self._base)
        self._bifrost = BifrostOps(self._base)
        self._loki = LokiOps(self._base)

        # Ensure schema is created
        self.ensure_schema()
        
        logger.info(f"BjornDatabase initialized: {self.db_path}")
    
    # =========================================================================
    # CORE PRIMITIVES - Delegated to base
    # =========================================================================
    
    @property
    def _conn(self):
        """Access to underlying connection"""
        return self._base._conn
    
    @property
    def _lock(self):
        """Access to thread lock"""
        return self._base._lock
    
    @property
    def _cache_ttl(self):
        return self._base._cache_ttl
    
    @property
    def _stats_cache(self):
        return self._base._stats_cache
    
    @_stats_cache.setter
    def _stats_cache(self, value):
        self._base._stats_cache = value
    
    def _cursor(self):
        return self._base._cursor()
    
    def transaction(self, immediate: bool = True):
        return self._base.transaction(immediate)
    
    def execute(self, sql: str, params: Iterable[Any] = (), many: bool = False) -> int:
        return self._base.execute(sql, params, many)
    
    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> int:
        return self._base.executemany(sql, seq_of_params)
    
    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        return self._base.query(sql, params)
    
    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        return self._base.query_one(sql, params)
    
    def invalidate_stats_cache(self):
        return self._base.invalidate_stats_cache()
    
    # =========================================================================
    # SCHEMA INITIALIZATION
    # =========================================================================
    
    def ensure_schema(self) -> None:
        """Create all database tables if missing"""
        logger.info("Ensuring database schema...")
        
        # Each module creates its own tables
        self._config.create_tables()
        self._actions.create_tables()
        self._hosts.create_tables()
        self._services.create_tables()
        self._queue.create_tables()
        self._stats.create_tables()
        self._vulnerabilities.create_tables()
        self._software.create_tables()
        self._credentials.create_tables()
        self._scripts.create_tables()
        self._backups.create_tables()
        self._comments.create_tables()
        self._agents.create_tables()
        self._studio.create_tables()
        self._webenum.create_tables()
        self._sentinel.create_tables()
        self._bifrost.create_tables()
        self._loki.create_tables()

        # Initialize stats singleton
        self._stats.ensure_stats_initialized()
        
        logger.info("Database schema ready")
    
    # =========================================================================
    # METHOD DELEGATION - All existing methods forwarded automatically
    # =========================================================================
    
    # Config operations
    def get_config(self) -> Dict[str, Any]:
        return self._config.get_config()
    
    def save_config(self, config: Dict[str, Any]) -> None:
        return self._config.save_config(config)
    
    # Host operations
    def get_host_by_mac(self, mac_address: str) -> Optional[Dict[str, Any]]:
        """Get a single host by MAC address"""
        try:
            results = self.query("SELECT * FROM hosts WHERE mac_address=? LIMIT 1", (mac_address,))
            return results[0] if results else None
        except Exception as e:
            logger.error(f"Error getting host by MAC {mac_address}: {e}")
            return None

    def get_all_hosts(self) -> List[Dict[str, Any]]:
        return self._hosts.get_all_hosts()
    
    def update_host(self, mac_address: str, ips: Optional[str] = None,
                    hostnames: Optional[str] = None, alive: Optional[int] = None,
                    ports: Optional[str] = None, vendor: Optional[str] = None,
                    essid: Optional[str] = None):
        return self._hosts.update_host(mac_address, ips, hostnames, alive, ports, vendor, essid)
    
    def merge_ip_stub_into_real(self, ip: str, real_mac: str, 
                                hostname: Optional[str] = None, essid_hint: Optional[str] = None):
        return self._hosts.merge_ip_stub_into_real(ip, real_mac, hostname, essid_hint)
    
    def update_hostname(self, mac_address: str, new_hostname: str):
        return self._hosts.update_hostname(mac_address, new_hostname)
    
    def get_current_hostname(self, mac_address: str) -> Optional[str]:
        return self._hosts.get_current_hostname(mac_address)
    
    def record_hostname_seen(self, mac_address: str, hostname: str):
        return self._hosts.record_hostname_seen(mac_address, hostname)
    
    def list_hostname_history(self, mac_address: str) -> List[Dict[str, Any]]:
        return self._hosts.list_hostname_history(mac_address)
    
    def update_ips_current(self, mac_address: str, current_ips: Iterable[str], cap_prev: int = 200):
        return self._hosts.update_ips_current(mac_address, current_ips, cap_prev)
    
    def update_ports_current(self, mac_address: str, current_ports: Iterable[int], cap_prev: int = 500):
        return self._hosts.update_ports_current(mac_address, current_ports, cap_prev)
    
    def update_essid_current(self, mac_address: str, new_essid: Optional[str], cap_prev: int = 50):
        return self._hosts.update_essid_current(mac_address, new_essid, cap_prev)
    
    # Action operations
    def sync_actions(self, actions):
        return self._actions.sync_actions(actions)
    
    def list_actions(self):
        return self._actions.list_actions()
    
    def list_studio_actions(self):
        return self._actions.list_studio_actions()
    
    def get_action_by_class(self, b_class: str) -> dict | None:
        return self._actions.get_action_by_class(b_class)
    
    def delete_action(self, b_class: str) -> None:
        return self._actions.delete_action(b_class)
    
    def upsert_simple_action(self, *, b_class: str, b_module: str, **kw) -> None:
        return self._actions.upsert_simple_action(b_class=b_class, b_module=b_module, **kw)
    
    def list_action_cards(self) -> list[dict]:
        return self._actions.list_action_cards()
    
    def get_action_definition(self, b_class: str) -> Optional[Dict[str, Any]]:
        return self._actions.get_action_definition(b_class)
    
    # Queue operations
    def get_next_queued_action(self) -> Optional[Dict[str, Any]]:
        return self._queue.get_next_queued_action()
    
    def update_queue_status(self, queue_id: int, status: str, error_msg: str = None, result: str = None):
        return self._queue.update_queue_status(queue_id, status, error_msg, result)
    
    def promote_due_scheduled_to_pending(self) -> int:
        return self._queue.promote_due_scheduled_to_pending()
    
    def ensure_scheduled_occurrence(self, action_name: str, next_run_at: str,
                                   mac: Optional[str] = "", ip: Optional[str] = "", **kwargs) -> bool:
        return self._queue.ensure_scheduled_occurrence(action_name, next_run_at, mac, ip, **kwargs)
    
    def queue_action(self, action_name: str, mac: str, ip: str, port: int = None,
                    priority: int = 50, trigger: str = None, metadata: Dict = None) -> None:
        return self._queue.queue_action(action_name, mac, ip, port, priority, trigger, metadata)
    
    def queue_action_at(self, action_name: str, mac: Optional[str] = "", ip: Optional[str] = "", **kwargs) -> None:
        return self._queue.queue_action_at(action_name, mac, ip, **kwargs)
    
    def list_action_queue(self, statuses: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        return self._queue.list_action_queue(statuses)
    
    def get_upcoming_actions_summary(self) -> List[Dict[str, Any]]:
        return self._queue.get_upcoming_actions_summary()
    
    def supersede_old_attempts(self, action_name: str, mac_address: str,
                              port: Optional[int] = None, ref_ts: Optional[str] = None) -> int:
        return self._queue.supersede_old_attempts(action_name, mac_address, port, ref_ts)
    
    def list_attempt_history(self, action_name: str, mac_address: str,
                            port: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
        return self._queue.list_attempt_history(action_name, mac_address, port, limit)
    
    def get_action_status_from_queue(self, action_name: str, 
                                    mac_address: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self._queue.get_action_status_from_queue(action_name, mac_address)
    
    def get_last_action_status_from_queue(self, mac_address: str, action_name: str) -> Optional[Dict[str, str]]:
        return self._queue.get_last_action_status_from_queue(mac_address, action_name)
    
    def get_last_action_statuses_for_mac(self, mac_address: str) -> Dict[str, Dict[str, str]]:
        return self._queue.get_last_action_statuses_for_mac(mac_address)

    # Circuit breaker operations
    def record_circuit_breaker_failure(self, action_name: str, mac: str = '',
                                       max_failures: int = 5, cooldown_s: int = 300) -> None:
        return self._queue.record_circuit_breaker_failure(action_name, mac, max_failures, cooldown_s)

    def record_circuit_breaker_success(self, action_name: str, mac: str = '') -> None:
        return self._queue.record_circuit_breaker_success(action_name, mac)

    def is_circuit_open(self, action_name: str, mac: str = '') -> bool:
        return self._queue.is_circuit_open(action_name, mac)

    def get_circuit_breaker_status(self, action_name: str, mac: str = '') -> Optional[Dict[str, Any]]:
        return self._queue.get_circuit_breaker_status(action_name, mac)

    def reset_circuit_breaker(self, action_name: str, mac: str = '') -> None:
        return self._queue.reset_circuit_breaker(action_name, mac)

    def count_running_actions(self, action_name: Optional[str] = None) -> int:
        return self._queue.count_running_actions(action_name)

    # Vulnerability operations
    def add_vulnerability(self, mac_address: str, vuln_id: str, ip: Optional[str] = None,
                         hostname: Optional[str] = None, port: Optional[int] = None):
        return self._vulnerabilities.add_vulnerability(mac_address, vuln_id, ip, hostname, port)
    
    def update_vulnerability_status(self, mac_address: str, current_vulns: List[str]):
        return self._vulnerabilities.update_vulnerability_status(mac_address, current_vulns)
    
    def update_vulnerability_status_by_port(self, mac_address: str, port: int, current_vulns: List[str]):
        return self._vulnerabilities.update_vulnerability_status_by_port(mac_address, port, current_vulns)
    
    def get_all_vulns(self) -> List[Dict[str, Any]]:
        return self._vulnerabilities.get_all_vulns()
    
    def save_vulnerabilities(self, mac: str, ip: str, findings: List[Dict]):
        return self._vulnerabilities.save_vulnerabilities(mac, ip, findings)
    
    def cleanup_vulnerability_duplicates(self):
        return self._vulnerabilities.cleanup_vulnerability_duplicates()
    
    def fix_vulnerability_history_nulls(self):
        return self._vulnerabilities.fix_vulnerability_history_nulls()
    
    def count_vulnerabilities_alive(self, distinct: bool = False, active_only: bool = True) -> int:
        return self._vulnerabilities.count_vulnerabilities_alive(distinct, active_only)
    
    def count_distinct_vulnerabilities(self, alive_only: bool = False) -> int:
        return self._vulnerabilities.count_distinct_vulnerabilities(alive_only)
    
    def get_vulnerabilities_for_alive_hosts(self) -> List[str]:
        return self._vulnerabilities.get_vulnerabilities_for_alive_hosts()
    
    def list_vulnerability_history(self, cve_id: str | None = None,
                                   mac: str | None = None, limit: int = 500) -> list[dict]:
        return self._vulnerabilities.list_vulnerability_history(cve_id, mac, limit)
    
    # CVE metadata
    def get_cve_meta(self, cve_id: str) -> Optional[Dict[str, Any]]:
        return self._vulnerabilities.get_cve_meta(cve_id)
    
    def upsert_cve_meta(self, meta: Dict[str, Any]) -> None:
        return self._vulnerabilities.upsert_cve_meta(meta)
    
    def get_cve_meta_bulk(self, cve_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        return self._vulnerabilities.get_cve_meta_bulk(cve_ids)
    
    # Software operations
    def add_detected_software(self, mac_address: str, cpe: str, ip: Optional[str] = None,
                             hostname: Optional[str] = None, port: Optional[int] = None) -> None:
        return self._software.add_detected_software(mac_address, cpe, ip, hostname, port)
    
    def update_detected_software_status(self, mac_address: str, current_cpes: List[str]) -> None:
        return self._software.update_detected_software_status(mac_address, current_cpes)
    
    def migrate_cpe_from_vulnerabilities(self) -> int:
        return self._software.migrate_cpe_from_vulnerabilities()
    
    # Credential operations
    def insert_cred(self, service: str, mac: Optional[str] = None, ip: Optional[str] = None,
                   hostname: Optional[str] = None, user: Optional[str] = None, 
                   password: Optional[str] = None, port: Optional[int] = None, 
                   database: Optional[str] = None, extra: Optional[Dict[str, Any]] = None):
        return self._credentials.insert_cred(service, mac, ip, hostname, user, password, port, database, extra)
    
    def list_creds_grouped(self) -> List[Dict[str, Any]]:
        return self._credentials.list_creds_grouped()
    
    # Service operations
    def upsert_port_service(self, mac_address: str, ip: Optional[str], port: int, **kwargs):
        return self._services.upsert_port_service(mac_address, ip, port, **kwargs)
    
    def get_services_for_host(self, mac_address: str) -> List[Dict]:
        return self._services.get_services_for_host(mac_address)
    
    def find_hosts_by_service(self, service: str) -> List[Dict]:
        return self._services.find_hosts_by_service(service)
    
    def get_service_for_host_port(self, mac_address: str, port: int, protocol: str = "tcp") -> Optional[Dict]:
        return self._services.get_service_for_host_port(mac_address, port, protocol)
    
    def _rebuild_host_ports(self, mac_address: str):
        return self._services._rebuild_host_ports(mac_address)
    
    # Script operations
    def add_script(self, name: str, type_: str, path: str, main_file: Optional[str] = None,
                  category: Optional[str] = None, description: Optional[str] = None):
        return self._scripts.add_script(name, type_, path, main_file, category, description)
    
    def list_scripts(self) -> List[Dict[str, Any]]:
        return self._scripts.list_scripts()
    
    def delete_script(self, name: str) -> None:
        return self._scripts.delete_script(name)
    
    # Stats operations
    def get_livestats(self) -> Dict[str, int]:
        return self._stats.get_livestats()
    
    def update_livestats(self, total_open_ports: int, alive_hosts_count: int,
                        all_known_hosts_count: int, vulnerabilities_count: int):
        return self._stats.update_livestats(total_open_ports, alive_hosts_count,
                                           all_known_hosts_count, vulnerabilities_count)
    
    def get_stats(self) -> Dict[str, int]:
        return self._stats.get_stats()
    
    def set_stats(self, total_open_ports: int, alive_hosts_count: int,
                 all_known_hosts_count: int, vulnerabilities_count: int):
        return self._stats.set_stats(total_open_ports, alive_hosts_count,
                                    all_known_hosts_count, vulnerabilities_count)
    
    def get_display_stats(self) -> Dict[str, int]:
        return self._stats.get_display_stats()
    
    def ensure_stats_initialized(self):
        return self._stats.ensure_stats_initialized()
    
    # Backup operations
    def add_backup(self, filename: str, description: str, date: str, type_: str = "User Backup",
                  is_default: bool = False, is_restore: bool = False, is_github: bool = False):
        return self._backups.add_backup(filename, description, date, type_, is_default, is_restore, is_github)
    
    def list_backups(self) -> List[Dict[str, Any]]:
        return self._backups.list_backups()
    
    def delete_backup(self, filename: str) -> None:
        return self._backups.delete_backup(filename)
    
    def clear_default_backup(self) -> None:
        return self._backups.clear_default_backup()
    
    def set_default_backup(self, filename: str) -> None:
        return self._backups.set_default_backup(filename)
    
    # Comment operations
    def count_comments(self) -> int:
        return self._comments.count_comments()
    
    def insert_comments(self, comments: List[Tuple[str, str, str, str, int]]):
        return self._comments.insert_comments(comments)
    
    def import_comments_from_json(self, json_path: str, lang: Optional[str] = None,
                                 default_theme: str = "general", default_weight: int = 1,
                                 clear_existing: bool = False) -> int:
        return self._comments.import_comments_from_json(json_path, lang, default_theme, 
                                                       default_weight, clear_existing)
    
    def random_comment_for(self, status: str, lang: str = "en") -> Optional[Dict[str, Any]]:
        return self._comments.random_comment_for(status, lang)
    
    # Agent operations (C2)
    def save_agent(self, agent_data: dict) -> None:
        return self._agents.save_agent(agent_data)
    
    def save_command(self, agent_id: str, command: str, response: str | None = None, success: bool = False) -> None:
        return self._agents.save_command(agent_id, command, response, success)
    
    def save_telemetry(self, agent_id: str, telemetry: dict) -> None:
        return self._agents.save_telemetry(agent_id, telemetry)
    
    def save_loot(self, loot: dict) -> None:
        return self._agents.save_loot(loot)
    
    def get_agent_history(self, agent_id: str) -> List[dict]:
        return self._agents.get_agent_history(agent_id)
    
    def purge_stale_agents(self, threshold_seconds: int) -> int:
        return self._agents.purge_stale_agents(threshold_seconds)
    
    def get_stale_agents(self, threshold_seconds: int) -> list[dict]:
        return self._agents.get_stale_agents(threshold_seconds)
    
    # Agent key management
    def get_active_key(self, agent_id: str) -> str | None:
        return self._agents.get_active_key(agent_id)
    
    def list_keys(self, agent_id: str) -> list[dict]:
        return self._agents.list_keys(agent_id)
    
    def save_new_key(self, agent_id: str, key_b64: str) -> int:
        return self._agents.save_new_key(agent_id, key_b64)
    
    def rotate_key(self, agent_id: str, new_key_b64: str) -> int:
        return self._agents.rotate_key(agent_id, new_key_b64)
    
    def revoke_keys(self, agent_id: str) -> int:
        return self._agents.revoke_keys(agent_id)
    
    def verify_client_key(self, agent_id: str, key_b64: str) -> bool:
        return self._agents.verify_client_key(agent_id, key_b64)
    
    def migrate_keys_from_file(self, json_path: str) -> int:
        return self._agents.migrate_keys_from_file(json_path)
    
    # Studio operations
    def get_studio_actions(self):
        return self._studio.get_studio_actions()
    
    def get_db_actions(self):
        return self._studio.get_db_actions()
    
    def update_studio_action(self, b_class: str, updates: dict):
        return self._studio.update_studio_action(b_class, updates)
    
    def get_studio_edges(self):
        return self._studio.get_studio_edges()
    
    def upsert_studio_edge(self, from_action: str, to_action: str, edge_type: str, metadata: dict = None):
        return self._studio.upsert_studio_edge(from_action, to_action, edge_type, metadata)
    
    def delete_studio_edge(self, edge_id: int):
        return self._studio.delete_studio_edge(edge_id)
    
    def get_studio_hosts(self, include_real: bool = True):
        return self._studio.get_studio_hosts(include_real)
    
    def upsert_studio_host(self, mac_address: str, data: dict):
        return self._studio.upsert_studio_host(mac_address, data)
    
    def delete_studio_host(self, mac: str):
        return self._studio.delete_studio_host(mac)
    
    def save_studio_layout(self, name: str, layout_data: dict, description: str = None):
        return self._studio.save_studio_layout(name, layout_data, description)
    
    def load_studio_layout(self, name: str):
        return self._studio.load_studio_layout(name)
    
    def apply_studio_to_runtime(self):
        return self._studio.apply_studio_to_runtime()
    
    def _replace_actions_studio_with_actions(self, vacuum: bool = False):
        return self._studio._replace_actions_studio_with_actions(vacuum)
    
    def _sync_actions_studio_schema_and_rows(self):
        return self._studio._sync_actions_studio_schema_and_rows()
    
    # WebEnum operations
    # Add webenum methods if you have any...
    
    # =========================================================================
    # UTILITY OPERATIONS
    # =========================================================================
    
    def checkpoint(self, mode: str = "TRUNCATE") -> Tuple[int, int, int]:
        """Force a WAL checkpoint"""
        return self._base.checkpoint(mode)
    
    def wal_checkpoint(self, mode: str = "TRUNCATE") -> Tuple[int, int, int]:
        """Alias for checkpoint"""
        return self.checkpoint(mode)
    
    def optimize(self) -> None:
        """Run PRAGMA optimize"""
        return self._base.optimize()
    
    def vacuum(self) -> None:
        """Vacuum the database"""
        return self._base.vacuum()

    def close(self) -> None:
        """Close database connection gracefully."""
        try:
            with self._lock:
                if hasattr(self, "_base") and self._base:
                    # DatabaseBase handles the actual connection closure
                    if hasattr(self._base, "_conn") and self._base._conn:
                        self._base._conn.close()
            logger.info("BjornDatabase connection closed")
        except Exception as e:
            logger.debug(f"Error during database closure (ignorable if already closed): {e}")

    # Removed __del__ as it can cause circular reference leaks and is not guaranteed to run.
    # Lifecycle should be managed by explicit close() calls.
    
    # Internal helper methods used by modules
    def _table_exists(self, name: str) -> bool:
        return self._base._table_exists(name)
    
    def _column_names(self, table: str) -> List[str]:
        return self._base._column_names(table)
    
    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        return self._base._ensure_column(table, column, ddl)
