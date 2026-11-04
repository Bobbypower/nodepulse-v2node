# NodePulse v2node

This repository builds pinned revisions of `wyx2685/v2node` and its compatible
Xray core, then applies the NodePulse patch set in GitHub Actions.

It intentionally does not vendor upstream source. The workflow checks out the
documented revisions, applies `ops/v2node/patches/*.patch` and
`ops/xray-core/patches/*.patch`, runs focused tests, builds Linux binaries, and
publishes a rolling prerelease with SHA-256 checksums.

## Runtime Additions

The local build definition now pins v2node
`cf513e1f0ba5f22eef6b40e65c22e3e9856d86b2` and its matching Xray core
`e087e74a1bf25b46f2776d1a3b53a68f6018d654` (26.9.30), using Go 1.27.1.
These include the upstream core/dependency update and installer version-format
fix. The NodePulse host installer remains the entry point.

### Account ban execution

Patch `0008-account-enforcement.patch` adds `nodepulse-enforcement/1`. The node
polls versioned restrictions during its existing monitor cycle, denies new
traffic, removes credentials on both primary and XHTTP download listeners,
closes tracked authenticated transports and traffic links, then sends a
delivery-bound receipt. Closing a writer half does not hide a live download;
connection tracking lasts until the session context finishes. Cleanup failures
and lost receipts retry, including a listener rebuild during partial cleanup.

Unban forces a fresh user snapshot before adding eligible users and confirming
release. Empty lists are applied, 304 means unchanged, and failed/invalid updates
cannot poison the user ETag. An empty node can start normally. The dispatcher and XHTTP/H3 tests run locally before a build; see
[Local checks](ops/tests/LOCAL_TESTS.md). The isolated service integration is
outside this local non-database test set and remains paused for this release.

The patch adds local `ConnectionConfig` support to v2node's JSON config and maps it to Xray level-0 policy:

```json
"ConnectionConfig": {
  "handshake": 4,
  "connIdle": 200,
  "uplinkOnly": 2,
  "downlinkOnly": 5,
  "statsUserUplink": true,
  "statsUserDownlink": true,
  "bufferSize": 128
}
```

It also adds file log rotation when `Log.Output` is set:

```json
"Log": {
  "Level": "error",
  "Output": "/var/log/v2node/v2node-20.log",
  "Access": "none",
  "MaxSizeMB": 100,
  "MaxBackups": 3
}
```

NodePulse remains responsible for `/api/v2/server/config.routes`; runtime connection and process log policy are written into the host-local v2node config JSON during deployment.

### Split XHTTP download inbound

The NodePulse server payload may include an optional second XHTTP listener:

```json
"xhttp_download_inbound": {
  "listen_ip": "0.0.0.0",
  "server_port": 80,
  "network": "xhttp",
  "security": "none",
  "network_settings": {
    "path": "/same-path-as-upload",
    "mode": "auto"
  }
}
```

v2node creates this listener in the same Xray process as the primary inbound,
adds and removes the same users on both inbounds, and aggregates their traffic.
The Xray patch shares XHTTP session state between listeners with the same
normalized path. This permits a direct REALITY upload listener and a
Cloudflare-origin HTTP download listener to participate in one XHTTP session.

The download listener is intentionally restricted to `network: xhttp` and
`security: none`. Client-side TLS, SNI, host, and CDN address remain subscription
settings and are not copied into the origin listener.

### VLESS XHTTP over HTTP/3

The v2node patch accepts `tls_settings.alpn` from the NodePulse server payload
and applies it to the generated Xray TLS inbound. A direct VLESS XHTTP/H3 node
uses this combination:

```json
{
  "network": "xhttp",
  "security": "tls",
  "tls_settings": {
    "alpn": ["h3"]
  }
}
```

H3 requires XHTTP (or its `splithttp` alias), TLS certificate mode, and ALPN
exactly equal to `["h3"]`. The build rejects mixed ALPN, Reality, plaintext,
and non-XHTTP combinations before starting the inbound. Native Hysteria2 and
TUIC behavior remains unchanged.

