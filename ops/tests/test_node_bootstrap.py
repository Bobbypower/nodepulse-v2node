"""First-install ownership, immutable receipts and inactive unit identity."""
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_gateway import node_bootstrap as b
from edge_gateway.contract import GatewayError


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "ROOT", tmp_path)
    # State tests simulate a privileged host without requiring the CI runner
    # to be root. The separate permission tests exercise trusted() directly.
    if os.name == "nt" or os.geteuid() != 0:
        monkeypatch.setattr(b, "trusted", lambda *a, **k: None)
    if os.name == "nt":
        def atomic(self, target, contents, mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = target.with_suffix(".new")
            stage.write_bytes(contents)
            os.replace(stage, target)
        monkeypatch.setattr(b.LinuxSystem, "atomic", atomic)
    props = {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "MainPID": "0", "FragmentPath": ""}
    monkeypatch.setattr(b, "properties", lambda node: dict(props))
    for item in ["etc/v2node", "etc/systemd/system", "etc/nodepulse/bootstrap"]:
        (tmp_path/item).mkdir(parents=True, exist_ok=True)
    (tmp_path/"etc/nodepulse/bootstrap").chmod(0o700)
    raw = b"synthetic-candidate-runtime"
    binary = "/usr/local/lib/nodepulse/v2node/" + b.hashed(raw) + "/v2node"
    b.path(binary).parent.mkdir(parents=True)
    b.path(binary).write_bytes(raw)
    local = tmp_path/"local.json"
    local.write_text(json.dumps({"Nodes": [{"NodeID": 900, "ListenIP": "0.0.0.0", "ListenPort": 8443, "Token": "synthetic-private-panel-value"}]}))
    local.chmod(0o600)
    server = tmp_path/"server.json"
    server.write_text(json.dumps({"protocol": "vless", "network": "xhttp", "tls": 1, "server_port": 8443,
        "tls_settings": {"server_name": "node.example.invalid", "alpn": ["h2"]},
        "cert_config": {"cert_mode": "file", "cert_file": "/etc/v2node/vless900.cer", "key_file": "/etc/v2node/vless900.key"}}))
    value = b.specification(900, binary, local, server, "origin_900", "initial_job_900", "a"*64)
    return SimpleNamespace(value=value, props=props, local=local, server=server, root=tmp_path)


def materialize(host):
    value = host.value
    b.path(value["config_path"]).write_bytes(host.local.read_bytes())
    b.path(value["config_path"]).chmod(0o600)
    b.path(value["unit_path"]).write_bytes(b.unit_text(900, value["binary_path"]))
    host.props.update({"Id": "v2node-900.service", "LoadState": "loaded", "FragmentPath": value["unit_path"],
        "User": "root", "Group": "", "DynamicUser": "no", "DropInPaths": "", "NeedDaemonReload": "no",
        "ExecStart": "{ path=" + value["binary_path"] + " ; argv[]=" + value["binary_path"] +
            " server --config /etc/v2node/v2node-900.json --watch=false ; ignore_errors=no ; }"})


def prepared(host):
    b.reserve(host.value, "prepare")
    materialize(host)
    return b.finish(900)


def test_prepare_reserves_before_config_creation_and_returns_task_bound_public_receipt(host):
    reservation = b.reserve(host.value, "prepare")
    assert b.load(900) == reservation and not b.path(reservation["unit_path"]).exists()
    materialize(host)
    receipt = b.finish(900)
    assert receipt["state"] == "prepared" and receipt["request_id"] == "initial_job_900"
    assert receipt["service_started"] is receipt["service_uid_verified"] is receipt["tls_handshake_verified"] is False
    assert "synthetic-private-panel-value" not in json.dumps(receipt)
    assert receipt["binding_sha256"] == reservation["binding_sha256"]


@pytest.mark.parametrize("suffix", [".cer", ".key", ".key.previous", ".key.pending-foreign_job", ".key.certificate-operation.lock"])
def test_first_prepare_rejects_any_unowned_certificate_artifact_without_changing_it(host, suffix):
    target = b.path("/etc/v2node/vless900" + suffix)
    target.write_bytes(b"opaque-existing-file")
    before = target.stat()
    with pytest.raises(GatewayError, match="bootstrap_unowned_existing_certificate"):
        b.reserve(host.value, "prepare")
    assert target.read_bytes() == b"opaque-existing-file" and target.stat().st_mtime_ns == before.st_mtime_ns
    assert not b.state_path(900).exists()


@pytest.mark.parametrize("field", ["config_path", "unit_path"])
def test_new_bootstrap_does_not_take_over_an_inactive_existing_deployment_file(host, field):
    target = b.path(host.value[field]); target.write_bytes(b"belongs-to-another-deployment")
    with pytest.raises(GatewayError, match="bootstrap_unowned_existing_config"):
        b.reserve(host.value, "prepare")
    assert target.read_bytes() == b"belongs-to-another-deployment"


def test_prepare_retry_recovers_only_its_original_reservation_and_exact_files(host):
    b.reserve(host.value, "prepare")
    materialize(host)
    assert b.reserve(host.value, "prepare")["state"] == "reserved"
    assert b.finish(900)["state"] == "prepared"
    assert b.reserve(host.value, "prepare")["state"] == "prepared"
    b.path(host.value["config_path"]).write_bytes(b"changed-config")
    with pytest.raises(GatewayError, match="bootstrap_identity_changed"):
        b.reserve(host.value, "prepare")


def test_prepare_retry_with_csr_or_installed_certificate_requires_activation_or_reconciliation(host):
    prepared(host)
    b.path(host.value["key_file"] + ".pending-initial_job_900").write_bytes(b"pending")
    with pytest.raises(GatewayError, match="bootstrap_certificate_exists_use_activate_or_reconcile"):
        b.reserve(host.value, "prepare")


@pytest.mark.parametrize("field,new", [("request_id", "another_job_900"), ("profile_id", "other_profile"), ("listen_port", 9443)])
def test_second_task_or_changed_plan_cannot_adopt_prepared_files(host, field, new):
    prepared(host)
    changed = deepcopy(host.value); changed[field] = new
    body = {key: item for key, item in changed.items() if key not in {"state", "binding_sha256"}}
    changed["binding_sha256"] = b.hashed(b.encoded(body))
    with pytest.raises(GatewayError, match="bootstrap_task_or_configuration_changed"):
        b.reserve(changed, "activate", host.value["binding_sha256"])


@pytest.mark.parametrize("binding", ["", "a"*64])
def test_activation_requires_exact_prepared_binding(host, binding):
    prepared(host)
    with pytest.raises(GatewayError, match="bootstrap_prepared_binding_required"):
        b.reserve(host.value, "activate", binding)


@pytest.mark.parametrize("field,value", [("User", "nobody"), ("DynamicUser", "yes"), ("DropInPaths", "/etc/other.conf"),
    ("FragmentPath", "/usr/lib/systemd/system/v2node-900.service"), ("NeedDaemonReload", "yes"),
    ("ExecStart", "{ path=/bin/false ; argv[]=/bin/false ; }")])
def test_static_unit_identity_must_match_the_installed_plan(host, field, value):
    prepared(host)
    host.props[field] = value
    with pytest.raises(GatewayError, match="bootstrap_static_unit_identity_conflict"):
        b.reserve(host.value, "activate", host.value["binding_sha256"])


@pytest.mark.parametrize("state,pid,sub", [("active", "77", "running"), ("activating", "77", "start"), ("deactivating", "77", "stop"), ("inactive", "0", "auto-restart")])
def test_bootstrap_never_restarts_or_adopts_a_live_or_transitioning_unit(host, state, pid, sub):
    host.props.update(ActiveState=state, MainPID=pid, SubState=sub)
    with pytest.raises(GatewayError, match="bootstrap_target_not_inactive"):
        b.reserve(host.value, "prepare")


def test_valid_activation_checks_bound_certificate_before_allowing_start(host, monkeypatch):
    prepared(host)
    checked = []
    monkeypatch.setattr(b, "certificate_identity", lambda value: checked.append(value["request_id"]))
    b.reserve(host.value, "activate", host.value["binding_sha256"])
    assert checked == ["initial_job_900"] and b.load(900)["state"] == "prepared"
    # Lost prepared receipt can be inspected without writing a new generation.
    before = b.state_path(900).read_bytes()
    assert b.finish(900, readonly=True)["state"] == "prepared"
    assert b.state_path(900).read_bytes() == before


def test_activation_rejects_other_task_certificate_receipt(host, monkeypatch):
    prepared(host)
    result = SimpleNamespace(stdout=json.dumps({"state": "persisted", "profile_id": "origin_900", "request_id": "foreign_job_900",
        "generation_bound": True, "runtime_uid": 0, "tls_handshake_verified": False}))
    monkeypatch.setattr(b.LinuxSystem, "command", lambda *a, **k: result)
    with pytest.raises(GatewayError, match="bootstrap_certificate_not_bound"):
        b.reserve(host.value, "activate", host.value["binding_sha256"])


def test_unit_template_matches_real_installer_bytes(host):
    script = (Path(__file__).resolve().parents[1]/"install-nodepulse-v2node.sh").read_text()
    actual = script.split('cat >"${TMP_SERVICE_UNIT}" <<EOF\n', 1)[1].split("\nEOF", 1)[0] + "\n"
    actual = actual.replace("${NODE_ID}", "900").replace("${BINARY_PATH}", host.value["binary_path"]).replace("${CONFIG_PATH}", host.value["config_path"])
    assert actual.encode() == b.unit_text(900, host.value["binary_path"])


def test_runtime_verifies_actual_effective_and_filesystem_uid_config_binary_and_socket(host, monkeypatch):
    prepared(host)
    proc = host.root/"proc"; base = proc/"777"; base.mkdir(parents=True)
    group = "/system.slice/v2node-900.service"
    identity = {"pid": 777, "start": 123, "cgroup": group, "exe": host.value["binary_path"]}
    monkeypatch.setattr(b, "owned_group", lambda *a, **k: group)
    monkeypatch.setattr(b, "process_identity", lambda *a, **k: dict(identity))
    sockets = []
    monkeypatch.setattr(b, "verify", lambda listeners, actual: sockets.append((listeners, actual)))
    (base/"status").write_text("Name:\tv2node\nUid:\t0\t0\t0\t0\n")
    (base/"cmdline").write_bytes(b"\0".join(item.encode() for item in [host.value["binary_path"], "server", "--config", host.value["config_path"], "--watch=false"]) + b"\0")
    (base/"exe").write_bytes(b.path(host.value["binary_path"]).read_bytes())
    props = {**host.props, "MainPID": "777"}
    assert b.runtime_identity(host.value, props, proc) == 777 and sockets[0][1] == group
    (base/"status").write_text("Uid:\t0\t0\t0\t65534\n")
    with pytest.raises(GatewayError, match="bootstrap_effective_uid_conflict"):
        b.runtime_identity(host.value, props, proc)
    (base/"status").write_text("Uid:\t0\t65534\t0\t65534\n")
    with pytest.raises(GatewayError, match="bootstrap_effective_uid_conflict"):
        b.runtime_identity(host.value, props, proc)
    (base/"status").write_text("Uid:\t0\t0\t0\t0\n")
    (base/"exe").write_bytes(b"replaced-runtime")
    with pytest.raises(GatewayError, match="bootstrap_running_binary_changed"):
        b.runtime_identity(host.value, props, proc)


def test_active_receipt_and_lost_receipt_status_do_not_restart_or_mutate(host, monkeypatch):
    prepared(host)
    host.props.update(ActiveState="active", SubState="running", MainPID="777")
    monkeypatch.setattr(b, "certificate_identity", lambda value: {})
    monkeypatch.setattr(b, "runtime_identity", lambda *a: 777)
    before = b.state_path(900).read_bytes()
    status = b.finish(900, readonly=True)
    assert status["state"] == "activated" and status["runtime_pid"] == 777 and status["runtime_uid"] == 0
    assert status["service_uid_verified"] is True and status["tls_handshake_verified"] is False
    assert b.state_path(900).read_bytes() == before
    assert b.finish(900, active=True)["state"] == "activated"
    with pytest.raises(GatewayError, match="bootstrap_target_not_inactive"):
        b.reserve(host.value, "activate", host.value["binding_sha256"])
    # An interrupted activation may have recorded success before the caller
    # rolled back the target start. Same-task retry can resume an inactive unit.
    host.props.update(ActiveState="inactive", SubState="dead", MainPID="0")
    assert b.reserve(host.value, "activate", host.value["binding_sha256"])["state"] == "activated"
    assert b.finish(900, readonly=True)["state"] == "prepared"


@pytest.mark.parametrize("mode,uid,nlink", [(stat.S_IFREG|0o644, 0, 1), (stat.S_IFREG|0o600, 65534, 1),
    (stat.S_IFREG|0o600, 0, 2), (stat.S_IFLNK|0o777, 0, 1), (stat.S_IFREG|0o666, 0, 1)])
def test_private_receipt_rejects_wrong_mode_owner_links_and_type(mode, uid, nlink):
    parent = SimpleNamespace(parents=[], exists=lambda: True, is_symlink=lambda: False,
        lstat=lambda: SimpleNamespace(st_mode=stat.S_IFDIR|0o755, st_uid=0))
    target = SimpleNamespace(parent=parent, lstat=lambda: SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=nlink))
    with pytest.raises(GatewayError):
        b.trusted(target, private=True)
