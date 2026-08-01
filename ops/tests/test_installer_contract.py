from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "ops" / "install-nodepulse-v2node.sh"
START_MARKER = (
    'python3 - "${TMP_SERVER_CONFIG}" "${NODE_PORT}" '
    '>"${TMP_EXPECTED_PORTS}" <<\'PY\'\n'
)
END_MARKER = "\nPY\n"


def validator_source() -> str:
    source = INSTALLER.read_text(encoding="utf-8")
    start = source.find(START_MARKER)
    if start < 0:
        raise AssertionError("server config validator heredoc was not found")
    start += len(START_MARKER)
    end = source.find(END_MARKER, start)
    if end < 0:
        raise AssertionError("server config validator heredoc is not terminated")
    return source[start:end]


def run_validator(config: dict[str, object], node_port: int) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = Path(temp_dir) / "server-config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-", str(config_path), str(node_port)],
            input=validator_source(),
            text=True,
            capture_output=True,
            check=False,
        )


def split_config() -> dict[str, object]:
    return {
        "server_port": 443,
        "network": "xhttp",
        "network_settings": {"path": "/same", "mode": "stream-up"},
        "xhttp_download_inbound": {
            "server_port": 80,
            "network": "xhttp",
            "security": "none",
            "network_settings": {"path": "/same", "mode": "auto"},
        },
    }


def main() -> None:
    valid = run_validator(split_config(), 443)
    assert valid.returncode == 0, valid.stderr
    assert valid.stdout.splitlines() == ["443", "80"]

    mismatched_path = split_config()
    download = dict(mismatched_path["xhttp_download_inbound"])
    download["network_settings"] = {"path": "/different", "mode": "auto"}
    mismatched_path["xhttp_download_inbound"] = download
    invalid_path = run_validator(mismatched_path, 443)
    assert invalid_path.returncode != 0
    assert "same normalized path" in invalid_path.stderr

    invalid_port = run_validator(split_config(), 8443)
    assert invalid_port.returncode != 0
    assert "does not match" in invalid_port.stderr

    invalid_security = split_config()
    insecure_download = dict(invalid_security["xhttp_download_inbound"])
    insecure_download["security"] = "tls"
    invalid_security["xhttp_download_inbound"] = insecure_download
    rejected_security = run_validator(invalid_security, 443)
    assert rejected_security.returncode != 0
    assert "security must be none" in rejected_security.stderr

    invalid_primary = split_config()
    invalid_primary["network"] = "tcp"
    rejected_primary = run_validator(invalid_primary, 443)
    assert rejected_primary.returncode != 0
    assert "primary network" in rejected_primary.stderr

    print("installer contract tests passed")


if __name__ == "__main__":
    main()
