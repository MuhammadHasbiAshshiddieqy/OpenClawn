"""Tests untuk tamper-evident audit trail (TODO.md § Prioritas 9.1).

Fokus test ini BUKAN "apakah entry tersimpan" (itu remeh), melainkan:
apakah rantai benar-benar MENDETEKSI manipulasi. Setiap bentuk manipulasi yang
realistis (ubah isi, hapus entry di tengah, sisipkan entry, ubah urutan) diuji
dengan menulis LANGSUNG ke DB di belakang punggung AuditChain — meniru penyerang
yang punya akses file DB, bukan yang lewat API.
"""

import pytest

from core.audit_chain import (
    ENTRY_APPROVAL_AUTO,
    ENTRY_APPROVAL_DECIDED,
    ENTRY_APPROVAL_REQUESTED,
    ENTRY_ROUTING_DECISION,
    ENTRY_ROUTING_FINALIZED,
    AuditChain,
    _canonical_body,
    canonical_payload,
    compute_hash,
)
from infra.config import AppConfig
from infra.database import DatabaseManager


@pytest.fixture
async def db():
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await manager.conn()
    await conn.executescript(sql)
    await conn.commit()
    yield manager
    await manager.close()


# ── Dasar: rantai terbentuk & verify hijau ───────────────────────────────────


async def test_empty_chain_verifies_ok(db):
    """Rantai kosong itu utuh secara trivial — bukan error."""
    result = await AuditChain(db).verify()
    assert result["ok"] is True
    assert result["checked"] == 0
    assert result["broken_at"] is None


async def test_append_links_entries_into_a_chain(db):
    """Tiap entry merantai ke hash entry sebelumnya; yang pertama ke genesis ''."""
    chain = AuditChain(db)
    await chain.append(ENTRY_ROUTING_DECISION, {"a": 1}, "routing_events", 1)
    await chain.append(ENTRY_ROUTING_FINALIZED, {"b": 2}, "routing_events", 1)
    await chain.append(ENTRY_APPROVAL_REQUESTED, {"c": 3}, "approval_log", None)

    rows = await db.fetchall("SELECT * FROM audit_chain ORDER BY id")
    assert len(rows) == 3
    assert rows[0]["prev_hash"] == ""  # genesis
    assert rows[1]["prev_hash"] == rows[0]["record_hash"]
    assert rows[2]["prev_hash"] == rows[1]["record_hash"]
    assert (await chain.verify())["ok"] is True


async def test_append_returns_record_hash(db):
    """Return value dipakai caller untuk anchoring — harus hash entry itu sendiri."""
    chain = AuditChain(db)
    returned = await chain.append(ENTRY_ROUTING_DECISION, {"a": 1})
    row = await db.fetchone("SELECT record_hash FROM audit_chain ORDER BY id DESC LIMIT 1")
    assert returned == row["record_hash"]
    assert len(returned) == 64  # SHA-256 hex


async def test_head_returns_last_entry(db):
    chain = AuditChain(db)
    assert await chain.head() is None
    await chain.append(ENTRY_ROUTING_DECISION, {"a": 1})
    last = await chain.append(ENTRY_ROUTING_FINALIZED, {"b": 2})
    head = await chain.head()
    assert head["record_hash"] == last


# ── Deteksi manipulasi (inti dari fitur ini) ─────────────────────────────────


async def test_detects_modified_payload(db):
    """Isi entry diubah langsung di DB → record_hash tak lagi cocok isinya."""
    chain = AuditChain(db)
    await chain.append(ENTRY_ROUTING_DECISION, {"model": "gemini-2.5-pro"}, "routing_events", 1)
    await chain.append(ENTRY_ROUTING_FINALIZED, {"cost_usd": 0.5}, "routing_events", 1)

    # Penyerang menyamarkan model mahal jadi model murah, tanpa menyentuh hash.
    await db.execute(
        "UPDATE audit_chain SET payload_json=? WHERE id=1",
        (canonical_payload({"model": "gemma4:e4b"}),),
    )

    result = await chain.verify()
    assert result["ok"] is False
    assert result["broken_at"] == 1
    assert "record_hash" in result["reason"]


async def test_detects_deleted_entry_in_middle(db):
    """Entry di tengah dihapus → prev_hash entry berikutnya menggantung."""
    chain = AuditChain(db)
    await chain.append(ENTRY_APPROVAL_REQUESTED, {"approval_id": "x"}, "approval_log", None)
    await chain.append(ENTRY_APPROVAL_DECIDED, {"decision": "rejected"}, "approval_log", None)
    await chain.append(ENTRY_ROUTING_DECISION, {"a": 1}, "routing_events", 2)

    # Menghapus jejak bahwa sebuah approval DITOLAK.
    await db.execute("DELETE FROM audit_chain WHERE id=2")

    result = await chain.verify()
    assert result["ok"] is False
    assert result["broken_at"] == 3
    assert "prev_hash" in result["reason"]


