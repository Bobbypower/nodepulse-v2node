# Local checks before a NodePulse build

The hosted workflow is manual-only and runs no test suite. Run the checks here
locally before asking the release owner to build. These commands do not access
a production node or database and do not publish artifacts.

Requirements: Python with `pytest` and `PyYAML`, Git, Bash, and a local Go
toolchain compatible with the pinned v2node module and `GOEXPERIMENT=jsonv2`
(the current pin uses Go 1.27.1). Git Bash is detected on Windows. Go may fetch
public module dependencies if they are absent from the local cache.

```powershell
# Full local shell, Python, Xray and v2node checks on a Linux host.
python ops/tests/run_local_checks.py --go "<path-to-compatible-go-executable>"

# On Windows, run the Go portion in an already-pulled local Linux image.
python ops/tests/run_local_checks.py --go "<path-to-compatible-go-executable>" --linux-image golang:1.27.1-alpine

# Shell and Python checks only. This explicitly reports Go as not run.
python ops/tests/run_local_checks.py --python-only
```

For the Go checks, the runner reads the two full commit pins from the workflow,
checks that neither upstream tree has submodules, downloads those pinned
source archives into an automatically removed temporary directory, and applies
the current local patch files there. Temporary patch copies use LF, matching
the hosted Linux checkout; the working files are not rewritten. It does not edit
an existing checkout. If a
future pinned tree adds submodules, the runner stops instead of silently testing
incomplete sources. The account-enforcement test requiring local services and
SQLite is explicitly excluded, and its service URL variables are removed from
the Go test environment. Database tests remain paused for this release.

The shell/Python portion checks both installer syntax, the installer contract,
gateway and bootstrap unit tests, release isolation and the manual-build
workflow policy. These checks use synthetic inputs and temporary files. Go tests
run on the local Linux host or in the requested Linux container; this does not
constitute real node, client, CDN, migration or database acceptance.

The optional Linux container mounts only the temporary source export and the Go
module cache (read-only); it uses `--network none`, no published ports, no
privileged mode and is removed after the checks. Module dependencies are fetched
into the host Go cache before the network-isolated container starts. It is a
local test environment, not a production node or deployment. The container
runs as root so the synthetic effective-UID isolation cases execute; their test
binary is placed in accessible temporary storage for the dropped-UID subprocess.

`workflow_dispatch` is the only configured GitHub trigger. When the workflow is
enabled and manually run on `main`, the existing release job can upload assets
and update the shared rolling release; it does not deploy a node. Coordinate
one intentional run with Prism Git after the exact desired commit is pushed and
the workflow is re-enabled. Local edits do not change the hosted workflow.
