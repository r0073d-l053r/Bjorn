"""orchestrator_utils.py - Attack execution, scanning, and credential management."""
from __future__ import annotations
import json
import html
import importlib
import threading
from datetime import datetime
from typing import Any, Dict, Optional
import logging
from logger import Logger
logger = Logger(name="orchestrator_utils.py", level=logging.DEBUG)

class OrchestratorUtils:
    """Utilities for orchestrator and attack management."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data
        # ORCH-03: Background scan timer for MANUAL mode
        self._scan_timer = None
        self._scan_stop_event = threading.Event()

    def execute_manual_attack(self, params):
        """Execute a manual attack on a specific target."""
        try:
            ip = params['ip']
            port = params['port']
            action_class = params['action']
            self.shared_data.bjorn_status_text2 = ""

            self.logger.info(f"Received request to execute {action_class} on {ip}:{port}")

            # Load actions
            self._load_actions()
            action_instance = next((action for action in self.shared_data.actions if action.action_name == action_class), None)
            if action_instance is None:
                raise Exception(f"Action class {action_class} not found")

            current_data = self.shared_data.read_data()
            row = next((r for r in current_data if r["IPs"] == ip), None)
            if row is None:
                raise Exception(f"No data found for IP: {ip}")

            action_key = action_instance.action_name
            self.logger.info(f"Executing [MANUAL]: {action_key} on {ip}:{port}")
            result = action_instance.execute(ip, port, row, action_key)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if result == 'success':
                row[action_key] = f'success_{timestamp}'
                self.logger.info(f"Action {action_key} executed successfully on {ip}:{port}")
            else:
                row[action_key] = f'failed_{timestamp}'
                self.logger.error(f"Action {action_key} failed on {ip}:{port}")
            self.shared_data.write_data(current_data)

            # Update status after completion
            self.shared_data.bjorn_orch_status = "IDLE"
            self.shared_data.bjorn_status_text2 = "Waiting for instructions..."

            return {"status": "success", "message": "Manual attack executed"}

        except Exception as e:
            self.logger.error(f"Error executing manual attack: {e}")
            self.shared_data.bjorn_orch_status = "IDLE"
            self.shared_data.bjorn_status_text2 = "Waiting for instructions..."
            return {"status": "error", "message": str(e)}

    def execute_manual_scan(self):
        """Execute a manual network scan."""
        try:
            # Import network scanner
            module = importlib.import_module('actions.scanning')
            scanner_class = getattr(module, getattr(module, 'b_class'))
            network_scanner = scanner_class(self.shared_data)

            # Update status
            self.shared_data.bjorn_orch_status = "NetworkScanner"
            self.shared_data.bjorn_status_text2 = "Manual scan..."
            
            # Execute scan
            network_scanner.scan()
            
            # Reset status
            self.shared_data.bjorn_orch_status = "IDLE"
            self.shared_data.bjorn_status_text2 = "Waiting for instructions..."

            return {"status": "success", "message": "Network scan completed"}

        except Exception as e:
            self.logger.error(f"Error executing manual scan: {e}")
            self.shared_data.bjorn_orch_status = "IDLE"
            self.shared_data.bjorn_status_text2 = "Waiting for instructions..."
            return {"status": "error", "message": str(e)}

    def start_orchestrator(self):
        """Start the orchestrator."""
        try:
            # ORCH-03: Stop background scan timer when switching to AUTO/AI
            self._stop_scan_timer()

            bjorn_instance = self.shared_data.bjorn_instance
            if getattr(self.shared_data, "ai_mode", False):
                self.shared_data.operation_mode = "AI"
            else:
                self.shared_data.operation_mode = "AUTO"
            self.shared_data.orchestrator_should_exit = False
            bjorn_instance.start_orchestrator()
            return {"status": "success", "message": "Orchestrator starting..."}
        except Exception as e:
            self.logger.error(f"Error starting orchestrator: {e}")
            return {"status": "error", "message": str(e)}

    def stop_orchestrator(self):
        """Stop the orchestrator and reset all status fields to IDLE."""
        try:
            bjorn_instance = self.shared_data.bjorn_instance
            self.shared_data.operation_mode = "MANUAL"
            bjorn_instance.stop_orchestrator()
            self.shared_data.orchestrator_should_exit = True
            # Explicit reset so the web UI reflects IDLE immediately,
            # even if the orchestrator thread is still finishing up.
            self.shared_data.bjorn_orch_status = "IDLE"
            self.shared_data.bjorn_status_text = "IDLE"
            self.shared_data.bjorn_status_text2 = "Waiting for instructions..."
            self.shared_data.action_target_ip = ""
            self.shared_data.active_action = None
            self.shared_data.update_status("IDLE", "")

            # ORCH-03: Start background scan timer if enabled
            if getattr(self.shared_data, 'manual_mode_auto_scan', True):
                self._start_scan_timer()

            return {"status": "success", "message": "Orchestrator stopped"}
        except Exception as e:
            self.logger.error(f"Error stopping orchestrator: {e}")
            return {"status": "error", "message": str(e)}

    # =========================================================================
    # ORCH-03: Background scan timer for MANUAL mode
    # =========================================================================

    def _start_scan_timer(self):
        """Start a background thread that periodically scans in MANUAL mode."""
        if self._scan_timer and self._scan_timer.is_alive():
            return
        self._scan_stop_event.clear()
        self._scan_timer = threading.Thread(
            target=self._scan_loop, daemon=True, name="ManualModeScanTimer"
        )
        self._scan_timer.start()
        self.logger.info("ORCH-03: Background scan timer started for MANUAL mode")

    def _scan_loop(self):
        """Periodically run network scan while in MANUAL mode."""
        interval = int(getattr(self.shared_data, 'manual_mode_scan_interval', 180))
        while not self._scan_stop_event.wait(timeout=interval):
            if self.shared_data.operation_mode != "MANUAL":
                self.logger.info("ORCH-03: Exiting scan timer, no longer in MANUAL mode")
                break
            try:
                self.logger.info("ORCH-03: Manual mode background scan starting")
                self.execute_manual_scan()
            except Exception as e:
                self.logger.error(f"ORCH-03: Background scan error: {e}")

    def _stop_scan_timer(self):
        """Stop the background scan timer."""
        if self._scan_timer:
            self._scan_stop_event.set()
            self._scan_timer.join(timeout=5)
            self._scan_timer = None
            self.logger.debug("ORCH-03: Background scan timer stopped")

    def serve_credentials_data(self, handler):
        """Serve credentials data as HTML."""
        try:
            creds = self.shared_data.db.list_creds_grouped()
            html_content = self._html_from_creds(creds)
            handler.send_response(200)
            handler.send_header("Content-type", "text/html")
            handler.end_headers()
            handler.wfile.write(html_content.encode("utf-8"))
        except Exception as e:
            handler.send_response(500)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))

    def _html_from_creds(self, rows):
        """Generate HTML table from credentials data."""
        out = ['<div class="credentials-container">']
        
        # Group by service
        by_service = {}
        for r in rows:
            by_service.setdefault(r["service"], []).append(r)
        
        for svc, items in by_service.items():
            out.append(f"<h2>{html.escape(svc)}.db</h2>")
            out.append('<table class="styled-table"><thead><tr>')
            for h in ["MAC", "IP", "Hostname", "User", "Password", "Port", "Database", "Last Seen"]:
                out.append(f"<th>{h}</th>")
            out.append("</tr></thead><tbody>")
            
            for r in items:
                out.append("<tr>")
                out.append(f"<td>{html.escape(r.get('mac_address') or '')}</td>")
                out.append(f"<td>{html.escape(r.get('ip') or '')}</td>")
                out.append(f"<td>{html.escape(r.get('hostname') or '')}</td>")
                out.append(f"<td>{html.escape(r.get('user') or '')}</td>")
                out.append(f"<td>{html.escape(r.get('password') or '')}</td>")
                out.append(f"<td>{html.escape(str(r.get('port') or ''))}</td>")
                out.append(f"<td>{html.escape(r.get('database') or '')}</td>")
                out.append(f"<td>{html.escape(r.get('last_seen') or '')}</td>")
                out.append("</tr>")
            
            out.append("</tbody></table>")
        
        out.append("</div>")
        return "\n".join(out)

    def _load_actions(self):
        """Load actions from database."""
        if self.shared_data.actions is None or self.shared_data.standalone_actions is None:
            self.shared_data.actions, self.shared_data.standalone_actions = [], []
            for action in self.shared_data.db.list_actions():
                module_name = action["b_module"]
                if module_name == 'scanning':
                    self._load_scanner(module_name)
                else:
                    self._load_action(module_name, action)

    def _load_scanner(self, module_name):
        """Load the network scanner."""
        module = importlib.import_module(f'actions.{module_name}')
        b_class = getattr(module, 'b_class')
        self.shared_data.network_scanner = getattr(module, b_class)(self.shared_data)

    def _load_action(self, module_name, action):
        """Load an action from the actions directory."""
        module = importlib.import_module(f'actions.{module_name}')
        try:
            b_class = action["b_class"]
            action_instance = getattr(module, b_class)(self.shared_data)
            action_instance.action_name = b_class
            action_instance.port = action.get("b_port")
            action_instance.b_parent_action = action.get("b_parent")
            if action_instance.port == 0:
                self.shared_data.standalone_actions.append(action_instance)
            else:
                self.shared_data.actions.append(action_instance)
        except AttributeError as e:
            self.logger.error(f"Module {module_name} is missing required attributes: {e}")
