"""Identitas agent sebagai first-class citizen (TODO.md § Prioritas 9.2, Non-Human Identity).

MENGAPA ADA: OpenCLAWN sudah membedakan "agent vs manusia" (`actor_is_agent`,
selalu `1`) dan "user mana yang memicu" (`owner_user_id`, § audit 2026-07-29)
— tapi keduanya flag/ID user, belum menjawab pertanyaan audit yang lebih
tajam: **agent dengan KONFIGURASI mana** yang melakukan tindakan ini?
`soul.toml` satu role bisa berubah kapan saja (tool allow-list, `[policy]`,
system prompt) — tanpa identitas per-versi, dua tindakan oleh role yang sama
tapi PERMISSION BERBEDA terlihat identik di audit trail, padahal salah
satunya mungkin dilakukan di bawah konfigurasi yang sudah tak berlaku lagi.

Melengkapi § Prioritas 9.1 (`core/audit_chain.py`): hash chaining membuktikan
LOG tak diubah; identitas di sini menjawab log itu TENTANG SIAPA. Kombinasi
keduanya menjawab "siapa melakukan apa, dan buktinya tak bisa direkayasa" —
persis pertanyaan yang diajukan riset Non-Human Identity 2026 (68% organisasi
tak bisa membedakan aktivitas agent dari manusia, CSA survey).

DESAIN: identitas = role + hash SELURUH `soul.toml` yang dimuat (bukan subset
field yang dipilih tangan) — supaya field baru yang ditambahkan ke soul.toml
di masa depan otomatis ikut tercermin di identitas tanpa perlu mengingat
memperbarui modul ini tiap kali skema soul.toml berubah. Hash BUKAN
kriptografi rahasia (tak ada kunci, tak menyembunyikan apa pun) — murni
fingerprint deterministik untuk membedakan versi konfigurasi.
"""

import hashlib
import json

# Panjang hash yang ditampilkan di identitas — cukup untuk membedakan versi
# konfigurasi dalam praktik (satu role, jumlah versi soul.toml yang pernah
# ada sepanjang umur proyek), BUKAN untuk resistensi collision kriptografis.
# Butuh itu → pakai full hex dari config_hash() langsung, jangan perpanjang
# konstanta ini (identitas yang sudah tercatat di DB lama akan tetap pendek).
IDENTITY_HASH_LEN = 12


def config_hash(soul: dict) -> str:
    """SHA-256 hex PENUH dari seluruh isi `soul.toml` yang dimuat, di-canonical-kan
    sebagai JSON `sort_keys` supaya urutan key TOML (yang tak bermakna secara
    semantik) tak memengaruhi hash. Deterministik: konten sama → hash sama, di
    proses/mesin/waktu mana pun."""
    canonical = json.dumps(soul, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def agent_identity(role: str, soul: dict) -> str:
    """Identitas stabil `"{role}@{hash_pendek}"`.

    Role yang sama dengan config yang SAMA PERSIS selalu menghasilkan
    identitas yang SAMA — lintas sesi, lintas restart server, tanpa tabel
    versioning terpisah. Config BERUBAH (tool ditambah/dicabut, `[policy]`
    diedit, system prompt diubah) otomatis menghasilkan identitas BARU —
    perubahan permission agent jadi terlihat langsung di jejak audit, bukan
    tersembunyi di balik nama role yang tak berubah.
    """
    return f"{role}@{config_hash(soul)[:IDENTITY_HASH_LEN]}"