async def test_detects_deleted_last_entry_only_via_anchor(db):
    """BATAS JAMINAN yang harus disadari: menghapus entry TERAKHIR tidak
    memutus rantai apa pun — sisa rantai tetap konsisten sendiri.

    Ini bukan bug, ini sifat inheren hash chain: yang mendeteksi truncation
    adalah ANCHORING (membandingkan head tersimpan di luar sistem dengan head
    sekarang), bukan verify() internal. Test ini mengunci fakta itu supaya
    tak ada yang salah mengira verify() sendirian sudah cukup.
    """
    chain = AuditChain(db)
    await chain.append(ENTRY_ROUTING_DECISION, {"a": 1})
    anchored = await chain.append(ENTRY_APPROVAL_AUTO, {"tool_name": "shell_run"})

    await db.execute("DELETE FROM audit_chain WHERE id=2")

    # verify() TETAP hijau — inilah keterbatasannya.
    assert (await chain.verify())["ok"] is True
    # Yang menangkapnya: head sekarang ≠ head yang pernah di-anchor.
    assert (await chain.head())["record_hash"] != anchored


async def test_detects_reordered_entries(db):
    """Urutan ditukar → rantai putus (prev_hash tak lagi runut)."""
    chain = AuditChain(db)
    await chain.append(ENTRY_ROUTING_DECISION, {"n": 1})
    await chain.append(ENTRY_ROUTING_DECISION, {"n": 2})
    await chain.append(ENTRY_ROUTING_DECISION, {"n": 3})

    rows = await db.fetchall("SELECT * FROM audit_chain ORDER BY id")
    # Tukar payload entry 2 dan 3 (isi bertukar, hash tetap di tempat semula).
    await db.execute("UPDATE audit_chain SET payload_json=? WHERE id=2", (rows[2]["payload_json"],))
    await db.execute("UPDATE audit_chain SET payload_json=? WHERE id=3", (rows[1]["payload_json"],))

    assert (await chain.verify())["ok"] is False


async def test_detects_forged_entry_appended_without_correct_prev(db):
    """Entry disisipkan manual tanpa merantai benar → langsung ketahuan."""
    chain = AuditChain(db)
    await chain.append(ENTRY_ROUTING_DECISION, {"a": 1})

    await db.execute(
        """INSERT INTO audit_chain
           (entry_type, ref_table, ref_id, payload_json, created_at, prev_hash, record_hash)
           VALUES (?,?,?,?,?,?,?)""",
        (
            "approval.decided",
            "approval_log",
            None,
            "{}",
            "2026-01-01T00:00:00+00:00",
            "",
            "deadbeef",
        ),
    )

    result = await chain.verify()
    assert result["ok"] is False
    assert result["broken_at"] == 2


async def test_rewriting_whole_chain_is_not_detected_by_verify_alone(db):
    """BATAS JAMINAN kedua: penyerang yang menulis ULANG seluruh rantai dengan
    hash yang benar akan lolos verify(). Didokumentasikan eksplisit di
    core/audit_chain.py; test ini mengunci klaim itu agar tidak dilebih-lebihkan
    di dokumentasi/README ("terdeteksi", bukan "mustahil")."""
    chain = AuditChain(db)
    await chain.append(ENTRY_ROUTING_DECISION, {"model": "mahal"})
    original_head = (await chain.head())["record_hash"]

    # Penyerang membangun ulang rantai dari nol dengan isi palsu, hash konsisten.
    await db.execute("DELETE FROM audit_chain")
    created = "2026-01-01T00:00:00+00:00"
    payload = canonical_payload({"model": "murah"})
    body = _canonical_body(ENTRY_ROUTING_DECISION, "", None, payload, created)
    await db.execute(
        """INSERT INTO audit_chain
           (entry_type, ref_table, ref_id, payload_json, created_at, prev_hash, record_hash)
           VALUES (?,?,?,?,?,?,?)""",
        (ENTRY_ROUTING_DECISION, "", None, payload, created, "", compute_hash(body, "")),
    )

    assert (await chain.verify())["ok"] is True  # lolos — inilah batasnya
    assert (await chain.head())["record_hash"] != original_head  # anchoring menangkapnya


# ── Ketahanan & sifat hash ───────────────────────────────────────────────────


async def test_delimiter_injection_does_not_collide(db):
    """Dua entry berbeda yang nilainya mengandung karakter pemisah TIDAK boleh
    menghasilkan hash sama — alasan body dibangun sebagai JSON kanonik, bukan
    penggabungan string berdelimiter."""
    chain = AuditChain(db)
    await chain.append("a|b", {"x": "c"}, "t", 1)
    await chain.append("a", {"x": "b|c"}, "t", 1)
    rows = await db.fetchall("SELECT record_hash FROM audit_chain ORDER BY id")
    assert rows[0]["record_hash"] != rows[1]["record_hash"]
    assert (await chain.verify())["ok"] is True


async def test_append_is_fail_soft_on_db_error(db):
    """Kegagalan menulis rantai TIDAK boleh melempar ke caller (turn user harus
    tetap jalan) — cukup return None + ter-log."""
    await db.execute("DROP TABLE audit_chain")
    assert await AuditChain(db).append(ENTRY_ROUTING_DECISION, {"a": 1}) is None


async def test_unicode_payload_roundtrips(db):
    """Payload non-ASCII (query berbahasa Indonesia/CJK) harus konsisten antara
    penulisan dan verifikasi — ensure_ascii=False di kedua sisi."""
    chain = AuditChain(db)
    await chain.append(ENTRY_ROUTING_DECISION, {"query_preview": "buatkan ringkasan 日本語"})
    assert (await chain.verify())["ok"] is True
