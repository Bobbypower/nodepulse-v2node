"""Real origin test for a compiler-produced external_cdn payload; no gateway calls."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

# Reuse only client/echo utilities. Any gateway operation is made a hard failure.
from edge_gateway_linux_acceptance import XHTTP, Echo, ThreadServer, GatewayManager


def forbidden(*args, **kwargs):
    raise AssertionError("external_cdn must not invoke the gateway helper")


for method in ("apply", "initialize", "stop", "enable", "recover", "status"):
    setattr(GatewayManager, method, forbidden)

ROOT = Path("/opt/external-cdn-evidence")
PROFILE = json.loads(Path(sys.argv[1]).read_text())
NODE = 901


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-800:]
    return result.stdout.strip()


class Panel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/v2/server/config":
            result = PROFILE
        elif path.endswith("/user"):
            result = {"users": [{"id": 101, "uuid": "11111111-1111-4111-8111-111111111111", "speed_limit": 0, "device_limit": 0}]}
        elif path.endswith("/account-enforcement"):
            result = {"commands": [], "next_cursor": 0}
        else:
            result = {"alive": {}}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")


def main():
    ROOT.mkdir(exist_ok=True)
    assert PROFILE["listen_ip"] == "0.0.0.0" and PROFILE["server_port"] == 8443
    assert PROFILE["tls"] == 1 and PROFILE["tls_settings"]["alpn"] == ["h2"]
    assert PROFILE["tls_settings"]["server_name"] == "origin.example.invalid"
    assert PROFILE["cert_config"] == {"cert_mode": "file", "cert_file": "/etc/v2node/origin.crt", "key_file": "/etc/v2node/origin.key"}
    assert "caddy" not in json.dumps(PROFILE).lower() and "edge_gateway" not in PROFILE
    Path("/etc/v2node").mkdir(exist_ok=True)
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=origin.example.invalid",
        "-addext", "subjectAltName=DNS:origin.example.invalid,DNS:node.example.invalid", "-out", "/etc/v2node/origin.crt", "-keyout", "/etc/v2node/origin.key")
    # The only harness augmentation permits the otherwise blocked local echo
    # destination. All compiler-produced listener/TLS/transport fields stay exact.
    original = dict(PROFILE)
    PROFILE["routes"] = [{"id": 900001, "action": "route_ip", "match": ["127.0.0.1/32"], "action_value": json.dumps({
        "tag": "isolated-echo", "protocol": "freedom", "settings": {"finalRules": [{"action": "allow", "ip": ["127.0.0.1/32"], "port": "21010"}]}})}]
    panel = ThreadingHTTPServer(("127.0.0.1", 28080), Panel)
    echo = ThreadServer(("127.0.0.1", 21010), Echo)
    for server in (panel, echo):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    config = Path("/etc/v2node/v2node-901.json")
    config.write_text(json.dumps({"Log": {"Level": "warning", "Output": "", "Access": "none"}, "Nodes": [{
        "NodeID": NODE, "ApiHost": "http://127.0.0.1:28080", "ApiKey": "synthetic-only"}]}))
    Path("/etc/systemd/system/v2node-901.service").write_text(
        f"[Service]\nExecStart=/usr/local/bin/v2node server --config {config} --watch=false\nKillMode=control-group\n")
    sentinel = Path("/opt/external-cdn-test-caddy.json")
    sentinel.write_text(json.dumps({"admin": {"disabled": True}, "apps": {"http": {"servers": {"test": {
        "listen": ["127.0.0.1:19777"], "routes": [{"handle": [{"handler": "static_response", "body": "sentinel"}]}]}}}}}))
    Path("/etc/systemd/system/external-cdn-test-caddy.service").write_text(
        f"[Service]\nExecStart=/usr/bin/caddy run --config {sentinel}\nKillMode=control-group\nTimeoutStopSec=5s\n")
    run("systemctl", "daemon-reload")
    run("systemctl", "start", "external-cdn-test-caddy.service", "v2node-901.service")
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", 8443), timeout=.1):
                break
        except OSError:
            time.sleep(.1)
    def authenticate(server_name):
        client = XHTTP(8443, "/xhttp-test", host="node.example.invalid", tls_server_name=server_name, ca_file="/etc/v2node/origin.crt")
        data = b"compiled-external-origin-" * 1000
        client.send(data)
        assert client.read(len(data)) == data
        client.close()
    authenticate("origin.example.invalid")
    run("systemctl", "stop", "external-cdn-test-caddy.service")
    assert run("systemctl", "show", "external-cdn-test-caddy.service", "--property=ActiveState", "--value") == "inactive"
    authenticate("origin.example.invalid")
    authenticate("node.example.invalid")  # direct branch uses a cert covering both names
    pid = run("systemctl", "show", "v2node-901.service", "--property=MainPID", "--value")
    peer = Path("/proc") / pid
    assert peer.joinpath("cgroup").read_text().strip().endswith("/system.slice/v2node-901.service")
    listeners = run("ss", "-lntpH", "sport", "=", ":8443")
    # Go may implement a requested IPv4 wildcard with a dual-stack socket,
    # printed as *:8443 by ss. Record the observation instead of rewriting it.
    assert any(bind in listeners for bind in ("0.0.0.0:8443", "*:8443", "[::]:8443")) and f"pid={pid}," in listeners
    (ROOT / "compiled-input.json").write_text(json.dumps(original, indent=2))
    result = {"passed": True, "customer_testing": False, "real_cloudflare_tested": False, "gateway_helper_called": False,
        "listen": "0.0.0.0:8443", "client_public_port": 443, "origin_alpn": ["h2"], "origin_sni": "origin.example.invalid",
        "http_host": "node.example.invalid", "certificate": "file, strict local CA verification, SAN covers origin and direct names",
        "caddy_stopped_origin_still_authenticated": True, "actual_owner": "v2node-901.service", "listeners": listeners,
        "harness_only_change": "loopback echo allow route; compiled listener/TLS/transport fields unchanged"}
    (ROOT / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
