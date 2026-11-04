"""Opt-in: private systemd container, real Caddy and patched v2node/Xray.

The caller supplies /usr/local/bin/v2node from a recorded local artifact.
All identities, certificates and APIs below are synthetic and loopback-only.
No Cloudflare, DNSPod, production account or public network is exercised.
"""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
from pathlib import Path
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import threading
import time
import tarfile
from urllib.parse import parse_qs, urlsplit

import h2.config
import h2.connection
import h2.events
import h2.exceptions

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_gateway.contract import GatewayError, canonical
from edge_gateway.linux import LinuxSystem, file_hash, socket_table
from edge_gateway.manager import GatewayManager
from test_edge_gateway import plan

OUTPUT = Path("/opt/edge-gateway-evidence")
PROFILES = {}
COUNTS = {}


async def h3_vless():
    from aioquic.asyncio import QuicConnectionProtocol, connect
    from aioquic.h3.connection import H3Connection, H3_ALPN
    from aioquic.h3.events import DataReceived, HeadersReceived
    from aioquic.quic.configuration import QuicConfiguration
    class Client(QuicConnectionProtocol):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.http = H3Connection(self._quic)
            self.data = b""
            self.done = asyncio.get_running_loop().create_future()

        def quic_event_received(self, event):
            for item in self.http.handle_event(event):
                if isinstance(item, HeadersReceived):
                    if (b":status", b"200") not in item.headers and not self.done.done():
                        self.done.set_exception(AssertionError("unexpected HTTP3 response"))
                if isinstance(item, DataReceived):
                    self.data += item.data
                    if len(self.data) >= 7 and not self.done.done():
                        self.done.set_result(self.data)

        def request(self):
            stream = self._quic.get_next_available_stream_id()
            self.http.send_headers(stream, [(b":method", b"POST"), (b":scheme", b"https"),
                (b":authority", b"gateway.example.test"), (b":path", b"/node-46/"),
                (b"referer", b"https://gateway.example.test/?x_padding=" + b"X" * 200)])
            hello = b"\0" + bytes.fromhex("11111111111141118111111111111111") + b"\0\x01" + struct.pack("!H", 21010) + b"\x01\x7f\0\0\x01hello"
            self.http.send_data(stream, hello, end_stream=False)
            self.transmit()
    config = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN, server_name="gateway.example.test")
    config.load_verify_locations("/var/lib/nodepulse-edge/main/certs/cert.pem")
    async with connect("127.0.0.1", 24443, configuration=config, create_protocol=Client) as client:
        client.request()
        assert await asyncio.wait_for(client.done, 8) == b"\0\0hello"


def command(*argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=80)
    assert result.returncode == 0, (argv[0], result.stdout[-500:], result.stderr[-500:])
    return result.stdout


class Panel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path.startswith("/assets/"):
            name = path.path.removeprefix("/assets/")
            if name not in {"v2node-linux-amd64", "nodepulse-edge-gateway.tar.gz", "SHA256SUMS"}:
                self.send_error(404)
                return
            target = OUTPUT / "release" / name
            self.send_response(200)
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            with target.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile)
            return
        query = parse_qs(path.query)
        node = int(query.get("node_id", [45])[0])
        COUNTS[(node, path.path)] = COUNTS.get((node, path.path), 0) + 1
        if path.path == "/api/v2/server/config":
            body = PROFILES[node]
        elif path.path == "/api/v2/server/local_config":
            body = json.loads(Path(f"/etc/v2node/v2node-{node}.json").read_bytes())
        elif path.path.endswith("/user"):
            body = {"users": [{"id": 101, "uuid": "11111111-1111-4111-8111-111111111111", "speed_limit": 0, "device_limit": 0}]}
        elif path.path.endswith("/alivelist"):
            body = {"alive": {}}
        elif path.path.endswith("/account-enforcement"):
            body = {"commands": [], "next_cursor": 0}
        else:
            body = {}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            self.request.sendall(data)


class ThreadServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class XHTTP:
    def __init__(self, port, path, *, tls=True, host="gateway.example.test", session=None, tls_server_name=None, ca_file=None):
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        if tls:
            context = ssl.create_default_context(cafile=ca_file or "/var/lib/nodepulse-edge/main/certs/cert.pem")
            context.set_alpn_protocols(["h2"])
            self.socket = context.wrap_socket(raw, server_hostname=tls_server_name or host)
            assert self.socket.selected_alpn_protocol() == "h2"
        else:
            self.socket = raw
        self.socket.settimeout(5)
        self.h2 = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
        self.h2.initiate_connection()
        headers = [(":method", "GET" if session else "POST"), (":scheme", "https" if tls else "http"),
            (":authority", host), (":path", path + "/" + (session or "")), ("content-type", "application/grpc"),
            ("x-forwarded-for", "203.0.113.77"), ("cf-connecting-ip", "203.0.113.88"),
            ("referer", "https://gateway.example.test/?x_padding=" + "X" * 200)]
        self.h2.send_headers(1, headers, end_stream=bool(session))
        self.upload_stream = 3 if session else 1
        if session:
            self.h2.send_headers(3, [(key, "POST" if key == ":method" else value) for key, value in headers], end_stream=False)
        self.socket.sendall(self.h2.data_to_send())
        self.buffer = b""
        hello = b"\0" + bytes.fromhex("11111111111141118111111111111111") + b"\0\x01" + struct.pack("!H", 21010) + b"\x01\x7f\0\0\x01"
        self.send(hello + b"hello")
        assert self.read(7) == b"\0\0hello"

    def send(self, data):
        for start in range(0, len(data), 16384):
            self.h2.send_data(self.upload_stream, data[start:start + 16384])
        self.socket.sendall(self.h2.data_to_send())

    def read(self, size):
        while len(self.buffer) < size:
            data = self.socket.recv(65536)
            if not data:
                raise EOFError("HTTP2 connection closed")
            for event in self.h2.receive_data(data):
                if isinstance(event, h2.events.ResponseReceived):
                    assert (b":status", b"200") in event.headers, event.headers
                if isinstance(event, h2.events.DataReceived):
                    if event.stream_id == 1:
                        self.buffer += event.data
                    self.h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                if isinstance(event, (h2.events.StreamReset, h2.events.StreamEnded)) and event.stream_id == 1:
                    raise EOFError("HTTP2 stream ended")
            pending = self.h2.data_to_send()
            if pending:
                self.socket.sendall(pending)
        value, self.buffer = self.buffer[:size], self.buffer[size:]
        return value

    def close(self):
        self.socket.close()


def start_backend(node, tls=False, path=None, host=None):
    origin = 25000 + node
    local = {"Log": {"Level": "warning", "Output": "", "Access": "none"}, "Nodes": [{
        "NodeID": node, "ApiHost": "http://127.0.0.1:28080", "ApiKey": "synthetic-test-only", "ListenIP": "127.0.0.1", "ListenPort": origin}]}
    profile = {"protocol": "vless", "listen_ip": "0.0.0.0", "server_port": 23333,
        "tls": 1 if tls else 0, "network": "xhttp", "network_settings": {"path": path or f"/node-{node}", "mode": "auto"},
        "base_config": {"push_interval": 600, "pull_interval": 2},
        "routes": [{"id": 1, "action": "route_ip", "match": ["127.0.0.1/32"], "action_value": json.dumps({
            "tag": "isolated-echo", "protocol": "freedom", "settings": {"finalRules": [{"action": "allow", "ip": ["127.0.0.1/32"], "port": "21010"}]}})}]}
    if tls:
        # The published runtime requires certificate parents owned by its own
        # uid or root. Keep the independent Xray origin certificate separate
        # from the Caddy-owned certificate directory used by this fixture.
        origin_root = Path("/var/lib/nodepulse-test-origins")
        origin_root.mkdir(mode=0o700, exist_ok=True)
        origin_root.chmod(0o700)
        origin_certs = origin_root / str(node)
        origin_certs.mkdir(mode=0o700, exist_ok=True)
        origin_certs.chmod(0o700)
        for name in ("cert.pem", "key.pem"):
            target = origin_certs / name
            shutil.copyfile(Path("/var/lib/nodepulse-edge/main/certs") / name, target)
            target.chmod(0o600 if name == "key.pem" else 0o644)
        profile["tls_settings"] = {"alpn": ["h2"], "server_name": "gateway.example.test", "cert_mode": "file",
            "cert_file": str(origin_certs / "cert.pem"), "key_file": str(origin_certs / "key.pem")}
    if host:
        profile["network_settings"]["host"] = host
    PROFILES[node] = profile
    config = Path(f"/etc/v2node/v2node-{node}.json")
    config.parent.mkdir(exist_ok=True)
    config.write_bytes(canonical(local))
    unit = Path(f"/etc/systemd/system/v2node-{node}.service")
    unit.write_text(f"[Service]\nExecStart=/usr/local/bin/v2node server --config {config} --watch=false\nKillMode=control-group\n")
    command("systemctl", "daemon-reload")
    command("systemctl", "start", f"v2node-{node}.service")
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", origin), timeout=.2):
                return file_hash(config)
        except OSError:
            time.sleep(.1)
    print(command("journalctl", "-u", f"v2node-{node}.service", "-n", "80", "--no-pager"), flush=True)
    raise AssertionError("real v2node backend did not start")


