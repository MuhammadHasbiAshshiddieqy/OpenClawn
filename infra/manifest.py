"""clawn.yaml — manifest deklaratif tim/role DI ATAS soul.toml (TODO.md §
Prioritas 3). Menjawab "bukti runtime, bukan library" (TREND.md): operator
menulis satu file `[team.<role>.policy]` alih-alih menyunting `[policy.*]`
manual di tiap `soul.toml`.

Desain (§ keputusan eksplisit owner, lihat riwayat diskusi):
- PARSING pakai PyYAML (dependency baru disetujui — writer YAML lebih matang
  untuk kasus ini daripada menulis serializer TOML generik sendiri).
- PENULISAN ke soul.toml TETAP text-based section replace, BUKAN tulis-ulang
  seluruh file dari dict. `soul.toml` punya `system_prompt` multi-baris
  kompleks yang harus tetap byte-identik — hanya blok `[policy.*]` yang
  dicari & diganti, section lain (termasuk system_prompt) tidak disentuh.
- Hanya `policy` yang diproses saat ini (`model`/`approval` di skema PDF asli
  dicatat di manifest tapi belum di-generate — lihat TODO.md untuk scope
  lanjutan). Role yang tidak disebut di `team:` sama sekali → soul.toml-nya
  TIDAK disentuh (opt-in per-role, bukan all-or-nothing).
"""

import re
import json
import os
import tomllib
from pathlib import Path

import yaml


class ManifestError(Exception):
    """Kegagalan memuat/menerapkan clawn.yaml — pesan jelas ke operator,
    BUKAN traceback generik. Mencakup: file tak ada, YAML tak valid, skema
    tak sesuai (tanpa key 'team'), atau role yang disebut manifest tapi
    soul.toml-nya tak ada."""


# Audit 2026-09-26: nama tool & key kondisi disisipkan sebagai bare key TOML —
# harus identifier aman. Sebelumnya disisipkan mentah: key berisi `]`/newline bisa
# menyuntik section lain (mis. menimpa `[tools] allowed`).
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_POLICY_SECTION_RE = re.compile(r"^\[policy\.[^\]]+\]\n(?:(?!^\[).*\n?)*", re.MULTILINE)


def load_manifest(manifest_path: str) -> dict:
    """Baca & validasi clawn.yaml. Raise ManifestError untuk file tak ada,
    YAML tak valid, atau skema tanpa key 'team' di root."""
    path = Path(manifest_path)
    if not path.exists():
        raise ManifestError(f"Manifest tidak ditemukan: {manifest_path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ManifestError(f"YAML tidak valid di {manifest_path}: {exc}") from exc
    if not isinstance(data, dict) or "team" not in data:
        raise ManifestError(f"Manifest {manifest_path} harus punya key root 'team'")
    return data


def _toml_value(value) -> str:
    """Render satu nilai Python jadi literal TOML. Angka TANPA quote, string
    DENGAN quote — beda ini penting (mis. deny_if timeout > 300 harus banding
    angka, bukan string "300")."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    # Audit 2026-09-26: escape JSON (\n, \t, \", \\, \uXXXX) valid sebagai basic
    # string TOML — sebelumnya hanya \ dan " yang di-escape, newline/karakter
    # kontrol menghasilkan soul.toml INVALID (role tak bisa dimuat sama sekali).
    return json.dumps(str(value), ensure_ascii=False)


def _bare_key(key) -> str:
    key = str(key)
    if not _BARE_KEY_RE.match(key):
        raise ManifestError(f"Nama tidak valid di manifest (hanya huruf/angka/_/-): {key!r}")
    return key


def _condition_toml(cond: dict) -> str:
    parts = [f"{_bare_key(k)} = {_toml_value(v)}" for k, v in cond.items()]
    return "{ " + ", ".join(parts) + " }"


def generate_policy_toml_block(policy: dict) -> str:
    """Render dict `{tool_name: {deny_if: [...], approval_required_if: [...]}}`
    jadi blok TOML `[policy.<tool_name>]` siap disisipkan ke soul.toml.
    Dict kosong → string kosong (tidak ada apa-apa untuk ditulis)."""
    if not policy:
        return ""
    blocks = []
    for tool_name, rules in policy.items():
        lines = [f"[policy.{_bare_key(tool_name)}]"]
        for key in ("deny_if", "approval_required_if"):
            conditions = rules.get(key)
            if not conditions:
                continue
            rendered = ", ".join(_condition_toml(c) for c in conditions)
            lines.append(f"{key} = [{rendered}]")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _replace_policy_sections(soul_text: str, new_policy_block: str) -> str:
    """Hapus SEMUA blok [policy.*] existing dari teks soul.toml, lalu sisipkan
    blok baru di akhir file. Section lain (termasuk system_prompt multi-baris)
    tidak disentuh sama sekali — hanya baris yang match pola [policy.xxx]
    beserta isinya sampai section berikutnya yang dihapus."""
    cleaned = _POLICY_SECTION_RE.sub("", soul_text).rstrip("\n") + "\n"
    if not new_policy_block:
        return cleaned
    return cleaned + "\n" + new_policy_block


def apply_manifest(manifest_path: str, roles_dir: str = "roles") -> list[str]:
    """Terapkan clawn.yaml ke soul.toml tiap role yang disebut di `team:`.

    Return list role yang benar-benar diubah. Role tanpa key 'policy' di
    manifest-nya di-skip (no-op untuk role itu, bukan menghapus policy yang
    mungkin sudah ada dari sumber lain). Role yang disebut manifest tapi
    soul.toml-nya tidak ada di `roles_dir` → ManifestError (jangan diam-diam
    membuat file baru — itu keputusan operator, bukan default tersirat).
    """
    from roles.registry import available_roles

    manifest = load_manifest(manifest_path)
    known = available_roles(roles_dir)
    # Audit 2026-09-26: render & validasi SEMUA role dulu, baru tulis — satu role
    # yang salah tak boleh meninggalkan sebagian soul.toml sudah berubah.
    pending: list[tuple[str, Path, str]] = []
    for role, cfg in manifest["team"].items():
        if not isinstance(cfg, dict) or "policy" not in cfg:
            continue
        if role not in known:
            raise ManifestError(
                f"Role '{role}' ada di manifest tapi soul.toml tidak ditemukan di {roles_dir}"
            )
        soul_path = Path(roles_dir) / role / "soul.toml"
        policy_block = generate_policy_toml_block(cfg["policy"])
        updated = _replace_policy_sections(soul_path.read_text(), policy_block)
        try:
            tomllib.loads(updated)  # fail loud SEBELUM menulis, bukan saat agent dimuat
        except tomllib.TOMLDecodeError as exc:
            raise ManifestError(f"Hasil soul.toml role '{role}' tidak valid: {exc}") from exc
        pending.append((role, soul_path, updated))

    for _, soul_path, updated in pending:
        # Tulis atomik: file sementara di folder yang sama lalu os.replace —
        # crash di tengah tak meninggalkan soul.toml setengah tertulis.
        tmp = soul_path.with_suffix(".toml.tmp")
        tmp.write_text(updated)
        os.replace(tmp, soul_path)

    return [role for role, _, _ in pending]
