#!/usr/bin/env bash
set -euo pipefail

NODE_ID="${NODE_ID:-}"
NODE_PORT="${NODE_PORT:-0}"
NODEPULSE_URL="${NODEPULSE_URL:-https://node.eatp.top}"
NODEPULSE_TOKEN="${NODEPULSE_TOKEN:-}"
V2NODE_RELEASE_BASE="${V2NODE_RELEASE_BASE:-https://github.com/Bobbypower/nodepulse-v2node/releases/download/v2node-nodepulse-latest}"
V2NODE_SHA256="${V2NODE_SHA256:-}"
VERIFY_SECONDS="${VERIFY_SECONDS:-10}"
JOURNAL_VACUUM_SIZE="${JOURNAL_VACUUM_SIZE:-500M}"
ALLOW_UNVERIFIED_BINARY="${ALLOW_UNVERIFIED_BINARY:-0}"
ALLOW_SHARED_BINARY_UPDATE="${ALLOW_SHARED_BINARY_UPDATE:-0}"
EDGE_GATEWAY_PACKAGE_SHA256="${EDGE_GATEWAY_PACKAGE_SHA256:-}"
ISOLATED_RUNTIME="${ISOLATED_RUNTIME:-0}"
DEFER_START="${DEFER_START:-0}"
CERTIFICATE_PROFILE_ID="${CERTIFICATE_PROFILE_ID:-}"
CERTIFICATE_REQUEST_ID="${CERTIFICATE_REQUEST_ID:-}"
BOOTSTRAP_BINDING_SHA256="${BOOTSTRAP_BINDING_SHA256:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "This installer must run as root." >&2
  exit 2
fi
if [ -z "${NODE_ID}" ] || [ "${NODE_ID}" = "None" ]; then
  echo "Missing NODE_ID: select a Node before running this template." >&2
  exit 2
fi
if [ -z "${NODEPULSE_TOKEN}" ] || [ "${NODEPULSE_TOKEN}" = "None" ]; then
  echo "Missing NODEPULSE_TOKEN: set the panel token before deployment." >&2
  exit 2
fi
if ! [[ "${NODE_ID}" =~ ^[0-9]+$ ]] || [ "${NODE_ID}" -lt 1 ]; then
  echo "Invalid NODE_ID: ${NODE_ID}" >&2
  exit 2
fi
if ! [[ "${NODE_PORT}" =~ ^[0-9]+$ ]] || [ "${NODE_PORT}" -gt 65535 ]; then
  echo "Invalid NODE_PORT: ${NODE_PORT}" >&2
  exit 2
fi
if ! [[ "${VERIFY_SECONDS}" =~ ^[0-9]+$ ]] || [ "${VERIFY_SECONDS}" -lt 1 ]; then
  echo "Invalid VERIFY_SECONDS: ${VERIFY_SECONDS}" >&2
  exit 2
fi

if ! [[ "${ISOLATED_RUNTIME}" =~ ^[01]$ ]] || ! [[ "${DEFER_START}" =~ ^[01]$ ]] || \
   { [ "${DEFER_START}" = "1" ] && [ "${ISOLATED_RUNTIME}" != "1" ]; }; then
  echo "Deferred bootstrap requires a verified isolated runtime." >&2
  exit 2
fi

