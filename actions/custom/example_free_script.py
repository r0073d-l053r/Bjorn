"""example_free_script.py - Custom script template using plain Python (no shared_data)."""

import argparse
import time
import sys

# ---- Display metadata (optional, used by the web UI) ----
b_name = "Example Free Script"
b_description = "Standalone Python script demo with argparse and progress output."
b_author = "Bjorn Community"
b_version = "1.0.0"
b_tags = '["custom", "example", "template", "free"]'

# ---- Argument schema (drives the web UI controls, same format as Bjorn actions) ----
b_args = {
    "target": {
        "type": "text",
        "default": "192.168.1.0/24",
        "description": "Target host or CIDR range"
    },
    "timeout": {
        "type": "number",
        "default": 5,
        "min": 1,
        "max": 60,
        "description": "Timeout per probe in seconds"
    },
    "output_format": {
        "type": "select",
        "choices": ["text", "json", "csv"],
        "default": "text",
        "description": "Output format"
    },
    "dry_run": {
        "type": "checkbox",
        "default": False,
        "description": "Simulate without actually probing"
    }
}

b_examples = [
    {"name": "Quick local check", "args": {"target": "192.168.1.1", "timeout": 2, "output_format": "text"}},
    {"name": "Dry run JSON", "args": {"target": "10.0.0.0/24", "timeout": 5, "output_format": "json", "dry_run": True}},
]


def main():
    parser = argparse.ArgumentParser(description="Example free-form Bjorn custom script")
    parser.add_argument("--target", default="192.168.1.0/24", help="Target host or CIDR")
    parser.add_argument("--timeout", type=int, default=5, help="Timeout per probe (seconds)")
    parser.add_argument("--output-format", default="text", choices=["text", "json", "csv"])
    parser.add_argument("--dry-run", action="store_true", help="Simulate without probing")
    args = parser.parse_args()

    print(f"[*] Example Free Script starting")
    print(f"[*] Target: {args.target}")
    print(f"[*] Timeout: {args.timeout}s")
    print(f"[*] Format: {args.output_format}")
    print(f"[*] Dry run: {args.dry_run}")
    print()

    # Simulate some work with progress output
    steps = 5
    for i in range(steps):
        print(f"[*] Step {i+1}/{steps}: {'simulating' if args.dry_run else 'probing'} {args.target}...")
        time.sleep(1)

    # Example output in different formats
    results = [
        {"host": "192.168.1.1", "status": "up", "latency": "2ms"},
        {"host": "192.168.1.100", "status": "up", "latency": "5ms"},
    ]

    if args.output_format == "json":
        import json
        print(json.dumps(results, indent=2))
    elif args.output_format == "csv":
        print("host,status,latency")
        for r in results:
            print(f"{r['host']},{r['status']},{r['latency']}")
    else:
        for r in results:
            print(f"  {r['host']}  {r['status']}  ({r['latency']})")

    print()
    print(f"[+] Done. Found {len(results)} hosts.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Interrupted")
        sys.exit(130)
    except Exception as e:
        print(f"\n[!] Error: {e}")
        sys.exit(1)
