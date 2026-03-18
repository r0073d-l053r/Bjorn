"""ai_utils.py - Shared feature extraction and encoding helpers for the AI engine."""

import json
import numpy as np
from typing import Dict, List, Any, Optional

def extract_neural_features_dict(host_features: Dict[str, Any], network_features: Dict[str, Any], temporal_features: Dict[str, Any], action_features: Dict[str, Any]) -> Dict[str, float]:
    """
    Extracts all available features as a named dictionary.
    This allows the model to select exactly what it needs by name.
    """
    f = {}
    
    # 1. Host numericals
    f['host_port_count'] = float(host_features.get('port_count', 0))
    f['host_service_count'] = float(host_features.get('service_count', 0))
    f['host_ip_count'] = float(host_features.get('ip_count', 0))
    f['host_credential_count'] = float(host_features.get('credential_count', 0))
    f['host_age_hours'] = float(host_features.get('age_hours', 0))
    
    # 2. Host Booleans
    f['has_ssh'] = 1.0 if host_features.get('has_ssh') else 0.0
    f['has_http'] = 1.0 if host_features.get('has_http') else 0.0
    f['has_https'] = 1.0 if host_features.get('has_https') else 0.0
    f['has_smb'] = 1.0 if host_features.get('has_smb') else 0.0
    f['has_rdp'] = 1.0 if host_features.get('has_rdp') else 0.0
    f['has_database'] = 1.0 if host_features.get('has_database') else 0.0
    f['has_credentials'] = 1.0 if host_features.get('has_credentials') else 0.0
    f['is_new'] = 1.0 if host_features.get('is_new') else 0.0
    f['is_private'] = 1.0 if host_features.get('is_private') else 0.0
    f['has_multiple_ips'] = 1.0 if host_features.get('has_multiple_ips') else 0.0
    
    # 3. Vendor Category (One-Hot)
    vendor_cats = ['networking', 'iot', 'nas', 'compute', 'virtualization', 'mobile', 'other', 'unknown']
    current_vendor = host_features.get('vendor_category', 'unknown')
    for cat in vendor_cats:
        f[f'vendor_is_{cat}'] = 1.0 if cat == current_vendor else 0.0
    
    # 4. Port Profile (One-Hot)
    port_profiles = ['camera', 'web_server', 'nas', 'database', 'linux_server', 
                    'windows_server', 'printer', 'router', 'generic', 'unknown']
    current_profile = host_features.get('port_profile', 'unknown')
    for prof in port_profiles:
        f[f'profile_is_{prof}'] = 1.0 if prof == current_profile else 0.0
    
    # 5. Network Stats
    f['net_total_hosts'] = float(network_features.get('total_hosts', 0))
    f['net_subnet_count'] = float(network_features.get('subnet_count', 0))
    f['net_similar_vendor_count'] = float(network_features.get('similar_vendor_count', 0))
    f['net_similar_port_profile_count'] = float(network_features.get('similar_port_profile_count', 0))
    f['net_active_host_ratio'] = float(network_features.get('active_host_ratio', 0.0))
    
    # 6. Temporal features
    f['time_hour'] = float(temporal_features.get('hour_of_day', 0))
    f['time_day'] = float(temporal_features.get('day_of_week', 0))
    f['is_weekend'] = 1.0 if temporal_features.get('is_weekend') else 0.0
    f['is_night'] = 1.0 if temporal_features.get('is_night') else 0.0
    f['hist_action_count'] = float(temporal_features.get('previous_action_count', 0))
    f['hist_seconds_since_last'] = float(temporal_features.get('seconds_since_last', 0))
    f['hist_success_rate'] = float(temporal_features.get('historical_success_rate', 0.0))
    f['hist_same_attempts'] = float(temporal_features.get('same_action_attempts', 0))
    f['is_retry'] = 1.0 if temporal_features.get('is_retry') else 0.0
    f['global_success_rate'] = float(temporal_features.get('global_success_rate', 0.0))
    f['hours_since_discovery'] = float(temporal_features.get('hours_since_discovery', 0))
    
    # 7. Action Info
    action_types = ['bruteforce', 'enumeration', 'exploitation', 'extraction', 'other']
    current_type = action_features.get('action_type', 'other')
    for atype in action_types:
        f[f'action_is_{atype}'] = 1.0 if atype == current_type else 0.0
        
    f['action_target_port'] = float(action_features.get('target_port', 0))
    f['action_is_standard_port'] = 1.0 if action_features.get('is_standard_port') else 0.0
    
    return f

def extract_neural_features(host_features: Dict[str, Any], network_features: Dict[str, Any], temporal_features: Dict[str, Any], action_features: Dict[str, Any]) -> List[float]:
    """
    Deprecated: Hardcoded list. Use extract_neural_features_dict for evolution.
    Kept for backward compatibility during transition.
    """
    d = extract_neural_features_dict(host_features, network_features, temporal_features, action_features)
    # Return as a list in a fixed order (the one previously used)
    # This is fragile and will be replaced by manifest-based extraction.
    return list(d.values())

def get_system_mac() -> str:
    """
    Get the persistent MAC address of the device.
    Used for unique identification in Swarm mode.
    """
    try:
        import uuid
        mac = uuid.getnode()
        return ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))
    except:
        return "00:00:00:00:00:00"
