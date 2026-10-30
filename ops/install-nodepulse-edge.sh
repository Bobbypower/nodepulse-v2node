#!/usr/bin/env bash
set -euo pipefail
umask 022

# Installs a separate owned instance. It never takes over caddy.service.
EDGE_INSTANCE="${EDGE_INSTANCE:-main}"
EDGE_GATEWAY_RELEASE_BASE="${EDGE_GATEWAY_RELEASE_BASE:-https://github.com/Bobbypower/nodepulse-v2node/releases/download/v2node-nodepulse-latest}"
EDGE_GATEWAY_PACKAGE_SHA256="${EDGE_GATEWAY_PACKAGE_SHA256:-}"
CADDY_BIN="${CADDY_BIN:-/usr/bin/caddy}"
CADDY_SHA256="${CADDY_SHA256:-}"
if [ "$(id -u)" -ne 0 ] || ! [[ "${EDGE_INSTANCE}" =~ ^[a-z][a-z0-9-]{0,15}$ ]]; then
  echo "Requires root and a valid EDGE_INSTANCE." >&2
  exit 2
fi
for command_name in python3 systemctl sha256sum curl runuser useradd openssl; do
  command -v "${command_name}" >/dev/null || { echo "Missing ${command_name}." >&2; exit 2; }
done
if ! [[ "${CADDY_SHA256}" =~ ^[0-9a-f]{64}$ ]] || [ ! -f "${CADDY_BIN}" ]; then
  echo "Provide an installed Caddy binary and its reviewed CADDY_SHA256." >&2
  exit 2
fi
if [ "$(sha256sum "${CADDY_BIN}" | awk '{print $1}')" != "${CADDY_SHA256}" ]; then
  echo "Caddy checksum mismatch." >&2
  exit 2
fi
TMP_EDGE="$(mktemp -d)"
trap 'rm -rf -- "${TMP_EDGE}"' EXIT
curl -fLsS --retry 4 --connect-timeout 15 --max-time 180 \
  "${EDGE_GATEWAY_RELEASE_BASE%/}/nodepulse-edge-gateway.tar.gz" -o "${TMP_EDGE}/runtime.tar.gz"
if [ -z "${EDGE_GATEWAY_PACKAGE_SHA256}" ]; then
  curl -fLsS --retry 4 --connect-timeout 15 --max-time 60 "${EDGE_GATEWAY_RELEASE_BASE%/}/SHA256SUMS" -o "${TMP_EDGE}/SHA256SUMS"
  EDGE_GATEWAY_PACKAGE_SHA256="$(awk '$2 == "nodepulse-edge-gateway.tar.gz" {print $1; exit}' "${TMP_EDGE}/SHA256SUMS")"
fi
if ! [[ "${EDGE_GATEWAY_PACKAGE_SHA256}" =~ ^[0-9a-f]{64}$ ]] || \
  [ "$(sha256sum "${TMP_EDGE}/runtime.tar.gz" | awk '{print $1}')" != "${EDGE_GATEWAY_PACKAGE_SHA256}" ]; then
  echo "Gateway package checksum mismatch or absent." >&2
  exit 2
fi
PACKAGE_DIR="/usr/local/lib/nodepulse-edge-gateway/${EDGE_GATEWAY_PACKAGE_SHA256}"
python3 -I - "${TMP_EDGE}/runtime.tar.gz" "${PACKAGE_DIR}" <<'PY'
from pathlib import Path
import sys, tarfile
archive, target = Path(sys.argv[1]), Path(sys.argv[2])
with tarfile.open(archive) as package:
    members = package.getmembers()
    assert len(members) <= 32 and sum(item.size for item in members) <= 2 * 1024 * 1024
    for item in members:
        parts = Path(item.name).parts
        assert len(parts) in {1, 2} and parts[0] == "edge_gateway" and ".." not in parts
        assert (item.isdir() and len(parts) == 1) or (item.isfile() and len(parts) == 2 and parts[-1].endswith((".py", ".md")))
    assert not target.is_symlink()
    target.mkdir(parents=True, exist_ok=True)
    for item in members:
        if item.isfile():
            path = target / item.name
            assert not path.is_symlink() and not path.parent.is_symlink()
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(package.extractfile(item).read())
            path.chmod(0o644)
PY
LAUNCHER="/usr/local/sbin/nodepulse-edge-gateway"
if [ -e "${LAUNCHER}" ] || [ -L "${LAUNCHER}" ]; then
  if [ -L "${LAUNCHER}" ] || ! grep -q '^# NodePulse Edge Gateway launcher v1$' "${LAUNCHER}"; then
    echo "Existing launcher is not owned; refusing replacement." >&2
    exit 2
  fi
fi
STAGE="$(mktemp /usr/local/sbin/.nodepulse-edge.new.XXXXXX)"
cat >"${STAGE}" <<EOF
#!/usr/bin/env bash
# NodePulse Edge Gateway launcher v1
exec python3 -I -c 'import sys;sys.path.insert(0,"${PACKAGE_DIR}");from edge_gateway.__main__ import main;raise SystemExit(main())' "\$@"
EOF
chmod 0755 "${STAGE}"
mv -fT -- "${STAGE}" "${LAUNCHER}"
"${LAUNCHER}" --instance "${EDGE_INSTANCE}" initialize --binary "${CADDY_BIN}" --sha256 "${CADDY_SHA256}"
echo "Instance initialized. Apply a reviewed complete plan; enable-on-boot is a separate explicit operation."