NodePulse may send certificate automation in the top-level `cert_config`
object. The patched v2node merges those fields into its TLS certificate
settings, so `{"cert_mode":"http"}` uses v2node's built-in ACME HTTP-01
client and daily renewal task. The installer recognizes direct XHTTP/H3 as a
UDP listener and verifies the actual UDP socket before committing deployment.

Production deployment fetches that host-local runtime JSON from NodePulse:

`/api/v2/server/local_config?node_type=v2node&node_id=<id>&token=<token>`

## One-step Host Install

NodePulse operation templates should stay small and call the installer in this
repository instead of embedding the whole deployment script in database rows:

```bash
NODE_ID=20 \
NODE_PORT=23333 \
NODEPULSE_URL=https://node.eatp.top \
NODEPULSE_TOKEN=... \
bash <(curl -fsSL https://github.com/Bobbypower/nodepulse-v2node/releases/download/v2node-nodepulse-latest/install-nodepulse-v2node.sh)
```

The installer downloads the latest patched binary, verifies it against the
release `SHA256SUMS`, and fetches both the node-local and server runtime JSON
from NodePulse. Before changing the host it stores the existing binary, config,
and unit under `/root/nodepulse-backups`.

It derives the complete listener set from the server payload. A split XHTTP
node therefore succeeds only after both the primary port and the download port
are listening and the service remains stable without automatic restarts. Any
failure restores the previous systemd runtime and any previously running
verified generic v2node service. A running legacy Docker node is refused before
mutation and needs a separately reviewed migration; this installer does not
automatically stop or remove that container.

## Managed Edge Gateway

The host gateway is an independent, NodePulse-owned Caddy service. Its standard
library Python provider lives in `ops/edge_gateway`; the complete versioned
contract and operator commands are in [CONTRACT.md](ops/edge_gateway/CONTRACT.md).
It uses a dedicated account, private binary, bounded grace, systemd cgroups and a
permission-protected Unix Admin socket. File and running configuration hashes,
process identity, real TCP/UDP ownership and backend H2 handshakes are checked.

Install with `ops/install-nodepulse-edge.sh` using a reviewed Caddy binary and
`CADDY_SHA256`. Certificate files are a separate prerequisite; initialization
does not silently claim ACME or client acceptance. Apply a complete host snapshot
with `--expected-revision`; deleting one shared route reloads the rest. Explicit
stop, disable-on-boot and enable-on-boot are separate operations. Stop preserves
certificate data and reports a foreign listener as conflict. Recovery uses the
owned snapshot and pauses failed versions rather than repeatedly applying them.

The v2node installer now requires Python 3 and the checksummed runtime helper
package from the same release. It checks actual local overrides and Xray-owned
listeners. It refuses ambiguous generic-service or unverified legacy-container
takeover; perform those migrations as separately reviewed operations. Existing
unrelated Caddy services are never stopped or adopted. Gateway changes and node
installation use the same host lock.

External CDN goes directly to Xray/v2node and is rejected from Caddy routes. The
provider preserves unknown original-client IP unless a separate, verified trust
mechanism exists; it does not trust forwarded headers. One backend node per
v2node process is required, preserving split download listeners inside that node.

The opt-in isolated acceptance harness is
`python ops/tests/run_edge_gateway_acceptance.py --binary <reviewed-linux-binary>
--binary-sha256 <sha256> --output <new-output-directory> --build`. It owns a
private-cgroup Linux/systemd container with no host mounts or published ports,
checks real VLESS/XHTTP H2/H3 through Caddy, verified TLS/H2 origins, shared routes,
same-path/session isolation, finite shutdown, scoped forced cleanup and both
installers. Configuration reload can produce GOAWAY; local test results must not
be described as universal uninterrupted sessions or real CDN/client acceptance.