def main():
    assert os.getuid() == 0 and Path("/run/systemd/system").exists()
    OUTPUT.mkdir(exist_ok=True)
    panel, echo = ThreadingHTTPServer(("127.0.0.1", 28080), Panel), ThreadServer(("127.0.0.1", 21010), Echo)
    for service in (panel, echo):
        threading.Thread(target=service.serve_forever, daemon=True).start()
    io = LinuxSystem("main")
    manager = GatewayManager(io)
    result = {"identities": "synthetic only", "caddy": command("/usr/bin/caddy", "version").strip(), "checks": []}
    manager.initialize(Path("/usr/bin/caddy"), file_hash("/usr/bin/caddy"))
    certs = Path("/var/lib/nodepulse-edge/main/certs")
    command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=gateway.example.test",
        "-addext", "subjectAltName=DNS:gateway.example.test,DNS:node48.example.test,DNS:node49.example.test", "-keyout", str(certs / "key.pem"), "-out", str(certs / "cert.pem"))
    command("chown", "-R", "nodepulse-edge-main:nodepulse-edge-main", str(certs))
    value = plan()
    for route in value["routes"]:
        route["certificate"]["probe_ca_file"] = str(certs / "cert.pem")
        route["backend"]["local_config_sha256"] = start_backend(route["node_id"])
    # A separate public TLS/H2 origin is always Xray, never a Caddy route.
    start_backend(47, tls=True, host="origin.example.test")
    other_io = LinuxSystem("other")
    other = GatewayManager(other_io)
    other.initialize(Path("/usr/bin/caddy"), file_hash("/usr/bin/caddy"))
    other_certs = Path("/var/lib/nodepulse-edge/other/certs")
    for name in ("cert.pem", "key.pem"):
        shutil.copyfile(certs / name, other_certs / name)
    command("chown", "-R", "nodepulse-edge-other:nodepulse-edge-other", str(other_certs))
    other_plan = plan(nodes=(47,), protocols=["h2"])
    other_plan["instance_id"] = "other"
    other_route = other_plan["routes"][0]
    other_route["port"] = 24444
    other_route["certificate"] = {"cert_file": str(other_certs / "cert.pem"), "key_file": str(other_certs / "key.pem"),
                                  "probe_ca_file": str(other_certs / "cert.pem")}
    other_route["backend"].update(transport="h2_tls", server_name="gateway.example.test", ca_file=str(other_certs / "cert.pem"),
                                  host="origin.example.test", local_config_sha256=file_hash("/etc/v2node/v2node-47.json"))
    assert other.apply(other_plan, 0)["state"] == "applied"
    applied = manager.apply(value, 0)
    assert applied["state"] == "applied", applied
    result["checks"].append("real Caddy validate/start, private Admin API, TCP+UDP owners, actual Xray H2C preflight")
    live = XHTTP(24443, "/node-46")
    payload = b"synthetic-payload-" * 2000
    live.send(payload)
    assert live.read(len(payload)) == payload
    result["checks"].append("authenticated VLESS/XHTTP TLS-H2 -> Caddy -> H2C-Xray upload and download")
    asyncio.run(h3_vless())
    result["checks"].append("authenticated VLESS/XHTTP QUIC-H3 -> Caddy UDP -> H2C-Xray with certificate verification")
    live.close()
    PROFILES[46]["server_port"] = 23334  # a changed panel value forces v2node's own periodic reload
    time.sleep(3.5)
    io.backend(value["routes"][1])
    assert not any(row["protocol"] == "tcp" and row["port"] in {23333, 23334} for row in socket_table())
    live = XHTTP(24443, "/node-46")
    result["checks"].append("periodic v2node config reload retains loopback IP/port overrides instead of public panel ports")
    second = {**value, "revision": 2, "routes": value["routes"][1:]}
    assert manager.apply(second, 1)["state"] == "applied"
    try:
        live.send(b"after-reload")
        assert live.read(12) == b"after-reload"
        result["existing_connection_across_reload"] = "survived"
    except (EOFError, OSError, h2.exceptions.ProtocolError):
        result["existing_connection_across_reload"] = "GOAWAY/connection renewal required by this H2 test client; new connection restored"
        live.close()
        live = XHTTP(24443, "/node-46")
    result["checks"].append("delete one node route without stopping shared Caddy; other route authenticates")
    bad = {**second, "revision": 3, "routes": [dict(second["routes"][0])]}
    bad["routes"][0]["certificate"] = {**bad["routes"][0]["certificate"], "key_file": str(certs / "missing.pem")}
    try:
        manager.apply(bad, 2)
    except GatewayError as error:
        assert error.code == "caddy_validation_failed", error.code
    else:
        raise AssertionError("invalid certificate unexpectedly applied")
    assert manager.state()["revision"] == 2 and manager.status()["state"] == "running"
    result["checks"].append("invalid certificate/config leaves last running version unchanged")
    healthy_probe = io.public_probe
    def fail_once(value):
        io.public_probe = healthy_probe
        raise GatewayError("synthetic_health_failure")
    io.public_probe = fail_once
    rollback = manager.apply({**second, "revision": 3}, 2)
    assert rollback["state"] == "rolled_back" and manager.state()["revision"] == 2, rollback
    result["checks"].append("injected health failure executes real Caddy rollback and verifies original backends")
    started = time.monotonic()
    stopped = manager.stop(2, all_routes=True)
    assert stopped["state"] == "stopped" and stopped["cgroup_empty"] and stopped["ports_clear"], stopped
    result["stop_seconds"] = round(time.monotonic() - started, 3)
    try:
        live.send(b"after-stop")
        live.read(10)
    except (OSError, EOFError, h2.exceptions.ProtocolError):
        pass
    else:
        raise AssertionError("old connection survived explicit stop")
    live.close()
    time.sleep(3.2)
    assert io.show()["ActiveState"] == "inactive"
    assert manager.stop(2, all_routes=True)["state"] == "stopped"
    result["checks"].append("finite grace with a real long connection; manual stop stays stopped; repeat stop idempotent")
    separate = XHTTP(24444, "/node-47")
    separate.close()
    assert other.status()["state"] == "running" and other_certs.joinpath("key.pem").exists()
    result["checks"].append("stopping main preserves another managed Caddy instance and its verified TLS/H2 upstream")
    direct = XHTTP(25047, "/node-47", host="origin.example.test", tls_server_name="gateway.example.test")
    direct.send(b"cdn-origin-bypass")
    assert direct.read(17) == b"cdn-origin-bypass"
    direct.close()
    result["checks"].append("direct TLS/H2 Xray origin works while local Caddy is stopped (isolated CDN-origin path only)")
    assert manager.apply({**second, "revision": 4}, 2)["state"] == "applied"
    # Shorten only this disposable fixture's outer systemd timeout. Its root
    # authority explicitly records the new unit digest; production stays 70s.
    unit = io.unit_path.read_text().replace("TimeoutStopSec=70s", "TimeoutStopSec=5s")
    io.atomic(io.unit_path, unit.encode(), 0o644)
    state = manager.state()
    state["unit_sha256"] = file_hash(io.unit_path)
    manager.write(manager.state_path, state)
    command("systemctl", "daemon-reload")
    child = subprocess.Popen([sys.executable, "-u", "-c",
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);time.sleep(120)"], stdout=subprocess.PIPE)
    assert child.stdout.readline() == b"ready\n"
    (Path("/sys/fs/cgroup") / io.group.lstrip("/") / "cgroup.procs").write_text(str(child.pid))
    forced = manager.stop(4, all_routes=True)
    child.wait(timeout=2)
    assert forced["state"] == "stopped" and forced["forced"] and not io.members(), forced
    assert command("systemctl", "is-active", "v2node-47.service").strip() == "active"
    result["checks"].append("real managed-cgroup child ignores TERM: bounded systemd KILL recorded, other Xray service survives")
    shared = plan(5, nodes=(48, 49))
    for route in shared["routes"]:
        route.update(path="/same-path", domain=f"node{route['node_id']}.example.test")
        route["certificate"]["probe_ca_file"] = str(certs / "cert.pem")
        route["backend"]["local_config_sha256"] = start_backend(route["node_id"], path="/same-path")
    assert manager.apply(shared, 4)["state"] == "applied"
    left = XHTTP(24443, "/same-path", host="node48.example.test", session="same-synthetic-session-id")
    right = XHTTP(24443, "/same-path", host="node49.example.test", session="same-synthetic-session-id")
    left.send(b"node-48-data")
    right.send(b"node-49-data")
    assert left.read(12) == b"node-48-data" and right.read(12) == b"node-49-data"
    left.close()
    right.close()
    assert manager.stop(5, all_routes=True)["state"] == "stopped"
    result["checks"].append("two real node processes: identical XHTTP path and stream-up session ID stay isolated")
    foreign = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    foreign.bind(("::", 24443))
    conflict = manager.stop(5, all_routes=True)
    assert conflict["state"] == "stopped_port_conflict" and foreign.fileno() >= 0 and not conflict["escalated"]
    foreign.close()
    result["checks"].append("foreign IPv6 wildcard UDP occupation is conflict, with no kill")
    # Real installer execution, with a loopback release and recorded checksums.
    release = OUTPUT / "release"
    release.mkdir(exist_ok=True)
    shutil.copyfile("/usr/local/bin/v2node", release / "v2node-linux-amd64")
    source = Path(__file__).resolve().parents[1]
    archive_path = release / "nodepulse-edge-gateway.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source / "edge_gateway", arcname="edge_gateway", filter=lambda item: None if "__pycache__" in item.name else item)
    (release / "SHA256SUMS").write_text("".join(file_hash(release / name) + "  " + name + "\n" for name in
        ("v2node-linux-amd64", "nodepulse-edge-gateway.tar.gz")))
    env = {**os.environ, "EDGE_GATEWAY_RELEASE_BASE": "http://127.0.0.1:28080/assets", "CADDY_BIN": "/usr/bin/caddy",
           "CADDY_SHA256": file_hash("/usr/bin/caddy")}
    install_edge = subprocess.run(["bash", str(source / "install-nodepulse-edge.sh")], env=env, capture_output=True, text=True, timeout=30)
    (OUTPUT / "edge-installer.log").write_text(install_edge.stdout + install_edge.stderr)
    assert install_edge.returncode == 0, install_edge.stderr[-500:]
    assert json.loads(command("/usr/local/sbin/nodepulse-edge-gateway", "--instance", "main", "status"))["state"] == "stopped"
    command("systemctl", "stop", "v2node-46.service", "v2node-47.service", "v2node-48.service", "v2node-49.service")
    env.update(NODE_ID="45", NODE_PORT="25045", NODEPULSE_URL="http://127.0.0.1:28080", NODEPULSE_TOKEN="synthetic-test-only", TMPDIR="/var/tmp",
        V2NODE_RELEASE_BASE="http://127.0.0.1:28080/assets", VERIFY_SECONDS="3", ALLOW_SHARED_BINARY_UPDATE="1")
    install_node = subprocess.run(["bash", str(source / "install-nodepulse-v2node.sh")], env=env, capture_output=True, text=True, timeout=45)
    (OUTPUT / "node-installer.log").write_text(install_node.stdout + install_node.stderr)
    assert install_node.returncode == 0, install_node.stderr[-500:] + install_node.stdout[-500:]
    assert command("systemctl", "is-active", "v2node-45.service").strip() == "active"
    result["checks"].append("both real installers run against a checksummed loopback release; node installer verifies Xray-owned listener")
    result.update(passed=True, real_provider_tested=False, customer_testing=False)
    (OUTPUT / "result.json").write_text(json.dumps(result, indent=2))
    (OUTPUT / "state.json").write_text(json.dumps(manager.state(), indent=2))
    (OUTPUT / "socket-owners.json").write_text(json.dumps(socket_table(), indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
