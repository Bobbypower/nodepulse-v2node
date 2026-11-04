"""Task-bound receipts for a new, inactive, isolated v2node systemd unit.

Invoked by the root installer while it owns the host deployment lock. This is
not a network API. No certificate private key or panel configuration is output.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from .contract import GatewayError, Listener, require
from .linux import LinuxSystem, process_identity
from .node_listener import owned_group, prepare_bootstrap, runtime_binary, verify

ROOT = Path("/")
STATE_DIR = "/etc/nodepulse/bootstrap"


def path(value):
    return ROOT / value.lstrip("/")


def hashed(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def trusted(value, *, private=False, directory=False):
    """Reject foreign files rather than changing their permissions/ownership."""
    for parent in [value.parent, *value.parent.parents]:
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            sticky = bool(info.st_mode & stat.S_ISVTX)
            require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and
                    info.st_uid == 0 and (not info.st_mode & 0o022 or sticky), "untrusted_bootstrap_parent")
    info = value.lstat()
    require(info.st_uid == 0 and not stat.S_ISLNK(info.st_mode) and not info.st_mode & 0o022,
            "untrusted_bootstrap_file")
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
            "untrusted_bootstrap_file")
    if private:
        require(stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600), "bootstrap_private_mode_conflict")


def read(value, *, private=False):
    trusted(value, private=private)
    return value.read_bytes()


def state_path(node):
    return path(f"{STATE_DIR}/v2node-{node}.json")


def load(node):
    target = state_path(node)
    trusted(target.parent, private=True, directory=True)
    value = json.loads(read(target, private=True))
    body = {key: item for key, item in value.items() if key not in {"state", "binding_sha256"}}
    require(value.get("binding_sha256") == hashed(encoded(body)) and value.get("schema_version") == 1 and
            value.get("node_id") == node and value.get("state") in {"reserved", "prepared", "activated"},
            "invalid_bootstrap_receipt")
    return value


def save(value):
    target = state_path(value["node_id"])
    if not target.parent.exists():
        # Check ancestors before creating a root-owned private state directory.
        for parent in [target.parent.parent, *target.parent.parent.parents]:
            if parent.exists() or parent.is_symlink():
                info = parent.lstat()
                require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and
                        info.st_uid == 0 and not info.st_mode & 0o022, "untrusted_bootstrap_parent")
        target.parent.mkdir(parents=True, mode=0o700)
    trusted(target.parent, private=True, directory=True)
    if target.exists() or target.is_symlink():
        trusted(target, private=True)
    LinuxSystem("main", ROOT).atomic(target, encoded(value), 0o600)


def unit_text(node, binary):
    return (f"[Unit]\nDescription=NodePulse v2node {node}\nAfter=network-online.target\nWants=network-online.target\n\n"
            f"[Service]\nType=simple\nUser=root\nExecStart={binary} server --config /etc/v2node/v2node-{node}.json --watch=false\n"
            "Restart=on-failure\nRestartSec=3\nKillMode=control-group\nTimeoutStopSec=30s\nSendSIGKILL=yes\nLimitNOFILE=1048576\n\n"
            "[Install]\nWantedBy=multi-user.target\n").encode()


def specification(node, binary, local, server, profile, request, helper_sha256):
    require(1 <= node <= 9007199254740991 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", profile) and
            re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request), "invalid_bootstrap_task")
    runtime_binary(binary)
    require(binary != "/usr/local/bin/v2node", "bootstrap_requires_versioned_binary")
    require(bool(re.fullmatch(r"[a-f0-9]{64}", helper_sha256)), "bootstrap_requires_verified_helper")
    raw = read(Path(local), private=True)
    compiled_raw = read(Path(server))
    compiled = json.loads(compiled_raw)
    prepared = prepare_bootstrap(compiled, json.loads(raw), node)
    require(prepared == json.loads(raw), "bootstrap_local_endpoint_not_pinned")
    domain = compiled.get("tls_settings", {}).get("server_name")
    require(isinstance(domain, str) and len(domain) <= 253 and "." in domain and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", item) for item in domain.split(".")),
            "invalid_bootstrap_certificate_domain")
    value = {"schema_version": 1, "node_id": node, "profile_id": profile, "request_id": request,
             "binary_path": binary, "binary_sha256": binary.split("/")[-2],
             "helper_sha256": helper_sha256,
             "config_path": f"/etc/v2node/v2node-{node}.json", "config_sha256": hashed(raw),
             "unit_path": f"/etc/systemd/system/v2node-{node}.service", "unit_sha256": hashed(unit_text(node, binary)),
             "server_sha256": hashed(compiled_raw), "domain": domain,
             "cert_file": f"/etc/v2node/vless{node}.cer", "key_file": f"/etc/v2node/vless{node}.key",
             "listen_ip": compiled.get("listen_ip", "0.0.0.0"), "listen_port": compiled["server_port"],
             "planned_runtime_uid": 0}
    value["binding_sha256"] = hashed(encoded(value))
    value["state"] = "reserved"
    return value


def properties(node):
    output = LinuxSystem("main").command([
        "systemctl", "show", f"v2node-{node}.service", "--no-pager",
        "--property=Id,LoadState,ActiveState,SubState,FragmentPath,User,Group,DynamicUser,ExecStart,DropInPaths,MainPID,NeedDaemonReload"
    ], "bootstrap_unit_inspection_failed").stdout
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def inactive(props):
    require(props.get("ActiveState") in {"inactive", "failed"} and props.get("MainPID") == "0" and
            props.get("SubState") not in {"auto-restart", "start", "start-pre", "start-post", "stop", "stop-sigterm"},
            "bootstrap_target_not_inactive")


def static_identity(value, props=None):
    for field, private in [("binary_path", False), ("config_path", True), ("unit_path", False)]:
        content = read(path(value[field]), private=private)
        require(hashed(content) == value[field.replace("_path", "_sha256")], "bootstrap_identity_changed")
    config = json.loads(read(path(value["config_path"]), private=True))
    require(len(config.get("Nodes", [])) == 1 and config["Nodes"][0].get("NodeID") == value["node_id"],
            "bootstrap_config_identity_conflict")
    props = properties(value["node_id"]) if props is None else props
    args = f'{value["binary_path"]} server --config {value["config_path"]} --watch=false'
    starts = re.findall(r"\{ path=(.*?) ; argv\[\]=(.*?) ;", props.get("ExecStart", ""))
    require(props.get("Id") == f'v2node-{value["node_id"]}.service' and props.get("LoadState") == "loaded" and
            props.get("FragmentPath") == value["unit_path"] and props.get("User") == "root" and
            props.get("Group", "") in {"", "root"} and props.get("DynamicUser") == "no" and
            props.get("DropInPaths", "") == "" and props.get("NeedDaemonReload") == "no" and
            starts == [(value["binary_path"], args)] and props.get("ExecStart", "").count("{") == 1,
            "bootstrap_static_unit_identity_conflict")
    return props


def certificate_files(value):
    directory = path(value["key_file"]).parent
    result = [path(value["cert_file"])] if path(value["cert_file"]).exists() or path(value["cert_file"]).is_symlink() else []
    if directory.exists():
        stem = Path(value["key_file"]).name
        result.extend(item for item in directory.iterdir() if item.name == stem or item.name.startswith(stem + "."))
    return result


def certificate_identity(value):
    command = [value["binary_path"], "certificate", "inspect", "--profile-id", value["profile_id"]]
    result = LinuxSystem("main").command(command, "bootstrap_certificate_not_bound", timeout=30)
    receipt = json.loads(result.stdout)
    require(receipt.get("state") == "persisted" and receipt.get("profile_id") == value["profile_id"] and
            receipt.get("request_id") == value["request_id"] and receipt.get("generation_bound") is True and
            receipt.get("runtime_uid") == 0 and receipt.get("tls_handshake_verified") is False,
            "bootstrap_certificate_not_bound")
    return receipt


def reserve(value, phase, binding=""):
    target = state_path(value["node_id"])
    for field in ["config_path", "unit_path", "key_file", "binary_path"]:
        parent = path(value[field]).parent
        while not parent.exists() and not parent.is_symlink():
            parent = parent.parent
        trusted(parent, directory=True)
    props = properties(value["node_id"])
    inactive(props)
    if not target.exists() and not target.is_symlink():
        require(phase == "prepare", "bootstrap_preparation_receipt_missing")
        require(props.get("LoadState") == "not-found" and props.get("FragmentPath", "") == "",
                "bootstrap_unowned_existing_unit")
        require(not any(item.exists() or item.is_symlink() for item in [path(value["config_path"]), path(value["unit_path"])]),
                "bootstrap_unowned_existing_config")
        require(not certificate_files(value), "bootstrap_unowned_existing_certificate")
        save(value)  # Reserve identity before writing any target deployment file.
        return value
    previous = load(value["node_id"])
    require(previous["binding_sha256"] == value["binding_sha256"], "bootstrap_task_or_configuration_changed")
    if phase == "activate":
        require(previous["state"] in {"prepared", "activated"} and binding == previous["binding_sha256"], "bootstrap_prepared_binding_required")
        static_identity(previous, props)
        certificate_identity(previous)
    else:
        require(previous["state"] != "activated", "bootstrap_already_activated_reconcile_required")
        # Retry only owns files matching its immutable reservation. Unknown
        # certificate files are never adopted by prepare, even for the same ID.
        for field, private in [("config_path", True), ("unit_path", False), ("binary_path", False)]:
            item = path(previous[field])
            if item.exists() or item.is_symlink():
                require(hashed(read(item, private=private)) == previous[field.replace("_path", "_sha256")],
                        "bootstrap_identity_changed")
        require(not certificate_files(previous), "bootstrap_certificate_exists_use_activate_or_reconcile")
    return previous


def runtime_identity(value, props, proc=Path("/proc")):
    node = value["node_id"]
    group = owned_group(node, binary=value["binary_path"])
    require(group is not None, "bootstrap_runtime_identity_missing")
    pid = int(props.get("MainPID") or "0")
    identity = process_identity(pid, proc)
    require(identity and identity["cgroup"] == group and identity["exe"] == value["binary_path"], "bootstrap_runtime_identity_conflict")
    status = (proc / str(pid) / "status").read_text()
    uids = next(line.split()[1:] for line in status.splitlines() if line.startswith("Uid:"))
    require(len(uids) == 4 and uids[1] == uids[3] == "0", "bootstrap_effective_uid_conflict")
    args = (proc / str(pid) / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
    require(args == [value["binary_path"], "server", "--config", value["config_path"], "--watch=false"],
            "bootstrap_runtime_config_conflict")
    require(hashed((proc / str(pid) / "exe").read_bytes()) == value["binary_sha256"],
            "bootstrap_running_binary_changed")
    verify([Listener("tcp", value["listen_ip"], value["listen_port"])], group)
    require(process_identity(pid, proc) == identity, "bootstrap_process_changed_during_verification")
    return pid


def finish(node, active=False, readonly=False):
    value = load(node)
    require(readonly or value["state"] in ({"prepared", "activated"} if active else {"reserved", "prepared"}),
            "bootstrap_already_activated_reconcile_required")
    props = static_identity(value)
    if readonly:
        active = props.get("ActiveState") == "active"
    pid = None
    if not active:
        inactive(props)
    else:
        require(value["state"] in {"prepared", "activated"} and props.get("ActiveState") == "active", "bootstrap_target_not_active")
        certificate_identity(value)
        pid = runtime_identity(value, props)
    require(not readonly or active or value["state"] in {"prepared", "activated"}, "bootstrap_requires_reconciliation")
    value["state"] = "activated" if active else "prepared"
    if not readonly:
        save(value)
    return {**value, "service_started": active, "service_uid_verified": active, "runtime_pid": pid,
            "runtime_uid": 0 if active else None, "tls_handshake_verified": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["reserve", "prepared", "activated", "status", "hold"])
    parser.add_argument("--node", type=int, required=True)
    parser.add_argument("--binary")
    parser.add_argument("--local")
    parser.add_argument("--server")
    parser.add_argument("--profile-id")
    parser.add_argument("--request-id")
    parser.add_argument("--phase", choices=["prepare", "activate"])
    parser.add_argument("--binding-sha256", default="")
    parser.add_argument("--helper-sha256")
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0 and 1 <= args.node <= 9007199254740991, "bootstrap_requires_root")
        if args.action == "reserve":
            require(all([args.binary, args.local, args.server, args.profile_id, args.request_id, args.phase, args.helper_sha256]), "missing_bootstrap_arguments")
            value = specification(args.node, args.binary, args.local, args.server, args.profile_id, args.request_id, args.helper_sha256)
            reserve(value, args.phase, args.binding_sha256)
        elif args.action in {"status", "hold"}:
            with LinuxSystem("main").lock():
                if args.action == "hold":
                    value = load(args.node)
                    require(args.binding_sha256 == value["binding_sha256"], "bootstrap_prepared_binding_required")
                    # Prove actual identity before stopping only our new unit.
                    finish(args.node, readonly=True)
                    for action in ["stop", "disable"]:
                        LinuxSystem("main").command(["systemctl", action, f"v2node-{args.node}.service"], "bootstrap_hold_unknown", timeout=45)
                print(json.dumps(finish(args.node, readonly=True)))
        else:
            print(json.dumps(finish(args.node, active=args.action == "activated")))
        return 0
    except GatewayError as error:
        print(error.code, file=sys.stderr)
        return 1
    except Exception:
        print("bootstrap_inspection_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