if [ "${ISOLATED_RUNTIME}" = "1" ]; then
  if ! [[ "${CERTIFICATE_PROFILE_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$ ]] || \
     ! [[ "${CERTIFICATE_REQUEST_ID}" =~ ^[A-Za-z0-9_-]{8,80}$ ]]; then
    echo "Bootstrap preparation and activation requires stable certificate profile and request IDs." >&2
    exit 2
  fi
  if [ "${DEFER_START}" = "0" ] && ! [[ "${BOOTSTRAP_BINDING_SHA256}" =~ ^[a-f0-9]{64}$ ]]; then
    echo "Bootstrap activation requires the exact prepared binding SHA-256." >&2
    exit 2
  fi
fi

for command_name in systemctl sha256sum ss flock python3; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Missing required command: ${command_name}" >&2
    exit 2
  fi
done

install -d -m 0755 /run/lock
LOCK_PATH="/run/lock/nodepulse-v2node-deploy.lock"
exec 9>"${LOCK_PATH}"
if ! flock -n 9; then
  echo "Another NodePulse v2node deployment is already running on this host." >&2
  exit 75
fi
DOCKER_CONTAINER="v2node-${NODE_ID}"
SERVICE_NAME="v2node-${NODE_ID}"
SERVICE_UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
CONFIG_PATH="/etc/v2node/v2node-${NODE_ID}.json"
BINARY_PATH="/usr/local/bin/v2node"

case "$(uname -m)" in
  x86_64|amd64) V2NODE_ARCH="amd64" ;;
  aarch64|arm64) V2NODE_ARCH="arm64" ;;
  *)
    echo "Unsupported architecture: $(uname -m)" >&2
    exit 2
    ;;
esac

download_file() {
  local url="$1"
  local output="$2"
  local partial="${output}.part"
  if command -v curl >/dev/null 2>&1; then
    curl --fail --location --show-error --silent \
      --retry 8 --retry-delay 2 --retry-max-time 900 --retry-all-errors \
      --connect-timeout 20 --speed-time 45 --speed-limit 1024 \
      --continue-at - --output "${partial}" "${url}"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --https-only --continue --tries=8 --timeout=30 -O "${partial}" "${url}"
  else
    echo "Neither curl nor wget is installed." >&2
    return 1
  fi
  mv -f -- "${partial}" "${output}"
}

fetch_panel_json() {
  local endpoint="$1"
  local output="$2"
  NODEPULSE_FETCH_TOKEN="${NODEPULSE_TOKEN}" python3 -I - "${endpoint}" "${output}" "${NODE_ID}" "${NODEPULSE_URL}" "${ISOLATED_RUNTIME}" <<'PY'
import json, os, sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None
try:
    endpoint, output, node, base, isolated = sys.argv[1:]
    parsed = urlsplit(base)
    assert endpoint in {'/api/v2/server/config','/api/v2/server/local_config'}
    assert parsed.scheme in ({'https'} if isolated=='1' else {'http','https'}) and parsed.netloc
    assert not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
    token = os.environ.pop('NODEPULSE_FETCH_TOKEN')
    url = base.rstrip('/') + endpoint + '?' + urlencode({'node_type':'v2node','node_id':node,'token':token})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(Request(url), timeout=30) as response:
        assert response.status==200
        raw=response.read(4*1024*1024+1)
    assert len(raw)<=4*1024*1024 and isinstance(json.loads(raw),dict)
    with open(output,'xb') as target:
        os.fchmod(target.fileno(),0o600)
        target.write(raw)
except Exception:
    print('node_config_fetch_failed',file=sys.stderr)
    sys.exit(1)
PY
}

runtime_check() {
  python3 -I -c 'import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("edge_gateway.node_listener",run_name="__main__")' \
    "${TMP_DIR}/runtime" "$@" --binary "${BINARY_PATH}"
}

bootstrap_check() {
  python3 -I -c 'import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("edge_gateway.node_bootstrap",run_name="__main__")' \
    "${TMP_DIR}/runtime" "$@"
}

verify_owned_listeners() {
  runtime_check verify --node "${NODE_ID}" --server "${TMP_SERVER_CONFIG}" --local "${TMP_LOCAL_CONFIG}" --requested-port "${NODE_PORT}" >/dev/null
}

TMP_DIR="$(mktemp -d)"
TMP_BIN="${TMP_DIR}/v2node-linux-${V2NODE_ARCH}"
TMP_LOCAL_CONFIG="${TMP_DIR}/local-config.json"
TMP_SERVER_CONFIG="${TMP_DIR}/server-config.json"
TMP_CHECKSUMS="${TMP_DIR}/SHA256SUMS"
TMP_EXPECTED_ENDPOINTS="${TMP_DIR}/expected-endpoints"
CHANGES_STARTED=0
DEPLOY_COMMITTED=0
BACKUP_DIR=""

