# NodePulse v2node

This repository builds pinned revisions of `wyx2685/v2node` and its compatible
Xray core, then applies the NodePulse patch set in GitHub Actions.

It intentionally does not vendor upstream source. The workflow checks out the
documented revisions, applies `ops/v2node/patches/*.patch` and
`ops/xray-core/patches/*.patch`, runs focused tests, builds Linux binaries, and
publishes a rolling prerelease with SHA-256 checksums.

## Runtime Additions

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
Docker or generic v2node service. Only a fully verified deployment removes the
old container.
