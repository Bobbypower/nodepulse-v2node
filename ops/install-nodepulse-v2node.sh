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

for command_name in systemctl sha256sum ss; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Missing required command: ${command_name}" >&2
    exit 2
  fi
done
if command -v jq >/dev/null 2>&1; then
  JSON_PARSER="jq"
elif command -v python3 >/dev/null 2>&1; then
  JSON_PARSER="python3"
else
  echo "Missing JSON parser: install jq or python3." >&2
  exit 2
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
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "${output}" "${url}"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --https-only -O "${output}" "${url}"
  else
    echo "Neither curl nor wget is installed." >&2
    return 1
  fi
}

fetch_panel_json() {
  local endpoint="$1"
  local output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSLG -o "${output}" "${NODEPULSE_URL%/}${endpoint}" \
      --data-urlencode "node_type=v2node" \
      --data-urlencode "node_id=${NODE_ID}" \
      --data-urlencode "token=${NODEPULSE_TOKEN}"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --https-only -O "${output}" \
      "${NODEPULSE_URL%/}${endpoint}?node_type=v2node&node_id=${NODE_ID}&token=${NODEPULSE_TOKEN}"
  else
    echo "Neither curl nor wget is installed." >&2
    return 1
  fi
}

port_is_listening() {
  local transport="$1"
  local port="$2"
  case "${transport}" in
    tcp) ss -ltnH ;;
    udp) ss -lunH ;;
    *) return 2 ;;
  esac | awk '{print $4}' | grep -Eq "(:|\\])${port}$"
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
    cp -a "${BACKUP_DIR}/${backup_name}" "${target}"
  else
    rm -f -- "${target}"
  fi
}

