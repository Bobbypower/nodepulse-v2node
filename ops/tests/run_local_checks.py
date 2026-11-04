"""Run the non-database checks locally; never access a production node or DB."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

import yaml


ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "ops" / "tests"
PYTEST_FILES = [
    "test_edge_gateway.py", "test_node_listener.py",
    "test_bootstrap_installer_preconditions.py", "test_node_bootstrap.py",
    "test_installer_panel_fetch.py", "test_bootstrap_release_scope.py",
    "test_release_isolation.py", "test_build_workflow_policy.py",
]
DB_TEST = "^TestAccountEnforcementLocalServices$"


def capture_inputs() -> dict[str, bytes]:
    files = [ROOT / ".github" / "workflows" / "build.yml", ROOT / "ops" / "install-nodepulse-v2node.sh",
             ROOT / "ops" / "install-nodepulse-edge.sh", ROOT / "ops" / "build_bootstrap_bundle.py",
             Path(__file__), TESTS / "test_installer_contract.py", *[TESTS / name for name in PYTEST_FILES]]
    for folder, pattern in ((ROOT / "ops" / "v2node" / "patches", "*.patch"),
                            (ROOT / "ops" / "xray-core" / "patches", "*.patch"),
                            (ROOT / "ops" / "edge_gateway", "*.py")):
        files.extend(folder.glob(pattern))
    return {str(p.relative_to(ROOT)).replace("\\", "/"): p.read_bytes() for p in sorted(set(files))}


def input_digest(inputs: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, data in sorted(inputs.items()):
        digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(argv), flush=True)
    subprocess.run(argv, cwd=cwd, env=env, check=True)


def bash_executable(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
    result = shutil.which("bash")
    if not result:
        raise RuntimeError("Bash not found; supply --bash")
    return result


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "nodepulse-local-checks"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def export_upstream(repo: str, revision: str, dest: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError(f"Expected a full pinned commit for {repo}")
    tree = fetch_json(f"https://api.github.com/repos/wyx2685/{repo}/git/trees/{revision}?recursive=1")
    if tree.get("truncated") or any(item.get("mode") == "160000" for item in tree.get("tree", [])):
        raise RuntimeError(f"{repo} has submodules or a truncated tree; prepare a reviewed recursive checkout")
    url = f"https://codeload.github.com/wyx2685/{repo}/tar.gz/{revision}"
    request = urllib.request.Request(url, headers={"User-Agent": "nodepulse-local-checks"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise RuntimeError(f"{repo} source archive exceeds the size limit")
    dest.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if not parts or member.name.startswith("/") or ".." in parts:
                raise RuntimeError(f"Unsafe archive path in {repo}")
            if len(parts) == 1:
                continue
            path = dest.joinpath(*parts[1:])
            if not path.resolve().is_relative_to(dest.resolve()):
                raise RuntimeError(f"Archive entry escapes {repo} source directory")
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Cannot read archive entry in {repo}")
                with source, path.open("wb") as target:
                    shutil.copyfileobj(source, target)
            else:
                raise RuntimeError(f"Unsupported archive entry in {repo}")
    print(f"Exported pinned {repo} {revision}", flush=True)


def run_python_and_shell_checks(bash: str) -> None:
    for installer in ("install-nodepulse-v2node.sh", "install-nodepulse-edge.sh"):
        run([bash, "-n", (ROOT / "ops" / installer).as_posix()], cwd=ROOT)
    run([sys.executable, str(TESTS / "test_installer_contract.py")], cwd=ROOT)
    run([sys.executable, "-m", "pytest", "-q", *[str(TESTS / f) for f in PYTEST_FILES]], cwd=ROOT)


def run_go_in_linux_container(go: str, env: dict[str, str], temp: Path,
                              v2node: Path, xray: Path, image: str) -> None:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Docker is required for --linux-image")
    run([go, "mod", "download"], cwd=xray, env=env)
    run([go, "mod", "edit", "-replace", "github.com/xtls/xray-core=" + xray.as_posix()], cwd=v2node, env=env)
    run([go, "mod", "download"], cwd=v2node, env=env)
    modcache = subprocess.check_output([go, "env", "GOMODCACHE"], cwd=v2node, env=env, text=True).strip()
    if not Path(modcache).is_dir():
        raise RuntimeError("Go module cache is unavailable")
    script = """set -eu
