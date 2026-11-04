FROM caddy:2.10.2 AS caddy
FROM debian:12-slim
ENV container=docker
RUN apt-get -o Acquire::Retries=4 update && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=4 install -y --no-install-recommends \
    systemd systemd-sysv dbus python3 python3-pytest python3-h2 python3-venv \
    curl iproute2 openssl ca-certificates gcc libc6-dev unzip \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl mask systemd-remount-fs.service systemd-logind.service getty.target
COPY --from=caddy /usr/bin/caddy /usr/bin/caddy
RUN python3 -m venv --system-site-packages /opt/edge-test-venv \
    && /opt/edge-test-venv/bin/pip install --no-cache-dir aioquic==1.2.0
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
