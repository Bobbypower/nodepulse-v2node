"""Linux/systemd adapter. Signals are unit-scoped; PID files are never trusted."""
from contextlib import contextmanager
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

from .contract import GatewayError, Listener, canonical, endpoint, identifier, overlaps, require, unit_text, validate_backend_profile


def file_hash(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(handle.read()).hexdigest()


def process_identity(pid, proc=Path("/proc")):
    base = proc / str(pid)
    try:
        stat = (base / "stat").read_text().rsplit(")", 1)[1].split()
        cgroups = (base / "cgroup").read_text().splitlines()
        unified = next(line[3:] for line in cgroups if line.startswith("0::"))
        return {"pid": pid, "start": int(stat[19]), "state": stat[0], "cgroup": unified,
                "exe": os.readlink(base / "exe")}
    except (OSError, ValueError, StopIteration):
        return None


def in_group(value, group):
    return value == group or value.startswith(group + "/")


def decode_address(raw, family):
    address, port = raw.split(":")
    value = bytes.fromhex(address)
    # Linux proc inet words are printed in native byte order on supported LE hosts.
    value = b"".join(value[index:index + 4][::-1] for index in range(0, len(value), 4))
    ip = ipaddress.ip_address(socket.inet_ntop(family, value))
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip), int(port, 16)


def socket_table(proc=Path("/proc")):
    records = []
    for name, protocol, family in (("tcp", "tcp", socket.AF_INET), ("tcp6", "tcp", socket.AF_INET6),
                                  ("udp", "udp", socket.AF_INET), ("udp6", "udp", socket.AF_INET6)):
        path = proc / "net" / name
        require(path.exists(), "socket_inventory_unavailable")
        try:
            for line in path.read_text().splitlines()[1:]:
                columns = line.split()
                if protocol == "tcp" and columns[3] != "0A":
                    continue  # Established/TIME_WAIT do not own a TCP listener.
                address, port = decode_address(columns[1], family)
                if port:
                    records.append({"protocol": protocol, "address": address, "port": port,
                                    "inode": columns[9], "owners": []})
        except (OSError, ValueError, IndexError):
            raise GatewayError("socket_inventory_unavailable") from None
    by_inode = {row["inode"]: [] for row in records}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            for fd in (entry / "fd").iterdir():
                try:
                    link = os.readlink(fd)
                except OSError:
                    continue
                if link.startswith("socket:[") and link[8:-1] in by_inode:
                    identity = process_identity(int(entry.name), proc)
                    if identity and identity not in by_inode[link[8:-1]]:
                        by_inode[link[8:-1]].append(identity)
        except OSError:
            continue
    for record in records:
        record["owners"] = by_inode[record["inode"]]
    return records


