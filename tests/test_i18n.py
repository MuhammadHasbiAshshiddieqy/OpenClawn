"""Test untuk UI locale (bahasa tampilan web) — infra/i18n.py + SettingsStore.

Locale ini murni untuk teks statis di web/templates/*.html. TIDAK menyentuh
bahasa respons agent (agent selalu mengikuti bahasa pesan user, §1.5).
"""

import pytest

from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.i18n import DEFAULT_LOCALE, LOCALES, STRINGS, t, translator
from infra.settings import SettingsStore


# ── infra/i18n.py: t() dan translator() murni, tanpa DB ─────────────────────


def test_default_locale_is_english():
    assert DEFAULT_LOCALE == "en"
    assert "id" in LOCALES and "en" in LOCALES


def test_t_returns_english_by_default():
    assert t("common.save", "en") == "Save"


def test_t_returns_indonesian_when_requested():
    assert t("common.save", "id") == "Simpan"


def test_t_unknown_locale_falls_back_to_english():
    assert t("common.save", "fr") == "Save"


def test_t_unknown_key_returns_key_itself():
    """Fail-safe: key hilang tak boleh melempar exception / mematikan halaman."""
    assert t("this.key.does.not.exist", "en") == "this.key.does.not.exist"


def test_t_formats_placeholders():
    text = t("metrics.not_enough_data", "en", n="5")
    assert "5" in text and "{n}" not in text


def test_t_bad_format_kwargs_falls_back_to_unformatted_text():
    """kwargs salah/kurang → jangan crash, kembalikan teks apa adanya."""
    text = t("metrics.not_enough_data", "en")  # tanpa kwarg n
    assert "{n}" in text


def test_translator_closure_binds_locale():
    _t = translator("id")
    assert _t("common.save") == "Simpan"
    _t_en = translator("en")
    assert _t_en("common.save") == "Save"


def test_translator_unknown_locale_normalizes_to_default():
    _t = translator("klingon")
    assert _t("common.save") == "Save"


def test_every_string_has_both_locales():
    """Setiap entri STRINGS wajib punya en & id — cegah UI mendadak kosong saat toggle."""
    missing = [k for k, v in STRINGS.items() if not v.get("en") or not v.get("id")]
    assert missing == []


# ── SettingsStore.get/set_ui_locale ──────────────────────────────────────────


@pytest.fixture
async def db():
    manager = DatabaseManager(AppConfig(db_path=":memory:"))
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await manager.conn()
    await conn.executescript(sql)
    await conn.commit()
    yield manager
    await manager.close()


async def test_ui_locale_default_english(db):
    store = SettingsStore(db)
    assert await store.get_ui_locale() == "en"


async def test_ui_locale_roundtrip(db):
    store = SettingsStore(db)
    await store.set_ui_locale("id")
    assert await store.get_ui_locale() == "id"
    await store.set_ui_locale("en")
    assert await store.get_ui_locale() == "en"


async def test_ui_locale_invalid_falls_back_to_english(db):
    store = SettingsStore(db)
    await store.set_ui_locale("bogus")
    assert await store.get_ui_locale() == "en"


async def test_ui_locale_none_resets_to_english(db):
    store = SettingsStore(db)
    await store.set_ui_locale("id")
    await store.set_ui_locale(None)
    assert await store.get_ui_locale() == "en"
