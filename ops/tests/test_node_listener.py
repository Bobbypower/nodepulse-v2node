"""The node installer must verify the real backend, not a nearby Caddy port."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_gateway.contract import GatewayError, Listener
from edge_gateway.linux import decode_address
from edge_gateway.node_listener import bindings, verify, prepare_bootstrap, runtime_binary


def config():
    return {"protocol": "vless", "network": "xhttp", "tls": 0, "listen_ip": "0.0.0.0", "server_port": 443,
            "network_settings": {"path": "/a"}, "encryption": "preserve-encryption", "encryption_settings": {"private_key": "synthetic-private"}}


def local():
    return {"Nodes": [{"NodeID": 45, "ListenIP": "127.0.0.1", "ListenPort": 25445}]}


def row(protocol="tcp", address="127.0.0.1", group="/system.slice/v2node-45.service", *, known=True):
    return {"protocol": protocol, "address": address, "port": 25445, "inode": "123",
            "owners": [{"pid": 777, "start": 1, "cgroup": group}] if known else []}


def test_local_endpoint_is_not_the_public_entry_and_does_not_rewrite_protocol():
    server = config()
    original = deepcopy(server)
    assert bindings(server, local(), 45, 25445) == [Listener("tcp", "127.0.0.1", 25445)]
    assert server == original
    with pytest.raises(GatewayError, match="node_port_does_not_match_actual_listener"):
        bindings(server, local(), 45, 443)


def test_multiple_nodes_in_one_local_config_are_explicitly_rejected():
    value = local()
    value["Nodes"].append({"NodeID": 46})
    with pytest.raises(GatewayError, match="ambiguous_local_node"):
        bindings(config(), value, 45)


@pytest.mark.parametrize("alpn,tls", [(["h2", "h3"], 1), (["h3"], 0)])
def test_direct_h3_security_and_alpn_are_validated_before_install(alpn, tls):
    value = config()
    value.update(tls=tls, tls_settings={"alpn": alpn})
    with pytest.raises(GatewayError, match="invalid_direct_h3"):
        bindings(value, local(), 45)


def test_h3_requires_own_tcp_and_udp_evidence_as_applicable():
    group = "/system.slice/v2node-45.service"
    wanted = [Listener("tcp", "127.0.0.1", 25445), Listener("udp", "127.0.0.1", 25445)]
    with pytest.raises(GatewayError, match="node_listener_missing"):
        verify(wanted, group, inventory=[row()])
    verify(wanted, group, inventory=[row(), row("udp")])


@pytest.mark.parametrize("actual", [
    row(group="/system.slice/nodepulse-edge-main.service"), row(address="::", group="/other"), row(known=False),
])
def test_caddy_or_foreign_or_unknown_owner_is_not_node_install_success(actual):
    with pytest.raises(GatewayError, match="node_listener_owned_by_other_process"):
        verify([Listener("tcp", "127.0.0.1", 25445)], "/system.slice/v2node-45.service", inventory=[actual])


def test_preflight_allows_only_explicit_owned_old_runtime():
    wanted = [Listener("tcp", "127.0.0.1", 25445)]
    verify(wanted, None, preflight=True, inventory=[])
    verify(wanted, None, preflight=True, extra_groups=["/system.slice/v2node.service"], inventory=[row(group="/system.slice/v2node.service")])
    with pytest.raises(GatewayError, match="node_listener_owned_by_other_process"):
        verify(wanted, None, preflight=True, inventory=[row(group="/system.slice/other-node.service")])


def test_download_listener_retains_its_own_address_and_transport():
    server = config()
    server["xhttp_download_inbound"] = {"listen_ip": "::1", "server_port": 25446, "security": "none", "network": "xhttp", "network_settings": {"path": "/a"}}
    assert bindings(server, local(), 45) == [Listener("tcp", "127.0.0.1", 25445), Listener("tcp", "::1", 25446)]


def bootstrap_config():
    return {"protocol":"vless","network":"xhttp","tls":1,"listen_ip":"0.0.0.0","server_port":8443,
            "tls_settings":{"server_name":"node.example.invalid","alpn":["h2"]},
            "cert_config":{"cert_mode":"file","cert_file":"/etc/v2node/vless45.cer","key_file":"/etc/v2node/vless45.key"}}


def test_bootstrap_pins_only_this_nodes_origin_and_preserves_input():
    server=bootstrap_config();config={"Nodes":[{"NodeID":45,"ApiKey":"synthetic-node-token"}]};before=deepcopy(config)
    prepared=prepare_bootstrap(server,config,45,8443)
    assert config==before and prepared["Nodes"][0]["ListenPort"]==8443 and prepared["Nodes"][0]["ListenIP"]=="0.0.0.0"
    assert prepared["Nodes"][0]["ApiKey"]=="synthetic-node-token"


@pytest.mark.parametrize("mutation",[
    lambda s:s["cert_config"].update(key_file="/etc/v2node/vless66.key"),
    lambda s:s["cert_config"].update(dns_env="synthetic-credential"),
    lambda s:s.update(tls=0),
    lambda s:s["tls_settings"].update(alpn=["h3"]),
])
def test_bootstrap_rejects_foreign_keys_credentials_and_wrong_transport(mutation):
    server=bootstrap_config();mutation(server)
    with pytest.raises(GatewayError):prepare_bootstrap(server,{"Nodes":[{"NodeID":45}]},45,8443)


def test_isolated_runtime_path_is_fixed_to_an_immutable_sha_namespace():
    expected="/usr/local/lib/nodepulse/v2node/"+"a"*64+"/v2node"
    assert runtime_binary(expected)==expected
    for invalid in ["/tmp/v2node","/usr/local/lib/nodepulse/v2node/latest/v2node","/usr/local/lib/nodepulse/v2node/"+"a"*64+"/../v2node"]:
        with pytest.raises(GatewayError):runtime_binary(invalid)


def test_process_identity_uses_the_approved_versioned_binary(monkeypatch):
    import json
    from pathlib import PurePosixPath
    import edge_gateway.node_listener as module
    expected="/usr/local/lib/nodepulse/v2node/"+"a"*64+"/v2node"
    group="/system.slice/v2node-45.service"
    class System:
        def show(self,unit):return {"ActiveState":"active","ControlGroup":group,"MainPID":"777"}
    monkeypatch.setattr(module,"LinuxSystem",lambda _:System())
    monkeypatch.setattr(module,"process_identity",lambda _:{"pid":777,"exe":expected,"cgroup":group})
    class LinuxPath(PurePosixPath):
        def is_symlink(self):return False
        def read_bytes(self):
            if self.as_posix()=="/proc/777/cmdline":return (expected+"\0server\0--config\0/etc/v2node/v2node-45.json\0--watch=false\0").encode()
            return json.dumps({"Nodes":[{"NodeID":45}]}).encode()
    monkeypatch.setattr(module,"Path",LinuxPath)
    assert module.owned_group(45,binary=expected)==group
    with pytest.raises(GatewayError,match="node_process_identity_conflict"):module.owned_group(45)
