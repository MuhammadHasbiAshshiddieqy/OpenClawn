"""Tamper-evident audit trail — hash chaining append-only (TODO.md § Prioritas 9.1).

MENGAPA ADA: README menjual pilar "Immutable Audit Evidence", tapi sebelum modul
ini klaim itu belum didukung implementasi — `routing_events`/`approval_log` hanya
baris SQLite biasa, siapa pun dengan akses file DB bisa mengubah/menghapus riwayat
tanpa jejak. EU AI Act Article 12 (enforceable 2026-08-02) menuntut logging
otomatis yang cukup untuk traceability penuh; hash chaining adalah mekanisme
standar yang membuat modifikasi retroaktif TERDETEKSI (bukan tercegah — lihat
"Batas jaminan" di bawah).

KENAPA TABEL TERPISAH, BUKAN KOLOM HASH DI TABEL AUDIT YANG ADA:
`routing_events` DIMUTASI berkali-kali setelah INSERT — `finalize()` menulis
token/biaya/latency, `check_correction()` menulis had_correction di turn
BERIKUTNYA, `set_human_feedback()` menulis rating kapan saja setelahnya. Hash
in-place akan langsung invalid tiap mutasi, jadi rantai tak akan pernah verify.
Append-only chain menghindari masalah itu sepenuhnya: mutasi apa pun MENAMBAH
entry baru, tak pernah mengubah entry lama. Efek samping yang justru berguna
untuk audit — urutan "diputuskan → diselesaikan → dikoreksi" jadi terlihat
sebagai sejarah, bukan satu baris yang ditimpa.

BATAS JAMINAN (jujur, jangan diklaim lebih):
Hash chain membuat perubahan retroaktif TERDETEKSI, bukan MUSTAHIL. Penyerang
dengan akses tulis ke DB bisa menulis ulang SELURUH rantai dari titik yang
diubah sampai ujung (semua hash setelahnya ikut dihitung ulang). Yang mencegah
itu adalah menyalin `record_hash` terakhir ke luar sistem secara berkala
(anchoring) — mis. dicatat di sistem lain/paper trail. Modul ini menyediakan
`head()` untuk keperluan itu; kebijakan anchoring-nya di luar scope kode.
Tanda tangan kriptografis per-entry (ECDSA, mis. pola IETF
draft-sharif-agent-audit-trail) juga di luar scope saat ini — butuh manajemen
kunci yang belum ada di proyek ini.
"""

import hashlib
import json
from datetime import UTC, datetime

from infra.database import DatabaseManager
from infra.logging import log

# Entry type yang dicatat. Sengaja SELEKTIF — hanya titik keputusan yang relevan
# untuk traceability compliance (apa yang diputuskan, hasilnya, setiap titik
# pengawasan manusia, DAN sinyal bahwa suatu tindakan ternyata salah), bukan
# setiap perubahan kolom. Bisa ditambah nanti tanpa memecah rantai lama
# (entry_type baru = entry baru, bukan format baru).
#
# `had_correction`/`human_feedback` DIRANTAI (§ Prioritas 9.1 follow-up (a),
# direvisi dari keputusan awal "tak dirantai — itu sinyal kalibrasi, bukan
# bukti tindakan agent"): keduanya BUKAN cuma sinyal kalibrasi router — mereka
# BUKTI bahwa satu tindakan agent ternyata bermasalah menurut user, yang
# relevan-langsung untuk traceability EU AI Act Article 12 ("situasi yang
# mungkin menghadirkan risiko"). Tanpa dirantai, siapa pun dengan akses tulis
# DB bisa diam-diam menghapus jejak bahwa user pernah mengoreksi/memberi
# rating buruk pada satu tindakan — persis kelas manipulasi yang seharusnya
# ditutup fitur ini, bukan dikecualikan darinya.
ENTRY_ROUTING_DECISION = "routing.decision"  # SEBELUM LLM dipanggil
ENTRY_ROUTING_FINALIZED = "routing.finalized"  # SESUDAH turn selesai
ENTRY_APPROVAL_REQUESTED = "approval.requested"  # checkpoint manusia dibuka
ENTRY_APPROVAL_DECIDED = "approval.decided"  # manusia memutuskan / timeout
ENTRY_APPROVAL_AUTO = "approval.auto"  # trust mode MELEWATI klik manusia
ENTRY_ROUTING_CORRECTED = "routing.corrected"  # user mengoreksi turn sebelumnya (sinyal implisit)
ENTRY_HUMAN_FEEDBACK = "routing.human_feedback"  # rating eksplisit 1-5 user untuk satu turn

