"""
data_consolidator.py - Data Consolidation Engine for Deep Learning
═══════════════════════════════════════════════════════════════════════════

Purpose:
    Consolidate logged features into training-ready datasets.
    Prepare data exports for deep learning on external PC.

Features:
    - Aggregate features across time windows
    - Compute statistical features
    - Create feature vectors for neural networks
    - Export in formats ready for TensorFlow/PyTorch
    - Incremental consolidation (low memory footprint)

Author: Bjorn Team
Version: 2.0.0
"""

import json
import csv
import time
import gzip
import heapq
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
from logger import Logger

logger = Logger(name="data_consolidator.py", level=20)

try:
    import requests
except ImportError:
    requests = None


class DataConsolidator:
    """
    Consolidates raw feature logs into training datasets.
    Optimized for Raspberry Pi Zero - processes in batches.
    """

    def __init__(self, shared_data, export_dir: str = None):
        """
        Initialize data consolidator

        Args:
            shared_data: SharedData instance
            export_dir: Directory for export files
        """
        self.shared_data = shared_data
        self.db = shared_data.db

        if export_dir is None:
            # Default to shared_data path (cross-platform)
            self.export_dir = Path(getattr(shared_data, 'ml_exports_dir', Path(shared_data.data_dir) / "ml_exports"))
        else:
            self.export_dir = Path(export_dir)

        self.export_dir.mkdir(parents=True, exist_ok=True)
        # Server health state consumed by orchestrator fallback logic.
        self.last_server_attempted = False
        self.last_server_contact_ok = None
        self._upload_backoff_until = 0.0
        self._upload_backoff_current_s = 0.0

        # AI-01: Feature variance tracking for dimensionality reduction
        self._feature_variance_min = float(
            getattr(shared_data, 'ai_feature_selection_min_variance', 0.001)
        )
        # Accumulator: {feature_name: [sum, sum_of_squares, count]}
        self._feature_stats = {}

        logger.info(f"DataConsolidator initialized, exports: {self.export_dir}")

    def _set_server_contact_state(self, attempted: bool, ok: Optional[bool]) -> None:
        self.last_server_attempted = bool(attempted)
        self.last_server_contact_ok = ok if attempted else None

    def _apply_upload_backoff(self, base_backoff_s: int, max_backoff_s: int = 3600) -> int:
        """
        Exponential upload retry backoff:
        base -> base*2 -> base*4 ... capped at max_backoff_s.
        Returns the delay (seconds) applied for the next retry window.
        """
        base = max(10, int(base_backoff_s))
        cap = max(base, int(max_backoff_s))
        prev = float(getattr(self, "_upload_backoff_current_s", 0.0) or 0.0)

        if prev <= 0:
            delay = base
        else:
            delay = min(cap, max(base, int(prev * 2)))

        self._upload_backoff_current_s = float(delay)
        self._upload_backoff_until = time.monotonic() + delay
        return int(delay)
    
    # ═══════════════════════════════════════════════════════════════════════
    # CONSOLIDATION ENGINE
    # ═══════════════════════════════════════════════════════════════════════
    
    def consolidate_features(
        self,
        batch_size: int = None,
        max_batches: Optional[int] = None
    ) -> Dict[str, int]:
        """
        Consolidate raw features into aggregated feature vectors.
        Processes unconsolidated records in batches.
        """
        if batch_size is None:
            batch_size = int(getattr(self.shared_data, "ai_batch_size", 100))
        batch_size = max(1, min(int(batch_size), 5000))
        stats = {
            'records_processed': 0,
            'records_aggregated': 0,
            'batches_completed': 0,
            'errors': 0
        }
        
        try:
            # Get unconsolidated records
            unconsolidated = self.db.query("""
                SELECT COUNT(*) as cnt 
                FROM ml_features 
                WHERE consolidated=0
            """)[0]['cnt']
            
            if unconsolidated == 0:
                logger.info("No unconsolidated features to process")
                return stats
            
            logger.info(f"Consolidating {unconsolidated} feature records...")
            
            batch_count = 0
            while True:
                if max_batches and batch_count >= max_batches:
                    break
                
                # Fetch batch
                batch = self.db.query(f"""
                    SELECT * FROM ml_features 
                    WHERE consolidated=0 
                    ORDER BY timestamp 
                    LIMIT {batch_size}
                """)
                
                if not batch:
                    break
                
                # Process batch
                for record in batch:
                    try:
                        self._consolidate_single_record(record)
                        stats['records_processed'] += 1
                    except Exception as e:
                        logger.error(f"Error consolidating record {record['id']}: {e}")
                        stats['errors'] += 1
                
                # Mark as consolidated
                record_ids = [r['id'] for r in batch]
                placeholders = ','.join('?' * len(record_ids))
                self.db.execute(f"""
                    UPDATE ml_features 
                    SET consolidated=1 
                    WHERE id IN ({placeholders})
                """, record_ids)
                
                stats['batches_completed'] += 1
                batch_count += 1
                
                # Progress log
                if batch_count % 10 == 0:
                    logger.info(
                        f"Consolidation progress: {stats['records_processed']} records, "
                        f"{stats['batches_completed']} batches"
                    )
            
            logger.success(
                f"Consolidation complete: {stats['records_processed']} records processed, "
                f"{stats['errors']} errors"
            )
            
        except Exception as e:
            logger.error(f"Consolidation failed: {e}")
            stats['errors'] += 1
        
        return stats
    
    def _consolidate_single_record(self, record: Dict[str, Any]):
        """
        Process a single feature record into aggregated form.
        Computes statistical features and feature vectors.
        """
        try:
            # Parse JSON fields once — reused by _build_feature_vector to avoid double-parsing
            host_features = json.loads(record.get('host_features', '{}'))
            network_features = json.loads(record.get('network_features', '{}'))
            temporal_features = json.loads(record.get('temporal_features', '{}'))
            action_features = json.loads(record.get('action_features', '{}'))

            # Combine all features
            all_features = {
                **host_features,
                **network_features,
                **temporal_features,
                **action_features
            }

            # Build numerical feature vector — pass already-parsed dicts to avoid re-parsing
            feature_vector = self._build_feature_vector(
                host_features, network_features, temporal_features, action_features
            )

            # AI-01: Track feature variance for dimensionality reduction
            self._track_feature_variance(feature_vector)

            # Determine time window
            raw_ts = record['timestamp']
            if isinstance(raw_ts, str):
                try:
                    timestamp = datetime.fromisoformat(raw_ts)
                except ValueError:
                    timestamp = datetime.now()
            elif isinstance(raw_ts, datetime):
                timestamp = raw_ts
            else:
                timestamp = datetime.now()

            hourly_window = timestamp.replace(minute=0, second=0, microsecond=0).isoformat()
            
            # Update or insert aggregated record
            self._update_aggregated_features(
                mac_address=record['mac_address'],
                time_window='hourly',
                timestamp=hourly_window,
                action_name=record['action_name'],
                success=record['success'],
                duration=record['duration_seconds'],
                reward=record['reward'],
                feature_vector=feature_vector,
                all_features=all_features
            )
            
        except Exception as e:
            logger.error(f"Error consolidating single record: {e}")
            raise
    
    def _build_feature_vector(
        self,
        host_features: Dict[str, Any],
        network_features: Dict[str, Any],
        temporal_features: Dict[str, Any],
        action_features: Dict[str, Any],
    ) -> Dict[str, float]:
        """
        Build a named feature dictionary from already-parsed feature dicts.
        Accepts pre-parsed dicts so JSON is never decoded twice per record.
        Uses shared ai_utils for consistency.
        """
        from ai_utils import extract_neural_features_dict

        return extract_neural_features_dict(
            host_features=host_features,
            network_features=network_features,
            temporal_features=temporal_features,
            action_features=action_features,
        )
    
    def _update_aggregated_features(
        self,
        mac_address: str,
        time_window: str,
        timestamp: str,
        action_name: str,
        success: int,
        duration: float,
        reward: float,
        feature_vector: Dict[str, float],
        all_features: Dict[str, Any]
    ):
        """
        Update or insert aggregated feature record.
        Accumulates statistics over the time window.
        """
        try:
            # Check if record exists
            existing = self.db.query("""
                SELECT * FROM ml_features_aggregated 
                WHERE mac_address=? AND time_window=? AND computed_at=?
            """, (mac_address, time_window, timestamp))
            
            if existing:
                # Update existing record
                old = existing[0]
                new_total = old['total_actions'] + 1
                # ... typical stats update ...
                
                # Merge feature vectors (average each named feature)
                old_vector = json.loads(old['feature_vector']) # Now a Dict
                if isinstance(old_vector, list): # Migration handle
                     old_vector = {} 

                merged_vector = {}
                # Combine keys from both
                all_keys = set(old_vector.keys()) | set(feature_vector.keys())
                for k in all_keys:
                    v_old = old_vector.get(k, 0.0)
                    v_new = feature_vector.get(k, 0.0)
                    merged_vector[k] = (v_old * old['total_actions'] + v_new) / new_total
                
                self.db.execute("""
                    UPDATE ml_features_aggregated
                    SET total_actions=total_actions+1,
                        success_rate=(success_rate*total_actions + ?)/(total_actions+1),
                        avg_duration=(avg_duration*total_actions + ?)/(total_actions+1),
                        total_reward=total_reward + ?,
                        feature_vector=?
                    WHERE mac_address=? AND time_window=? AND computed_at=?
                """, (
                    success,
                    duration,
                    reward,
                    json.dumps(merged_vector),
                    mac_address,
                    time_window,
                    timestamp
                ))
            else:
                # Insert new record
                self.db.execute("""
                    INSERT INTO ml_features_aggregated (
                        mac_address, time_window, computed_at,
                        total_actions, success_rate, avg_duration, total_reward,
                        feature_vector
                    ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                """, (
                    mac_address,
                    time_window,
                    timestamp,
                    float(success),
                    duration,
                    reward,
                    json.dumps(feature_vector)
                ))
                
        except Exception as e:
            logger.error(f"Error updating aggregated features: {e}")
            raise
    
    # ═══════════════════════════════════════════════════════════════════════
    # AI-01: FEATURE VARIANCE TRACKING & SELECTION
    # ═══════════════════════════════════════════════════════════════════════

    def _track_feature_variance(self, feature_vector: Dict[str, float]):
        """
        Update running statistics (mean, variance) for each feature.
        Uses Welford's online algorithm via sum/sum_sq/count.
        """
        for name, value in feature_vector.items():
            try:
                val = float(value)
            except (TypeError, ValueError):
                continue
            if name not in self._feature_stats:
                self._feature_stats[name] = [0.0, 0.0, 0]
            stats = self._feature_stats[name]
            stats[0] += val          # sum
            stats[1] += val * val    # sum of squares
            stats[2] += 1            # count

    def _get_feature_variances(self) -> Dict[str, float]:
        """Return computed variance for each tracked feature."""
        variances = {}
        for name, (s, sq, n) in self._feature_stats.items():
            if n < 2:
                variances[name] = 0.0
            else:
                mean = s / n
                variances[name] = max(0.0, sq / n - mean * mean)
        return variances

    def _get_selected_features(self) -> List[str]:
        """Return feature names that pass the minimum variance threshold."""
        threshold = self._feature_variance_min
        variances = self._get_feature_variances()
        selected = [name for name, var in variances.items() if var >= threshold]
        dropped = len(variances) - len(selected)
        if dropped > 0:
            logger.info(
                f"Feature selection: kept {len(selected)}/{len(variances)} features "
                f"(dropped {dropped} near-zero variance < {threshold})"
            )
        return sorted(selected)

    def _write_feature_manifest(self, selected_features: List[str], export_filepath: str):
        """Write feature_manifest.json alongside the export file."""
        try:
            variances = self._get_feature_variances()
            manifest = {
                'created_at': datetime.now().isoformat(),
                'feature_count': len(selected_features),
                'min_variance_threshold': self._feature_variance_min,
                'features': {
                    name: {'variance': round(variances.get(name, 0.0), 6)}
                    for name in selected_features
                },
                'export_file': str(export_filepath),
            }
            manifest_path = self.export_dir / 'feature_manifest.json'
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, indent=2)
            logger.info(f"Feature manifest written: {manifest_path} ({len(selected_features)} features)")
        except Exception as e:
            logger.error(f"Failed to write feature manifest: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # EXPORT FUNCTIONS
    # ═══════════════════════════════════════════════════════════════════════
    
    def export_for_training(
        self,
        format: str = 'csv',
        compress: bool = True,
        max_records: Optional[int] = None
    ) -> Tuple[str, int]:
        """
        Export consolidated features for deep learning training.
        
        Args:
            format: 'csv', 'jsonl', or 'parquet'
            compress: Whether to gzip the output
            max_records: Maximum records to export (None = all)
        
        Returns:
            Tuple of (file_path, record_count)
        """
        try:
            if max_records is None:
                max_records = int(getattr(self.shared_data, "ai_export_max_records", 1000))
            max_records = max(100, min(int(max_records), 20000))

            # Generate filename
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            base_filename = f"bjorn_training_{timestamp}.{format}"
            
            if compress and format != 'parquet':
                base_filename += '.gz'
            
            filepath = self.export_dir / base_filename
            
            # Fetch data
            limit_clause = f"LIMIT {max_records}"
            records = self.db.query(f"""
                SELECT 
                    mf.*,
                    mfa.feature_vector,
                    mfa.success_rate as aggregated_success_rate,
                    mfa.total_actions as aggregated_total_actions
                FROM ml_features mf
                LEFT JOIN ml_features_aggregated mfa 
                    ON mf.mac_address = mfa.mac_address
                WHERE mf.consolidated=1 AND mf.export_batch_id IS NULL
                ORDER BY mf.timestamp DESC
                {limit_clause}
            """)
            
            if not records:
                logger.warning("No consolidated records to export")
                return "", 0

            # Extract IDs before export so we can free the records list early
            record_ids = [r['id'] for r in records]

            # Export based on format
            if format == 'csv':
                count = self._export_csv(records, filepath, compress)
            elif format == 'jsonl':
                count = self._export_jsonl(records, filepath, compress)
            elif format == 'parquet':
                count = self._export_parquet(records, filepath)
            else:
                raise ValueError(f"Unsupported format: {format}")

            # Free the large records list immediately after export — record_ids is all we still need
            del records

            # AI-01: Write feature manifest with variance-filtered feature names
            try:
                selected = self._get_selected_features()
                if selected:
                    self._write_feature_manifest(selected, str(filepath))
            except Exception as e:
                logger.error(f"Feature manifest generation failed: {e}")

            # Create export batch record
            batch_id = self._create_export_batch(filepath, count)

            # Update records with batch ID
            placeholders = ','.join('?' * len(record_ids))
            self.db.execute(f"""
                UPDATE ml_features
                SET export_batch_id=?
                WHERE id IN ({placeholders})
            """, [batch_id] + record_ids)
            del record_ids
            
            logger.success(
                f"Exported {count} records to {filepath} "
                f"(batch_id={batch_id})"
            )
            
            return str(filepath), count
            
        except Exception as e:
            logger.error(f"Export failed: {e}")
            raise
    
    def _export_csv(
        self,
        records: List[Dict],
        filepath: Path,
        compress: bool
    ) -> int:
        """Export records as CSV"""
        open_func = gzip.open if compress else open
        mode = 'wt' if compress else 'w'
        
        # 1. Flatten all records first to collect all possible fieldnames
        flattened = []
        all_fieldnames = set()
        
        for r in records:
            flat = {
                'timestamp': r['timestamp'],
                'mac_address': r['mac_address'],
                'ip_address': r['ip_address'],
                'action_name': r['action_name'],
                'success': r['success'],
                'duration_seconds': r['duration_seconds'],
                'reward': r['reward']
            }
            
            # Parse and flatten features
            for field in ['host_features', 'network_features', 'temporal_features', 'action_features']:
                try:
                    features = json.loads(r.get(field, '{}'))
                    for k, v in features.items():
                        if isinstance(v, (int, float, bool, str)):
                            flat_key = f"{field}_{k}"
                            flat[flat_key] = v
                except Exception as e:
                    logger.debug(f"Skip bad JSON in {field}: {e}")
            
            # Add named feature vector
            if r.get('feature_vector'):
                try:
                    vector = json.loads(r['feature_vector'])
                    if isinstance(vector, dict):
                        for k, v in vector.items():
                            flat[f'feat_{k}'] = v
                    elif isinstance(vector, list):
                        for i, v in enumerate(vector):
                            flat[f'feature_{i}'] = v
                except Exception as e:
                    logger.debug(f"Skip bad feature vector: {e}")
            
            flattened.append(flat)
            all_fieldnames.update(flat.keys())
            
        # 2. Sort fieldnames for consistency
        sorted_fieldnames = sorted(list(all_fieldnames))
        all_fieldnames = None  # Free the set

        # 3. Write CSV
        with open_func(filepath, mode, newline='', encoding='utf-8') as f:
            if flattened:
                writer = csv.DictWriter(f, fieldnames=sorted_fieldnames)
                writer.writeheader()
                writer.writerows(flattened)

        count = len(flattened)
        flattened = None  # Free the expanded list
        return count
    
    def _export_jsonl(
        self,
        records: List[Dict],
        filepath: Path,
        compress: bool
    ) -> int:
        """Export records as JSON Lines"""
        open_func = gzip.open if compress else open
        mode = 'wt' if compress else 'w'
        
        with open_func(filepath, mode, encoding='utf-8') as f:
            for r in records:
                # Avoid mutating `records` in place to keep memory growth predictable.
                row = dict(r)
                for field in ['host_features', 'network_features', 'temporal_features', 'action_features', 'raw_event']:
                    try:
                        row[field] = json.loads(row.get(field, '{}'))
                    except Exception:
                        row[field] = {}

                if row.get('feature_vector'):
                    try:
                        row['feature_vector'] = json.loads(row['feature_vector'])
                    except Exception:
                        row['feature_vector'] = {}

                f.write(json.dumps(row) + '\n')
        
        return len(records)
    
    def _export_parquet(self, records: List[Dict], filepath: Path) -> int:
        """Export records as Parquet (requires pyarrow)"""
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
            
            # Flatten records
            flattened = []
            for r in records:
                flat = dict(r)
                # Parse JSON fields
                for field in ['host_features', 'network_features', 'temporal_features', 'action_features', 'raw_event']:
                    flat[field] = json.loads(r.get(field, '{}'))
                
                if r.get('feature_vector'):
                    flat['feature_vector'] = json.loads(r['feature_vector'])
                
                flattened.append(flat)
            
            # Convert to Arrow table
            table = pa.Table.from_pylist(flattened)
            
            # Write parquet
            pq.write_table(table, filepath, compression='snappy')
            
            return len(records)
            
        except ImportError:
            logger.error("Parquet export requires pyarrow. Falling back to CSV.")
            return self._export_csv(records, filepath.with_suffix('.csv'), compress=True)
    
    def _create_export_batch(self, filepath: Path, count: int) -> int:
        """Create export batch record and return batch ID"""
        result = self.db.execute("""
            INSERT INTO ml_export_batches (file_path, record_count, status)
            VALUES (?, ?, 'exported')
        """, (str(filepath), count))
        
        # Get the inserted ID
        batch_id = self.db.query("SELECT last_insert_rowid() as id")[0]['id']
        return batch_id
    
    # ═══════════════════════════════════════════════════════════════════════
    # UTILITY METHODS
    # ═══════════════════════════════════════════════════════════════════════
    
    def get_export_stats(self) -> Dict[str, Any]:
        """Get statistics about exports"""
        try:
            batches = self.db.query("""
                SELECT COUNT(*) as total_batches,
                       SUM(record_count) as total_records,
                       MAX(created_at) as last_export
                FROM ml_export_batches
                WHERE status='exported'
            """)[0]
            
            pending = self.db.query("""
                SELECT COUNT(*) as cnt 
                FROM ml_features 
                WHERE consolidated=1 AND export_batch_id IS NULL
            """)[0]['cnt']
            
            return {
                'total_export_batches': batches.get('total_batches', 0),
                'total_records_exported': batches.get('total_records', 0),
                'last_export_time': batches.get('last_export'),
                'pending_export_count': pending
            }
        except Exception as e:
            logger.error(f"Error getting export stats: {e}")
            return {}

    def flush_pending_uploads(self, max_files: int = 3) -> int:
        """
        Retry uploads for previously exported batches that were not transferred yet.
        Returns the number of successfully transferred files.
        """
        max_files = max(0, int(max_files))
        if max_files <= 0:
            return 0

        # No heavy "reliquat" tracking needed: pending uploads = files present in export_dir.
        files = self._list_pending_export_files(limit=max_files)
        ok = 0
        for fp in files:
            if self.upload_to_server(fp):
                ok += 1
            else:
                # Stop early when server is unreachable to avoid repeated noise.
                if self.last_server_attempted and self.last_server_contact_ok is False:
                    break
        return ok

    def _list_pending_export_files(self, limit: int = 3) -> List[str]:
        """
        Return oldest export files present in export_dir.
        This makes the backlog naturally equal to the number of files on disk.
        """
        limit = max(0, int(limit))
        if limit <= 0:
            return []

        try:
            d = Path(self.export_dir)
            if not d.exists():
                return []

            def _safe_mtime(path: Path) -> float:
                try:
                    return path.stat().st_mtime
                except Exception:
                    return float("inf")

            # Keep only the N oldest files in memory instead of sorting all candidates.
            files_iter = (p for p in d.glob("bjorn_training_*") if p.is_file())
            oldest = heapq.nsmallest(limit, files_iter, key=_safe_mtime)
            return [str(p) for p in oldest]
        except Exception:
            return []

    def _mark_batch_status(self, filepath: str, status: str, notes: str = "") -> None:
        """Update ml_export_batches status for a given file path (best-effort)."""
        try:
            self.db.execute(
                """
                UPDATE ml_export_batches
                SET status=?, notes=?
                WHERE file_path=?
                """,
                (status, notes or "", str(filepath)),
            )
        except Exception:
            pass

    def _safe_delete_uploaded_export(self, filepath: Path) -> None:
        """Delete a successfully-uploaded export file if configured to do so."""
        try:
            if not bool(self.shared_data.config.get("ai_delete_export_after_upload", True)):
                return

            fp = filepath.resolve()
            base = Path(self.export_dir).resolve()
            # Safety: only delete files under export_dir.
            if base not in fp.parents:
                return

            fp.unlink(missing_ok=True)  # Python 3.8+ supports missing_ok
        except TypeError:
            # Python < 3.8 fallback (not expected here, but safe)
            try:
                if filepath.exists():
                    filepath.unlink()
            except Exception:
                pass
        except Exception:
            pass
    
    def upload_to_server(self, filepath: str) -> bool:
        """
        Upload export file to AI Validation Server.
        
        Args:
            filepath: Path to the file to upload
            
        Returns:
            True if upload successful
        """
        self._set_server_contact_state(False, None)
        try:
            import requests
        except ImportError:
            requests = None

        if requests is None:
            logger.info_throttled(
                "AI upload skipped: requests not installed",
                key="ai_upload_no_requests",
                interval_s=600.0,
            )
            return False
            
        url = self.shared_data.config.get("ai_server_url")
        if not url:
            logger.info_throttled(
                "AI upload skipped: ai_server_url not configured",
                key="ai_upload_no_url",
                interval_s=600.0,
            )
            return False

        backoff_s = max(10, int(self.shared_data.config.get("ai_upload_retry_backoff_s", 120)))
        max_backoff_s = 3600
        now_mono = time.monotonic()
        if now_mono < self._upload_backoff_until:
            remaining = int(self._upload_backoff_until - now_mono)
            logger.debug(f"AI upload backoff active ({remaining}s remaining)")
            logger.info_throttled(
                "AI upload deferred: backoff active",
                key="ai_upload_backoff_active",
                interval_s=180.0,
            )
            return False
            
        try:
            filepath = Path(filepath)
            
            if not filepath.exists():
                logger.warning(f"AI upload skipped: file not found: {filepath}")
                self._mark_batch_status(str(filepath), "missing", "file not found")
                return False

            # Get MAC address for unique identification
            try:
                from ai_utils import get_system_mac
                mac = get_system_mac()
            except ImportError:
                mac = "unknown"
                
            logger.debug(f"Uploading {filepath.name} to AI Server ({url}) unique_id={mac}")
            self._set_server_contact_state(True, None)
            
            with open(filepath, 'rb') as f:
                files = {'file': f}
                # Send MAC as query param
                # Server expects ?mac_addr=...
                params = {'mac_addr': mac}
                
                # Short timeout to avoid blocking
                response = requests.post(f"{url}/upload", files=files, params=params, timeout=10)
            
            if response.status_code == 200:
                self._set_server_contact_state(True, True)
                self._upload_backoff_until = 0.0
                self._upload_backoff_current_s = 0.0
                logger.success(f"Uploaded {filepath.name} successfully")
                self._mark_batch_status(str(filepath), "transferred", "uploaded")
                self._safe_delete_uploaded_export(filepath)
                return True
            else:
                self._set_server_contact_state(True, False)
                next_retry_s = self._apply_upload_backoff(backoff_s, max_backoff_s)
                logger.debug(
                    f"AI upload HTTP failure for {filepath.name}: status={response.status_code}, "
                    f"next retry in {next_retry_s}s"
                )
                logger.info_throttled(
                    f"AI upload deferred (HTTP {response.status_code})",
                    key=f"ai_upload_http_{response.status_code}",
                    interval_s=300.0,
                )
                return False
                
        except Exception as e:
            self._set_server_contact_state(True, False)
            next_retry_s = self._apply_upload_backoff(backoff_s, max_backoff_s)
            logger.debug(f"AI upload exception for {filepath}: {e} (next retry in {next_retry_s}s)")
            logger.info_throttled(
                "AI upload deferred: server unreachable (retry later)",
                key="ai_upload_exception",
                interval_s=300.0,
            )
            return False
    
    def cleanup_old_exports(self, days: int = 30):
        """Delete export files older than N days"""
        try:
            cutoff = datetime.now() - timedelta(days=days)
            
            old_batches = self.db.query("""
                SELECT file_path FROM ml_export_batches
                WHERE created_at < ?
            """, (cutoff.isoformat(),))
            
            deleted = 0
            for batch in old_batches:
                filepath = Path(batch['file_path'])
                if filepath.exists():
                    filepath.unlink()
                    deleted += 1
            
            # Clean up database records
            self.db.execute("""
                DELETE FROM ml_export_batches
                WHERE created_at < ?
            """, (cutoff.isoformat(),))
            
            logger.info(f"Cleaned up {deleted} old export files")
            
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# END OF FILE
# ═══════════════════════════════════════════════════════════════════════════
