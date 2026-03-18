"""demo_action.py - Minimal action template that just prints received arguments."""
b_class       = "DemoAction"
b_module      = "demo_action"
b_enabled     = 1
b_action      = "normal"           # normal | aggressive | stealth
b_category    = "demo"
b_name        = "Demo Action"
b_description = "Demonstration action: simply prints the received arguments."
b_author      = "Template"
b_version     = "0.1.0"
b_icon        = "demo_action.png"

b_examples = [
    {
        "profile": "quick",
        "interface": "auto",
        "target": "192.168.1.10",
        "port": 80,
        "protocol": "tcp",
        "verbose": True,
        "timeout": 30,
        "concurrency": 2,
        "notes": "Quick HTTP scan"
    },
    {
        "profile": "deep",
        "interface": "eth0",
        "target": "example.org",
        "port": 443,
        "protocol": "tcp",
        "verbose": False,
        "timeout": 120,
        "concurrency": 8,
        "notes": "Deep TLS profile"
    }
]

b_docs_url = "docs/actions/DemoAction.md"

# ---------------------------------------------------------------------------
# UI argument schema
# ---------------------------------------------------------------------------
b_args = {
    "profile": {
        "type": "select",
        "label": "Profile",
        "choices": ["quick", "balanced", "deep"],
        "default": "balanced",
        "help": "Choose a profile: speed vs depth."
    },
    "interface": {
        "type": "select",
        "label": "Network Interface",
        "choices": [],
        "default": "auto",
        "help": "'auto' tries to detect the default network interface."
    },
    "target": {
        "type": "text",
        "label": "Target (IP/Host)",
        "default": "192.168.1.1",
        "placeholder": "e.g. 192.168.1.10 or example.org",
        "help": "Main target."
    },
    "port": {
        "type": "number",
        "label": "Port",
        "min": 1,
        "max": 65535,
        "step": 1,
        "default": 80
    },
    "protocol": {
        "type": "select",
        "label": "Protocol",
        "choices": ["tcp", "udp"],
        "default": "tcp"
    },
    "verbose": {
        "type": "checkbox",
        "label": "Verbose output",
        "default": False
    },
    "timeout": {
        "type": "slider",
        "label": "Timeout (seconds)",
        "min": 5,
        "max": 600,
        "step": 5,
        "default": 60
    },
    "concurrency": {
        "type": "range",
        "label": "Concurrency",
        "min": 1,
        "max": 32,
        "step": 1,
        "default": 4,
        "help": "Number of parallel tasks (demo only)."
    },
    "notes": {
        "type": "text",
        "label": "Notes",
        "default": "",
        "placeholder": "Free-form comments",
        "help": "Free text field to demonstrate a simple string input."
    }
}

# ---------------------------------------------------------------------------
# Dynamic detection of interfaces
# ---------------------------------------------------------------------------
import os
try:
    import psutil
except Exception:
    psutil = None


def _list_net_ifaces() -> list[str]:
    names = set()
    if psutil:
        try:
            names.update(ifname for ifname in psutil.net_if_addrs().keys() if ifname != "lo")
        except Exception:
            pass
    try:
        for n in os.listdir("/sys/class/net"):
            if n and n != "lo":
                names.add(n)
    except Exception:
        pass
    out = ["auto"] + sorted(names)
    seen, unique = set(), []
    for x in out:
        if x not in seen:
            unique.append(x)
            seen.add(x)
    return unique


def compute_dynamic_b_args(base: dict) -> dict:
    d = dict(base or {})
    if "interface" in d:
        d["interface"]["choices"] = _list_net_ifaces() or ["auto", "eth0", "wlan0"]
        if d["interface"].get("default") not in d["interface"]["choices"]:
            d["interface"]["default"] = "auto"
    return d


# ---------------------------------------------------------------------------
# DemoAction class
# ---------------------------------------------------------------------------
import argparse


class DemoAction:
    """Wrapper called by the orchestrator."""

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.meta = {
            "class": b_class,
            "module": b_module,
            "enabled": b_enabled,
            "action": b_action,
            "category": b_category,
            "name": b_name,
            "description": b_description,
            "author": b_author,
            "version": b_version,
            "icon": b_icon,
            "examples": b_examples,
            "docs_url": b_docs_url,
            "args_schema": b_args,
        }

    def execute(self, ip=None, port=None, row=None, status_key=None):
        """Called by the orchestrator. This demo only prints arguments."""
        self.shared_data.bjorn_orch_status = "DemoAction"
        self.shared_data.comment_params = {"ip": ip, "port": port}

        print("=== DemoAction :: executed ===")
        print(f" IP/Target: {ip}:{port}")
        print(f" Row: {row}")
        print(f" Status key: {status_key}")
        print("No real action performed: demonstration only.")
        return "success"

    def run(self, argv=None):
        """Standalone CLI mode for testing."""
        parser = argparse.ArgumentParser(description=b_description)
        parser.add_argument("--profile", choices=b_args["profile"]["choices"],
                            default=b_args["profile"]["default"])
        parser.add_argument("--interface", default=b_args["interface"]["default"])
        parser.add_argument("--target", default=b_args["target"]["default"])
        parser.add_argument("--port", type=int, default=b_args["port"]["default"])
        parser.add_argument("--protocol", choices=b_args["protocol"]["choices"],
                            default=b_args["protocol"]["default"])
        parser.add_argument("--verbose", action="store_true",
                            default=bool(b_args["verbose"]["default"]))
        parser.add_argument("--timeout", type=int, default=b_args["timeout"]["default"])
        parser.add_argument("--concurrency", type=int, default=b_args["concurrency"]["default"])
        parser.add_argument("--notes", default=b_args["notes"]["default"])

        args = parser.parse_args(argv)

        print("=== DemoAction :: received parameters ===")
        for k, v in vars(args).items():
            print(f" {k:11}: {v}")

        print("\n=== Demo usage of parameters ===")
        if args.verbose:
            print("[verbose] Verbose mode enabled → simulated detailed logs...")

        if args.profile == "quick":
            print("Profile: quick → would perform fast operations.")
        elif args.profile == "deep":
            print("Profile: deep → would perform longer, more thorough operations.")
        else:
            print("Profile: balanced → compromise between speed and depth.")

        print(f"Target: {args.target}:{args.port}/{args.protocol} via {args.interface}")
        print(f"Timeout: {args.timeout} sec, Concurrency: {args.concurrency}")
        print("No real action performed: demonstration only.")


if __name__ == "__main__":
    DemoAction(shared_data=None).run()