def matches(row, listener):
    return row["protocol"] == listener.protocol and row["port"] == listener.port and overlaps(row["address"], listener.address)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=5)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LinuxSystem:
    def __init__(self, instance, root=Path("/")):
        self.instance = identifier(instance)
        self.root = root
        self.unit = f"nodepulse-edge-{instance}.service"
        self.group = f"/system.slice/{self.unit}"
        self.user = f"nodepulse-edge-{instance}"
        self.config = self.path(f"/etc/nodepulse-edge/{instance}/active.json")
        self.unit_path = self.path(f"/etc/systemd/system/{self.unit}")
        self.binary = self.path(f"/usr/local/libexec/nodepulse-edge/{instance}/caddy")
        # Caddy owns its home, never the parent of the authoritative root state.
        self.state_dir = self.path(f"/var/lib/nodepulse-edge-control/{instance}")

    def path(self, absolute):
        return self.root / absolute.lstrip("/")

    def safe(self, path):
        require(path.is_relative_to(self.root), "path_outside_scope")
        current = path
        while current != self.root:
            require(not current.is_symlink(), "symlink_conflict")
            current = current.parent
        return path

    @contextmanager
    def lock(self):
        import fcntl
        # Existing v2node installer and gateway changes share one host lock.
        path = self.path("/run/lock/nodepulse-v2node-deploy.lock")
        self.safe(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise GatewayError("host_locked") from None
            yield
        finally:
            os.close(fd)

    def atomic(self, path, contents, mode=0o600):
        self.safe(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".nodepulse-", dir=path.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(contents)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def command(self, argv, code, timeout=15, check=True):
        try:
            result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                                    text=True, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise GatewayError(code) from None
        if check and result.returncode:
            raise GatewayError(code)
        return result

    def show(self, unit=None):
        props = "LoadState,ActiveState,SubState,MainPID,ControlGroup,FragmentPath,DropInPaths,User,Result,ExecMainCode,ExecMainStatus,InvocationID,NRestarts"
        result = self.command(["systemctl", "show", unit or self.unit, "--property=" + props], "unit_inspection_failed", check=False)
        value = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        require(result.returncode == 0 or value.get("LoadState") == "not-found", "unit_inspection_failed")
        return value

    def inspect(self, state):
        self.safe(self.unit_path)
        self.safe(self.binary)
        require(file_hash(self.unit_path) == state["unit_sha256"] and file_hash(self.binary) == state["binary_sha256"], "ownership_conflict")
        props = self.show()
        require(props.get("FragmentPath") == str(self.unit_path) and not props.get("DropInPaths") and
                props.get("User") == self.user and props.get("ControlGroup", "") in {"", self.group}, "ownership_conflict")
        pid = int(props.get("MainPID") or 0)
        if pid:
            identity = process_identity(pid)
            require(identity and in_group(identity["cgroup"], self.group) and identity["exe"] == str(self.binary), "process_identity_conflict")
            args = (Path("/proc") / str(pid) / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            require(args == [str(self.binary).encode(), b"run", b"--config", str(self.config).encode()], "process_identity_conflict")
        return props

    def members(self):
        directory = Path("/sys/fs/cgroup") / self.group.lstrip("/")
        if not directory.exists():
            return []
        require(directory.resolve() == directory, "cgroup_identity_conflict")
        result = []
        for path in [directory / "cgroup.procs", *directory.glob("**/cgroup.procs")]:
            require(not path.is_symlink(), "cgroup_identity_conflict")
            for value in path.read_text().splitlines():
                identity = process_identity(int(value))
                require(identity is None or in_group(identity["cgroup"], self.group), "cgroup_identity_conflict")
                if identity and identity not in result:
                    result.append(identity)
        return result

    def sockets(self):
        return socket_table()

    def admin_config(self):
        connection = UnixConnection(f"/run/nodepulse-edge-{self.instance}/admin.sock")
        try:
            connection.request("GET", "/config/")
            response = connection.getresponse()
            require(response.status == 200, "running_config_unavailable")
            data = response.read(4 * 1024 * 1024 + 1)
            require(len(data) <= 4 * 1024 * 1024, "running_config_unavailable")
            return json.loads(data)
        except (OSError, ValueError, http.client.HTTPException):
            raise GatewayError("running_config_unavailable") from None
        finally:
            connection.close()

    def validate(self, path, plan):
        for route in plan["routes"]:
            cert = route["certificate"]["cert_file"]
            self.command(["openssl", "x509", "-in", cert, "-noout", "-checkend", "0"], "certificate_expired")
            name_check = self.command(["openssl", "x509", "-in", cert, "-noout", "-checkhost", route["domain"]], "certificate_name_mismatch")
            require(name_check.stdout.strip().endswith("does match certificate"), "certificate_name_mismatch")
        self.command(["runuser", "-u", self.user, "--", str(self.binary), "validate", "--config", str(path)], "caddy_validation_failed")

    def backend(self, route):
        node = route["node_id"]
        backend = route["backend"]
        config_path = self.path(f"/etc/v2node/v2node-{node}.json")
        self.safe(config_path)
        require(file_hash(config_path) == backend["local_config_sha256"], "backend_config_changed")
        try:
            local = json.loads(config_path.read_bytes())
            nodes = local["Nodes"]
            require(len(nodes) == 1 and nodes[0]["NodeID"] == node, "backend_config_mismatch")
            config = nodes[0]
            require(config.get("ListenIP") == backend["address"] and config.get("ListenPort") == backend["port"], "backend_config_mismatch")
            url = urlsplit(config["ApiHost"])
            require(url.scheme == "https" or (url.scheme == "http" and ipaddress.ip_address(url.hostname).is_loopback), "unsafe_panel_url")
            require(not url.username and not url.password and not url.query and not url.fragment, "unsafe_panel_url")
            query = urlencode({"node_type": "v2node", "node_id": node, "token": config["ApiKey"]})
            opener = build_opener(ProxyHandler({}), NoRedirect())
            with opener.open(Request(config["ApiHost"].rstrip("/") + "/api/v2/server/config?" + query), timeout=8) as response:
                server_bytes = response.read(1024 * 1024 + 1)
            require(len(server_bytes) <= 1024 * 1024, "backend_config_unavailable")
            server = json.loads(server_bytes)
            validate_backend_profile(server, route)
        except GatewayError:
            raise
        except Exception:
            raise GatewayError("backend_config_unavailable") from None
        props = self.show(f"v2node-{node}.service")
        group = f"/system.slice/v2node-{node}.service"
        require(props.get("ActiveState") == "active" and props.get("ControlGroup") == group and not props.get("DropInPaths"), "backend_not_ready")
        identity = process_identity(int(props.get("MainPID") or 0))
        require(identity and identity["cgroup"] == group and identity["exe"] == "/usr/local/bin/v2node", "backend_identity_conflict")
        args = (Path("/proc") / str(identity["pid"]) / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        require(args == [b"/usr/local/bin/v2node", b"server", b"--config", str(config_path).encode(), b"--watch=false"], "backend_identity_conflict")
        found = [row for row in self.sockets() if matches(row, Listener("tcp", backend["address"], backend["port"]))]
        require(found and all(row["owners"] and all(in_group(owner["cgroup"], group) for owner in row["owners"]) for row in found), "backend_listener_conflict")
        self.h2_probe(backend["address"], backend["port"], backend if backend["transport"] == "h2_tls" else None)

    def h2_probe(self, address, port, tls=None):
        try:
            with socket.create_connection((address, port), timeout=4) as raw:
                connection = raw
                if tls:
                    context = ssl.create_default_context(cafile=tls.get("ca_file"))
                    context.set_alpn_protocols(["h2"])
                    connection = context.wrap_socket(raw, server_hostname=tls["server_name"])
                    require(connection.selected_alpn_protocol() == "h2", "h2_probe_failed")
                with connection:
                    connection.sendall(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n" + b"\0\0\0\x04\0\0\0\0\0")
                    header = b""
                    while len(header) < 9:
                        chunk = connection.recv(9 - len(header))
                        require(chunk, "h2_probe_failed")
                        header += chunk
                    require(header[3] == 4 and header[5:] == b"\0\0\0\0" and int.from_bytes(header[:3], "big") <= 65536, "h2_probe_failed")
        except (OSError, ssl.SSLError):
            raise GatewayError("h2_probe_failed") from None

    def public_probe(self, plan):
        for route in plan["routes"]:
            for addr in route["listen"]:
                target = "127.0.0.1" if addr == "0.0.0.0" else "::1" if addr == "::" else addr
                cert = route["certificate"]
                self.h2_probe(target, route["port"], {"server_name": route["domain"], "ca_file": cert.get("probe_ca_file")})

    def action(self, action):
        if action == "reload":
            self.command(["runuser", "-u", self.user, "--", str(self.binary), "reload", "--config", str(self.config),
                          "--address", f"unix//run/nodepulse-edge-{self.instance}/admin.sock"], "reload_failed", timeout=20)
        elif action == "kill":
            self.command(["systemctl", "kill", "--kill-whom=all", "--signal=KILL", self.unit], "scoped_cleanup_failed")
        else:
            require(action in {"start", "stop", "enable", "disable"}, "invalid_action")
            self.command(["systemctl", action, self.unit], "unit_" + action + "_failed", timeout=80 if action == "stop" else 30)

    def pause(self):
        time.sleep(.1)

    def stable(self, state, props):
        for _ in range(10):
            self.pause()
            current = self.inspect(state)
            require(current.get("ActiveState") == "active" and all(current.get(key) == props.get(key)
                    for key in ("MainPID", "InvocationID", "NRestarts")), "runtime_unstable")
