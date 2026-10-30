# Host runtime contract v1

This runtime owns only `nodepulse-edge-<instance>.service`, its dedicated user,
private binary and configuration. The entrypoint takes a complete host snapshot,
not per-node Caddy snippets. Routes from multiple nodes share one instance.
`schema_version=1`, monotonically increasing `revision`, and the caller's
`expected_revision` are required. Absent legacy gateway settings keep the existing
direct-Xray installation path. No Caddy process is adopted by name or port.

The control plane should compile existing `/api/v2/server/config` into **actual
server** parameters. For managed Caddy H2C, that means loopback XHTTP with no TLS,
not the client's public H3/TLS parameters. Preserve protocol authentication,
ENC/decryption, path, users, limits and accounting. `local_config_sha256` below
binds the existing `/etc/v2node/v2node-<node_id>.json`; the runtime checks that
unit, its cgroup, listener ownership and an actual H2 backend handshake.
Both H2C and verified TLS/H2 loopback upstreams are supported. UDS and public H1
upstreams are explicitly unsupported in v1, rather than silently downgraded.

```json
{
  "schema_version": 1,
  "instance_id": "main",
  "revision": 1,
  "grace_seconds": 5,
  "routes": [{
    "route_id": "node45-public",
    "node_id": 45,
    "mode": "managed_caddy",
    "domain": "gateway.example.test",
    "path": "/xhttp-45",
    "listen": ["0.0.0.0"],
    "port": 443,
    "protocols": ["h2", "h3"],
    "certificate": {
      "cert_file": "/var/lib/nodepulse-edge/main/certs/fullchain.pem",
      "key_file": "/var/lib/nodepulse-edge/main/certs/key.pem"
    },
    "backend": {
      "address": "127.0.0.1",
      "port": 24445,
      "transport": "h2c",
      "local_config_sha256": "0000000000000000000000000000000000000000000000000000000000000000"
    }
  }]
}
```

Only `managed_caddy` belongs in `routes`. `external_cdn` is rejected: its origin
is Xray/v2node directly, with its own certificate and public TCP listener. The
control plane must not invoke this gateway for CDN-only changes, even on a host
which also has a managed Caddy. DNS and provider writes are outside this helper.

Certificates are explicitly supplied files under the instance's `certs/`
directory, readable by its service user. Their provisioning/renewal remains an
external, explicitly owned prerequisite. v1 does not silently start ACME on 80 or
adopt another certificate manager. `probe_ca_file` is optional for an explicitly
trusted private CA; omitting it uses system trust. TLS/H2 backend optional fields
are `server_name` and `ca_file`, with strict verification and no skip-verify flag.
`backend.host` may explicitly set the upstream HTTP Host separately from TLS SNI.
The active panel backend profile's path and Host must match those compiled route
parameters; protocol-ready ports alone do not establish a valid route.

The tested Caddy 2.10.2 configuration includes its `h1` TCP accept path to enable
TLS, with TCP TLS ALPN explicitly `h2`; optional H3 has a separate UDP listener.
This is distinct from Xray's single-branch H3 handling. No client H3/H2 fallback
is inferred from either server's configuration.

Apply returns **applied**, not end-to-end verified. Real XHTTP authenticated
upload/download/long-session and provider/client acceptance remain coordinator
gates. Forwarded addresses are untrusted: upstream forwarding headers are removed;
do not enable Xray's unvalidated forwarded-IP trust or label a socket peer as the
original client. NodePulse should preserve unknown client origin in that case.

Deleting one route is a new complete snapshot and reload. Other references stay
running. Explicit stop is different from disable; neither removes certificates.
Stop refuses live references unless the operator explicitly requests all routes.
The result separates service stop, cgroup cleanup, forced termination and foreign
port occupation. Failed apply rolls back only unchanged, owned configuration;
recovery failure is degraded, external change is conflict. A failed revision is
paused until an explicit retry; interrupted transactions require recovery first.

The gateway and v2node installer share `/run/lock/nodepulse-v2node-deploy.lock`.
Control metadata lives in root-only `/var/lib/nodepulse-edge-control/<instance>`;
it is outside the service account's writable home. Enable-on-boot is explicit:
`nodepulse-edge-gateway --instance main enable --expected-revision N`. Normal
stop leaves it unchanged; `stop --disable` explicitly disables it as well.
