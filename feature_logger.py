"""feature_logger.py - Auto-capture action execution features for deep learning training."""

import json
import time
import hashlib
import random
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict, deque
from logger import Logger

logger = Logger(name="feature_logger.py", level=20)


class FeatureLogger:
    """
    Captures comprehensive features from network reconnaissance
    and action execution for deep learning.
    """
    
    def __init__(self, shared_data):
        """Initialize feature logger with database connection"""
        self.shared_data = shared_data
        self.db = shared_data.db
        self._max_hosts_tracked = max(
            64, int(getattr(self.shared_data, "ai_feature_hosts_limit", 512))
        )
        
        # Rolling windows for temporal features (memory efficient)
        self.recent_actions = deque(maxlen=100)
        self.host_history = defaultdict(lambda: deque(maxlen=50))
        
        # Initialize feature tables
        self._ensure_tables_exist()
        
        logger.info("FeatureLogger initialized - auto-discovery mode enabled")
    
    # ═══════════════════════════════════════════════════════════════════════
    # DATABASE SCHEMA
    # ═══════════════════════════════════════════════════════════════════════
    
    def _ensure_tables_exist(self):
        """Create feature logging tables if they don't exist"""
        try:
            # Main feature log table
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS ml_features (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    
                    -- Identifiers
                    mac_address TEXT,
                    ip_address TEXT,
                    action_name TEXT,
                    
                    -- Context features (JSON)
                    host_features TEXT,      -- Vendor, ports, services, etc.
                    network_features TEXT,   -- Topology, neighbors, subnets
                    temporal_features TEXT,  -- Time patterns, sequences
                    action_features TEXT,    -- Action-specific metadata
                    
                    -- Outcome
                    success INTEGER,
                    duration_seconds REAL,
                    reward REAL,
                    
                    -- Raw event data (for replay)
                    raw_event TEXT,
                    
                    -- Consolidation status
                    consolidated INTEGER DEFAULT 0,
                    export_batch_id INTEGER
                )
            """)
            
            # Index for fast queries
            self.db.execute("""
                CREATE INDEX IF NOT EXISTS idx_ml_features_mac 
                ON ml_features(mac_address, timestamp DESC)
            """)
            
            self.db.execute("""
                CREATE INDEX IF NOT EXISTS idx_ml_features_consolidated 
                ON ml_features(consolidated, timestamp)
            """)
            
            # Aggregated features table (pre-computed for efficiency)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS ml_features_aggregated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    
                    mac_address TEXT,
                    time_window TEXT,  -- 'hourly', 'daily', 'weekly'
                    
                    -- Aggregated metrics
                    total_actions INTEGER,
                    success_rate REAL,
                    avg_duration REAL,
                    total_reward REAL,
                    
                    -- Action distribution
                    action_counts TEXT,  -- JSON: {action_name: count}
                    
                    -- Discovery metrics
                    new_ports_found INTEGER,
                    new_services_found INTEGER,
                    credentials_found INTEGER,
                    
                    -- Feature vector (for DL)
                    feature_vector TEXT,  -- JSON array of numerical features
                    
                    UNIQUE(mac_address, time_window, computed_at)
                )
            """)
            
            # Export batches tracking
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS ml_export_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    record_count INTEGER,
                    file_path TEXT,
                    status TEXT DEFAULT 'pending',  -- pending, exported, transferred
                    notes TEXT
                )
            """)
            
            logger.info("ML feature tables initialized")
            
        except Exception as e:
            logger.error(f"Failed to create ML tables: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════
    # AUTOMATIC FEATURE EXTRACTION
    # ═══════════════════════════════════════════════════════════════════════
    
    def log_action_execution(
        self,
        mac_address: str,
        ip_address: str,
        action_name: str,
        success: bool,
        duration: float,
        reward: float,
        raw_event: Dict[str, Any]
    ):
        """
        Log a complete action execution with automatically extracted features.
        
        Args:
            mac_address: Target MAC address
            ip_address: Target IP address
            action_name: Name of executed action
            success: Whether action succeeded
            duration: Execution time in seconds
            reward: Calculated reward value
            raw_event: Complete event data (for replay/debugging)
        """
        try:
            # Shield against missing MAC
            if not mac_address:
                logger.debug("Skipping ML log: missing MAC address")
                return

            # Extract features from multiple sources
            host_features = self._extract_host_features(mac_address, ip_address)
            network_features = self._extract_network_features(mac_address)
            temporal_features = self._extract_temporal_features(mac_address, action_name)
            action_features = self._extract_action_features(action_name, raw_event)
            
            # Store in database
            self.db.execute("""
                INSERT INTO ml_features (
                    mac_address, ip_address, action_name,
                    host_features, network_features, temporal_features, action_features,
                    success, duration_seconds, reward, raw_event
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                mac_address, ip_address, action_name,
                json.dumps(host_features),
                json.dumps(network_features),
                json.dumps(temporal_features),
                json.dumps(action_features),
                1 if success else 0,
                duration,
                reward,
                json.dumps(raw_event)
            ))
            
            # Update rolling windows
            self.recent_actions.append({
                'mac': mac_address,
                'action': action_name,
                'success': success,
                'timestamp': time.time()
            })
            
            self.host_history[mac_address].append({
                'action': action_name,
                'success': success,
                'timestamp': time.time()
            })
            if len(self.host_history) > 1000:
                self._prune_host_history()
            
            logger.debug(
                f"Logged features for {action_name} on {mac_address} "
                f"(success={success}, features={len(host_features)}+{len(network_features)}+"
                f"{len(temporal_features)}+{len(action_features)})"
            )
            
            # Prune old database records to save disk space (keep last 1000)
            if random.random() < 0.05: # 5% chance to prune to avoid overhead every hit
                self._prune_database_records()
            
        except Exception as e:
            logger.error(f"Failed to log action execution: {e}")

    def _prune_host_history(self):
        """Bound host_history keys to avoid unbounded growth over very long runtimes."""
        try:
            current_size = len(self.host_history)
            if current_size <= self._max_hosts_tracked:
                return

            overflow = current_size - self._max_hosts_tracked
            ranked = []
            for mac, entries in self.host_history.items():
                if entries:
                    ranked.append((entries[-1]['timestamp'], mac))
                else:
                    ranked.append((0.0, mac))
            ranked.sort(key=lambda x: x[0])  # oldest first

            for _, mac in ranked[:overflow]:
                self.host_history.pop(mac, None)
        except Exception:
            pass

    def _prune_database_records(self, limit: int = 1000):
        """Keep the ml_features table within a reasonable size limit."""
        try:
            self.db.execute(f"""
                DELETE FROM ml_features 
                WHERE id NOT IN (
                    SELECT id FROM ml_features 
                    ORDER BY timestamp DESC 
                    LIMIT {limit}
                )
            """)
        except Exception as e:
            logger.debug(f"Failed to prune ml_features: {e}")
    
    def _extract_host_features(self, mac: str, ip: str) -> Dict[str, Any]:
        """
        Extract features about the target host.
        Auto-discovers all relevant attributes from database.
        """
        features = {}
        
        try:
            # Get host data
            host = self.db.get_host_by_mac(mac)
            if not host:
                return features
            
            # Basic identifiers (hashed for privacy if needed)
            features['mac_hash'] = hashlib.md5(mac.encode()).hexdigest()[:8]
            features['vendor_oui'] = mac[:8].upper() if mac else None
            
            # Vendor classification
            vendor = host.get('vendor', '')
            features['vendor'] = vendor
            features['vendor_category'] = self._categorize_vendor(vendor)
            
            # Network interfaces
            ips = [p.strip() for p in (host.get('ips', '') or '').split(';') if p.strip()]
            features['ip_count'] = len(ips)
            features['has_multiple_ips'] = len(ips) > 1
            
            # Subnet classification
            if ips:
                features['subnet'] = '.'.join(ips[0].split('.')[:3]) + '.0/24'
                features['is_private'] = self._is_private_ip(ips[0])
            
            # Open ports
            ports_str = host.get('ports', '') or ''
            ports = [int(p) for p in ports_str.split(';') if p.strip().isdigit()]
            features['port_count'] = len(ports)
            features['ports'] = sorted(ports)[:20]  # Limit to top 20
            
            # Port profiles (auto-detect common patterns)
            features['port_profile'] = self._detect_port_profile(ports)
            features['has_ssh'] = 22 in ports
            features['has_http'] = 80 in ports or 8080 in ports
            features['has_https'] = 443 in ports
            features['has_smb'] = 445 in ports
            features['has_rdp'] = 3389 in ports
            features['has_database'] = any(p in ports for p in [3306, 5432, 1433, 27017])
            
            # Services detected
            services = self._get_services_for_host(mac)
            features['service_count'] = len(services)
            features['services'] = services
            
            # Hostnames
            hostnames = [h.strip() for h in (host.get('hostnames', '') or '').split(';') if h.strip()]
            features['hostname_count'] = len(hostnames)
            if hostnames:
                features['primary_hostname'] = hostnames[0]
                features['hostname_hints'] = self._extract_hostname_hints(hostnames[0])
            
            # First/last seen
            features['first_seen'] = host.get('first_seen')
            features['last_seen'] = host.get('last_seen')
            
            # Calculate age
            if host.get('first_seen'):
                ts = host['first_seen']
                if isinstance(ts, str):
                    try:
                        first_seen_dt = datetime.fromisoformat(ts)
                    except ValueError:
                        # Fallback for other formats if needed
                        first_seen_dt = datetime.now()
                elif isinstance(ts, datetime):
                    first_seen_dt = ts
                else:
                    first_seen_dt = datetime.now()

                age_hours = (datetime.now() - first_seen_dt).total_seconds() / 3600
                features['age_hours'] = round(age_hours, 2)
                features['is_new'] = age_hours < 24
            
            # Credentials found
            creds = self._get_credentials_for_host(mac)
            features['credential_count'] = len(creds)
            features['has_credentials'] = len(creds) > 0
            
            # OS fingerprinting hints
            features['os_hints'] = self._guess_os(vendor, ports, hostnames)
            
        except Exception as e:
            logger.error(f"Error extracting host features: {e}")
        
        return features
    
    def _extract_network_features(self, mac: str) -> Dict[str, Any]:
        """
        Extract network topology and relationship features.
        Discovers patterns in the network structure.
        """
        features = {}
        
        try:
            # Get all hosts
            all_hosts = self.db.get_all_hosts()
            
            # Network size
            features['total_hosts'] = len(all_hosts)
            
            # Subnet distribution
            subnet_counts = defaultdict(int)
            for h in all_hosts:
                ips = [p.strip() for p in (h.get('ips', '') or '').split(';') if p.strip()]
                if ips:
                    subnet = '.'.join(ips[0].split('.')[:3]) + '.0'
                    subnet_counts[subnet] += 1
            
            features['subnet_count'] = len(subnet_counts)
            features['largest_subnet_size'] = max(subnet_counts.values()) if subnet_counts else 0
            
            # Similar hosts (same vendor)
            target_host = self.db.get_host_by_mac(mac)
            if target_host:
                vendor = target_host.get('vendor', '')
                similar = sum(1 for h in all_hosts if h.get('vendor') == vendor)
                features['similar_vendor_count'] = similar
            
            # Port correlation (hosts with similar port profiles)
            target_ports = set()
            if target_host:
                ports_str = target_host.get('ports', '') or ''
                target_ports = {int(p) for p in ports_str.split(';') if p.strip().isdigit()}
            
            if target_ports:
                similar_port_hosts = 0
                for h in all_hosts:
                    if h.get('mac_address') == mac:
                        continue
                    ports_str = h.get('ports', '') or ''
                    other_ports = {int(p) for p in ports_str.split(';') if p.strip().isdigit()}
                    
                    # Calculate Jaccard similarity
                    if other_ports:
                        intersection = len(target_ports & other_ports)
                        union = len(target_ports | other_ports)
                        similarity = intersection / union if union > 0 else 0
                        if similarity > 0.5:  # >50% similar
                            similar_port_hosts += 1
                
                features['similar_port_profile_count'] = similar_port_hosts
            
            # Network activity level
            recent_hosts = sum(1 for h in all_hosts 
                             if self._is_recently_active(h.get('last_seen')))
            features['active_host_ratio'] = round(recent_hosts / len(all_hosts), 2) if all_hosts else 0
            
        except Exception as e:
            logger.error(f"Error extracting network features: {e}")
        
        return features
    
    def _extract_temporal_features(self, mac: str, action: str) -> Dict[str, Any]:
        """
        Extract time-based and sequence features.
        Discovers temporal patterns in attack sequences.
        """
        features = {}
        
        try:
            # Current time features
            now = datetime.now()
            features['hour_of_day'] = now.hour
            features['day_of_week'] = now.weekday()
            features['is_weekend'] = now.weekday() >= 5
            features['is_night'] = now.hour < 6 or now.hour >= 22
            
            # Action history for this host
            history = list(self.host_history.get(mac, []))
            features['previous_action_count'] = len(history)
            
            if history:
                # Last action
                last = history[-1]
                features['last_action'] = last['action']
                features['last_action_success'] = last['success']
                features['seconds_since_last'] = round(time.time() - last['timestamp'], 1)
                
                # Success rate history
                successes = sum(1 for h in history if h['success'])
                features['historical_success_rate'] = round(successes / len(history), 2)
                
                # Action sequence
                recent_sequence = [h['action'] for h in history[-5:]]
                features['recent_action_sequence'] = recent_sequence
                
                # Repeated action detection
                same_action_count = sum(1 for h in history if h['action'] == action)
                features['same_action_attempts'] = same_action_count
                features['is_retry'] = same_action_count > 0
            
            # Global action patterns
            recent = list(self.recent_actions)
            if recent:
                # Action distribution in recent history
                action_counts = defaultdict(int)
                for a in recent:
                    action_counts[a['action']] += 1
                
                features['most_common_recent_action'] = max(
                    action_counts.items(), 
                    key=lambda x: x[1]
                )[0] if action_counts else None
                
                # Global success rate
                global_successes = sum(1 for a in recent if a['success'])
                features['global_success_rate'] = round(
                    global_successes / len(recent), 2
                )
            
            # Time since first seen
            host = self.db.get_host_by_mac(mac)
            if host and host.get('first_seen'):
                ts = host['first_seen']
                if isinstance(ts, str):
                    try:
                        first_seen = datetime.fromisoformat(ts)
                    except ValueError:
                        first_seen = now
                elif isinstance(ts, datetime):
                    first_seen = ts
                else:
                    first_seen = now
                
                features['hours_since_discovery'] = round(
                    (now - first_seen).total_seconds() / 3600, 1
                )
            
        except Exception as e:
            logger.error(f"Error extracting temporal features: {e}")
        
        return features
    
    def _extract_action_features(self, action_name: str, raw_event: Dict) -> Dict[str, Any]:
        """
        Extract action-specific features.
        Auto-discovers relevant metadata from action execution.
        """
        features = {}
        
        try:
            features['action_name'] = action_name
            
            # Action type classification
            features['action_type'] = self._classify_action_type(action_name)
            
            # Port-specific actions
            port = raw_event.get('port')
            if port:
                features['target_port'] = int(port)
                features['is_standard_port'] = int(port) < 1024
            
            # Extract any additional metadata from raw event
            # This allows actions to add custom features
            if 'metadata' in raw_event:
                metadata = raw_event['metadata']
                if isinstance(metadata, dict):
                    # Flatten metadata into features
                    for key, value in metadata.items():
                        if isinstance(value, (int, float, bool, str)):
                            features[f'meta_{key}'] = value
            
            # Execution context
            features['operation_mode'] = self.shared_data.operation_mode
            
        except Exception as e:
            logger.error(f"Error extracting action features: {e}")
        
        return features
    
    # ═══════════════════════════════════════════════════════════════════════
    # HELPER METHODS
    # ═══════════════════════════════════════════════════════════════════════
    
    def _categorize_vendor(self, vendor: str) -> str:
        """Categorize vendor into high-level groups"""
        if not vendor:
            return 'unknown'
        
        vendor_lower = vendor.lower()
        
        categories = {
            'networking': ['cisco', 'juniper', 'ubiquiti', 'mikrotik', 'tp-link', 'netgear', 'asus', 'd-link', 'linksys'],
            'iot': ['hikvision', 'dahua', 'axis', 'hanwha', 'tuya', 'sonoff', 'shelly', 'xiaomi', 'yeelight'],
            'nas': ['synology', 'qnap', 'netapp', 'truenas', 'unraid'],
            'compute': ['raspberry', 'intel', 'apple', 'dell', 'hp', 'lenovo', 'acer'],
            'virtualization': ['vmware', 'microsoft', 'citrix', 'proxmox'],
            'mobile': ['apple', 'samsung', 'huawei', 'xiaomi', 'google', 'oneplus']
        }
        
        for category, vendors in categories.items():
            if any(v in vendor_lower for v in vendors):
                return category
        
        return 'other'
    
    def _is_private_ip(self, ip: str) -> bool:
        """Check if IP is in private range"""
        if not ip:
            return False
        
        parts = ip.split('.')
        if len(parts) != 4:
            return False
        
        try:
            first = int(parts[0])
            second = int(parts[1])
            
            return (
                first == 10 or
                (first == 172 and 16 <= second <= 31) or
                (first == 192 and second == 168)
            )
        except:
            return False
    
    def _detect_port_profile(self, ports: List[int]) -> str:
        """Auto-detect device type from port signature"""
        if not ports:
            return 'unknown'
        
        port_set = set(ports)
        
        profiles = {
            'camera': {554, 80, 8000, 37777},
            'web_server': {80, 443, 8080, 8443},
            'nas': {5000, 5001, 548, 139, 445},
            'database': {3306, 5432, 1433, 27017, 6379},
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
    
    def _get_services_for_host(self, mac: str) -> List[str]:
        """Get list of detected services for host"""
        try:
            results = self.db.query("""
                SELECT DISTINCT service 
                FROM port_services 
                WHERE mac_address=?
            """, (mac,))
            
            return [r['service'] for r in results if r.get('service')]
        except:
            return []
    
    def _extract_hostname_hints(self, hostname: str) -> List[str]:
        """Extract hints from hostname"""
        if not hostname:
            return []
        
        hints = []
        hostname_lower = hostname.lower()
        
        keywords = {
            'nas': ['nas', 'storage', 'diskstation'],
            'camera': ['cam', 'ipc', 'nvr', 'dvr'],
            'router': ['router', 'gateway', 'gw'],
            'server': ['server', 'srv', 'host'],
            'printer': ['printer', 'print'],
            'iot': ['iot', 'sensor', 'smart']
        }
        
        for hint, words in keywords.items():
            if any(word in hostname_lower for word in words):
                hints.append(hint)
        
        return hints
    
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
    
    def _guess_os(self, vendor: str, ports: List[int], hostnames: List[str]) -> str:
        """Guess OS from available indicators"""
        if not vendor and not ports and not hostnames:
            return 'unknown'
        
        vendor_lower = (vendor or '').lower()
        port_set = set(ports or [])
        hostname = hostnames[0].lower() if hostnames else ''
        
        # Strong indicators
        if 'microsoft' in vendor_lower or 3389 in port_set:
            return 'windows'
        if 'apple' in vendor_lower or 'mac' in hostname:
            return 'macos'
        if 'raspberry' in vendor_lower:
            return 'linux'
        
        # Port-based guessing
        if {22, 80} <= port_set:
            return 'linux'
        if {135, 139, 445} <= port_set:
            return 'windows'
        
        # Hostname hints
        if any(word in hostname for word in ['ubuntu', 'debian', 'centos', 'rhel']):
            return 'linux'
        
        return 'unknown'
    
    def _is_recently_active(self, last_seen: Optional[str]) -> bool:
        """Check if host was active in last 24h"""
        if not last_seen:
            return False
        
        try:
            if isinstance(last_seen, str):
                last_seen_dt = datetime.fromisoformat(last_seen)
            elif isinstance(last_seen, datetime):
                last_seen_dt = last_seen
            else:
                return False

            hours_ago = (datetime.now() - last_seen_dt).total_seconds() / 3600
            return hours_ago < 24
        except:
            return False
    
    def _classify_action_type(self, action_name: str) -> str:
        """Classify action into high-level categories"""
        action_lower = action_name.lower()
        
        if 'brute' in action_lower or 'crack' in action_lower:
            return 'bruteforce'
        elif 'scan' in action_lower or 'enum' in action_lower:
            return 'enumeration'
        elif 'exploit' in action_lower:
            return 'exploitation'
        elif 'dump' in action_lower or 'extract' in action_lower:
            return 'extraction'
        else:
            return 'other'
    
    # ═══════════════════════════════════════════════════════════════════════
    # FEATURE AGGREGATION & EXPORT
    # ═══════════════════════════════════════════════════════════════════════
    
    def get_feature_importance(self) -> List[Dict[str, Any]]:
        """
        AI-01: Return features sorted by variance from the ml_features_aggregated table.
        Features with higher variance carry more discriminative information.

        Returns:
            List of dicts: [{name, variance, sample_count}, ...] sorted by variance descending.
        """
        min_variance = float(
            getattr(self.shared_data, 'ai_feature_selection_min_variance', 0.001)
        )
        results = []
        try:
            rows = self.db.query(
                "SELECT feature_vector, total_actions FROM ml_features_aggregated"
            )
            if not rows:
                return results

            # Accumulate per-feature running stats (Welford-style via sum/sq/n)
            stats = {}  # {feature_name: [sum, sum_sq, count]}
            for row in rows:
                try:
                    vec = json.loads(row.get('feature_vector', '{}'))
                except Exception:
                    continue
                if not isinstance(vec, dict):
                    continue
                for name, value in vec.items():
                    try:
                        val = float(value)
                    except (TypeError, ValueError):
                        continue
                    if name not in stats:
                        stats[name] = [0.0, 0.0, 0]
                    s = stats[name]
                    s[0] += val
                    s[1] += val * val
                    s[2] += 1

            for name, (s, sq, n) in stats.items():
                if n < 2:
                    variance = 0.0
                else:
                    mean = s / n
                    variance = max(0.0, sq / n - mean * mean)
                results.append({
                    'name': name,
                    'variance': round(variance, 6),
                    'sample_count': n,
                    'above_threshold': variance >= min_variance,
                })

            results.sort(key=lambda x: x['variance'], reverse=True)
            logger.debug(f"Feature importance: {len(results)} features analyzed, "
                         f"{sum(1 for r in results if r['above_threshold'])} above threshold")

        except Exception as e:
            logger.error(f"Error computing feature importance: {e}")

        return results

    def get_stats(self) -> Dict[str, Any]:
        """Get current feature logging statistics"""
        try:
            total = self.db.query("SELECT COUNT(*) as cnt FROM ml_features")[0]['cnt']
            unconsolidated = self.db.query(
                "SELECT COUNT(*) as cnt FROM ml_features WHERE consolidated=0"
            )[0]['cnt']
            
            return {
                'total_features_logged': total,
                'unconsolidated_count': unconsolidated,
                'ready_for_export': unconsolidated,
                'recent_actions_buffer': len(self.recent_actions),
                'hosts_tracked': len(self.host_history)
            }
        except Exception as e:
            logger.error(f"Error getting feature stats: {e}")
            return {}


# ═══════════════════════════════════════════════════════════════════════════
# END OF FILE
# ═══════════════════════════════════════════════════════════════════════════