test "$(id -u)" -eq 0
go version
cd /workspace/xray-core
go test ./transport/internet/splithttp
go test -c -o /tmp/nodepulse-filesystem.test ./common/platform/filesystem
chmod 755 /tmp/nodepulse-filesystem.test
/tmp/nodepulse-filesystem.test -test.v
go test ./transport/internet/tls -run 'TestReloadKeepsPair|TestActualTLSHandshake|TestCertificateWatcherStops'
go test ./infra/conf -run TestNodePulseManagedCertificate
cd /workspace/v2node
go mod edit -replace github.com/xtls/xray-core=../xray-core
go test -skip '^TestAccountEnforcementLocalServices$' ./api/v2board ./core/app/dispatcher ./core ./limiter ./node ./cmd
"""
    for path in (str(temp), modcache):
        if "," in path:
            raise RuntimeError("Docker bind paths containing commas are not supported")
    run([docker, "run", "--rm", "--network", "none",
         "--mount", f"type=bind,source={temp},target=/workspace",
         "--mount", f"type=bind,source={modcache},target=/gomodcache,readonly",
         "--env", "GOEXPERIMENT=jsonv2", "--env", "GOTOOLCHAIN=local",
         "--env", "GOMODCACHE=/gomodcache", "--env", "GOPROXY=off", "--env", "CGO_ENABLED=0",
         "--workdir", "/workspace", "--entrypoint", "sh", image, "-c", script], cwd=ROOT)


def run_go_checks(go: str, inputs: dict[str, bytes], linux_image: str | None) -> None:
    config = yaml.safe_load(inputs[".github/workflows/build.yml"])
    refs = config["env"]
    env = os.environ.copy()
    env.update(GOEXPERIMENT="jsonv2", GOTOOLCHAIN="local")
    env.pop("NODE_ENFORCEMENT_COMMERCE_URL", None)
    env.pop("NODE_ENFORCEMENT_NODEPULSE_URL", None)
    run([go, "version"], cwd=ROOT, env=env)
    with tempfile.TemporaryDirectory(prefix="nodepulse-local-go-") as folder:
        temp = Path(folder)
        v2node, xray = temp / "v2node", temp / "xray-core"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(export_upstream, "v2node", refs["V2NODE_REF"], v2node),
                       pool.submit(export_upstream, "xray-core", refs["XRAY_CORE_REF"], xray)]
            for future in futures:
                future.result()
        for worktree, prefix in ((v2node, "ops/v2node/patches/"),
                                 (xray, "ops/xray-core/patches/")):
            names = sorted(name for name in inputs if name.startswith(prefix) and name.endswith(".patch"))
            if not names:
                raise RuntimeError(f"No patches found under {prefix}")
            patch_dir = temp / ("v2node-patches" if worktree == v2node else "xray-patches")
            patch_dir.mkdir()
            for name in names:
                patch = patch_dir / Path(name).name
                data = inputs[name]
                if b"\x00" in data:
                    raise RuntimeError(f"Binary patch is not supported by local archive checks: {name}")
                # Linux Actions checks out text patches with LF even when the
                # user's Windows working copy uses CRLF. Normalize only this copy.
                patch.write_bytes(data.replace(b"\r\n", b"\n"))
                run(["git", "apply", "--whitespace=fix", str(patch)], cwd=worktree)
        if linux_image:
            run_go_in_linux_container(go, env, temp, v2node, xray, linux_image)
            return
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            raise RuntimeError("Controlled UID tests require the isolated root container; use --linux-image")
        temp.chmod(0o755)
        run([go, "test", "./transport/internet/splithttp"], cwd=xray, env=env)
        binary = temp / "filesystem.test"
        run([go, "test", "-c", "-o", str(binary), "./common/platform/filesystem"], cwd=xray, env=env)
        binary.chmod(0o755)
        run([str(binary), "-test.v"], cwd=xray, env=env)
        run([go, "test", "./transport/internet/tls", "-run",
             "TestReloadKeepsPair|TestActualTLSHandshake|TestCertificateWatcherStops"], cwd=xray, env=env)
        run([go, "test", "./infra/conf", "-run", "TestNodePulseManagedCertificate"], cwd=xray, env=env)
        run([go, "mod", "edit", "-replace", "github.com/xtls/xray-core=" + xray.as_posix()], cwd=v2node, env=env)
        run([go, "test", "-skip", DB_TEST, "./api/v2board", "./core/app/dispatcher", "./core",
             "./limiter", "./node", "./cmd"], cwd=v2node, env=env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-only", action="store_true", help="run shell/Python checks only and explicitly skip Go")
    parser.add_argument("--go", default=shutil.which("go") or "go", help="Go executable matching the pinned module")
    parser.add_argument("--bash", help="Bash executable (Git Bash is selected automatically on Windows)")
    parser.add_argument("--linux-image", help="run Go tests in a disposable, network-isolated Linux Go image")
    args = parser.parse_args()
    inputs = capture_inputs()
    print("Local input SHA256:", input_digest(inputs), flush=True)
    run_python_and_shell_checks(bash_executable(args.bash))
    if args.python_only:
        if input_digest(capture_inputs()) != input_digest(inputs):
            raise RuntimeError("Local inputs changed during the checks; run them again")
        print("PARTIAL: shell/Python checks passed; Go checks explicitly skipped", flush=True)
    else:
        if sys.platform != "linux" and not args.linux_image:
            raise RuntimeError("Full Go checks require Linux; use --linux-image on this host")
        run_go_checks(args.go, inputs, args.linux_image)
        if input_digest(capture_inputs()) != input_digest(inputs):
            raise RuntimeError("Local inputs changed during the checks; run them again")
        print("PASS: local shell, Python and Go checks; database integration test excluded", flush=True)


if __name__ == "__main__":
    main()
