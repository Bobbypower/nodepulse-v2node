#!/usr/bin/env bash
set -euo pipefail

NODE_ID="${NODE_ID:-}"
NODE_PORT="${NODE_PORT:-0}"
NODEPULSE_URL="${NODEPULSE_URL:-https://node.eatp.top}"
NODEPULSE_TOKEN="${NODEPULSE_TOKEN:-}"
V2NODE_RELEASE_BASE="${V2NODE_RELEASE_BASE:-https://github.com/Bobbypower/nodepulse-v2node/releases/download/v2node-nodepulse-latest}"
VERIFY_SECONDS="${VERIFY_SECONDS:-10}"
JOURNAL_VACUUM_SIZE="${JOURNAL_VACUUM_SIZE:-500M}"

if [ -z "${NODE_ID}" ] || [ "${NODE_ID}" = "None" ]; then
  echo "Missing NODE_ID: select a Node before running this template." >&2
  exit 2
fi
if [ -z "${NODEPULSE_TOKEN}" ] || [ "${NODEPULSE_TOKEN}" = "None" ]; then
  echo "Missing NODEPULSE_TOKEN: set the panel token before deployment." >&2
  exit 2
fi
if ! [[ "${NODE_PORT}" =~ ^[0-9]+$ ]]; then
  echo "Invalid NODE_PORT: ${NODE_PORT}" >&2
  exit 2
fi

DOCKER_CONTAINER="v2node-${NODE_ID}"
SERVICE_NAME="v2node-${NODE_ID}"
CONFIG_PATH="/etc/v2node/v2node-${NODE_ID}.json"

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
  else
    wget -q --no-check-certificate -O "${output}" "${url}"
  fi
}

OLD_DOCKER_RUNNING=0
if command -v docker >/dev/null 2>&1 && docker inspect "${DOCKER_CONTAINER}" >/dev/null 2>&1; then
  if [ "$(docker inspect -f '{{.State.Running}}' "${DOCKER_CONTAINER}" 2>/dev/null || echo false)" = "true" ]; then
    OLD_DOCKER_RUNNING=1
  fi
fi

OLD_GENERIC_SERVICE_RUNNING=0
if systemctl is-active --quiet v2node.service 2>/dev/null; then
  OLD_GENERIC_SERVICE_RUNNING=1
fi

mkdir -p /etc/v2node /var/log/v2node
tmp_bin="$(mktemp)"
tmp_config="$(mktemp)"
trap 'rm -f "${tmp_bin}" "${tmp_config}"' EXIT

V2NODE_BINARY_URL="${V2NODE_RELEASE_BASE%/}/v2node-linux-${V2NODE_ARCH}"
echo "Downloading NodePulse v2node: ${V2NODE_BINARY_URL}"
if ! download_file "${V2NODE_BINARY_URL}" "${tmp_bin}"; then
  echo "Failed to download patched v2node; old runtime was not touched." >&2
  exit 1
fi
install -m 0755 "${tmp_bin}" /usr/local/bin/v2node

CONFIG_URL="${NODEPULSE_URL%/}/api/v2/server/local_config?node_type=v2node&node_id=${NODE_ID}&token=${NODEPULSE_TOKEN}"
echo "Fetching local runtime config from NodePulse."
if ! download_file "${CONFIG_URL}" "${tmp_config}"; then
  echo "Failed to fetch NodePulse local config; old runtime was not touched." >&2
  exit 1
fi
if command -v python3 >/dev/null 2>&1; then
  python3 -m json.tool "${tmp_config}" >/dev/null
fi
install -m 0600 "${tmp_config}" "${CONFIG_PATH}"

cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=NodePulse v2node ${NODE_ID}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/v2node server --config ${CONFIG_PATH} --watch=false
Restart=always
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

echo "Starting ${SERVICE_NAME}. Old runtime will be restored if health check fails."
if [ "${OLD_DOCKER_RUNNING}" = "1" ]; then
  docker stop "${DOCKER_CONTAINER}"
fi
if [ "${OLD_GENERIC_SERVICE_RUNNING}" = "1" ]; then
  systemctl stop v2node.service || true
fi

systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
if ! systemctl restart "${SERVICE_NAME}"; then
  echo "v2node restart failed; restoring previous runtime." >&2
  if [ "${OLD_DOCKER_RUNNING}" = "1" ]; then docker start "${DOCKER_CONTAINER}" || true; fi
  if [ "${OLD_GENERIC_SERVICE_RUNNING}" = "1" ]; then systemctl start v2node.service || true; fi
  systemctl status "${SERVICE_NAME}" --no-pager -l || true
  exit 1
fi

sleep "${VERIFY_SECONDS}"
if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
  echo "v2node service is not active; restoring previous runtime." >&2
  if [ "${OLD_DOCKER_RUNNING}" = "1" ]; then docker start "${DOCKER_CONTAINER}" || true; fi
  if [ "${OLD_GENERIC_SERVICE_RUNNING}" = "1" ]; then systemctl start v2node.service || true; fi
  systemctl status "${SERVICE_NAME}" --no-pager -l || true
  journalctl -u "${SERVICE_NAME}" -n 120 --no-pager || true
  exit 1
fi

if [ "${NODE_PORT}" != "0" ]; then
  echo "Verifying listen port ${NODE_PORT}."
  PORT_READY=0
  for _ in $(seq 1 "${VERIFY_SECONDS}"); do
    if command -v ss >/dev/null 2>&1 && ss -ltnH | awk '{print $4}' | grep -Eq "(:|\\])${NODE_PORT}$"; then
      PORT_READY=1
      break
    fi
    sleep 1
  done
  if [ "${PORT_READY}" != "1" ]; then
    echo "v2node is active but port ${NODE_PORT} is not listening; restoring previous runtime." >&2
    systemctl stop "${SERVICE_NAME}" || true
    if [ "${OLD_DOCKER_RUNNING}" = "1" ]; then docker start "${DOCKER_CONTAINER}" || true; fi
    if [ "${OLD_GENERIC_SERVICE_RUNNING}" = "1" ]; then systemctl start v2node.service || true; fi
    systemctl status "${SERVICE_NAME}" --no-pager -l || true
    journalctl -u "${SERVICE_NAME}" -n 120 --no-pager || true
    exit 1
  fi
fi

if command -v docker >/dev/null 2>&1 && docker inspect "${DOCKER_CONTAINER}" >/dev/null 2>&1; then
  echo "${SERVICE_NAME} is active; removing old Docker container ${DOCKER_CONTAINER}."
  docker rm -f "${DOCKER_CONTAINER}"
fi
if [ "${OLD_GENERIC_SERVICE_RUNNING}" = "1" ]; then
  systemctl disable v2node.service 2>/dev/null || true
fi

journalctl --rotate || true
journalctl --vacuum-size="${JOURNAL_VACUUM_SIZE}" || true
systemctl status "${SERVICE_NAME}" --no-pager -l | sed -n '1,80p'
