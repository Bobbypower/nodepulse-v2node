"""Build the public control-plane bundle from the exact tested release outputs."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def build(dist, commit):
    if not re.fullmatch(r"[a-f0-9]{40}", commit): raise ValueError("exact_release_commit_required")
    names = ["install-nodepulse-v2node.sh", "nodepulse-edge-gateway.tar.gz", "v2node-linux-amd64", "v2node-linux-arm64"]
    data = {name: (dist/name).read_bytes() for name in names}
    checksums = {name: hashlib.sha256(raw).hexdigest() for name, raw in data.items()}
    value = {"schema_version": 1, "source_commit": commit,
        "release_base": f"https://github.com/Bobbypower/nodepulse-v2node/releases/download/nodepulse-{commit}",
        "installer_file": names[0], "installer_sha256": checksums[names[0]],
        "helper_file": names[1], "helper_sha256": checksums[names[1]],
        "runtimes": {arch: checksums[f"v2node-linux-{arch}"] for arch in ["amd64", "arm64"]}}
    path = dist/"bootstrap-control.tar.gz"
    with path.open("wb") as output, gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, raw in [("release.json", json.dumps(value, sort_keys=True, separators=(",", ":")).encode()),
                              (names[0], data[names[0]]), (names[1], data[names[1]])]:
                info = tarfile.TarInfo(name); info.size = len(raw); info.mode = 0o644; info.mtime = 0
                archive.addfile(info, io.BytesIO(raw))
    (dist/"bootstrap-control.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest()+"  bootstrap-control.tar.gz\n")
    return value


def published_identically(dist, commit):
    tag = 'nodepulse-' + commit
    api = 'https://api.github.com/repos/Bobbypower/nodepulse-v2node/releases/tags/' + tag
    try:
        with urlopen(Request(api, headers={'User-Agent':'NodePulse-bootstrap-build'}), timeout=30) as response:
            release = json.loads(response.read(1024*1024))
    except HTTPError as error:
        if error.code == 404: return False
        raise
    if release.get('tag_name') != tag or release.get('draft') is not False: raise ValueError('immutable_release_conflict')
    url = 'https://github.com/Bobbypower/nodepulse-v2node/releases/download/' + tag + '/bootstrap-control.sha256'
    with urlopen(url, timeout=30) as response: remote = response.read(1024)
    if remote != (dist/'bootstrap-control.sha256').read_bytes(): raise ValueError('immutable_release_digest_changed')
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--dist", type=Path, required=True); parser.add_argument("--commit", required=True)
    parser.add_argument('--check-published', action='store_true')
    args = parser.parse_args()
    if args.check_published: print('exists=' + str(published_identically(args.dist, args.commit)).lower())
    else: build(args.dist, args.commit)
