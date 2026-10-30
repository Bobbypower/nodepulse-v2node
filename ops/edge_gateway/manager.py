"""File-authoritative gateway operations with CAS, a journal and scoped recovery."""
import hashlib
import json
import os
from pathlib import Path
import time

from .contract import GatewayError, canonical, compile_caddy, digest, expected_listeners, normalize_plan, require, unit_text
from .linux import file_hash, in_group, matches, process_identity


class GatewayManager:
    def __init__(self, system):
        self.io = system
        self.state_path = system.state_dir / "state.json"
        self.journal_path = system.state_dir / "pending.json"
        self.failure_path = system.state_dir / "failure.json"

    def read(self, path):
        self.io.safe(path)
        if not path.exists():
            return None
        try:
            data = path.read_bytes()
            require(len(data) <= 4 * 1024 * 1024, "invalid_state")
            return json.loads(data)
        except (OSError, ValueError):
            raise GatewayError("invalid_state") from None

    def write(self, path, data):
        self.io.atomic(path, canonical(data))

    def audit(self, action, **facts):
        path = self.io.state_dir / "audit.jsonl"
        self.io.safe(path)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(canonical({"at": time.time(), "action": action, "instance_id": self.io.instance, **facts}) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    def state(self, expected_revision=None, *, pending=False):
        state = self.read(self.state_path)
        require(state and state.get("schema_version") == 1 and state.get("instance_id") == self.io.instance, "instance_not_owned")
        if expected_revision is not None:
            require(type(expected_revision) is int and state["revision"] == expected_revision, "version_conflict")
        require(pending or not self.journal_path.exists(), "recovery_required")
        return state

    def assert_current(self, state):
        props = self.io.inspect(state)
        require(digest(json.loads(self.io.config.read_bytes())) == state["config_sha256"], "config_file_conflict")
        if props.get("ActiveState") == "active":
            require(digest(self.io.admin_config()) == state["config_sha256"], "running_config_conflict")
        return props

    def check_ports(self, listeners, *, require_owned=False):
        inventory = self.io.sockets()
        conflicts = []
        for listener in listeners:
            found = [row for row in inventory if matches(row, listener)]
            if require_owned and not found:
                raise GatewayError("listener_missing", protocol=listener.protocol, address=listener.address, port=listener.port)
            for row in found:
                if not row["owners"] or any(not in_group(owner["cgroup"], self.io.group) for owner in row["owners"]):
                    conflicts.append(row)
        require(not conflicts, "foreign_listener_conflict")

    def ready(self, state, config, plan):
        props = self.io.inspect(state)
        require(props.get("ActiveState") == "active", "unit_not_active")
        require(digest(self.io.admin_config()) == digest(config), "running_config_conflict")
        self.check_ports(expected_listeners(plan), require_owned=True)
        self.io.public_probe(plan)
        self.io.stable(state, props)

    def _stop_runtime(self, state):
        before = self.io.inspect(state)
        self.audit("stop_begin", revision=state["revision"], unit_invocation_id=before.get("InvocationID"))
        stop_error = None
        try:
            self.io.action("stop")
        except GatewayError as error:
            stop_error = error.code
        after = self.io.inspect(state)
        members = self.io.members()
        escalated = False
        if members and after.get("ActiveState") not in {"active", "activating", "reloading"}:
            # Recheck births/cgroups before a unit-scoped escalation; never kill a PID.
            require(all(item["state"] != "Z" for item in members), "zombie_residual_requires_review")
            guard = self.io.inspect(state)
            require(guard.get("ActiveState") not in {"active", "activating", "reloading"} and
                    guard.get("InvocationID") == before.get("InvocationID"), "residual_invocation_changed")
            require(self.io.members() == members, "residual_identity_changed")
            self.io.action("kill")
            escalated = True
            for _ in range(50):
                if not self.io.members():
                    break
                self.io.pause()
        after = self.io.inspect(state)
        members = self.io.members()
        inventory = self.io.sockets()
        occupied = [row for row in inventory if any(matches(row, listener) for listener in expected_listeners(state["plan"]))]
        stopped = after.get("ActiveState") in {"inactive", "failed"} and int(after.get("MainPID") or 0) == 0
        result = {"service_stopped": stopped, "cgroup_empty": not members, "ports_clear": not occupied,
            "escalated": escalated, "forced": escalated or after.get("Result") == "timeout" or after.get("ExecMainStatus") == "9",
            "unit_result": after.get("Result"), "stop_error": stop_error,
            "unit_invocation_id": after.get("InvocationID"),
            "state": "stopped" if stopped and not members and not occupied else "stopped_port_conflict" if stopped and not members else "cleanup_failed",
            "occupied": occupied}
        self.audit("stop", revision=state["revision"], **result)
        return result

    def stop(self, expected_revision, *, all_routes=False, disable=False):
        with self.io.lock():
            state = self.state(expected_revision)
            self.assert_current(state)
            require(all_routes or not state["plan"]["routes"], "route_references_remain")
            result = self._stop_runtime(state)
            if result["service_stopped"] and result["cgroup_empty"]:
                if disable:
                    self.io.action("disable")
                state["lifecycle"] = result["state"]
                state["last_stop"] = result
                self.write(self.state_path, state)
            return {**result, "revision": state["revision"], "disabled": bool(disable and result["service_stopped"] and result["cgroup_empty"])}

    def apply(self, value, expected_revision, *, retry_failed=False):
        plan = normalize_plan(value)
        require(plan["instance_id"] == self.io.instance, "instance_mismatch")
        with self.io.lock():
            state = self.state(expected_revision)
            props = self.assert_current(state)
            if plan["revision"] == state["revision"]:
                require(plan == state["plan"], "same_version_content_conflict")
                if plan["routes"]:
                    require(props.get("ActiveState") == "active", "unit_stopped_requires_new_revision")
                    self.ready(state, compile_caddy(plan), plan)
                return {"state": "applied", "revision": state["revision"], "idempotent": True, "end_to_end_verified": False}
            require(plan["revision"] > state["revision"], "version_conflict")
            failure = self.read(self.failure_path)
            require(retry_failed or not failure or failure["revision"] != plan["revision"], "failed_revision_paused")
            self.check_ports(expected_listeners(plan))
            for route in plan["routes"]:
                self.io.backend(route)
            config = compile_caddy(plan)
            candidate = self.io.config.with_name("candidate.json")
            self.io.atomic(candidate, canonical(config), 0o644)
            self.io.validate(candidate, plan)
            # Refresh guards after preflight network I/O before changing the authority file.
            props = self.assert_current(state)
            journal = {"schema_version": 1, "before": state, "candidate": plan, "candidate_config_sha256": digest(config),
                       "before_running": props.get("ActiveState") == "active", "before_config": json.loads(self.io.config.read_bytes())}
            self.write(self.journal_path, journal)
            self.audit("apply_begin", revision=plan["revision"], previous_revision=state["revision"])
            try:
                self.io.atomic(self.io.config, canonical(config), 0o644)
                if plan["routes"]:
                    self.io.action("reload" if journal["before_running"] else "start")
                    self.ready(state, config, plan)
                    for route in plan["routes"]:
                        self.io.backend(route)
                else:
                    result = self._stop_runtime(state)
                    require(result["state"] == "stopped", "gateway_stop_incomplete")
                next_state = {**state, "revision": plan["revision"], "plan": plan, "config_sha256": digest(config),
                              "lifecycle": "applied" if plan["routes"] else "stopped"}
                next_state.pop("last_stop", None)
                self.write(self.state_path, next_state)
                self.journal_path.unlink()
                if self.failure_path.exists():
                    self.failure_path.unlink()
                self.audit("apply_complete", revision=plan["revision"], node_ids=sorted({r["node_id"] for r in plan["routes"]}))
                return {"state": next_state["lifecycle"], "revision": plan["revision"], "config_sha256": digest(config), "end_to_end_verified": False}
            except Exception as error:
                cause = error.code if isinstance(error, GatewayError) else "runtime_operation_failed"
                return self._rollback(journal, cause)

    def _rollback(self, journal, cause):
        state, plan = journal["before"], journal["candidate"]
        self.audit("rollback_begin", revision=plan["revision"], cause=cause)
        try:
            props = self.io.inspect(state)
            allowed = {state["config_sha256"], journal["candidate_config_sha256"]}
            require(digest(json.loads(self.io.config.read_bytes())) in allowed, "rollback_file_conflict")
            if props.get("ActiveState") == "active":
                require(digest(self.io.admin_config()) in allowed, "rollback_running_conflict")
            self.io.atomic(self.io.config, canonical(journal["before_config"]), 0o644)
            if journal["before_running"]:
                self.io.validate(self.io.config, state["plan"])
                self.io.action("reload" if props.get("ActiveState") == "active" else "start")
                self.ready(state, journal["before_config"], state["plan"])
                for route in state["plan"]["routes"]:
                    self.io.backend(route)
            else:
                # A failed first start must not leave a new gateway running.
                stop_state = {**state, "plan": plan}
                result = self._stop_runtime(stop_state)
                require(result["service_stopped"] and result["cgroup_empty"], "rollback_stop_failed")
            self.write(self.state_path, state)
            self.write(self.failure_path, {"revision": plan["revision"], "cause": cause, "state": "rolled_back"})
            self.journal_path.unlink()
            self.audit("rollback_complete", revision=plan["revision"], restored_revision=state["revision"])
            return {"state": "rolled_back", "cause": cause, "revision": state["revision"], "failed_revision": plan["revision"], "retry_paused": True}
        except Exception as error:
            code = error.code if isinstance(error, GatewayError) else "rollback_failed"
            self.audit("rollback_failed", revision=plan["revision"], cause=code)
            return {"state": "conflict" if "conflict" in code else "degraded", "cause": cause,
                    "recovery_error": code, "revision": state["revision"], "failed_revision": plan["revision"], "recovery_required": True}

    def recover(self, expected_revision):
        with self.io.lock():
            state = self.state(expected_revision, pending=True)
            journal = self.read(self.journal_path)
            require(journal is not None, "no_pending_operation")
            if state["revision"] == journal["candidate"]["revision"]:
                self.assert_current(state)
                self.journal_path.unlink()
                return {"state": state["lifecycle"], "revision": state["revision"]}
            require(state == journal["before"], "recovery_version_conflict")
            return self._rollback(journal, "interrupted_operation")

    def status(self):
        with self.io.lock():
            state = self.state(pending=True)
            try:
                props = self.assert_current(state)
                self.check_ports(expected_listeners(state["plan"]), require_owned=props.get("ActiveState") == "active")
                observed = "running" if props.get("ActiveState") == "active" else "stopped"
                if observed == "stopped" and self.io.members():
                    observed = "cleanup_failed"
            except GatewayError as error:
                return {"state": "conflict", "cause": error.code, "revision": state["revision"], "end_to_end_verified": False}
            return {"state": "recovery_required" if self.journal_path.exists() else observed, "revision": state["revision"],
                    "lifecycle": state["lifecycle"], "node_ids": sorted({r["node_id"] for r in state["plan"]["routes"]}),
                    "last_stop": state.get("last_stop"),
                    "failed_revision": self.read(self.failure_path), "end_to_end_verified": False}

    def enable(self, expected_revision):
        with self.io.lock():
            state = self.state(expected_revision)
            self.assert_current(state)
            self.io.action("enable")
            self.audit("enabled", revision=state["revision"])
            return {"state": "enabled", "revision": state["revision"]}

    def initialize(self, binary, expected_hash):
        require(type(expected_hash) is str and len(expected_hash) == 64 and file_hash(binary) == expected_hash, "binary_checksum_mismatch")
        with self.io.lock():
            if self.state_path.exists():
                state = self.state()
                self.assert_current(state)
                require(state["binary_sha256"] == expected_hash, "binary_update_requires_separate_operation")
                return {"state": "owned", "revision": state["revision"]}
            require(not self.io.unit_path.exists() and not self.io.binary.exists() and not self.io.config.exists(), "existing_runtime_not_owned")
            props = self.io.show()
            require(not props.get("FragmentPath") and props.get("ActiveState") != "active", "existing_unit_not_owned")
            import pwd
            try:
                pwd.getpwnam(self.io.user)
            except KeyError:
                pass
            else:
                raise GatewayError("existing_account_not_owned")
            home = f"/var/lib/nodepulse-edge/{self.io.instance}"
            self.io.command(["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", home,
                             "--shell", "/usr/sbin/nologin", self.io.user], "account_creation_failed")
            account = pwd.getpwnam(self.io.user)
            self.io.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            certs = self.io.path(home + "/certs")
            certs.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(certs.parent, 0o700)
            os.chown(certs.parent, account.pw_uid, account.pw_gid)
            os.chown(certs, account.pw_uid, account.pw_gid)
            self.io.atomic(self.io.binary, binary.read_bytes(), 0o755)
            self.io.atomic(self.io.unit_path, unit_text(self.io.instance, 60).encode(), 0o644)
            plan = {"schema_version": 1, "instance_id": self.io.instance, "revision": 0, "grace_seconds": 5, "routes": []}
            config = compile_caddy(plan)
            self.io.atomic(self.io.config, canonical(config), 0o644)
            self.io.command(["systemctl", "daemon-reload"], "daemon_reload_failed")
            state = {"schema_version": 1, "instance_id": self.io.instance, "revision": 0, "plan": plan,
                     "config_sha256": digest(config), "unit_sha256": file_hash(self.io.unit_path),
                     "binary_sha256": expected_hash, "lifecycle": "stopped"}
            self.write(self.state_path, state)
            self.audit("initialized", revision=0)
            return {"state": "initialized", "revision": 0}
