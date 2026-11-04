"""Synthetic contract/transaction tests, with no host services or public listeners."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_gateway.contract import GatewayError, canonical, compile_caddy, digest, expected_listeners, normalize_plan, unit_text, validate_backend_profile
from edge_gateway.linux import LinuxSystem, file_hash, matches
from edge_gateway.manager import GatewayManager


def plan(revision=1, nodes=(45, 46), *, protocols=None):
    return {"schema_version": 1, "instance_id": "main", "revision": revision, "grace_seconds": 2, "routes": [{
        "route_id": f"node{node}", "node_id": node, "mode": "managed_caddy", "domain": "gateway.example.test",
        "path": f"/node-{node}", "listen": ["127.0.0.1"], "port": 24443, "protocols": protocols or ["h2", "h3"],
        "certificate": {"cert_file": "/var/lib/nodepulse-edge/main/certs/cert.pem", "key_file": "/var/lib/nodepulse-edge/main/certs/key.pem"},
        "backend": {"address": "127.0.0.1", "port": 25000 + node, "transport": "h2c", "local_config_sha256": "0" * 64}
    } for node in nodes]}


class FakeSystem(LinuxSystem):
    def __init__(self, root):
        super().__init__("main", root)
        self.running = False
        self.live = None
        self.calls = []
        self.foreign = []
        self.residual = []
        self.fail = {}
        self.after_fail = None
        self.mutex = threading.Lock()

    @contextmanager
    def lock(self):
        if not self.mutex.acquire(False):
            raise GatewayError("host_locked")
        try:
            yield
        finally:
            self.mutex.release()

    def atomic(self, path, contents, mode=0o600):
        self.safe(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    def trip(self, action):
        if self.fail.get(action, 0):
            self.fail[action] -= 1
            if self.after_fail:
                self.after_fail(self)
            raise GatewayError(action + "_failed")

    def inspect(self, state):
        assert file_hash(self.unit_path) == state["unit_sha256"]
        assert file_hash(self.binary) == state["binary_sha256"]
        return {"ActiveState": "active" if self.running else "inactive", "MainPID": "123" if self.running else "0",
                "Result": "success", "ControlGroup": self.group}

    def admin_config(self):
        return self.live

    def members(self):
        return self.residual.copy()

    def sockets(self):
        result = deepcopy(self.foreign)
        if self.running:
            for server in self.live["apps"]["http"]["servers"].values():
                for raw in server["listen"]:
                    address, number = raw.rsplit(":", 1)
                    for transport in (["tcp", "udp"] if "h3" in server["protocols"] else ["tcp"]):
                        result.append({"protocol": transport, "address": address.strip("[]"), "port": int(number), "inode": "owned",
                            "owners": [{"pid": 123, "start": 1, "state": "S", "cgroup": self.group, "exe": str(self.binary)}]})
        return result

    def validate(self, path, plan):
        self.trip("validate")

    def backend(self, route):
        self.trip("backend")

    def public_probe(self, plan):
        self.trip("probe")

    def action(self, action):
        self.calls.append(action)
        self.trip(action)
        if action in {"start", "reload"}:
            self.running = True
            self.live = json.loads(self.config.read_bytes())
        if action == "stop":
            self.running = False
        if action == "kill":
            self.residual = []

    def pause(self):
        pass


@pytest.fixture
def runtime(tmp_path):
    io = FakeSystem(tmp_path)
    io.atomic(io.binary, b"synthetic caddy executable")
    io.atomic(io.unit_path, unit_text("main", 60).encode())
    empty = {**plan(), "revision": 0, "routes": []}
    config = compile_caddy(empty)
    io.atomic(io.config, canonical(config))
    state = {"schema_version": 1, "instance_id": "main", "revision": 0, "plan": empty,
             "config_sha256": digest(config), "binary_sha256": file_hash(io.binary),
             "unit_sha256": file_hash(io.unit_path), "lifecycle": "stopped"}
    manager = GatewayManager(io)
    manager.write(manager.state_path, state)
    return manager, io


def test_shared_routes_compile_one_listener_and_explicit_udp():
    value = normalize_plan(plan())
    config = compile_caddy(value)
    servers = list(config["apps"]["http"]["servers"].values())
    assert len(servers) == 1 and len(servers[0]["routes"]) == 3
    assert {listener.protocol for listener in expected_listeners(value)} == {"tcp", "udp"}
    assert config["apps"]["http"]["grace_period"] == "2s"
    assert config["admin"]["listen"].startswith("unix//run/nodepulse-edge-main/")
    assert not config["admin"]["config"]["persist"]
    upstream = servers[0]["routes"][0]["handle"][0]
    assert upstream["transport"]["versions"] == ["h2c"]
    assert "X-Forwarded-For" in upstream["headers"]["request"]["delete"]
    assert "decryption" not in json.dumps(config)


@pytest.mark.parametrize("field,value,code", [
    ("mode", "external_cdn", "external_cdn_must_bypass_caddy"),
    ("mode", "direct_xray", "external_cdn_must_bypass_caddy"),
    ("protocols", ["h3"], "unsupported_public_protocols"),
    ("path", "/x*", "invalid_path"), ("path", "/a/../b", "invalid_path"),
    ("path", "/a/", "invalid_path"), ("domain", "*.example.test", "invalid_domain"),
    ("domain", "EXAMPLE.test", "invalid_domain"), ("node_id", True, "invalid_integer"),
    ("listen", ["0.0.0.0;rm"], "invalid_listen"),
])
def test_invalid_route_is_rejected(field, value, code):
    data = plan()
    data["routes"][0][field] = value
    with pytest.raises(GatewayError, match=code):
        normalize_plan(data)


def test_unknown_fields_and_unsafe_certificate_backend_are_rejected():
    for change, code in [
        (lambda r: r.update(shell_command="anything"), "invalid_fields"),
        (lambda r: r["certificate"].update(key_file="/etc/shadow"), "invalid_certificate_path"),
        (lambda r: r["certificate"].update(key_file="/var/lib/nodepulse-edge/main/certs/../key"), "invalid_certificate_path"),
        (lambda r: r["backend"].update(address="192.0.2.1"), "backend_must_be_loopback"),
        (lambda r: r["backend"].update(transport="http1"), "unsupported_backend_transport"),
        (lambda r: r["backend"].update(ca_file="/tmp/ca"), "plaintext_backend_has_tls_options"),
    ]:
        value = plan()
        change(value["routes"][0])
        with pytest.raises(GatewayError, match=code):
            normalize_plan(value)


def test_verified_tls_backend_has_no_skip_verify():
    value = plan(nodes=(45,))
    value["routes"][0]["backend"].update(transport="h2_tls", server_name="origin.example.test",
        ca_file="/var/lib/nodepulse-edge/main/certs/root.pem")
    compiled = compile_caddy(normalize_plan(value))
    transport = compiled["apps"]["http"]["servers"]["managed_0"]["routes"][0]["handle"][0]["transport"]
    assert transport["versions"] == ["2"] and "insecure_skip_verify" not in transport["tls"]


def test_duplicate_and_wildcard_conflicts_are_not_merged_silently():
    data = plan()
    data["routes"][1]["route_id"] = data["routes"][0]["route_id"]
    with pytest.raises(GatewayError, match="duplicate_or_invalid_route"):
        normalize_plan(data)
    data = plan()
    data["routes"][1]["listen"] = ["::"]
    with pytest.raises(GatewayError, match="listener_overlap"):
        normalize_plan(data)


def test_apply_remove_one_route_keeps_the_other_running(runtime):
    manager, io = runtime
    assert manager.apply(plan(), 0)["state"] == "applied"
    assert manager.apply(plan(2, nodes=(46,)), 1)["state"] == "applied"
    assert io.calls == ["start", "reload"] and io.running
    assert manager.status()["node_ids"] == [46]
    assert manager.apply(plan(2, nodes=(46,)), 2)["idempotent"]
    with pytest.raises(GatewayError, match="route_references_remain"):
        manager.stop(2)
    assert io.running


def test_last_route_removed_stops_and_preserves_certificate_data(runtime):
    manager, io = runtime
    cert = io.state_dir / "retained-certificate-data"
    cert.write_text("must remain")
    manager.apply(plan(), 0)
    result = manager.apply(plan(2, nodes=()), 1)
    assert result["state"] == "stopped" and not io.running and cert.read_text() == "must remain"
    assert manager.stop(2)["state"] == "stopped"


@pytest.mark.parametrize("failure", ["validate", "backend"])
def test_invalid_candidate_or_backend_does_not_change_authority(runtime, failure):
    manager, io = runtime
    manager.apply(plan(), 0)
    before = io.config.read_bytes()
    io.fail[failure] = 1
    with pytest.raises(GatewayError):
        manager.apply(plan(2, nodes=(46,)), 1)
    assert io.config.read_bytes() == before and manager.state()["revision"] == 1 and io.running
    assert not manager.journal_path.exists()


@pytest.mark.parametrize("failure", ["reload", "probe"])
def test_failed_apply_rolls_back_and_pauses_same_revision(runtime, failure):
    manager, io = runtime
    manager.apply(plan(), 0)
    before = io.config.read_bytes()
    io.fail[failure] = 1
    result = manager.apply(plan(2, nodes=(46,)), 1)
    assert result["state"] == "rolled_back" and result["retry_paused"]
    assert io.config.read_bytes() == before and manager.status()["node_ids"] == [45, 46]
    with pytest.raises(GatewayError, match="failed_revision_paused"):
        manager.apply(plan(2, nodes=(46,)), 1)
    assert manager.apply(plan(2, nodes=(46,)), 1, retry_failed=True)["state"] == "applied"


def test_rollback_failure_is_degraded_and_needs_recovery(runtime):
    manager, io = runtime
    manager.apply(plan(), 0)
    io.fail["reload"] = 2
    result = manager.apply(plan(2, nodes=(46,)), 1)
    assert result["state"] == "degraded" and result["recovery_required"]
    with pytest.raises(GatewayError, match="recovery_required"):
        manager.apply(plan(3), 1)
    assert manager.recover(1)["state"] == "rolled_back"


def test_external_change_is_never_overwritten_during_rollback(runtime):
    manager, io = runtime
    manager.apply(plan(), 0)
    io.fail["reload"] = 1
    io.after_fail = lambda io: io.atomic(io.config, canonical({"external": "changed"}))
    result = manager.apply(plan(2, nodes=(46,)), 1)
    assert result["state"] == "conflict" and result["recovery_error"] == "rollback_file_conflict"
    assert json.loads(io.config.read_bytes()) == {"external": "changed"}


def test_interruption_recovery_restores_owned_snapshot(runtime):
    manager, io = runtime
    manager.apply(plan(), 0)
    state = manager.state()
    candidate = normalize_plan(plan(2, nodes=(46,)))
    config = compile_caddy(candidate)
    manager.write(manager.journal_path, {"before": state, "candidate": candidate, "candidate_config_sha256": digest(config),
        "before_running": True, "before_config": deepcopy(io.live)})
    io.atomic(io.config, canonical(config))
    assert manager.status()["state"] == "conflict"
    assert manager.recover(1)["state"] == "rolled_back"
    assert manager.status()["node_ids"] == [45, 46]


@pytest.mark.parametrize("protocol,address", [("tcp", "0.0.0.0"), ("udp", "::"), ("tcp", "::")])
def test_foreign_listener_conflict_is_not_cleanup_success(runtime, protocol, address):
    manager, io = runtime
    manager.apply(plan(), 0)
    io.foreign = [{"protocol": protocol, "address": address, "port": 24443, "inode": "foreign",
        "owners": [{"pid": 999, "start": 777, "state": "S", "cgroup": "/system.slice/unrelated.service", "exe": "/unrelated"}]}]
    result = manager.stop(1, all_routes=True)
    assert result["state"] == "stopped_port_conflict" and result["service_stopped"] and not result["ports_clear"]
    assert "kill" not in io.calls and "disable" not in io.calls
    assert manager.stop(1, all_routes=True)["state"] == "stopped_port_conflict"


def test_managed_cgroup_residual_escalation_is_scoped(runtime):
    manager, io = runtime
    manager.apply(plan(), 0)
    io.residual = [{"pid": 123, "start": 1, "state": "S", "cgroup": io.group, "exe": str(io.binary)}]
    result = manager.stop(1, all_routes=True, disable=True)
    assert result["state"] == "stopped" and result["escalated"] and result["disabled"]
    assert io.calls[-3:] == ["stop", "kill", "disable"]


def test_zombie_residual_does_not_trigger_a_pid_kill(runtime):
    manager, io = runtime
    manager.apply(plan(), 0)
    io.residual = [{"pid": 123, "start": 1, "state": "Z", "cgroup": io.group, "exe": str(io.binary)}]
    with pytest.raises(GatewayError, match="zombie_residual_requires_review"):
        manager.stop(1, all_routes=True)
    assert "kill" not in io.calls


def test_host_lock_cas_and_running_config_conflict(runtime):
    manager, io = runtime
    with io.lock(), pytest.raises(GatewayError, match="host_locked"):
        manager.apply(plan(), 0)
    manager.apply(plan(), 0)
    with pytest.raises(GatewayError, match="version_conflict"):
        manager.apply(plan(2), 0)
    io.live = {"external": "changed via admin"}
    with pytest.raises(GatewayError, match="running_config_conflict"):
        manager.apply(plan(2), 1)
    assert "stop" not in io.calls


def test_unit_has_bounded_cgroup_stop_without_global_process_cleanup():
    unit = unit_text("main", 60)
    assert "Restart=on-failure" in unit and "Restart=always" not in unit
    assert "TimeoutStopSec=70s" in unit and "KillMode=control-group" in unit
    assert "--resume" not in unit and "ExecStop=" not in unit
    assert "User=nodepulse-edge-main" in unit and "PrivateTmp=yes" in unit


@pytest.mark.parametrize("change,code", [
    ({"network": "tcp"}, "unsafe_backend_transport"),
    ({"trusted_x_forwarded_for": ["any-header"]}, "unsafe_backend_transport"),
    ({"tls": 1}, "backend_security_mismatch"),
    ({"network_settings": {"path": "/other"}}, "backend_path_mismatch"),
    ({"network_settings": {"path": "/node-45", "host": "other.example.test"}}, "backend_host_mismatch"),
])
def test_actual_backend_profile_must_match_route_without_unsafe_ip_trust(change, code):
    route = normalize_plan(plan(nodes=(45,)))["routes"][0]
    server = {"network": "xhttp", "tls": 0, "network_settings": {"path": "/node-45"}, **change}
    with pytest.raises(GatewayError, match=code):
        validate_backend_profile(server, route)


def test_upstream_http_host_is_distinct_from_tls_server_name():
    value = plan(nodes=(45,))
    value["routes"][0]["backend"].update(transport="h2_tls", host="origin.example.test", server_name="tls.example.test")
    route = normalize_plan(value)["routes"][0]
    validate_backend_profile({"network": "xhttp", "tls": 1, "tls_settings": {"alpn": ["h2"]},
                             "network_settings": {"path": "/node-45/", "host": "origin.example.test"}}, route)
    handler = compile_caddy(normalize_plan(value))["apps"]["http"]["servers"]["managed_0"]["routes"][0]["handle"][0]
    assert handler["headers"]["request"]["set"]["Host"] == ["origin.example.test"]
    assert handler["transport"]["tls"]["server_name"] == "tls.example.test"


def test_enable_is_separate_from_stop_and_last_stop_is_observable(runtime):
    manager, io = runtime
    manager.apply(plan(), 0)
    assert manager.enable(1)["state"] == "enabled" and io.running
    result = manager.stop(1, all_routes=True)
    assert result["state"] == "stopped" and manager.status()["last_stop"]["ports_clear"]
    assert "disable" not in io.calls
