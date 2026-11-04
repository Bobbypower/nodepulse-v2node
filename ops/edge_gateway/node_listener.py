"""Actual v2node listener compilation and Linux ownership checks for its installer."""
import argparse
import ipaddress
import json
from copy import deepcopy
import re
from pathlib import Path
import sys

from .contract import GatewayError, Listener, canonical, positive, require
from .linux import LinuxSystem, in_group, matches, process_identity, socket_table


def bindings(server, local, node_id, requested_port=0):
    require(isinstance(server, dict) and server.get("status") != "fail", "invalid_server_config")
    require(isinstance(local, dict) and isinstance(local.get("Nodes"), list), "invalid_local_config")
    require(len(local["Nodes"]) == 1 and local["Nodes"][0].get("NodeID") == node_id, "ambiguous_local_node")
    item = local["Nodes"][0]
    try:
        port = int(item.get("ListenPort") or server.get("server_port") or 0)
        address = str(ipaddress.ip_address(item.get("ListenIP") or server.get("listen_ip") or "0.0.0.0"))
    except (ValueError, TypeError):
        raise GatewayError("invalid_listener") from None
    positive(port, 65535)
    require(not requested_port or requested_port == port, "node_port_does_not_match_actual_listener")
    alpn = server.get("tls_settings", {}).get("alpn", [])
    protocol, network = server.get("protocol"), server.get("network")
    if "h3" in alpn:
        require(alpn == ["h3"] and server.get("tls") == 1 and network in {"xhttp", "splithttp"}, "invalid_direct_h3")
    transport = "udp" if protocol in {"tuic", "hysteria2"} or alpn == ["h3"] else "tcp"
    result = [Listener(transport, address, port)]
    download = server.get("xhttp_download_inbound")
    if download is not None:
        require(isinstance(download, dict) and network == "xhttp" and download.get("network", "xhttp") == "xhttp", "invalid_download_network")
        require(download.get("security", "none") == "none", "download_security_must_be_none")
        require(download.get("network_settings", {}).get("path", server.get("network_settings", {}).get("path", "/")) ==
                server.get("network_settings", {}).get("path", "/"), "download_requires_same_normalized_path")
        try:
            number = int(download.get("server_port") or 0)
            address = str(ipaddress.ip_address(download.get("listen_ip") or "0.0.0.0"))
        except (ValueError, TypeError):
            raise GatewayError("invalid_download_listener") from None
        positive(number, 65535)
        result.append(Listener("tcp", address, number))
    return list(dict.fromkeys(result))


def runtime_binary(value):
    require(value == "/usr/local/bin/v2node" or bool(re.fullmatch(
        r"/usr/local/lib/nodepulse/v2node/[a-f0-9]{64}/v2node", value)), "invalid_runtime_binary_path")
    return value


def prepare_bootstrap(server, local, node, requested_port=0):
    """Pin a new external H2 origin before any service is started."""
    require(server.get("protocol") == "vless" and server.get("network") == "xhttp" and
            type(server.get("tls")) is int and server.get("tls") == 1 and type(server.get("server_port")) is int and
            server.get("tls_settings", {}).get("alpn") == ["h2"] and
            server.get("xhttp_download_inbound") is None, "bootstrap_requires_external_h2_origin")
    cert = server.get("cert_config")
    require(isinstance(cert, dict) and cert.get("cert_mode") == "file" and
            set(cert).issubset({"cert_mode", "cert_file", "key_file", "reject_unknown_sni"}) and
            cert.get("cert_file") == f"/etc/v2node/vless{node}.cer" and
            cert.get("key_file") == f"/etc/v2node/vless{node}.key", "bootstrap_requires_managed_node_certificate_paths")
    require(cert.get("reject_unknown_sni", "0") in {"0", "1"}, "invalid_bootstrap_sni_flag")
    require(isinstance(local.get("Nodes"), list) and len(local["Nodes"]) == 1 and
            type(local["Nodes"][0].get("NodeID")) is int, "invalid_bootstrap_node_identity")
    wanted = bindings(server, local, node, requested_port)
    require(len(wanted) == 1 and wanted[0].protocol == "tcp" and wanted[0].port == server.get("server_port") and
            wanted[0].address == server.get("listen_ip", "0.0.0.0"), "bootstrap_listener_override_conflict")
    ip = ipaddress.ip_address(wanted[0].address)
    require(not ip.is_loopback and not ip.is_link_local and not ip.is_multicast, "bootstrap_origin_must_be_reachable")
    result = deepcopy(local)
    result["Nodes"][0]["ListenIP"], result["Nodes"][0]["ListenPort"] = wanted[0].address, wanted[0].port
    return result


