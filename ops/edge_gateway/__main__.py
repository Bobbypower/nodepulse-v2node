import argparse
import json
import os
from pathlib import Path
import sys

from .contract import GatewayError, compile_caddy, normalize_plan


def main():
    parser = argparse.ArgumentParser(description="Manage only NodePulse-owned Edge Gateway instances")
    parser.add_argument("--instance", default="main")
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview")
    preview.add_argument("--plan", required=True)
    init = commands.add_parser("initialize")
    init.add_argument("--binary", type=Path, required=True)
    init.add_argument("--sha256", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--expected-revision", type=int, required=True)
    apply.add_argument("--retry-failed", action="store_true")
    stop = commands.add_parser("stop")
    stop.add_argument("--expected-revision", type=int, required=True)
    stop.add_argument("--all-routes", action="store_true")
    stop.add_argument("--disable", action="store_true")
    recover = commands.add_parser("recover")
    recover.add_argument("--expected-revision", type=int, required=True)
    commands.add_parser("status")
    enable = commands.add_parser("enable")
    enable.add_argument("--expected-revision", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.command == "preview":
            plan = normalize_plan(json.loads(Path(args.plan).read_bytes()))
            result = {"state": "planned", "plan": plan, "caddy": compile_caddy(plan), "end_to_end_verified": False}
        else:
            if sys.platform != "linux" or os.getuid() != 0:
                raise GatewayError("requires_linux_root")
            from .linux import LinuxSystem
            from .manager import GatewayManager
            manager = GatewayManager(LinuxSystem(args.instance))
            if args.command == "initialize":
                result = manager.initialize(args.binary, args.sha256)
            elif args.command == "apply":
                result = manager.apply(json.loads(Path(args.plan).read_bytes()), args.expected_revision, retry_failed=args.retry_failed)
            elif args.command == "stop":
                result = manager.stop(args.expected_revision, all_routes=args.all_routes, disable=args.disable)
            elif args.command == "recover":
                result = manager.recover(args.expected_revision)
            elif args.command == "enable":
                result = manager.enable(args.expected_revision)
            else:
                result = manager.status()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["state"] in {"planned", "initialized", "owned", "applied", "stopped", "running", "enabled"} else 1
    except GatewayError as error:
        print(json.dumps({"state": "conflict" if "conflict" in error.code else "blocked", "error": error.code}))
        return 1
    except Exception:
        print(json.dumps({"state": "degraded", "error": "operation_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
