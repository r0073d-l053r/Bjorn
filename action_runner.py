"""action_runner.py - Generic subprocess wrapper for running Bjorn actions from the web UI."""

import sys
import os
import signal
import importlib
import argparse
import traceback


def _inject_extra_args(shared_data, remaining):
    """Parse leftover --key value pairs and set them as shared_data attributes."""
    i = 0
    while i < len(remaining):
        token = remaining[i]
        if token.startswith("--"):
            key = token[2:].replace("-", "_")
            if i + 1 < len(remaining) and not remaining[i + 1].startswith("--"):
                val = remaining[i + 1]
                # Auto-cast numeric values
                try:
                    val = int(val)
                except ValueError:
                    try:
                        val = float(val)
                    except ValueError:
                        pass
                setattr(shared_data, key, val)
                i += 2
            else:
                setattr(shared_data, key, True)
                i += 1
        else:
            i += 1


def main():
    parser = argparse.ArgumentParser(
        description="Bjorn Action Runner - bootstraps shared_data and calls action.execute()"
    )
    parser.add_argument("b_module", help="Action module name (e.g. ssh_bruteforce)")
    parser.add_argument("b_class", help="Action class name (e.g. SSHBruteforce)")
    parser.add_argument("--ip", default="", help="Target IP address")
    parser.add_argument("--port", default="", help="Target port")
    parser.add_argument("--mac", default="", help="Target MAC address")

    args, remaining = parser.parse_known_args()

    # Bootstrap shared_data (creates fresh DB conn, loads config)
    print(f"[runner] Loading shared_data for {args.b_class}...")
    from init_shared import shared_data

    # Graceful shutdown on SIGTERM (user clicks Stop in the UI)
    def _sigterm(signum, frame):
        print("[runner] SIGTERM received, requesting graceful stop...")
        shared_data.orchestrator_should_exit = True

    signal.signal(signal.SIGTERM, _sigterm)

    # Inject extra CLI flags as shared_data attributes
    # e.g. --berserker-mode tcp -> shared_data.berserker_mode = "tcp"
    _inject_extra_args(shared_data, remaining)

    # Dynamic import (custom/ paths use dots: actions.custom.my_script)
    module_path = f"actions.{args.b_module.replace('/', '.')}"
    print(f"[runner] Importing {module_path}...")
    module = importlib.import_module(module_path)
    action_class = getattr(module, args.b_class)

    # Instantiate with shared_data (same as orchestrator)
    action_instance = action_class(shared_data)

    # Resolve MAC from DB if not provided
    mac = args.mac
    if not mac and args.ip:
        try:
            rows = shared_data.db.query(
                "SELECT \"MAC Address\" FROM hosts WHERE IPs = ? LIMIT 1",
                (args.ip,)
            )
            if rows:
                mac = rows[0].get("MAC Address", "") or ""
        except Exception:
            mac = ""

    # Build row dict (matches orchestrator.py:609-614)
    ip = args.ip or ""
    port = args.port or ""
    row = {
        "MAC Address": mac or "",
        "IPs": ip,
        "Ports": port,
        "Alive": 1,
    }

    # Execute
    print(f"[runner] Executing {args.b_class} on {ip or 'global'}:{port}...")

    if hasattr(action_instance, "scan") and not ip:
        # Global action (e.g. NetworkScanner)
        action_instance.scan()
        result = "success"
    else:
        if not ip:
            print(f"[runner] ERROR: {args.b_class} requires --ip but none provided")
            sys.exit(1)
        result = action_instance.execute(ip, port, row, args.b_class)

    print(f"[runner] Finished with result: {result}")
    sys.exit(0 if result == "success" else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[runner] Interrupted")
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(2)