HAD_BINARY=0
HAD_CONFIG=0
HAD_UNIT=0
OLD_SERVICE_ACTIVE=0
OLD_SERVICE_ENABLED=0
OLD_DOCKER_RUNNING=0
OLD_GENERIC_SERVICE_RUNNING=0

restore_path() {
  local existed="$1"
  local backup_name="$2"
  local target="$3"
  if [ "${existed}" = "1" ]; then
    rm -f -- "${target}"
    cp -a "${BACKUP_DIR}/${backup_name}" "${target}"
  else
    rm -f -- "${target}"
  fi
}

atomic_install() {
  local source="$1"
  local target="$2"
  local mode="$3"
  local target_dir stage
  target_dir="$(dirname "${target}")"
  install -d "${target_dir}"
  stage="$(mktemp "${target_dir}/.$(basename "${target}").new.XXXXXX")"
  install -m "${mode}" "${source}" "${stage}"
  mv -fT -- "${stage}" "${target}"
}

rollback_runtime() {
  echo "Deployment failed; restoring the previous v2node runtime." >&2
  set +e
  systemctl stop "${SERVICE_NAME}.service" >/dev/null 2>&1
  if [ "${ISOLATED_RUNTIME}" != "1" ]; then
    restore_path "${HAD_BINARY}" "v2node" "${BINARY_PATH}"
  fi
  restore_path "${HAD_CONFIG}" "v2node-${NODE_ID}.json" "${CONFIG_PATH}"
  restore_path "${HAD_UNIT}" "${SERVICE_NAME}.service" "${SERVICE_UNIT}"
  systemctl daemon-reload

  if [ "${OLD_SERVICE_ENABLED}" = "1" ]; then
    systemctl enable "${SERVICE_NAME}.service" >/dev/null 2>&1
  else
    systemctl disable "${SERVICE_NAME}.service" >/dev/null 2>&1
  fi
  if [ "${OLD_SERVICE_ACTIVE}" = "1" ]; then
    systemctl start "${SERVICE_NAME}.service"
  fi
  if [ "${OLD_DOCKER_RUNNING}" = "1" ]; then
    docker start "${DOCKER_CONTAINER}" >/dev/null 2>&1
  fi
  if [ "${OLD_GENERIC_SERVICE_RUNNING}" = "1" ]; then
    systemctl start v2node.service >/dev/null 2>&1
  fi
  set -e
}

finish() {
  local rc=$?
  trap - EXIT
  if [ "${rc}" -ne 0 ] && [ "${CHANGES_STARTED}" = "1" ] && [ "${DEPLOY_COMMITTED}" != "1" ]; then
    rollback_runtime
  fi
  rm -rf -- "${TMP_DIR}"
  exit "${rc}"
}
trap finish EXIT

other_units="$(
  systemctl list-unit-files 'v2node-*.service' --no-legend 2>/dev/null \
    | awk '{print $1}' \
    | grep -Fvx "${SERVICE_NAME}.service" || true
)"
if [ "${ISOLATED_RUNTIME}" != "1" ] && [ -n "${other_units}" ] && [ "${ALLOW_SHARED_BINARY_UPDATE}" != "1" ]; then
  echo "Other v2node systemd units share ${BINARY_PATH}:" >&2
  printf '%s\n' "${other_units}" >&2
  echo "Deploy each node independently or set ALLOW_SHARED_BINARY_UPDATE=1 after reviewing impact." >&2
  exit 2
fi

V2NODE_BINARY_URL="${V2NODE_RELEASE_BASE%/}/v2node-linux-${V2NODE_ARCH}"
echo "Downloading NodePulse v2node: ${V2NODE_BINARY_URL}"
if ! download_file "${V2NODE_BINARY_URL}" "${TMP_BIN}"; then
  echo "Failed to download v2node; the existing runtime was not touched." >&2
  exit 1
fi

