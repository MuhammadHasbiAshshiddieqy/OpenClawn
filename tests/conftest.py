"""Fixture bersama seluruh suite."""

import os

# TestClient FastAPI mengirim `Host: testserver`. Saat auth nonaktif, middleware
# hanya menerima localhost + OPENCLAWN_ALLOWED_HOSTS (audit 2026-09-25, anti DNS
# rebinding) — diset SEBELUM infra.config diimpor agar CONFIG (dan setiap reload
# di test web) ikut membacanya.
os.environ.setdefault("OPENCLAWN_ALLOWED_HOSTS", "testserver")

import pytest  # noqa: E402

import infra.config as config_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_global_config():
    """Pulihkan `infra.config.CONFIG` setelah tiap test.

    Test web (mis. tests/test_rbac_web.py) me-`reload` modul config dengan env
    auth aktif. Modul yang membaca config SAAT DIPANGGIL (infra/workspace.py,
    tools/data.py — audit 2026-09-25) akan ikut melihat config itu di test
    berikutnya tanpa fixture ini, membuat hasil test bergantung urutan.
    """
    original = config_mod.CONFIG
    yield
    config_mod.CONFIG = original