# Hash entry pertama merantai ke string kosong — penanda awal rantai yang
# eksplisit, bukan NULL (NULL menyulitkan verifikasi: "belum diisi" vs "awal").
GENESIS_PREV_HASH = ""


def _canonical_body(
    entry_type: str, ref_table: str, ref_id: int | None, payload_json: str, created_at: str
) -> str:
    """Representasi kanonik satu entry (TANPA prev_hash) — input hash.

    Dibangun sebagai JSON dengan `sort_keys` + separator rapat supaya
    deterministik & tak ambigu. TIDAK memakai penggabungan string berdelimiter
    (mis. `a|b|c`) karena nilai yang MENGANDUNG delimiter akan membuat dua entry
    berbeda menghasilkan body identik — celah tabrakan yang bisa dieksploitasi.

    `payload_json` disisipkan sebagai STRING (bukan objek nested) agar body bisa
    direkonstruksi PERSIS dari kolom yang tersimpan saat verifikasi — re-serialize
    dict hasil parse bisa berbeda byte-nya (urutan kunci, format float).

    ASUMSI: ini bukan RFC 8785 (JCS) penuh. Cukup untuk verifikasi oleh
    implementasi ini sendiri (satu-satunya konsumen saat ini). Bila suatu saat
    sistem LAIN perlu memverifikasi rantai kita, JCS penuh jadi relevan —
    terutama untuk float, yang aturan serialisasinya beda antar bahasa.
    """
    return json.dumps(
        {
            "created_at": created_at,
            "entry_type": entry_type,
            "payload_json": payload_json,
            "ref_id": ref_id,
            "ref_table": ref_table,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_payload(payload: dict) -> str:
    """Serialisasi payload jadi JSON kanonik — dipakai sebelum `append`."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(body: str, prev_hash: str) -> str:
    """SHA-256 dari body + hash entry sebelumnya. Satu-satunya definisi hash —
    dipakai saat menulis (via fungsi SQLite `SHA256`) maupun saat verifikasi,
    supaya keduanya tak bisa divergen diam-diam."""
    return hashlib.sha256((body + prev_hash).encode("utf-8")).hexdigest()


class AuditChain:
    """Rantai audit append-only. Satu instance per pemanggil (murah — tanpa state).

    Penulisan ATOMIK dalam SATU statement SQL: `prev_hash` dibaca lewat subquery
    di dalam INSERT yang sama, jadi dua turn bersamaan tak bisa membaca head yang
    sama lalu menulis rantai bercabang (yang akan tampak sebagai "rantai rusak"
    padahal tak ada yang mengubah apa pun — alarm palsu yang justru merusak
    kepercayaan pada mekanisme ini). Hash dihitung fungsi SQLite `SHA256` yang
    didaftarkan `DatabaseManager` — pola sama `POWER()` untuk skill decay.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def append(
        self,
        entry_type: str,
        payload: dict,
        ref_table: str = "",
        ref_id: int | None = None,
    ) -> str | None:
        """Tambah satu entry ke ujung rantai. Return `record_hash`, atau None bila gagal.

        FAIL-SOFT (keputusan sadar): kegagalan menulis rantai TIDAK menjatuhkan
        turn user — di-log sebagai error. Alasannya: entry yang hilang tetap
        TERDETEKSI oleh `verify()` sebagai lompatan hash, jadi kegagalan tak
        pernah senyap; sedangkan menggagalkan turn karena masalah audit akan
        membuat sistem berhenti melayani justru saat DB sedang bermasalah.
        Konsisten pola fail-soft audit lain di proyek ini (`queue_proposal`,
        `_log_attempt`).
        """
        created_at = datetime.now(UTC).isoformat()
        payload_json = canonical_payload(payload)
        body = _canonical_body(entry_type, ref_table, ref_id, payload_json, created_at)
        try:
            cursor = await self.db.execute(
                """
                INSERT INTO audit_chain
                    (entry_type, ref_table, ref_id, payload_json, created_at,
                     prev_hash, record_hash)
                SELECT ?, ?, ?, ?, ?,
                       COALESCE((SELECT record_hash FROM audit_chain
                                 ORDER BY id DESC LIMIT 1), ?),
                       SHA256(? || COALESCE((SELECT record_hash FROM audit_chain
                                             ORDER BY id DESC LIMIT 1), ?))
                """,
                (
                    entry_type,
                    ref_table,
                    ref_id,
                    payload_json,
                    created_at,
                    GENESIS_PREV_HASH,
                    body,
                    GENESIS_PREV_HASH,
                ),
            )
            row = await self.db.fetchone(
                "SELECT record_hash FROM audit_chain WHERE id=?", (cursor.lastrowid,)
            )
            return row["record_hash"] if row else None
        except Exception as e:  # noqa: BLE001 — audit tak boleh menjatuhkan turn
            log.error(
                "audit_chain_append_failed",
                entry_type=entry_type,
                ref_table=ref_table,
                ref_id=ref_id,
                error=str(e),
            )
            return None

    async def head(self) -> dict | None:
        """Entry terakhir (id + record_hash + created_at) — untuk anchoring ke luar
        sistem. Menyalin nilai ini secara berkala ke tempat lain adalah yang membuat
        penulisan-ulang seluruh rantai ikut ketahuan (lihat "Batas jaminan" modul)."""
        return await self.db.fetchone(
            "SELECT id, record_hash, created_at FROM audit_chain ORDER BY id DESC LIMIT 1"
        )

    async def verify(self) -> dict:
        """Verifikasi integritas seluruh rantai.

        Mengecek DUA hal per entry: (1) `prev_hash` cocok `record_hash` entry
        sebelumnya (rantai tak terputus/tersisip), dan (2) `record_hash` benar
        hasil hash isi entry itu sendiri (isi tak diubah). Mengecek hanya salah
        satu tak cukup — (1) saja lolos bila isi diubah tanpa menyentuh hash,
        (2) saja lolos bila satu entry di tengah dihapus.

        Return `{"ok", "checked", "broken_at", "reason"}`. `broken_at` = `id`
        entry pertama yang bermasalah (None bila utuh) — bukan cuma boolean,
        supaya operator tahu SEJAK KAPAN riwayat tak bisa dipercaya.
        """
        rows = await self.db.fetchall(
            """SELECT id, entry_type, ref_table, ref_id, payload_json,
                      created_at, prev_hash, record_hash
               FROM audit_chain ORDER BY id ASC"""
        )
        expected_prev = GENESIS_PREV_HASH
        for row in rows:
            if row["prev_hash"] != expected_prev:
                return {
                    "ok": False,
                    "checked": len(rows),
                    "broken_at": row["id"],
                    "reason": "prev_hash tidak cocok dengan entry sebelumnya "
                    "(entry disisipkan/dihapus, atau urutan diubah)",
                }
            body = _canonical_body(
                row["entry_type"],
                row["ref_table"] or "",
                row["ref_id"],
                row["payload_json"],
                row["created_at"],
            )
            if compute_hash(body, row["prev_hash"]) != row["record_hash"]:
                return {
                    "ok": False,
                    "checked": len(rows),
                    "broken_at": row["id"],
                    "reason": "record_hash tidak cocok dengan isi entry (isi diubah setelah ditulis)",
                }
            expected_prev = row["record_hash"]
        return {"ok": True, "checked": len(rows), "broken_at": None, "reason": ""}