expected_sha256="${V2NODE_SHA256,,}"
if [ -z "${expected_sha256}" ]; then
  checksum_url="${V2NODE_RELEASE_BASE%/}/SHA256SUMS"
  if download_file "${checksum_url}" "${TMP_CHECKSUMS}"; then
    expected_sha256="$(
      awk -v file="v2node-linux-${V2NODE_ARCH}" \
        '$2 == file || $2 == "*" file {print tolower($1); exit}' "${TMP_CHECKSUMS}"
    )"
  elif [ "${ALLOW_UNVERIFIED_BINARY}" != "1" ]; then
    echo "Release checksum is unavailable: ${checksum_url}" >&2
    echo "Refusing an unverified binary. Set ALLOW_UNVERIFIED_BINARY=1 only for an audited legacy release." >&2
    exit 1
  fi
fi
if [ -n "${expected_sha256}" ]; then
  if ! [[ "${expected_sha256}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Invalid SHA-256 value for v2node-linux-${V2NODE_ARCH}." >&2
    exit 1
  fi
  actual_sha256="$(sha256sum "${TMP_BIN}" | awk '{print tolower($1)}')"
  if [ "${actual_sha256}" != "${expected_sha256}" ]; then
    echo "v2node SHA-256 mismatch: expected ${expected_sha256}, got ${actual_sha256}." >&2
    exit 1
  fi
  echo "Verified v2node SHA-256: ${actual_sha256}"
else
  echo "WARNING: installing an unverified v2node binary by explicit override." >&2
fi
if [ "${ISOLATED_RUNTIME}" = "1" ]; then
  if ! [[ "${expected_sha256}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "An isolated runtime requires a verified release SHA-256." >&2
    exit 2
  fi
  BINARY_PATH="/usr/local/lib/nodepulse/v2node/${expected_sha256}/v2node"
fi
chmod 0755 "${TMP_BIN}"
if ! candidate_version="$("${TMP_BIN}" version 2>&1)"; then
  echo "Candidate binary cannot execute; check architecture and use an executable TMPDIR. The existing runtime was not touched." >&2
  exit 2
fi
echo "Downloaded version: ${candidate_version%%$'\n'*}"

echo "Fetching local and server runtime configuration from NodePulse."
fetch_panel_json "/api/v2/server/local_config" "${TMP_LOCAL_CONFIG}"
fetch_panel_json "/api/v2/server/config" "${TMP_SERVER_CONFIG}"

# The helper is part of this release and is verified before touching a runtime.
download_file "${V2NODE_RELEASE_BASE%/}/nodepulse-edge-gateway.tar.gz" "${TMP_DIR}/runtime.tar.gz"
if [ -z "${EDGE_GATEWAY_PACKAGE_SHA256}" ]; then
  if [ ! -f "${TMP_CHECKSUMS}" ]; then
    download_file "${V2NODE_RELEASE_BASE%/}/SHA256SUMS" "${TMP_CHECKSUMS}"
  fi
  EDGE_GATEWAY_PACKAGE_SHA256="$(awk '$2 == "nodepulse-edge-gateway.tar.gz" {print $1; exit}' "${TMP_CHECKSUMS}")"
fi
if ! [[ "${EDGE_GATEWAY_PACKAGE_SHA256}" =~ ^[0-9a-f]{64}$ ]] || \
  [ "$(sha256sum "${TMP_DIR}/runtime.tar.gz" | awk '{print $1}')" != "${EDGE_GATEWAY_PACKAGE_SHA256}" ]; then
  echo "Runtime helper checksum missing or mismatched; the existing runtime was not touched." >&2
  exit 2
fi
python3 -I - "${TMP_DIR}/runtime.tar.gz" "${TMP_DIR}/runtime" <<'PY'
from pathlib import Path
import sys, tarfile
target = Path(sys.argv[2])
with tarfile.open(sys.argv[1]) as archive:
    members = archive.getmembers()
    assert len(members) <= 32 and sum(item.size for item in members) <= 2 * 1024 * 1024
    for item in members:
        parts = Path(item.name).parts
        assert len(parts) in {1, 2} and parts[0] == "edge_gateway" and ".." not in parts
        assert (item.isdir() and len(parts) == 1) or (item.isfile() and len(parts) == 2 and parts[-1].endswith((".py", ".md")))
    for item in members:
        if item.isfile():
            path = target / item.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.extractfile(item).read())
PY
if [ "${ISOLATED_RUNTIME}" = "1" ]; then
  runtime_check prepare --node "${NODE_ID}" --server "${TMP_SERVER_CONFIG}" --local "${TMP_LOCAL_CONFIG}" \
    --requested-port "${NODE_PORT}" >"${TMP_DIR}/pinned-local-config.json"
  chmod 0600 "${TMP_DIR}/pinned-local-config.json"
  mv -f -- "${TMP_DIR}/pinned-local-config.json" "${TMP_LOCAL_CONFIG}"
fi
runtime_check compile --node "${NODE_ID}" --server "${TMP_SERVER_CONFIG}" --local "${TMP_LOCAL_CONFIG}" \
  --requested-port "${NODE_PORT}" >"${TMP_EXPECTED_ENDPOINTS}"

mapfile -t EXPECTED_ENDPOINTS <"${TMP_EXPECTED_ENDPOINTS}"
if [ "${#EXPECTED_ENDPOINTS[@]}" -eq 0 ]; then
  echo "No expected listener endpoints were resolved from NodePulse." >&2
  exit 1
fi
echo "Required listener endpoints: ${EXPECTED_ENDPOINTS[*]}"

if [ -e "${BINARY_PATH}" ] || [ -L "${BINARY_PATH}" ]; then HAD_BINARY=1; fi
if [ -e "${CONFIG_PATH}" ] || [ -L "${CONFIG_PATH}" ]; then HAD_CONFIG=1; fi
if [ -e "${SERVICE_UNIT}" ] || [ -L "${SERVICE_UNIT}" ]; then HAD_UNIT=1; fi
if systemctl is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null; then OLD_SERVICE_ACTIVE=1; fi
if systemctl is-enabled --quiet "${SERVICE_NAME}.service" 2>/dev/null; then OLD_SERVICE_ENABLED=1; fi
if command -v docker >/dev/null 2>&1 && docker inspect "${DOCKER_CONTAINER}" >/dev/null 2>&1; then
  if [ "$(docker inspect -f '{{.State.Running}}' "${DOCKER_CONTAINER}" 2>/dev/null || echo false)" = "true" ]; then
    OLD_DOCKER_RUNNING=1
  fi
fi
if systemctl is-active --quiet v2node.service 2>/dev/null; then
  if runtime_check owned-generic --node "${NODE_ID}"; then
    OLD_GENERIC_SERVICE_RUNNING=1
  else
    echo "Existing generic v2node service is not proven to belong to this node; refusing takeover." >&2
    exit 2
  fi
fi
if [ "${OLD_DOCKER_RUNNING}" = "1" ]; then
  echo "Legacy Docker migration needs separate container/config ownership verification; refusing automatic takeover." >&2
  exit 2
fi
if [ "${ISOLATED_RUNTIME}" = "1" ] && { [ "${OLD_SERVICE_ACTIVE}" = "1" ] || [ "${OLD_GENERIC_SERVICE_RUNNING}" = "1" ]; }; then
  echo "Bootstrap only accepts an inactive target; reconcile an already running node instead of restarting it." >&2
  exit 2
fi
runtime_check preflight --node "${NODE_ID}" --server "${TMP_SERVER_CONFIG}" --local "${TMP_LOCAL_CONFIG}" \
  --requested-port "${NODE_PORT}" >/dev/null

if [ "${ISOLATED_RUNTIME}" = "1" ]; then
  bootstrap_phase=prepare
  if [ "${DEFER_START}" = "0" ]; then bootstrap_phase=activate; fi
  bootstrap_check reserve --node "${NODE_ID}" --binary "${BINARY_PATH}" \
    --server "${TMP_SERVER_CONFIG}" --local "${TMP_LOCAL_CONFIG}" \
    --profile-id "${CERTIFICATE_PROFILE_ID}" --request-id "${CERTIFICATE_REQUEST_ID}" \
    --phase "${bootstrap_phase}" --binding-sha256 "${BOOTSTRAP_BINDING_SHA256}" --helper-sha256 "${EDGE_GATEWAY_PACKAGE_SHA256}"
fi

if [ "${ISOLATED_RUNTIME}" = "1" ] && [ "${DEFER_START}" = "0" ]; then
  "${TMP_BIN}" certificate profile --profile-id "${CERTIFICATE_PROFILE_ID}" >"${TMP_DIR}/certificate-profile.json"
  "${TMP_BIN}" certificate inspect --profile-id "${CERTIFICATE_PROFILE_ID}" >"${TMP_DIR}/certificate-inspect.json"
  python3 -I - "${TMP_DIR}/certificate-profile.json" "${TMP_DIR}/certificate-inspect.json" "${TMP_SERVER_CONFIG}" "${CERTIFICATE_REQUEST_ID}" <<'PY'
import json, sys
from pathlib import Path
profile, receipt, server = [json.loads(Path(value).read_bytes()) for value in sys.argv[1:4]]
cert=server['cert_config']; tls=server['tls_settings']
assert profile['runtime_uid']==0 and profile['runtime_uid_source']=='cli' and profile['service_uid_verified'] is False, 'certificate_cli_identity_mismatch'
assert profile['domain']==tls['server_name'] and profile['cert_file']==cert['cert_file'] and profile['key_file']==cert['key_file'], 'certificate_target_mismatch'
assert receipt['state']=='persisted' and receipt['generation_bound'] is True and receipt['request_id']==sys.argv[4], 'certificate_task_mismatch'
assert receipt['profile_id']==profile['profile_id'] and receipt['profile_sha256']==profile['profile_sha256'] and receipt['runtime_uid']==0, 'certificate_profile_mismatch'
assert receipt['tls_handshake_verified'] is False, 'unexpected_certificate_receipt'
PY
fi

backup_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 /root/nodepulse-backups
BACKUP_DIR="$(mktemp -d "/root/nodepulse-backups/${SERVICE_NAME}-${backup_stamp}.XXXXXX")"
chmod 0700 "${BACKUP_DIR}"
if [ "${HAD_BINARY}" = "1" ]; then cp -a "${BINARY_PATH}" "${BACKUP_DIR}/v2node"; fi
if [ "${HAD_CONFIG}" = "1" ]; then cp -a "${CONFIG_PATH}" "${BACKUP_DIR}/v2node-${NODE_ID}.json"; fi
if [ "${HAD_UNIT}" = "1" ]; then cp -a "${SERVICE_UNIT}" "${BACKUP_DIR}/${SERVICE_NAME}.service"; fi
{
  echo "created_utc=${backup_stamp}"
  echo "service_active=${OLD_SERVICE_ACTIVE}"
  echo "service_enabled=${OLD_SERVICE_ENABLED}"
  echo "docker_running=${OLD_DOCKER_RUNNING}"
  echo "generic_service_running=${OLD_GENERIC_SERVICE_RUNNING}"
  if [ "${HAD_BINARY}" = "1" ]; then sha256sum "${BINARY_PATH}"; fi
} >"${BACKUP_DIR}/manifest.txt"
chmod -R go-rwx "${BACKUP_DIR}"
echo "Previous runtime backup: ${BACKUP_DIR}"

mkdir -p /etc/v2node /var/log/v2node
CHANGES_STARTED=1
if [ "${ISOLATED_RUNTIME}" = "1" ]; then
  python3 -I - "${BINARY_PATH}" "${expected_sha256}" <<'PY'
import hashlib, os, stat, sys
from pathlib import Path
path=Path(sys.argv[1])
for parent in [path.parent, *path.parent.parents]:
    if parent.exists() or parent.is_symlink():
        info=parent.lstat()
        assert stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022, 'untrusted_runtime_directory'
if path.exists() or path.is_symlink():
    info=path.lstat()
    assert stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022, 'untrusted_runtime_file'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==sys.argv[2], 'immutable_runtime_hash_mismatch'
PY
  if [ ! -f "${BINARY_PATH}" ]; then atomic_install "${TMP_BIN}" "${BINARY_PATH}" 0755; fi
else
  atomic_install "${TMP_BIN}" "${BINARY_PATH}" 0755
fi
if [ "${ISOLATED_RUNTIME}" != "1" ] || [ "${DEFER_START}" = "1" ]; then
  atomic_install "${TMP_LOCAL_CONFIG}" "${CONFIG_PATH}" 0600

TMP_SERVICE_UNIT="$(mktemp "$(dirname "${SERVICE_UNIT}")/.${SERVICE_NAME}.service.new.XXXXXX")"
cat >"${TMP_SERVICE_UNIT}" <<EOF
[Unit]
Description=NodePulse v2node ${NODE_ID}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=${BINARY_PATH} server --config ${CONFIG_PATH} --watch=false
Restart=on-failure
RestartSec=3
KillMode=control-group
TimeoutStopSec=30s
SendSIGKILL=yes
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "${TMP_SERVICE_UNIT}"
mv -fT -- "${TMP_SERVICE_UNIT}" "${SERVICE_UNIT}"
systemctl daemon-reload
fi

if [ "${DEFER_START}" = "1" ]; then
  systemctl disable "${SERVICE_NAME}.service" >/dev/null
  bootstrap_check prepared --node "${NODE_ID}"
  DEPLOY_COMMITTED=1
  exit 0
fi

if [ "${OLD_GENERIC_SERVICE_RUNNING}" = "1" ]; then
  systemctl stop v2node.service
fi

systemctl enable "${SERVICE_NAME}.service" >/dev/null
service_action=restart
if [ "${ISOLATED_RUNTIME}" = "1" ]; then service_action=start; fi
if ! systemctl "${service_action}" "${SERVICE_NAME}.service"; then
  systemctl status "${SERVICE_NAME}.service" --no-pager -l || true
  exit 1
fi

restart_count="$(systemctl show "${SERVICE_NAME}.service" -p NRestarts --value)"
all_ports_ready=0
for _ in $(seq 1 "${VERIFY_SECONDS}"); do
  all_ports_ready=1
  if ! verify_owned_listeners; then
    all_ports_ready=0
  fi
  if [ "${all_ports_ready}" = "1" ]; then
    break
  fi
  sleep 1
done

sleep "${VERIFY_SECONDS}"
if ! systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  echo "${SERVICE_NAME} is not active after the stability window." >&2
  systemctl status "${SERVICE_NAME}.service" --no-pager -l || true
  journalctl -u "${SERVICE_NAME}.service" -n 120 --no-pager || true
  exit 1
fi
if [ "${all_ports_ready}" != "1" ]; then
  echo "${SERVICE_NAME} did not open every required listener: ${EXPECTED_ENDPOINTS[*]}" >&2
  ss -lntupH || true
  journalctl -u "${SERVICE_NAME}.service" -n 120 --no-pager || true
  exit 1
fi
if ! verify_owned_listeners; then
  echo "The required listeners are missing or belong to another process; refusing deployment success." >&2
  exit 1
fi
current_restart_count="$(systemctl show "${SERVICE_NAME}.service" -p NRestarts --value)"
if [ "${current_restart_count}" != "${restart_count}" ]; then
  echo "${SERVICE_NAME} restarted during verification (${restart_count} -> ${current_restart_count})." >&2
  journalctl -u "${SERVICE_NAME}.service" -n 120 --no-pager || true
  exit 1
fi

if [ "${OLD_GENERIC_SERVICE_RUNNING}" = "1" ]; then
  systemctl disable v2node.service >/dev/null 2>&1 || true
fi

if [ "${ISOLATED_RUNTIME}" = "1" ]; then
  bootstrap_check activated --node "${NODE_ID}"
  DEPLOY_COMMITTED=1
  exit 0
fi
DEPLOY_COMMITTED=1
systemctl status "${SERVICE_NAME}.service" --no-pager -l | sed -n '1,80p'
echo "Verified listeners: ${EXPECTED_ENDPOINTS[*]}"
echo "Installed version: $("${BINARY_PATH}" version 2>&1 | head -n 1)"
