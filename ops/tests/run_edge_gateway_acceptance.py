"""Own a disposable Linux/systemd container; no host mounts or public ports."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="prism-edge-gateway-test:20261030")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--external-config", type=Path, help="test a compiler-produced External CDN origin without gateway calls")
    args = parser.parse_args()
    assert hashlib.sha256(args.binary.read_bytes()).hexdigest() == args.binary_sha256
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    scope = "edge-gateway-" + uuid.uuid4().hex
    name = "prism-" + scope
    container = None
    ops = Path(__file__).resolve().parents[1]
    def run(*argv, timeout=90, log=None):
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if log:
            (output / log).write_bytes(result.stdout)
        if result.returncode:
            raise RuntimeError(f"Isolated command failed ({argv[0]}): " + result.stdout[-1600:].decode(errors="replace"))
        return result.stdout.decode()
    try:
        if args.build:
            run("docker", "build", "-f", str(ops / "tests/edge-gateway.Dockerfile"), "-t", args.image,
                str(ops / "tests"), timeout=600, log="build.log")
        image = run("docker", "image", "inspect", args.image, "--format", "{{.Id}}").strip()
        container = run("docker", "run", "-d", "--privileged", "--cgroupns=private", "--memory=1g", "--cpus=2",
            "--tmpfs", "/run", "--tmpfs", "/run/lock", "--tmpfs", "/tmp", "--label", "codex.scope=" + scope,
            "--name", name, args.image).strip()
        run("docker", "exec", container, "mkdir", "-p", "/opt/nodepulse-v2node")
        run("docker", "cp", str(ops), container + ":/opt/nodepulse-v2node/ops")
        run("docker", "cp", str(args.binary), container + ":/usr/local/bin/v2node")
        run("docker", "exec", container, "chmod", "755", "/usr/local/bin/v2node")
        if args.external_config:
            run("docker", "cp", str(args.external_config), container + ":/opt/external-origin.json")
            run("docker", "exec", container, "/opt/edge-test-venv/bin/python",
                "/opt/nodepulse-v2node/ops/tests/external_cdn_linux_acceptance.py", "/opt/external-origin.json", timeout=60, log="acceptance.log")
            for filename in ("result.json", "compiled-input.json"):
                run("docker", "cp", container + ":/opt/external-cdn-evidence/" + filename, str(output / filename))
            run("docker", "exec", container, "journalctl", "-u", "v2node-901.service", "--no-pager", log="xray-journal.log")
        else:
            run("docker", "exec", container, "python3", "-m", "pytest", "-q",
                "/opt/nodepulse-v2node/ops/tests/test_edge_gateway.py", "/opt/nodepulse-v2node/ops/tests/test_node_listener.py",
                "/opt/nodepulse-v2node/ops/tests/test_installer_contract.py", log="unit-tests.log")
            run("docker", "exec", container, "python3", "/opt/nodepulse-v2node/ops/tests/test_installer_contract.py", log="installer-contract.log")
            run("docker", "exec", container, "/opt/edge-test-venv/bin/python",
                "/opt/nodepulse-v2node/ops/tests/edge_gateway_linux_acceptance.py", timeout=180, log="acceptance.log")
            for filename in ("result.json", "state.json", "socket-owners.json", "node-installer.log", "edge-installer.log"):
                run("docker", "cp", container + ":/opt/edge-gateway-evidence/" + filename, str(output / filename))
            run("docker", "cp", container + ":/opt/edge-gateway-evidence/release/nodepulse-edge-gateway.tar.gz", str(output / "tested-runtime.tar.gz"))
            run("docker", "cp", container + ":/var/lib/nodepulse-edge-control/main/audit.jsonl", str(output / "audit.jsonl"))
            run("docker", "exec", container, "journalctl", "-u", "nodepulse-edge-main.service", "--no-pager", log="caddy-journal.log")
        result = json.loads((output / "result.json").read_text())
        assert result["passed"]
        (output / "provenance.json").write_text(json.dumps({"image": image, "container_id": container, "scope": scope,
            "binary_sha256": args.binary_sha256, "host_mounts": False, "published_ports": False,
            "source_files": {str(path.relative_to(ops)): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in ops.rglob("*") if path.is_file() and path.suffix in {".py", ".sh", ".Dockerfile"}}}, indent=2))
        print("EXTERNAL_ORIGIN_ACCEPTANCE passed: compiler output -> real Xray TLS/H2, Caddy stopped, no gateway calls" if args.external_config else
              "EDGE_GATEWAY_ACCEPTANCE passed: real Caddy, systemd, Xray, H2/H3, shared routes, scoped stop and installers")
    finally:
        if container:
            label = run("docker", "inspect", "--format", '{{index .Config.Labels "codex.scope"}}', container).strip()
            if label != scope:
                raise RuntimeError("Container ownership changed; refusing cleanup")
            run("docker", "rm", "-f", container)
            (output / "cleanup.json").write_text(json.dumps({"owned_container_removed": True, "container_id": container}))


if __name__ == "__main__":
    main()
