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

# setpriv (util-linux — sudah ada di base image python:3.12-slim, TANPA
# paket tambahan) drop privilege via execve() langsung, BUKAN fork+relay
# seperti su/sudo — SIGTERM saat `docker stop` sampai tepat ke proses
# target untuk graceful shutdown uvicorn, bukan lewat proses perantara
# yang mungkin tak meneruskan sinyal dengan benar.
exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
