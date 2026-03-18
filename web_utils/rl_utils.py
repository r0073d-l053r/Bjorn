"""rl_utils.py - Backend utilities for RL/AI dashboard endpoints."""
import json
from typing import Any, Dict, List

from ai_engine import get_or_create_ai_engine
from logger import Logger

logger = Logger(name="rl_utils")


class RLUtils:
    """
    Backend utilities for RL/AI dashboard endpoints.
    """

    def __init__(self, shared_data):
        self.shared_data = shared_data
        # Use the process-level singleton to avoid reloading model weights
        self.ai_engine = get_or_create_ai_engine(shared_data)

    def get_stats(self, handler) -> None:
        """
        API Endpoint: GET /api/rl/stats
        """
        try:
            ai_stats = self.ai_engine.get_stats() if self.ai_engine else {}
            ai_stats = ai_stats if isinstance(ai_stats, dict) else {}

            episodes = self._query_scalar("SELECT COUNT(*) AS c FROM ml_features", key="c", default=0)
            recent_activity = self._query_rows(
                """
                SELECT action_name AS action, reward, success, timestamp
                FROM ml_features
                ORDER BY timestamp DESC
                LIMIT 5
                """
            )

            payload = {
                "enabled": bool(self.ai_engine is not None),
                "episodes": int(episodes),
                "epsilon": float(getattr(self.shared_data, "ai_exploration_rate", 0.1)),
                "q_table_size": int(ai_stats.get("q_table_size", 0) or 0),
                "recent_activity": recent_activity,
                "last_loss": 0.0,
                "status": self.shared_data.get_status().get("status", "Idle"),
                "ai_mode": bool(getattr(self.shared_data, "ai_mode", False)),
                "mode": str(getattr(self.shared_data, "operation_mode", "AUTO")),
                "manual_mode": bool(getattr(self.shared_data, "manual_mode", False)),
                "model_loaded": bool(ai_stats.get("model_loaded", False)),
                "model_version": ai_stats.get("model_version"),
                "model_trained_at": ai_stats.get("model_trained_at"),
                "model_accuracy": ai_stats.get("model_accuracy"),
                "training_samples": ai_stats.get("training_samples"),
            }
            payload.update(self._extract_model_meta())

            self._send_json(handler, payload)
        except Exception as exc:
            logger.error(f"Error fetching AI stats: {exc}")
            self._send_json(handler, {"error": str(exc)}, 500)

    def get_training_history(self, handler) -> None:
        """
        API Endpoint: GET /api/rl/history
        """
        try:
            rows = self._query_rows(
                """
                SELECT id, id AS batch_id, record_count, file_path AS filepath, created_at AS timestamp
                FROM ml_export_batches
                ORDER BY created_at DESC
                LIMIT 50
                """
            )
            self._send_json(handler, {"history": rows})
        except Exception as exc:
            logger.error(f"Error fetching training history: {exc}")
            self._send_json(handler, {"error": str(exc)}, 500)

    def get_recent_experiences(self, handler) -> None:
        """
        API Endpoint: GET /api/rl/experiences
        """
        try:
            rows = self._query_rows(
                """
                SELECT action_name, reward, success, duration_seconds, timestamp, ip_address
                FROM ml_features
                ORDER BY timestamp DESC
                LIMIT 20
                """
            )
            self._send_json(handler, {"experiences": rows})
        except Exception as exc:
            logger.error(f"Error fetching experiences: {exc}")
            self._send_json(handler, {"error": str(exc)}, 500)

    def set_mode(self, handler, data: Dict) -> Dict:
        """
        API Endpoint: POST /api/rl/config
        """
        try:
            mode = str(data.get("mode", "")).upper()
            if mode not in ["MANUAL", "AUTO", "AI"]:
                return {"status": "error", "message": f"Invalid mode: {mode}"}

            self.shared_data.operation_mode = mode

            bjorn = getattr(self.shared_data, "bjorn_instance", None)
            if bjorn:
                if mode == "MANUAL":
                    bjorn.stop_orchestrator()
                else:
                    bjorn.check_and_start_orchestrator()
            else:
                logger.warning("Bjorn instance not found in shared_data")

            return {
                "status": "ok",
                "mode": mode,
                "manual_mode": bool(getattr(self.shared_data, "manual_mode", False)),
                "ai_mode": bool(getattr(self.shared_data, "ai_mode", False)),
            }
        except Exception as exc:
            logger.error(f"Error setting mode: {exc}")
            return {"status": "error", "message": str(exc)}

    # ------------------------------------------------------------------ helpers

    def _extract_model_meta(self) -> Dict[str, Any]:
        """
        Returns model metadata useful for abstract visualization only.
        """
        default = {
            "model_param_count": 0,
            "model_layer_count": 0,
            "model_feature_count": 0,
        }
        if not self.ai_engine or not self.ai_engine.model_loaded:
            return default

        try:
            param_count = 0
            layer_count = 0
            weights = self.ai_engine.model_weights or {}
            for name, arr in weights.items():
                shape = getattr(arr, "shape", None)
                if shape is not None:
                    try:
                        size = int(arr.size)
                    except Exception:
                        size = 0
                    param_count += max(0, size)
                if isinstance(name, str) and name.startswith("w"):
                    layer_count += 1

            feature_count = 0
            cfg = self.ai_engine.model_config or {}
            arch = cfg.get("architecture", {}) if isinstance(cfg, dict) else {}
            feats = arch.get("feature_names", []) if isinstance(arch, dict) else []
            if isinstance(feats, list):
                feature_count = len(feats)

            return {
                "model_param_count": int(param_count),
                "model_layer_count": int(layer_count),
                "model_feature_count": int(feature_count),
            }
        except Exception as exc:
            logger.error(f"Failed extracting model meta: {exc}")
            return default

    def _query_rows(self, sql: str) -> List[Dict[str, Any]]:
        try:
            return self.shared_data.db.query(sql) or []
        except Exception as exc:
            msg = str(exc)
            if "no such table" in msg:
                logger.debug(f"Table not yet created (AI not active): {msg}")
            else:
                logger.error(f"DB query failed: {exc}")
            return []

    def _query_scalar(self, sql: str, key: str, default: int = 0) -> int:
        rows = self._query_rows(sql)
        if not rows:
            return default
        try:
            return int(rows[0].get(key, default) or default)
        except Exception:
            return default

    def _send_json(self, handler, data, status: int = 200):
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(data).encode("utf-8"))

