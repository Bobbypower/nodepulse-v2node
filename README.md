# NodePulse v2node

This repository follows upstream `wyx2685/v2node` and applies a small NodePulse patch set during GitHub Actions builds.

It intentionally does not vendor upstream source. The workflow checks out upstream, applies `ops/v2node/patches/*.patch`, builds Linux binaries, and publishes a rolling prerelease.

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

Production deployment fetches that host-local runtime JSON from NodePulse:

`/api/v2/server/local_config?node_type=v2node&node_id=<id>&token=<token>`
