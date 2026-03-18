"""ai_engine.py - Lightweight AI decision engine for action selection on Pi Zero.

Loads pre-trained model weights from PC; falls back to heuristics when unavailable.
"""

import json
import numpy as np
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
from logger import Logger

logger = Logger(name="ai_engine.py", level=20)


class BjornAIEngine:
    """
    Dynamic AI engine for action selection and prioritization.
    Uses pre-trained model from external PC or falls back to heuristics.
    """
    
    def __init__(self, shared_data, model_dir: str = None):
        """
        Initialize AI engine
        """
        self.shared_data = shared_data
        self.db = shared_data.db
        
        if model_dir is None:
            self.model_dir = Path(getattr(shared_data, 'ai_models_dir', '/home/bjorn/ai_models'))
        else:
            self.model_dir = Path(model_dir)
            
        self.model_dir.mkdir(parents=True, exist_ok=True)
        
        # Model state
        self.model_loaded = False
        self.model_weights = None
        self.model_config = None
        self.feature_config = None
        self.last_server_attempted = False
        self.last_server_contact_ok = None

        # AI-03: Model versioning & rollback
        self._previous_model = None       # {weights, config, feature_config}
        self._model_history = []           # [{version, loaded_at, accuracy, avg_reward}]
        self._max_model_versions_on_disk = 3
        self._performance_window = []      # recent reward values for current model
        self._performance_check_interval = int(
            getattr(shared_data, 'ai_model_perf_check_interval', 50)
        )
        self._prev_model_avg_reward = None  # avg reward of the model we replaced

        # AI-04: Cold-start bootstrap scores
        self._bootstrap_scores = {}        # {(action_name, port_profile): [total_reward, count]}
        self._bootstrap_file = self.model_dir / 'ai_bootstrap_scores.json'
        self._bootstrap_weight = float(
            getattr(shared_data, 'ai_cold_start_bootstrap_weight', 0.6)
        )
        self._load_bootstrap_scores()

        # Try to load latest model
        self._load_latest_model()

        # Fallback heuristics (always available)
        self._init_heuristics()
        
        logger.info(
            f"AI Engine initialized (model_loaded={self.model_loaded}, "
            f"heuristics_available=True)"
        )
    
    # ═══════════════════════════════════════════════════════════════════════
    # MODEL LOADING
    # ═══════════════════════════════════════════════════════════════════════
    
    def _load_latest_model(self):
        """Load the most recent model from model directory"""
        try:
            # Find all potential model configs
            all_json_files = [f for f in self.model_dir.glob("bjorn_model_*.json")
                             if "_weights.json" not in f.name]

            # 1. Filter for files that have matching weights
            valid_models = []
            for f in all_json_files:
                weights_path = f.with_name(f.stem + '_weights.json')
                if weights_path.exists():
                    valid_models.append(f)
                else:
                    logger.debug(f"Skipping model {f.name}: Weights file missing")

            if not valid_models:
                logger.info(f"No complete models found in {self.model_dir}. Checking server...")
                # Try to download from server
                if self.check_for_updates():
                    return

                logger.info_throttled(
                    "No AI model available (server offline or empty). Using heuristics only.",
                    key="ai_no_model_available",
                    interval_s=600.0,
                )
                return

            # 2. Sort by timestamp in filename (lexicographical) and pick latest
            valid_models = sorted(valid_models)
            latest_model = valid_models[-1]
            weights_file = latest_model.with_name(latest_model.stem + '_weights.json')

            logger.info(f"Loading model: {latest_model.name} (Weights exists!)")

            with open(latest_model, 'r') as f:
                model_data = json.load(f)

            new_config = model_data.get('config', model_data)
            new_feature_config = model_data.get('features', {})

            # Load weights
            with open(weights_file, 'r') as f:
                weights_data = json.load(f)
            new_weights = {
                k: np.array(v) for k, v in weights_data.items()
            }
            del weights_data  # Free raw dict - numpy arrays are the canonical form

            # AI-03: Save previous model for rollback
            if self.model_loaded and self.model_weights is not None:
                self._previous_model = {
                    'weights': self.model_weights,
                    'config': self.model_config,
                    'feature_config': self.feature_config,
                }
                # Record avg reward of outgoing model for performance comparison
                if self._performance_window:
                    self._prev_model_avg_reward = (
                        sum(self._performance_window) / len(self._performance_window)
                    )
                self._performance_window = []  # reset for new model

            self.model_config = new_config
            self.feature_config = new_feature_config
            self.model_weights = new_weights
            self.model_loaded = True

            # AI-03: Track model history
            from datetime import datetime as _dt
            version = self.model_config.get('version', 'unknown')
            self._model_history.append({
                'version': version,
                'loaded_at': _dt.now().isoformat(),
                'accuracy': self.model_config.get('accuracy'),
                'avg_reward': None,  # filled later as decisions accumulate
            })
            # Keep history bounded
            if len(self._model_history) > 10:
                self._model_history = self._model_history[-10:]

            logger.success(
                f"Model loaded successfully: {version}"
            )

            # AI-03: Prune old model versions on disk (keep N most recent)
            self._prune_old_model_files(valid_models)

        except Exception as e:
            logger.error(f"Failed to load model: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            self.model_loaded = False

    def _prune_old_model_files(self, valid_models: list):
        """AI-03: Keep only the N most recent model versions on disk."""
        try:
            keep = self._max_model_versions_on_disk
            if len(valid_models) <= keep:
                return
            to_remove = valid_models[:-keep]
            for config_path in to_remove:
                weights_path = config_path.with_name(config_path.stem + '_weights.json')
                try:
                    config_path.unlink(missing_ok=True)
                    weights_path.unlink(missing_ok=True)
                    logger.info(f"Pruned old model: {config_path.name}")
                except Exception as e:
                    logger.debug(f"Could not prune {config_path.name}: {e}")
        except Exception as e:
            logger.debug(f"Model pruning error: {e}")
    
    def reload_model(self) -> bool:
        """Reload model from disk"""
        logger.info("Reloading AI model...")
        self.model_loaded = False
        self.model_weights = None
        self.model_config = None
        self.feature_config = None

        self._load_latest_model()
        return self.model_loaded

    def rollback_model(self) -> bool:
        """
        AI-03: Rollback to the previous model version.
        Returns True if rollback succeeded.
        """
        if self._previous_model is None:
            logger.warning("No previous model available for rollback")
            return False

        logger.info("Rolling back to previous model version...")
        # Current model becomes the "next" previous (so we can undo a rollback)
        current_backup = None
        if self.model_loaded and self.model_weights is not None:
            current_backup = {
                'weights': self.model_weights,
                'config': self.model_config,
                'feature_config': self.feature_config,
            }

        self.model_weights = self._previous_model['weights']
        self.model_config = self._previous_model['config']
        self.feature_config = self._previous_model['feature_config']
        self.model_loaded = True
        self._previous_model = current_backup
        self._performance_window = []  # reset

        version = self.model_config.get('version', 'unknown')
        from datetime import datetime as _dt
        self._model_history.append({
            'version': f"{version}_rollback",
            'loaded_at': _dt.now().isoformat(),
            'accuracy': self.model_config.get('accuracy'),
            'avg_reward': None,
        })

        logger.success(f"Rolled back to model version: {version}")
        return True

    def record_reward(self, reward: float):
        """
        AI-03: Record a reward for performance tracking.
        After N decisions, auto-rollback if performance has degraded.
        """
        self._performance_window.append(reward)

        # Update current history entry
        if self._model_history and len(self._performance_window) > 0:
            self._model_history[-1]['avg_reward'] = round(
                sum(self._performance_window) / len(self._performance_window), 2
            )

        # Check for auto-rollback after sufficient samples
        if len(self._performance_window) >= self._performance_check_interval:
            current_avg = sum(self._performance_window) / len(self._performance_window)

            if (
                self._prev_model_avg_reward is not None
                and current_avg < self._prev_model_avg_reward
                and self._previous_model is not None
            ):
                logger.warning(
                    f"Model performance degraded: current avg={current_avg:.2f} vs "
                    f"previous avg={self._prev_model_avg_reward:.2f}. Auto-rolling back."
                )
                self.rollback_model()
            else:
                logger.info(
                    f"Model performance check passed: avg_reward={current_avg:.2f} "
                    f"over {len(self._performance_window)} decisions"
                )
                # Reset window for next check cycle
                self._performance_window = []

    def get_model_info(self) -> Dict[str, Any]:
        """AI-03: Return current version, history, and performance stats."""
        current_avg = None
        if self._performance_window:
            current_avg = round(
                sum(self._performance_window) / len(self._performance_window), 2
            )

        return {
            'current_version': self.model_config.get('version') if self.model_config else None,
            'model_loaded': self.model_loaded,
            'has_previous_model': self._previous_model is not None,
            'history': list(self._model_history),
            'performance': {
                'current_avg_reward': current_avg,
                'decisions_since_load': len(self._performance_window),
                'check_interval': self._performance_check_interval,
                'previous_model_avg_reward': self._prev_model_avg_reward,
            },
        }
    
    def check_for_updates(self) -> bool:
        """Check AI Server for new model version."""
        self.last_server_attempted = False
        self.last_server_contact_ok = None
        try:
            import requests
            import os
        except ImportError:
            return False
            
        url = self.shared_data.config.get("ai_server_url")
        if not url:
            return False
            
        try:
            logger.debug(f"Checking AI Server for updates at {url}/model/latest")
            from ai_utils import get_system_mac
            params = {'mac_addr': get_system_mac()}
            self.last_server_attempted = True
            resp = requests.get(f"{url}/model/latest", params=params, timeout=5)
            # Any HTTP response means server is reachable.
            self.last_server_contact_ok = True
            
            if resp.status_code != 200:
                return False
                
            remote_config = resp.json()
            remote_version = str(remote_config.get("version", "")).strip()
            
            if not remote_version:
                return False
                
            current_version = str(self.model_config.get("version", "0")).strip() if self.model_config else "0"
            
            def _version_tuple(v: str) -> tuple:
                """Parse version string like '1.2.3' into comparable tuple (1, 2, 3)."""
                try:
                    return tuple(int(x) for x in v.split('.'))
                except (ValueError, AttributeError):
                    return (0,)

            if _version_tuple(remote_version) > _version_tuple(current_version):
                logger.info(f"New model available: {remote_version} (Local: {current_version})")
                
                # Download config (stream to avoid loading the whole file into RAM)
                r_conf = requests.get(
                    f"{url}/model/download/bjorn_model_{remote_version}.json",
                    stream=True, timeout=15,
                )
                if r_conf.status_code == 200:
                    conf_path = self.model_dir / f"bjorn_model_{remote_version}.json"
                    with open(conf_path, 'wb') as f:
                        for chunk in r_conf.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                else:
                    logger.info_throttled(
                        f"AI model download skipped (config HTTP {r_conf.status_code})",
                        key=f"ai_model_dl_conf_{r_conf.status_code}",
                        interval_s=300.0,
                    )
                    return False

                # Download weights (stream to avoid loading the whole file into RAM)
                r_weights = requests.get(
                    f"{url}/model/download/bjorn_model_{remote_version}_weights.json",
                    stream=True, timeout=30,
                )
                if r_weights.status_code == 200:
                    weights_path = self.model_dir / f"bjorn_model_{remote_version}_weights.json"
                    with open(weights_path, 'wb') as f:
                        for chunk in r_weights.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                    
                    logger.success(f"Downloaded model {remote_version} files to Pi.")
                else:
                    logger.info_throttled(
                        f"AI model download skipped (weights HTTP {r_weights.status_code})",
                        key=f"ai_model_dl_weights_{r_weights.status_code}",
                        interval_s=300.0,
                    )
                    return False
                
                # Reload explicitly
                return self.reload_model()
            
            logger.debug(f"Server model ({remote_version}) is not newer than local ({current_version})")
            return False
                
        except Exception as e:
            self.last_server_attempted = True
            self.last_server_contact_ok = False
            # Server may be offline; avoid spamming errors in AI mode.
            logger.info_throttled(
                f"AI server unavailable for model update check: {e}",
                key="ai_model_update_check_failed",
                interval_s=300.0,
            )
            return False
    
    # ═══════════════════════════════════════════════════════════════════════
    # DECISION MAKING
    # ═══════════════════════════════════════════════════════════════════════
    
    def choose_action(
        self,
        host_context: Dict[str, Any],
        available_actions: List[str],
        exploration_rate: float = None
    ) -> Tuple[str, float, Dict[str, Any]]:
        """
        Choose the best action for a given host.
        
        Args:
            host_context: Dict with host information (mac, ports, hostname, etc.)
            available_actions: List of action names that can be executed
            exploration_rate: Probability of random exploration (0.0-1.0)
        
        Returns:
            Tuple of (action_name, confidence_score, debug_info)
        """
        if exploration_rate is None:
            exploration_rate = float(getattr(self.shared_data, "ai_exploration_rate", 0.1))
            
        try:
            # Exploration: random action
            if exploration_rate > 0 and np.random.random() < exploration_rate:
                import random
                action = random.choice(available_actions)
                return action, 0.0, {'method': 'exploration', 'exploration_rate': exploration_rate}
            
            # If model is loaded, use it for prediction
            if self.model_loaded and self.model_weights:
                return self._predict_with_model(host_context, available_actions)
            
            # Fallback to heuristics
            return self._predict_with_heuristics(host_context, available_actions)
            
        except Exception as e:
            logger.error(f"Error choosing action: {e}")
            # Ultimate fallback: first available action
            if available_actions:
                return available_actions[0], 0.0, {'method': 'fallback_error', 'error': str(e)}
            return None, 0.0, {'method': 'no_actions', 'error': 'No available actions'}
    
    def _predict_with_model(
        self,
        host_context: Dict[str, Any],
        available_actions: List[str]
    ) -> Tuple[str, float, Dict[str, Any]]:
        """
        Use loaded neural network model for prediction.
        Dynamically maps extracted features to model manifest.
        """
        try:
            from ai_utils import extract_neural_features_dict
            
            # 1. Get model feature manifest
            manifest = self.model_config.get('architecture', {}).get('feature_names', [])
            if not manifest:
                 # Legacy fallback
                 return self._predict_with_model_legacy(host_context, available_actions)

            # 2. Extract host-level features
            mac = host_context.get('mac', '')
            host = self.db.get_host_by_mac(mac) if mac else {}
            
            host_data = self._get_host_context_from_db(mac, host)
            net_data = self._get_network_context()
            temp_data_base = self._get_temporal_context(mac)  # MAC-level temporal, called once

            best_action = None
            best_score = -1.0
            all_scores = {}

            # 3. Score each action
            for action in available_actions:
                action_data = self._get_action_context(action, host, mac)

                # Merge action-level temporal overrides into temporal context copy
                temp_data = dict(temp_data_base)
                temp_data['same_action_attempts'] = action_data.pop('same_action_attempts', 0)
                temp_data['is_retry'] = action_data.pop('is_retry', False)

                # Extract all known features into a dict
                features_dict = extract_neural_features_dict(
                    host_features=host_data,
                    network_features=net_data,
                    temporal_features=temp_data,
                    action_features=action_data
                )
                
                # Dynamic mapping: Pull features requested by model manifest
                # Defaults to 0.0 if the Pi doesn't know this feature yet
                input_vector = np.array([float(features_dict.get(name, 0.0)) for name in manifest], dtype=float)

                # Neural inference (supports variable hidden depth from exported model).
                z_out = self._forward_network(input_vector)
                z_out = np.array(z_out).reshape(-1)
                if z_out.size == 1:
                    # Binary classifier exported with 1-neuron sigmoid output.
                    score = float(self._sigmoid(z_out[0]))
                else:
                    probs = self._softmax(z_out)
                    score = float(probs[1] if len(probs) > 1 else probs[0])
                
                all_scores[action] = score
                if score > best_score:
                    best_score = score
                    best_action = action

            if best_action is None:
                return self._predict_with_heuristics(host_context, available_actions)

            # Capture the last input vector (for visualization)
            # Since we iterate, we'll just take the one from the best_action or the last one.
            # Usually input_vector is almost the same for all actions except action-specific bits.
            
            debug_info = {
                'method': 'neural_network_v3',
                'model_version': self.model_config.get('version'),
                'feature_count': len(manifest),
                'all_scores': all_scores,
                # Convert numpy ndarray → plain Python list so debug_info is
                # always JSON-serialisable (scheduler stores it in action_queue metadata).
                'input_vector': input_vector.tolist(),
            }
            
            return best_action, float(best_score), debug_info
            
        except Exception as e:
            logger.error(f"Dynamic model prediction failed: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return self._predict_with_heuristics(host_context, available_actions)

    def _predict_with_model_legacy(self, host_context: Dict[str, Any], available_actions: List[str]) -> Tuple[str, float, Dict[str, Any]]:
        """Fallback for models without feature_names manifest (fixed length 56)"""
        # ... very similar to previous v2 but using hardcoded list ...
        return self._predict_with_heuristics(host_context, available_actions)

    def _get_host_context_from_db(self, mac: str, host: Dict) -> Dict:
        """Helper to collect host features from DB"""
        ports_str = host.get('ports', '') or ''
        ports = [int(p) for p in ports_str.split(';') if p.strip().isdigit()]
        vendor = host.get('vendor', '')
        
        # Calculate age
        age_hours = 0.0
        if host.get('first_seen'):
            from datetime import datetime
            try:
                ts = host['first_seen']
                first_seen = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
                age_hours = (datetime.now() - first_seen).total_seconds() / 3600
            except: pass

        creds = self._get_credentials_for_host(mac)
        
        return {
            'port_count': len(ports),
            'service_count': len(self._get_services_for_host(mac)),
            'ip_count': len((host.get('ips') or '').split(';')),
            'credential_count': len(creds),
            'age_hours': round(age_hours, 2),
            'has_ssh': 22 in ports,
            'has_http': 80 in ports or 8080 in ports,
            'has_https': 443 in ports,
            'has_smb': 445 in ports,
            'has_rdp': 3389 in ports,
            'has_database': any(p in ports for p in [3306, 5432, 1433]),
            'has_credentials': len(creds) > 0,
            'is_new': age_hours < 24,
            'is_private': True, # Simple assumption for now
            'has_multiple_ips': len((host.get('ips') or '').split(';')) > 1,
            'vendor_category': self._categorize_vendor(vendor),
            'port_profile': self._detect_port_profile(ports)
        }

    def _get_network_context(self) -> Dict:
        """Collect real network-wide stats from DB (called once per choose_action)."""
        try:
            all_hosts = self.db.get_all_hosts()
            total = len(all_hosts)

            # Subnet diversity
            subnets = set()
            active = 0
            for h in all_hosts:
                ips = (h.get('ips') or '').split(';')
                for ip in ips:
                    ip = ip.strip()
                    if ip:
                        subnets.add('.'.join(ip.split('.')[:3]))
                        break
                if h.get('alive'):
                    active += 1

            return {
                'total_hosts': total,
                'subnet_count': len(subnets),
                'similar_vendor_count': 0,       # filled by caller if needed
                'similar_port_profile_count': 0,  # filled by caller if needed
                'active_host_ratio': round(active / total, 2) if total else 0.0,
            }
        except Exception as e:
            logger.error(f"Error collecting network context: {e}")
            return {
                'total_hosts': 0, 'subnet_count': 1,
                'similar_vendor_count': 0, 'similar_port_profile_count': 0,
                'active_host_ratio': 1.0,
            }

    def _get_temporal_context(self, mac: str) -> Dict:
        """
        Collect real temporal features for a MAC from DB.
        same_action_attempts / is_retry are action-specific - they are NOT
        included here; instead they are merged from _get_action_context()
        inside the per-action loop in _predict_with_model().
        """
        from datetime import datetime
        now = datetime.now()

        ctx = {
            'hour_of_day': now.hour,
            'day_of_week': now.weekday(),
            'is_weekend': now.weekday() >= 5,
            'is_night': now.hour < 6 or now.hour >= 22,
            'previous_action_count': 0,
            'seconds_since_last': 0,
            'historical_success_rate': 0.0,
            'same_action_attempts': 0,   # placeholder; overwritten per-action
            'is_retry': False,            # placeholder; overwritten per-action
            'global_success_rate': 0.0,
            'hours_since_discovery': 0,
        }

        try:
            # Per-host stats from ml_features (persistent training log)
            rows = self.db.query(
                """
                SELECT
                    COUNT(*)                         AS cnt,
                    AVG(CAST(success AS REAL))       AS success_rate,
                    MAX(timestamp)                   AS last_ts
                FROM ml_features
                WHERE mac_address = ?
                """,
                (mac,),
            )
            if rows and rows[0]['cnt']:
                ctx['previous_action_count'] = int(rows[0]['cnt'])
                ctx['historical_success_rate'] = round(float(rows[0]['success_rate'] or 0.0), 2)
                if rows[0]['last_ts']:
                    try:
                        last_dt = datetime.fromisoformat(str(rows[0]['last_ts']))
                        ctx['seconds_since_last'] = round(
                            (now - last_dt).total_seconds(), 1
                        )
                    except Exception:
                        pass

            # Global success rate (all hosts)
            g = self.db.query(
                "SELECT AVG(CAST(success AS REAL)) AS gsr FROM ml_features"
            )
            if g and g[0]['gsr'] is not None:
                ctx['global_success_rate'] = round(float(g[0]['gsr']), 2)

            # Hours since host first seen
            host = self.db.get_host_by_mac(mac)
            if host and host.get('first_seen'):
                try:
                    ts = host['first_seen']
                    first_seen = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
                    ctx['hours_since_discovery'] = round(
                        (now - first_seen).total_seconds() / 3600, 1
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Error collecting temporal context for {mac}: {e}")

        return ctx

    # Action-specific temporal fields populated by _get_action_context
    _ACTION_PORTS = {
        'SSHBruteforce': 22, 'SSHEnumeration': 22, 'StealFilesSSH': 22,
        'WebEnumeration': 80, 'WebVulnScan': 80, 'WebLoginProfiler': 80,
        'WebSurfaceMapper': 80,
        'SMBBruteforce': 445, 'StealFilesSMB': 445,
        'FTPBruteforce': 21, 'StealFilesFTP': 21,
        'TelnetBruteforce': 23, 'StealFilesTelnet': 23,
        'SQLBruteforce': 3306, 'StealDataSQL': 3306,
        'NmapVulnScanner': 0, 'NetworkScanner': 0,
        'RDPBruteforce': 3389,
    }

    def _get_action_context(self, action_name: str, host: Dict, mac: str = '') -> Dict:
        """
        Collect action-specific features including per-action attempt history.
        Merges action-type + target-port info with action-level temporal stats.
        """
        action_type = self._classify_action_type(action_name)
        target_port = self._ACTION_PORTS.get(action_name, 0)

        # If port not in lookup, try to infer from action name
        if target_port == 0:
            name_lower = action_name.lower()
            for svc, port in [('ssh', 22), ('http', 80), ('smb', 445), ('ftp', 21),
                               ('telnet', 23), ('sql', 3306), ('rdp', 3389)]:
                if svc in name_lower:
                    target_port = port
                    break

        ctx = {
            'action_type': action_type,
            'target_port': target_port,
            'is_standard_port': 0 < target_port < 1024,
            # Action-level temporal (overrides placeholder in temporal_context)
            'same_action_attempts': 0,
            'is_retry': False,
        }

        if mac:
            try:
                r = self.db.query(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM ml_features
                    WHERE mac_address = ? AND action_name = ?
                    """,
                    (mac, action_name),
                )
                attempts = int(r[0]['cnt']) if r else 0
                ctx['same_action_attempts'] = attempts
                ctx['is_retry'] = attempts > 0
            except Exception as e:
                logger.debug(f"Action context DB query failed for {action_name}: {e}")

        return ctx

    def _classify_action_type(self, action_name: str) -> str:
        """Classify action name into a type"""
        name = action_name.lower()
        if 'brute' in name: return 'bruteforce'
        if 'enum' in name or 'scan' in name: return 'enumeration'
        if 'exploit' in name: return 'exploitation'
        if 'dump' in name or 'extract' in name: return 'extraction'
        return 'other'
    
    # ═══════════════════════════════════════════════════════════════════════
    # AI-04: COLD-START BOOTSTRAP
    # ═══════════════════════════════════════════════════════════════════════

    def _load_bootstrap_scores(self):
        """Load persisted bootstrap scores from disk."""
        try:
            if self._bootstrap_file.exists():
                with open(self._bootstrap_file, 'r') as f:
                    raw = json.load(f)
                # Stored as {"action|profile": [total_reward, count], ...}
                for key_str, val in raw.items():
                    parts = key_str.split('|', 1)
                    if len(parts) == 2 and isinstance(val, list) and len(val) == 2:
                        self._bootstrap_scores[(parts[0], parts[1])] = val
                logger.info(f"Loaded {len(self._bootstrap_scores)} bootstrap score entries")
        except Exception as e:
            logger.debug(f"Could not load bootstrap scores: {e}")

    def _save_bootstrap_scores(self):
        """Persist bootstrap scores to disk."""
        try:
            serializable = {
                f"{k[0]}|{k[1]}": v for k, v in self._bootstrap_scores.items()
            }
            with open(self._bootstrap_file, 'w', encoding='utf-8') as f:
                json.dump(serializable, f)
        except Exception as e:
            logger.debug(f"Could not save bootstrap scores: {e}")

    def update_bootstrap(self, action_name: str, port_profile: str, reward: float):
        """
        AI-04: Update running average reward for an (action, port_profile) pair.
        Called after each action execution to accumulate real performance data.
        """
        key = (action_name, port_profile)
        if key not in self._bootstrap_scores:
            self._bootstrap_scores[key] = [0.0, 0]
        entry = self._bootstrap_scores[key]
        entry[0] += reward
        entry[1] += 1

        # Persist periodically (every 5 updates to reduce disk writes)
        total_updates = sum(v[1] for v in self._bootstrap_scores.values())
        if total_updates % 5 == 0:
            self._save_bootstrap_scores()

        logger.debug(
            f"Bootstrap updated: {action_name}+{port_profile} "
            f"avg={entry[0]/entry[1]:.1f} (n={entry[1]})"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # HEURISTIC FALLBACK
    # ═══════════════════════════════════════════════════════════════════════

    def _init_heuristics(self):
        """Initialize rule-based heuristics for cold start"""
        self.heuristics = {
            'port_based': {
                22: ['SSHBruteforce', 'SSHEnumeration'],
                80: ['WebEnumeration', 'WebVulnScan'],
                443: ['WebEnumeration', 'SSLScan'],
                445: ['SMBBruteforce', 'SMBEnumeration'],
                3389: ['RDPBruteforce'],
                21: ['FTPBruteforce', 'FTPEnumeration'],
                23: ['TelnetBruteforce'],
                3306: ['MySQLBruteforce'],
                5432: ['PostgresBruteforce'],
                1433: ['MSSQLBruteforce']
            },
            'service_based': {
                'ssh': ['SSHBruteforce', 'SSHEnumeration'],
                'http': ['WebEnumeration', 'WebVulnScan'],
                'https': ['WebEnumeration', 'SSLScan'],
                'smb': ['SMBBruteforce', 'SMBEnumeration'],
                'ftp': ['FTPBruteforce', 'FTPEnumeration'],
                'mysql': ['MySQLBruteforce'],
                'postgres': ['PostgresBruteforce']
            },
            'profile_based': {
                'camera': ['WebEnumeration', 'DefaultCredCheck', 'RTSPBruteforce'],
                'nas': ['SMBBruteforce', 'WebEnumeration', 'SSHBruteforce'],
                'web_server': ['WebEnumeration', 'WebVulnScan'],
                'database': ['MySQLBruteforce', 'PostgresBruteforce'],
                'linux_server': ['SSHBruteforce', 'WebEnumeration'],
                'windows_server': ['SMBBruteforce', 'RDPBruteforce']
            }
        }
    
    def _predict_with_heuristics(
        self,
        host_context: Dict[str, Any],
        available_actions: List[str]
    ) -> Tuple[str, float, Dict[str, Any]]:
        """
        Use rule-based heuristics for action selection.
        AI-04: Blends static rules with bootstrap scores from actual execution data.
        """
        try:
            mac = host_context.get('mac', '')
            host = self.db.get_host_by_mac(mac) if mac else {}

            # Get ports and services
            ports_str = host.get('ports', '') or ''
            ports = {int(p) for p in ports_str.split(';') if p.strip().isdigit()}
            services = self._get_services_for_host(mac)

            # Detect port profile
            port_profile = self._detect_port_profile(ports)

            # Static heuristic scoring
            static_scores = {action: 0.0 for action in available_actions}

            # Score based on ports
            for port in ports:
                if port in self.heuristics['port_based']:
                    for action in self.heuristics['port_based'][port]:
                        if action in static_scores:
                            static_scores[action] += 0.3

            # Score based on services
            for service in services:
                if service in self.heuristics['service_based']:
                    for action in self.heuristics['service_based'][service]:
                        if action in static_scores:
                            static_scores[action] += 0.4

            # Score based on port profile
            if port_profile in self.heuristics['profile_based']:
                for action in self.heuristics['profile_based'][port_profile]:
                    if action in static_scores:
                        static_scores[action] += 0.3

            # AI-04: Blend static scores with bootstrap scores
            blended_scores = {}
            bootstrap_used = False
            for action in available_actions:
                static_score = static_scores.get(action, 0.0)
                key = (action, port_profile)
                entry = self._bootstrap_scores.get(key)

                if entry and entry[1] > 0:
                    bootstrap_used = True
                    bootstrap_avg = entry[0] / entry[1]
                    # Normalize bootstrap avg to 0-1 range (assume reward range ~-30 to +200)
                    bootstrap_norm = max(0.0, min(1.0, (bootstrap_avg + 30) / 230))
                    sample_count = entry[1]

                    # Lerp bootstrap weight from 40% to 80% over 20 samples
                    base_weight = self._bootstrap_weight  # default 0.6
                    if sample_count < 20:
                        # Interpolate: at 1 sample -> 0.4, at 20 samples -> 0.8
                        t = (sample_count - 1) / 19.0
                        bootstrap_w = 0.4 + t * (0.8 - 0.4)
                    else:
                        bootstrap_w = 0.8
                    static_w = 1.0 - bootstrap_w

                    blended_scores[action] = static_w * static_score + bootstrap_w * bootstrap_norm
                else:
                    blended_scores[action] = static_score

            # Find best action
            action_scores = blended_scores
            if action_scores:
                best_action = max(action_scores, key=action_scores.get)
                best_score = action_scores[best_action]

                # Normalize score to 0-1 range
                # Static heuristic scores can exceed 1.0 when multiple port/service
                # rules match, so we normalize by the maximum observed score.
                if best_score > 1.0:
                    all_vals = action_scores.values()
                    max_val = max(all_vals) if all_vals else 1.0
                    best_score = best_score / max_val if max_val > 0 else 1.0
                best_score = min(best_score, 1.0)

                debug_info = {
                    'method': 'heuristics_bootstrap' if bootstrap_used else 'heuristics',
                    'port_profile': port_profile,
                    'ports': list(ports)[:10],
                    'services': services,
                    'bootstrap_used': bootstrap_used,
                    'all_scores': {k: round(v, 4) for k, v in action_scores.items() if v > 0}
                }

                return best_action, best_score, debug_info

            # Ultimate fallback
            if available_actions:
                return available_actions[0], 0.1, {'method': 'fallback_first'}

            return None, 0.0, {'method': 'no_actions'}

        except Exception as e:
            logger.error(f"Heuristic prediction failed: {e}")
            if available_actions:
                return available_actions[0], 0.0, {'method': 'fallback_error', 'error': str(e)}
            return None, 0.0, {'method': 'error', 'error': str(e)}
    
    # ═══════════════════════════════════════════════════════════════════════
    # HELPER METHODS
    # ═══════════════════════════════════════════════════════════════════════
    
    @staticmethod
    def _relu(x):
        """ReLU activation function"""
        return np.maximum(0, x)

    @staticmethod
    def _sigmoid(x):
        """Sigmoid activation function"""
        return 1.0 / (1.0 + np.exp(-x))
    
    @staticmethod
    def _softmax(x):
        """Softmax activation function"""
        exp_x = np.exp(x - np.max(x))  # Numerical stability
        return exp_x / exp_x.sum()

    def _forward_network(self, input_vector: np.ndarray) -> np.ndarray:
        """
        Forward pass through exported dense network with dynamic hidden depth.
        Expected keys: w1/b1, w2/b2, ..., w_out/b_out
        """
        a = input_vector
        layer_idx = 1
        while f'w{layer_idx}' in self.model_weights:
            w = self.model_weights[f'w{layer_idx}']
            b = self.model_weights[f'b{layer_idx}']
            a = self._relu(np.dot(a, w) + b)
            layer_idx += 1
        return np.dot(a, self.model_weights['w_out']) + self.model_weights['b_out']
    
    def _get_services_for_host(self, mac: str) -> List[str]:
        """Get detected services for host"""
        try:
            results = self.db.query("""
                SELECT DISTINCT service 
                FROM port_services 
                WHERE mac_address=?
            """, (mac,))
            return [r['service'] for r in results if r.get('service')]
        except:
            return []
    
    def _get_credentials_for_host(self, mac: str) -> List[Dict]:
        """Get credentials found for host"""
        try:
            return self.db.query("""
                SELECT service, user, port 
                FROM creds 
                WHERE mac_address=?
            """, (mac,))
        except:
            return []
    
    def _categorize_vendor(self, vendor: str) -> str:
        """Categorize vendor (same as feature_logger)"""
        if not vendor:
            return 'unknown'
        
        vendor_lower = vendor.lower()
        categories = {
            'networking': ['cisco', 'juniper', 'ubiquiti', 'mikrotik', 'tp-link'],
            'iot': ['hikvision', 'dahua', 'axis'],
            'nas': ['synology', 'qnap'],
            'compute': ['raspberry', 'intel', 'apple', 'dell', 'hp'],
            'virtualization': ['vmware', 'microsoft'],
            'mobile': ['apple', 'samsung', 'huawei']
        }
        
        for category, vendors in categories.items():
            if any(v in vendor_lower for v in vendors):
                return category
        
        return 'other'
    
    def _detect_port_profile(self, ports) -> str:
        """Detect device profile from ports (same as feature_logger)"""
        port_set = set(ports)
        
        profiles = {
            'camera': {554, 80, 8000},
            'web_server': {80, 443, 8080},
            'nas': {5000, 5001, 548, 139, 445},
            'database': {3306, 5432, 1433, 27017},
            'linux_server': {22, 80, 443},
            'windows_server': {135, 139, 445, 3389},
            'printer': {9100, 515, 631},
            'router': {22, 23, 80, 443, 161}
        }
        
        max_overlap = 0
        best_profile = 'generic'
        
        for profile_name, profile_ports in profiles.items():
            overlap = len(port_set & profile_ports)
            if overlap > max_overlap:
                max_overlap = overlap
                best_profile = profile_name
        
        return best_profile if max_overlap >= 2 else 'generic'
    
    # ═══════════════════════════════════════════════════════════════════════
    # STATISTICS
    # ═══════════════════════════════════════════════════════════════════════
    
    def get_stats(self) -> Dict[str, Any]:
        """Get AI engine statistics"""
        stats = {
            'model_loaded': self.model_loaded,
            'heuristics_available': True,
            'decision_mode': 'neural_network' if self.model_loaded else 'heuristics'
        }

        if self.model_loaded and self.model_config:
            stats.update({
                'model_version': self.model_config.get('version'),
                'model_trained_at': self.model_config.get('trained_at'),
                'model_accuracy': self.model_config.get('accuracy'),
                'training_samples': self.model_config.get('training_samples')
            })

        # AI-03: Include model versioning info
        stats['model_info'] = self.get_model_info()

        # AI-04: Include bootstrap stats
        stats['bootstrap_entries'] = len(self._bootstrap_scores)

        return stats


# ═══════════════════════════════════════════════════════════════════════════
# SINGLETON FACTORY
# ═══════════════════════════════════════════════════════════════════════════

def get_or_create_ai_engine(shared_data) -> Optional['BjornAIEngine']:
    """
    Return the single BjornAIEngine instance attached to shared_data.
    Creates it on first call; subsequent calls return the cached instance.

    Use this instead of BjornAIEngine(shared_data) to avoid loading model
    weights multiple times (orchestrator + scheduler + web each need AI).
    """
    if getattr(shared_data, '_ai_engine_singleton', None) is None:
        try:
            shared_data._ai_engine_singleton = BjornAIEngine(shared_data)
        except Exception as e:
            logger.error(f"Failed to create BjornAIEngine singleton: {e}")
            shared_data._ai_engine_singleton = None
    return shared_data._ai_engine_singleton


def invalidate_ai_engine(shared_data) -> None:
    """Drop the cached singleton (e.g. after a mode reset or model update)."""
    shared_data._ai_engine_singleton = None


# ═══════════════════════════════════════════════════════════════════════════
# END OF FILE
# ═══════════════════════════════════════════════════════════════════════════
