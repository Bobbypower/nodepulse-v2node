from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "ops" / "install-nodepulse-v2node.sh"
def run_validator(config: dict[str, object], node_port: int) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temp_dir:
        directory = Path(temp_dir)
        (directory / "server.json").write_text(json.dumps(config), encoding="utf-8")
        (directory / "local.json").write_text(json.dumps({"Nodes": [{"NodeID": 1}]}), encoding="utf-8")
        return subprocess.run([sys.executable, "-c",
            "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('edge_gateway.node_listener',run_name='__main__')",
            str(ROOT / "ops"), "compile", "--node", "1", "--server", str(directory / "server.json"),
            "--local", str(directory / "local.json"), "--requested-port", str(node_port)],
            capture_output=True, text=True, check=False)


def split_config(
    primary_port: int = 443, download_port: int = 80
) -> dict[str, object]:
    return {
        "server_port": primary_port,
        "network": "xhttp",
        "network_settings": {"path": "/same", "mode": "stream-up"},
        "xhttp_download_inbound": {
            "server_port": download_port,
            "network": "xhttp",
            "security": "none",
            "network_settings": {"path": "/same", "mode": "auto"},
        },
    }


def main() -> None:
    validators = [run_validator]
    for validator in validators:
        valid = validator(split_config(), 443)
        assert valid.returncode == 0, valid.stderr
        assert valid.stdout.splitlines() == ["tcp:443", "tcp:80"]

        backend_ports = validator(split_config(8443, 8080), 8443)
        assert backend_ports.returncode == 0, backend_ports.stderr
        assert backend_ports.stdout.splitlines() == ["tcp:8443", "tcp:8080"]

        h3 = split_config(23333, 80)
        h3.pop("xhttp_download_inbound")
        h3["protocol"] = "vless"
        h3["tls"] = 1
        h3["tls_settings"] = {"alpn": ["h3"]}
        valid_h3 = validator(h3, 23333)
        assert valid_h3.returncode == 0, valid_h3.stderr
        assert valid_h3.stdout.splitlines() == ["udp:23333"]

        mixed_alpn = dict(h3)
        mixed_alpn["tls_settings"] = {"alpn": ["h2", "h3"]}
        valid_mixed_alpn = validator(mixed_alpn, 23333)
        assert valid_mixed_alpn.returncode != 0
        assert "invalid_direct_h3" in valid_mixed_alpn.stderr

        hysteria = {
            "protocol": "hysteria2",
            "server_port": 24444,
            "network": "udp",
        }
        valid_hysteria = validator(hysteria, 24444)
        assert valid_hysteria.returncode == 0, valid_hysteria.stderr
        assert valid_hysteria.stdout.splitlines() == ["udp:24444"]

        mismatched_path = split_config()
        download = dict(mismatched_path["xhttp_download_inbound"])
        download["network_settings"] = {"path": "/different", "mode": "auto"}
        mismatched_path["xhttp_download_inbound"] = download
        invalid_path = validator(mismatched_path, 443)
        assert invalid_path.returncode != 0
        assert "same_normalized_path" in invalid_path.stderr

        invalid_port = validator(split_config(), 8443)
        assert invalid_port.returncode != 0
        assert "does_not_match" in invalid_port.stderr

        invalid_security = split_config()
        insecure_download = dict(invalid_security["xhttp_download_inbound"])
        insecure_download["security"] = "tls"
        invalid_security["xhttp_download_inbound"] = insecure_download
        rejected_security = validator(invalid_security, 443)
        assert rejected_security.returncode != 0
        assert "security_must_be_none" in rejected_security.stderr

        invalid_primary = split_config()
        invalid_primary["network"] = "tcp"
        rejected_primary = validator(invalid_primary, 443)
        assert rejected_primary.returncode != 0
        assert "invalid_download_network" in rejected_primary.stderr

    print("installer contract tests passed")


def test_installer_serializes_shared_binary_updates() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert 'LOCK_PATH="/run/lock/nodepulse-v2node-deploy.lock"' in source
    assert "flock -n 9" in source
    assert "exit 75" in source


def test_installer_downloads_with_resume_and_retries() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert '--retry-all-errors' in source
    assert '--continue-at -' in source
    assert 'local partial="${output}.part"' in source
    assert 'mv -f -- "${partial}" "${output}"' in source


def test_installer_uses_unique_backups_and_atomic_replacement() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert 'mktemp -d "/root/nodepulse-backups/${SERVICE_NAME}-${backup_stamp}.XXXXXX"' in source
    assert 'stage="$(mktemp "${target_dir}/.$(basename "${target}").new.XXXXXX")"' in source
    assert 'mv -fT -- "${stage}" "${target}"' in source
    assert '[ -e "${BINARY_PATH}" ] || [ -L "${BINARY_PATH}" ]' in source


if __name__ == "__main__":
    main()
