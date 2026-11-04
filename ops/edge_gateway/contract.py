"""Strict, deterministic host configuration compiler; standard library only."""
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import re


class GatewayError(Exception):
    def __init__(self, code, **facts):
        super().__init__(code)
        self.code, self.facts = code, facts


def require(condition, code):
    if not condition:
        raise GatewayError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def fields(value, allowed, required=()):
    require(isinstance(value, dict) and set(value) <= set(allowed) and set(required) <= set(value), "invalid_fields")


def positive(value, maximum=2**53 - 1):
    require(type(value) is int and 0 < value <= maximum, "invalid_integer")
    return value


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,15}", value), "invalid_instance_id")
    return value


def domain(value):
    require(isinstance(value, str) and len(value) <= 253 and value == value.lower(), "invalid_domain")
    require(all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in value.split("."))
            and "." in value, "invalid_domain")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise GatewayError("invalid_domain")


def cert_path(value, instance):
    prefix = f"/var/lib/nodepulse-edge/{instance}/certs/"
    require(isinstance(value, str) and value.startswith(prefix) and
            re.fullmatch(r"[a-zA-Z0-9_.-]+", value[len(prefix):]) and value[len(prefix):] not in {".", ".."}, "invalid_certificate_path")
    return value


def endpoint(address, port):
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


@dataclass(frozen=True)
class Listener:
    protocol: str
    address: str
    port: int


def normalize_plan(value):
    fields(value, {"schema_version", "instance_id", "revision", "grace_seconds", "routes"},
           {"schema_version", "instance_id", "revision", "routes"})
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, "unsupported_schema")
    instance = identifier(value["instance_id"])
    revision = positive(value["revision"])
    grace = positive(value.get("grace_seconds", 5), 60)
    require(isinstance(value["routes"], list) and len(value["routes"]) <= 256, "invalid_routes")
    routes, seen, listeners = [], set(), {}
    for item in value["routes"]:
        fields(item, {"route_id", "node_id", "mode", "domain", "path", "listen", "port", "protocols", "certificate", "backend"},
               {"route_id", "node_id", "mode", "domain", "path", "listen", "port", "protocols", "certificate", "backend"})
        require(item["mode"] == "managed_caddy", "external_cdn_must_bypass_caddy")
        rid = item["route_id"]
        require(isinstance(rid, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", rid) and rid not in seen, "duplicate_or_invalid_route")
        seen.add(rid)
        node = positive(item["node_id"])
        host = domain(item["domain"])
        path = item["path"]
        require(isinstance(path, str) and re.fullmatch(r"/[A-Za-z0-9_/-]*", path) and "//" not in path
                and (path == "/" or not path.endswith("/")), "invalid_path")
        require(isinstance(item["listen"], list) and 1 <= len(item["listen"]) <= 8, "invalid_listen")
        try:
            addresses = sorted({str(ipaddress.ip_address(addr)) for addr in item["listen"]})
        except (ValueError, TypeError):
            raise GatewayError("invalid_listen") from None
        require(len(addresses) == len(item["listen"]), "duplicate_listen")
        port = positive(item["port"], 65535)
        require(item["protocols"] in (["h2"], ["h2", "h3"]), "unsupported_public_protocols")
        protocols = item["protocols"]
        for addr in addresses:
            key = (addr, port)
            require(key not in listeners or listeners[key] == protocols, "listener_policy_conflict")
            listeners[key] = protocols
        cert = item["certificate"]
        fields(cert, {"cert_file", "key_file", "probe_ca_file"}, {"cert_file", "key_file"})
        cert = {key: cert_path(val, instance) for key, val in cert.items()}
        backend = item["backend"]
        fields(backend, {"address", "port", "transport", "server_name", "host", "ca_file", "local_config_sha256"},
               {"address", "port", "transport", "local_config_sha256"})
        try:
            address = ipaddress.ip_address(backend["address"])
        except (ValueError, TypeError):
            raise GatewayError("invalid_backend_address") from None
        require(address.is_loopback, "backend_must_be_loopback")
        positive(backend["port"], 65535)
        require(backend["transport"] in {"h2c", "h2_tls"}, "unsupported_backend_transport")
        require(isinstance(backend["local_config_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", backend["local_config_sha256"]), "invalid_backend_digest")
        if "host" in backend:
            domain(backend["host"])
        if backend["transport"] == "h2_tls":
            domain(backend.get("server_name"))
            if "ca_file" in backend:
                cert_path(backend["ca_file"], instance)
        else:
            require(not ({"server_name", "ca_file"} & set(backend)), "plaintext_backend_has_tls_options")
        require(not any(r["domain"] == host and r["path"] == path and r["port"] == port and
                        set(r["listen"]) & set(addresses) for r in routes), "route_match_conflict")
        routes.append({**item, "listen": addresses, "certificate": cert, "backend": {**backend, "address": str(address)}})
    # One socket cannot have multiple server configurations, including wildcard overlap.
    groups = {}
    for route in routes:
        key = (tuple(route["listen"]), route["port"], tuple(route["protocols"]))
        for other in groups:
            if key == other or key[1] != other[1]:
                continue
            for a in key[0]:
                for b in other[0]:
                    require(not overlaps(a, b), "listener_overlap")
        groups.setdefault(key, []).append(route)
    return {"schema_version": 1, "instance_id": instance, "revision": revision, "grace_seconds": grace,
            "routes": sorted(routes, key=lambda r: r["route_id"])}


def overlaps(a, b):
    left, right = ipaddress.ip_address(a), ipaddress.ip_address(b)
    # IPv6 wildcard may also own IPv4. Fail closed rather than assuming V6ONLY.
    return left == right or str(left) == "::" or str(right) == "::" or (
        left.version == right.version and (left.is_unspecified or right.is_unspecified))


def expected_listeners(plan):
    return sorted({Listener(transport, addr, r["port"]) for r in plan["routes"]
                   for addr in r["listen"] for transport in (["tcp", "udp"] if "h3" in r["protocols"] else ["tcp"])},
                  key=lambda e: (e.protocol, e.address, e.port))


def compile_caddy(plan):
    instance = plan["instance_id"]
    servers, files, groups = {}, {}, {}
    for route in plan["routes"]:
        key = (tuple(route["listen"]), route["port"], tuple(route["protocols"]))
        groups.setdefault(key, []).append(route)
        cert = route["certificate"]
        files[(cert["cert_file"], cert["key_file"])] = {"certificate": cert["cert_file"], "key": cert["key_file"]}
    for index, ((addresses, port, protocols), routes) in enumerate(sorted(groups.items())):
        handles = []
        for route in sorted(routes, key=lambda r: (-len(r["path"]), r["route_id"])):
            backend = route["backend"]
            transport = {"protocol": "http", "versions": ["h2c"] if backend["transport"] == "h2c" else ["2"]}
            if backend["transport"] == "h2_tls":
                transport["tls"] = {"server_name": backend["server_name"]}
                if backend.get("ca_file"):
                    transport["tls"]["root_ca_pem_files"] = [backend["ca_file"]]
            path = route["path"]
            request_headers = {"delete": ["X-Forwarded-For", "Forwarded", "CF-Connecting-IP", "X-Real-IP"]}
            if backend.get("host"):
                request_headers["set"] = {"Host": [backend["host"]]}
            handles.append({"@id": "nodepulse_route_" + route["route_id"],
                "match": [{"host": [route["domain"]], "path": ["/*"] if path == "/" else [path, path + "/*"]}],
                "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": endpoint(backend["address"], backend["port"])}],
                    "transport": transport, "flush_interval": -1,
                    "headers": {"request": request_headers}}],
                "terminal": True})
        handles.append({"handle": [{"handler": "static_response", "status_code": 404}]})
        # Caddy 2.10's TLS TCP accept path requires h1 in this server list even
        # for h2 ALPN. Advertise only h2 on TLS/TCP; HTTP/3 owns its UDP ALPN.
        servers[f"managed_{index}"] = {"listen": [endpoint(a, port) for a in addresses], "protocols": ["h1", *protocols],
            "routes": handles, "tls_connection_policies": [{"alpn": ["h2"]}], "automatic_https": {"disable": True}}
    config = {"admin": {"listen": f"unix//run/nodepulse-edge-{instance}/admin.sock", "config": {"persist": False}},
              "apps": {"http": {"grace_period": f"{plan['grace_seconds']}s", "servers": servers}}}
    if files:
        config["apps"]["tls"] = {"certificates": {"load_files": [files[key] for key in sorted(files)]}}
    return config


