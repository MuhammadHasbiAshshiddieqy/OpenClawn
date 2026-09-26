#!/bin/bash
set -e

# Audit produksi 2026-07-30 (CLAUDE.md §1 keamanan dulu): Dockerfile.role
# sebelumnya berjalan sebagai root — satu-satunya image di proyek ini tanpa
# non-root user (beda dari Dockerfile.sandbox yang sudah `USER nobody`).
# TIDAK diperbaiki dengan `USER appuser` statis di Dockerfile — bind-mount
# host (`./data` di docker-compose.yml) yang di-auto-buat Docker saat
# direktori belum ada di host biasanya dimiliki root:root, jadi proses
# non-root gagal menulis `data/openclawn.db` pada deployment yang sudah
# jalan. Entrypoint ini chown SEBELUM drop privilege, tiap start (bukan
# cuma sekali di build), agar aman terlepas dari ownership host asli.
mkdir -p /app/data
chown -R appuser:appuser /app/data

# Keputusan 2026-09-26 (sandbox via Docker-in-Docker, docker-compose.sandbox.yml):
# workspace & direktori temp sandbox ada di volume bersama dengan daemon DinD —
# pastikan ada & dimiliki appuser. Sertifikat klien TLS dari DinD dimiliki root
# dengan key.pem 0600; salin ke lokasi yang bisa dibaca appuser (tetap di dalam
# container, tak pernah ke host).
for dir in "${OPENCLAWN_WORKSPACE:-}" "${OPENCLAWN_SANDBOX_TMPDIR:-}"; do
    if [ -n "$dir" ] && [ "$dir" != "." ]; then
        mkdir -p "$dir"
        chown appuser:appuser "$dir"
    fi
done
if [ -n "${DOCKER_CERT_PATH:-}" ] && [ -f "$DOCKER_CERT_PATH/key.pem" ]; then
    install -d -m 700 -o appuser -g appuser /tmp/openclawn-docker-certs
    install -m 600 -o appuser -g appuser "$DOCKER_CERT_PATH"/*.pem /tmp/openclawn-docker-certs/
    export DOCKER_CERT_PATH=/tmp/openclawn-docker-certs
fi

# setpriv (util-linux — sudah ada di base image python:3.12-slim, TANPA
# paket tambahan) drop privilege via execve() langsung, BUKAN fork+relay
# seperti su/sudo — SIGTERM saat `docker stop` sampai tepat ke proses
# target untuk graceful shutdown uvicorn, bukan lewat proses perantara
# yang mungkin tak meneruskan sinyal dengan benar.
exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