rollback_runtime() {
  echo "Deployment failed; restoring the previous v2node runtime." >&2
  set +e
  systemctl stop "${SERVICE_NAME}.service" >/dev/null 2>&1
  restore_path "${HAD_BINARY}" "v2node" "${BINARY_PATH}"
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
if [ -n "${other_units}" ] && [ "${ALLOW_SHARED_BINARY_UPDATE}" != "1" ]; then
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
chmod 0755 "${TMP_BIN}"
echo "Downloaded version: $("${TMP_BIN}" version 2>&1 | head -n 1)"

echo "Fetching local and server runtime configuration from NodePulse."
fetch_panel_json "/api/v2/server/local_config" "${TMP_LOCAL_CONFIG}"
fetch_panel_json "/api/v2/server/config" "${TMP_SERVER_CONFIG}"

if [ "${JSON_PARSER}" = "jq" ]; then
  jq -e . "${TMP_LOCAL_CONFIG}" >/dev/null
  read -r -d '' JQ_SERVER_CONFIG_FILTER <<'JQ' || true
def number_or_null:
  if type == "number" then
    .
  elif type == "string" then
    (try tonumber catch null)
  else
    null
  end;

. as $config
| ($requested_port | tonumber) as $requested
| if $config.status == "fail" then
    error("NodePulse server config failed: \($config.message // "unknown error")")
  else
    .
  end
| ($config.server_port | number_or_null) as $primary
| if ($primary == null or $primary < 1 or $primary > 65535) then
    error("NodePulse server config has no valid server_port")
  elif ($requested != 0 and $requested != $primary) then
    error("NODE_PORT=\($requested) does not match NodePulse server_port=\($primary)")
  else
    .
  end
| ($config.protocol // "" | tostring | ascii_downcase) as $protocol
| ($config.network // "" | tostring | ascii_downcase) as $network
| (($config.tls_settings.alpn // []) | if type == "array" then map(tostring | ascii_downcase) else [] end) as $alpn
| (if ($protocol == "hysteria2" or $protocol == "tuic" or ((($network == "xhttp") or ($network == "splithttp")) and $alpn == ["h3"])) then "udp" else "tcp" end) as $primary_transport
| if (($config.xhttp_download_inbound | type) == "object") then
    if (($config.network // "" | tostring | ascii_downcase) != "xhttp") then
      error("xhttp_download_inbound requires the primary network to be xhttp")
    elif (($config.xhttp_download_inbound.network // "xhttp" | tostring | ascii_downcase) != "xhttp") then
      error("xhttp_download_inbound.network must be xhttp")
    elif (($config.xhttp_download_inbound.security // "none" | tostring | ascii_downcase) != "none") then
      error("xhttp_download_inbound.security must be none")
    else
      .
    end
    | ($config.xhttp_download_inbound.server_port | number_or_null) as $download
    | if ($download == null or $download < 1 or $download > 65535) then
        error("xhttp_download_inbound has no valid server_port")
      else
        .
      end
    | ($config.network_settings.path // "/" | tostring) as $main_path
    | ($config.xhttp_download_inbound.network_settings.path // $main_path | tostring) as $download_path
    | if ($main_path != $download_path) then
      error("XHTTP upload and download inbounds must use the same normalized path")
      else
        if ($primary == $download and $primary_transport == "tcp") then
          "tcp:\($primary)"
        else
          "\($primary_transport):\($primary)", "tcp:\($download)"
        end
      end
  else
    "\($primary_transport):\($primary)"
  end
JQ
  jq -er --arg requested_port "${NODE_PORT}" "${JQ_SERVER_CONFIG_FILTER}" \
    "${TMP_SERVER_CONFIG}" >"${TMP_EXPECTED_ENDPOINTS}"
else
  python3 -m json.tool "${TMP_LOCAL_CONFIG}" >/dev/null
  python3 - "${TMP_SERVER_CONFIG}" "${NODE_PORT}" >"${TMP_EXPECTED_ENDPOINTS}" <<'PY'
import json
import sys

config_path, requested_port = sys.argv[1], int(sys.argv[2])
with open(config_path, "r", encoding="utf-8") as handle:
    config = json.load(handle)

if config.get("status") == "fail":
    raise SystemExit(f"NodePulse server config failed: {config.get('message', 'unknown error')}")

try:
    primary_port = int(config.get("server_port") or 0)
except (TypeError, ValueError):
    primary_port = 0
if not 1 <= primary_port <= 65535:
    raise SystemExit("NodePulse server config has no valid server_port")
if requested_port and requested_port != primary_port:
    raise SystemExit(
        f"NODE_PORT={requested_port} does not match NodePulse server_port={primary_port}"
    )

protocol = str(config.get("protocol") or "").lower()
network = str(config.get("network") or "").lower()
tls_settings = config.get("tls_settings") or {}
alpn = tls_settings.get("alpn") if isinstance(tls_settings, dict) else []
alpn = [str(item).lower() for item in alpn] if isinstance(alpn, list) else []
primary_transport = (
    "udp"
    if protocol in {"hysteria2", "tuic"}
    or (network in {"xhttp", "splithttp"} and alpn == ["h3"])
    else "tcp"
)
endpoints = [f"{primary_transport}:{primary_port}"]
download = config.get("xhttp_download_inbound")
if isinstance(download, dict):
    if str(config.get("network") or "").lower() != "xhttp":
        raise SystemExit("xhttp_download_inbound requires the primary network to be xhttp")
    if str(download.get("network") or "xhttp").lower() != "xhttp":
        raise SystemExit("xhttp_download_inbound.network must be xhttp")
    if str(download.get("security") or "none").lower() != "none":
        raise SystemExit("xhttp_download_inbound.security must be none")
    try:
        download_port = int(download.get("server_port") or 0)
    except (TypeError, ValueError):
        download_port = 0
    if not 1 <= download_port <= 65535:
        raise SystemExit("xhttp_download_inbound has no valid server_port")

    main_settings = config.get("network_settings") or {}
    download_settings = download.get("network_settings") or {}
    main_path = str(main_settings.get("path") or "/")
    download_path = str(download_settings.get("path") or main_path)
    if main_path != download_path:
        raise SystemExit(
            "XHTTP upload and download inbounds must use the same normalized path"
        )
    endpoints.append(f"tcp:{download_port}")

for endpoint in dict.fromkeys(endpoints):
    print(endpoint)
PY
fi

mapfile -t EXPECTED_ENDPOINTS <"${TMP_EXPECTED_ENDPOINTS}"
if [ "${#EXPECTED_ENDPOINTS[@]}" -eq 0 ]; then
  echo "No expected listener endpoints were resolved from NodePulse." >&2
  exit 1
fi
echo "Required listener endpoints: ${EXPECTED_ENDPOINTS[*]}"

if [ -e "${BINARY_PATH}" ]; then HAD_BINARY=1; fi
if [ -e "${CONFIG_PATH}" ]; then HAD_CONFIG=1; fi
if [ -e "${SERVICE_UNIT}" ]; then HAD_UNIT=1; fi
if systemctl is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null; then OLD_SERVICE_ACTIVE=1; fi
if systemctl is-enabled --quiet "${SERVICE_NAME}.service" 2>/dev/null; then OLD_SERVICE_ENABLED=1; fi
if command -v docker >/dev/null 2>&1 && docker inspect "${DOCKER_CONTAINER}" >/dev/null 2>&1; then
  if [ "$(docker inspect -f '{{.State.Running}}' "${DOCKER_CONTAINER}" 2>/dev/null || echo false)" = "true" ]; then
    OLD_DOCKER_RUNNING=1
  fi
fi
if systemctl is-active --quiet v2node.service 2>/dev/null; then
  OLD_GENERIC_SERVICE_RUNNING=1
fi

backup_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/root/nodepulse-backups/${SERVICE_NAME}-${backup_stamp}"
install -d -m 0700 "${BACKUP_DIR}"
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
install -m 0755 "${TMP_BIN}" "${BINARY_PATH}"
install -m 0600 "${TMP_LOCAL_CONFIG}" "${CONFIG_PATH}"

cat >"${SERVICE_UNIT}" <<EOF
[Unit]
Description=NodePulse v2node ${NODE_ID}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${BINARY_PATH} server --config ${CONFIG_PATH} --watch=false
Restart=always
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

if [ "${OLD_DOCKER_RUNNING}" = "1" ]; then
  docker stop "${DOCKER_CONTAINER}" >/dev/null
fi
if [ "${OLD_GENERIC_SERVICE_RUNNING}" = "1" ]; then
  systemctl stop v2node.service
fi

systemctl enable "${SERVICE_NAME}.service" >/dev/null
if ! systemctl restart "${SERVICE_NAME}.service"; then
  systemctl status "${SERVICE_NAME}.service" --no-pager -l || true
  exit 1
fi

restart_count="$(systemctl show "${SERVICE_NAME}.service" -p NRestarts --value)"
all_ports_ready=0
for _ in $(seq 1 "${VERIFY_SECONDS}"); do
  all_ports_ready=1
  for endpoint in "${EXPECTED_ENDPOINTS[@]}"; do
    transport="${endpoint%%:*}"
    port="${endpoint#*:}"
    if ! port_is_listening "${transport}" "${port}"; then
      all_ports_ready=0
      break
    fi
  done
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
for endpoint in "${EXPECTED_ENDPOINTS[@]}"; do
  transport="${endpoint%%:*}"
  port="${endpoint#*:}"
  if ! port_is_listening "${transport}" "${port}"; then
    echo "${SERVICE_NAME} lost required listener ${endpoint} during verification." >&2
    ss -lntupH || true
    journalctl -u "${SERVICE_NAME}.service" -n 120 --no-pager || true
    exit 1
  fi
done
current_restart_count="$(systemctl show "${SERVICE_NAME}.service" -p NRestarts --value)"
if [ "${current_restart_count}" != "${restart_count}" ]; then
  echo "${SERVICE_NAME} restarted during verification (${restart_count} -> ${current_restart_count})." >&2
  journalctl -u "${SERVICE_NAME}.service" -n 120 --no-pager || true
  exit 1
fi

if command -v docker >/dev/null 2>&1 && docker inspect "${DOCKER_CONTAINER}" >/dev/null 2>&1; then
  docker rm -f "${DOCKER_CONTAINER}" >/dev/null || true
fi
if [ "${OLD_GENERIC_SERVICE_RUNNING}" = "1" ]; then
  systemctl disable v2node.service >/dev/null 2>&1 || true
fi

DEPLOY_COMMITTED=1
journalctl --rotate || true
journalctl --vacuum-size="${JOURNAL_VACUUM_SIZE}" || true
systemctl status "${SERVICE_NAME}.service" --no-pager -l | sed -n '1,80p'
echo "Verified listeners: ${EXPECTED_ENDPOINTS[*]}"
echo "Installed version: $("${BINARY_PATH}" version 2>&1 | head -n 1)"