def validate_backend_profile(server, route):
    backend = route["backend"]
    require(isinstance(server, dict) and server.get("network") in {"xhttp", "splithttp"}
            and not server.get("trusted_x_forwarded_for"), "unsafe_backend_transport")
    settings = server.get("network_settings") or {}
    require(isinstance(settings, dict), "unsafe_backend_transport")
    path = settings.get("path") or "/"
    require(isinstance(path, str) and (path.rstrip("/") or "/") == route["path"], "backend_path_mismatch")
    host = settings.get("host") or ""
    require(not host or host == backend.get("host", route["domain"]), "backend_host_mismatch")
    if backend["transport"] == "h2c":
        require(server.get("tls") == 0, "backend_security_mismatch")
    else:
        require(server.get("tls") == 1 and server.get("tls_settings", {}).get("alpn") == ["h2"], "backend_security_mismatch")


def unit_text(instance, grace=5):
    identifier(instance)
    return f"""# NodePulse-owned Edge Gateway v1: {instance}
[Unit]
Description=NodePulse Edge Gateway {instance}
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=nodepulse-edge-{instance}
Group=nodepulse-edge-{instance}
ExecStart=/usr/local/libexec/nodepulse-edge/{instance}/caddy run --config /etc/nodepulse-edge/{instance}/active.json
ExecReload=/usr/local/libexec/nodepulse-edge/{instance}/caddy reload --config /etc/nodepulse-edge/{instance}/active.json --address unix//run/nodepulse-edge-{instance}/admin.sock
Restart=on-failure
RestartSec=3
KillMode=control-group
TimeoutStopSec={grace + 10}s
SendSIGKILL=yes
RuntimeDirectory=nodepulse-edge-{instance}
RuntimeDirectoryMode=0750
StateDirectory=nodepulse-edge/{instance}
StateDirectoryMode=0700
Environment=XDG_DATA_HOME=/var/lib/nodepulse-edge/{instance}/data
Environment=XDG_CONFIG_HOME=/var/lib/nodepulse-edge/{instance}/config
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/nodepulse-edge/{instance} /run/nodepulse-edge-{instance}
LimitNOFILE=1048576
UMask=0077

[Install]
WantedBy=multi-user.target
"""
