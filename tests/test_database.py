"""Test untuk DatabaseManager, khususnya auto-tambal kolom di tabel lama.

Regresi nyata: `data/openclawn.db` yang dibuat sebelum kolom I1 (curation_log.status,
curation_log.merged_content, skills.merged_into/version) ditambahkan ke
migrations/001_initial.sql tidak pernah dapat kolom itu — `CREATE TABLE IF NOT
EXISTS` adalah no-op pada tabel existing → "no such column" saat /skills diakses.
"""

from infra.config import AppConfig
from infra.database import DatabaseManager


async def _old_schema_db(tmp_path):
    """DB dengan skema LAMA (sebelum kolom I1 ditambahkan) — meniru DB nyata yang stale."""
    db_path = tmp_path / "old.db"
    manager = DatabaseManager(AppConfig(db_path=str(db_path)))
    conn = await manager.conn()
    await conn.executescript(
        """
        CREATE TABLE skills (
            id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            skill_content TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            confidence REAL DEFAULT 0.0,
            use_count INTEGER DEFAULT 0,
            decay_score REAL DEFAULT 1.0
        );
        CREATE TABLE curation_log (
            id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            action TEXT NOT NULL,
            winner_id INTEGER,
            loser_ids TEXT,
            similarity REAL,
            judge_confidence INTEGER,
            reasoning TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    await conn.execute(
        "INSERT INTO skills (id, role, skill_name, skill_content) VALUES (1,'dev','x','isi')"
    )
    await conn.commit()
    return manager


async def test_ensure_columns_patches_missing_curation_log_columns(tmp_path):
    """Regresi: curation_log.status hilang di DB lama → query /skills gagal sebelum fix."""
    manager = await _old_schema_db(tmp_path)
    await manager.run_migration("migrations/001_initial.sql")

    row = await manager.fetchone(
        "SELECT status, merged_content FROM curation_log WHERE 1=0 UNION ALL "
        "SELECT status, merged_content FROM curation_log LIMIT 1"
    )
    # Tabel kosong tapi query tak lagi melempar "no such column".
    assert row is None
    await manager.close()


async def test_ensure_columns_patches_missing_skills_columns(tmp_path):
    """Regresi: skills.merged_into/version hilang di DB lama."""
    manager = await _old_schema_db(tmp_path)
    await manager.run_migration("migrations/001_initial.sql")

    row = await manager.fetchone(
        "SELECT merged_into, version, draft_success_count FROM skills WHERE id=1"
    )
    assert row is not None
    assert row["merged_into"] is None
    assert row["version"] == 1  # default diterapkan pada baris existing
    assert row["draft_success_count"] == 0
    await manager.close()


async def test_ensure_columns_preserves_existing_data(tmp_path):
    """Tambal kolom TIDAK boleh menghapus/mengubah data lama."""
    manager = await _old_schema_db(tmp_path)
    await manager.run_migration("migrations/001_initial.sql")

    row = await manager.fetchone("SELECT skill_name, skill_content FROM skills WHERE id=1")
    assert row["skill_name"] == "x"
    assert row["skill_content"] == "isi"
    await manager.close()


async def test_ensure_columns_idempotent_on_second_run(tmp_path):
    """Jalan dua kali (mis. restart server berulang) tidak boleh error kolom duplikat."""
    manager = await _old_schema_db(tmp_path)
    await manager.run_migration("migrations/001_initial.sql")
    await manager.run_migration("migrations/001_initial.sql")  # kedua kali — no-op aman

    row = await manager.fetchone("SELECT version FROM skills WHERE id=1")
    assert row["version"] == 1
    await manager.close()


async def test_ensure_columns_noop_on_fresh_db(tmp_path):
    """DB baru (skema sudah lengkap dari CREATE TABLE) — _ensure_columns tak berefek."""
    db_path = tmp_path / "fresh.db"
    manager = DatabaseManager(AppConfig(db_path=str(db_path)))
    await manager.run_migration("migrations/001_initial.sql")

    row = await manager.fetchone(
        "SELECT status, merged_content FROM curation_log WHERE 1=0 UNION ALL "
        "SELECT status, merged_content FROM curation_log LIMIT 1"
    )
    assert row is None  # tak error, tabel memang kosong
    await manager.close()
