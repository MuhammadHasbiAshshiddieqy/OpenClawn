"""Kebijakan retensi minimum untuk tabel audit (TODO.md § Prioritas 9.1
follow-up, EU AI Act Article 12).

MENGAPA ADA: Article 12 mewajibkan retensi minimum 6 bulan untuk log sistem
AI risiko-tinggi. Per audit 2026-08-03: proyek ini TIDAK punya mekanisme
pruning/penghapusan sama sekali untuk `routing_events`, `approval_log`, atau
`audit_chain` — jadi syarat itu terpenuhi TRIVIAL hari ini (data tak pernah
dihapus). Gap sesungguhnya bukan pelanggaran yang terjadi SEKARANG, melainkan
TIDAK ADANYA PAGAR yang mencegah kode masa depan (mis. skrip pruning yang
ditambahkan nanti untuk alasan ruang disk) melanggar retensi tanpa disadari.

PENEGAKAN SUNGGUHAN ADA DI TRIGGER SQLite (`migrations/001_initial.sql`),
BUKAN di modul Python ini — sengaja. Guard di level aplikasi ("panggil
fungsi ini sebelum DELETE") bisa dilupakan; trigger database tidak bisa
dilewati oleh jalur kode mana pun, termasuk raw SQL yang ditulis maintainer
masa depan yang belum baca modul ini. Modul ini HANYA menyimpan konstanta
`MIN_RETENTION_DAYS` sebagai satu sumber kebenaran untuk dokumentasi & test
— trigger di SQL memakai literal `180` (SQL tak bisa import konstanta
Python), jadi **bila angka ini diubah, trigger di migrations/001_initial.sql
HARUS diubah manual mengikutinya** (dicatat di komentar trigger itu sendiri).

BATAS: trigger ini memblokir DELETE berdasarkan `created_at`, TIDAK
menyelesaikan tegangan dengan permintaan penghapusan GDPR (hak dihapus)
untuk PII yang mungkin ikut tersimpan di `query_text`/`tool_input`. Itu
kelas masalah berbeda (butuh redaksi konten, bukan penolakan hapus baris) —
di luar scope perubahan ini, sengaja tidak diselesaikan spekulatif di sini.
"""

MIN_RETENTION_DAYS = 180  # EU AI Act Article 12: retensi minimum 6 bulan