def owned_group(node, *, generic=False, binary="/usr/local/bin/v2node"):
    binary = runtime_binary("/usr/local/bin/v2node" if generic else binary)
    system = LinuxSystem("main")
    unit = "v2node.service" if generic else f"v2node-{node}.service"
    props = system.show(unit)
    if props.get("ActiveState") != "active":
        return None
    group = f"/system.slice/{unit}"
    require(props.get("ControlGroup") == group, "node_unit_cgroup_conflict")
    identity = process_identity(int(props.get("MainPID") or 0))
    require(identity and identity["exe"] == binary and identity["cgroup"] == group, "node_process_identity_conflict")
    args = (Path("/proc") / str(identity["pid"]) / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
    require(len(args) == 5 and args[0:2] == [binary, "server"] and args[2] == "--config"
            and args[4] == "--watch=false", "node_process_identity_conflict")
    path = Path(args[3])
    require(path.is_absolute() and not path.is_symlink(), "node_config_identity_conflict")
    config = json.loads(path.read_bytes())
    require(len(config.get("Nodes", [])) == 1 and config["Nodes"][0].get("NodeID") == node, "generic_service_belongs_to_other_node")
    return group


def verify(listeners, group, *, preflight=False, extra_groups=(), inventory=None):
    allowed = [group, *extra_groups]
    rows = socket_table() if inventory is None else inventory
    for listener in listeners:
        found = [row for row in rows if matches(row, listener)]
        require(preflight or found, "node_listener_missing")
        require(all(row["owners"] and all(any(in_group(owner["cgroup"], item) for item in allowed if item)
                    for owner in row["owners"]) for row in found), "node_listener_owned_by_other_process")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["compile", "prepare", "preflight", "verify", "owned-generic"])
    parser.add_argument("--node", type=int, required=True)
    parser.add_argument("--server")
    parser.add_argument("--local")
    parser.add_argument("--requested-port", type=int, default=0)
    parser.add_argument("--binary", default="/usr/local/bin/v2node")
    args = parser.parse_args()
    try:
        positive(args.node)
        runtime_binary(args.binary)
        if args.action == "owned-generic":
            require(owned_group(args.node, generic=True) is not None, "generic_not_active")
            return 0
        server, local = json.loads(Path(args.server).read_bytes()), json.loads(Path(args.local).read_bytes())
        if args.action == "prepare":
            json.dump(prepare_bootstrap(server, local, args.node, args.requested_port), sys.stdout)
            return 0
        listeners = bindings(server, local, args.node, args.requested_port)
        if args.action != "compile":
            own = owned_group(args.node, binary=args.binary)
            others = []
            if args.action == "preflight":
                props = LinuxSystem("main").show("v2node.service")
                if props.get("ActiveState") == "active":
                    others.append(owned_group(args.node, generic=True))
            else:
                require(own is not None, "node_unit_not_active")
            verify(listeners, own, preflight=args.action == "preflight", extra_groups=others)
        for item in listeners:
            print(f"{item.protocol}:{item.port}")
        return 0
    except GatewayError as error:
        print(error.code, file=sys.stderr)
        return 1
    except Exception:
        print("node_listener_inspection_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
