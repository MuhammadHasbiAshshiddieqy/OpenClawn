"""Skill packs — ekspor & impor skill antar-instalasi OpenCLAWN.

Terinspirasi sistem skill + `skills-lock.json` Multica: skill dapat dibagikan sebagai
berkas Markdown portabel, dengan integritas dijaga hash (mirip package-lock).

Berbeda dari Multica yang skill-nya statis & human-written: skill OpenCLAWN bisa
lahir dari crystallization (dievaluasi). Modul ini menambah jalur BERBAGI: ekspor
skill aktif → Markdown, dan impor skill orang lain → DB.

Keamanan (CLAUDE.md §1) — impor = teks eksternal masuk ke ranah agent, jadi BERLAPIS:
  1. URL  → `_ssrf_guard` (tolak host internal) + scheme http(s) saja.
  2. Isi  → `Shield.scan_input` (tolak pola prompt-injection).
  3. Scanner → `scan_skill` (AST + pola): risiko tinggi (exec/eval/subprocess/
     eksfiltrasi) DITOLAK; risiko sedang masuk draft dengan label. SELALU aktif.
  4. Status → DRAFT (skill impor TIDAK auto-masuk context; user aktifkan manual).
  5. Integritas → SHA-256 tiap skill dicatat di `skills-lock.json`.

Extractable: hanya bergantung `DatabaseManager` + stdlib + (opsional) httpx untuk URL.
"""

import hashlib
import json
import re
from datetime import datetime

import httpx

from infra.config import CONFIG, AppConfig
from infra.database import DatabaseManager
from infra.logging import log
from security.shield import Shield
from security.skill_scanner import scan_skill
from tools.web import _ssrf_guard

# Penanda batas antar-skill dalam satu pack Markdown.
SKILL_DELIMITER = "\n---\n"
# Header frontmatter sederhana (key: value) di awal tiap skill.
_FRONTMATTER_RE = re.compile(r"^([a-z_]+):\s*(.*)$")
LOCKFILE_NAME = "skills-lock.json"
MAX_IMPORT_BYTES = 256_000  # batas ukuran pack agar tak membanjiri (token/DoS-ringan)


def _skill_hash(name: str, content: str) -> str:
    """SHA-256 dari nama+konten skill — integritas (selaras computedHash Multica)."""
    return hashlib.sha256(f"{name}\n{content}".encode()).hexdigest()


def _render_skill(row: dict) -> str:
    """Satu skill → blok Markdown berfrontmatter ringkas + konten."""
    name = row["skill_name"]
    content = row["skill_content"] or ""
    lines = [
        f"name: {name}",
        f"role: {row.get('role', '')}",
        f"trigger_pattern: {row.get('trigger_pattern') or ''}",
        f"generator_model: {row.get('generator_model') or ''}",
        f"confidence: {row.get('confidence') or 0.0}",
        f"hash: {_skill_hash(name, content)}",
        "",
        content.strip(),
    ]
    return "\n".join(lines)


def _parse_pack(text: str) -> list[dict]:
    """Pisah pack Markdown → list skill {name, role, trigger_pattern, ..., content, hash}.

    Toleran: blok tanpa `name:` di-skip. Tidak pernah raise (parsing input eksternal).
    """
    skills: list[dict] = []
    for block in text.split(SKILL_DELIMITER):
        block = block.strip()
        if not block:
            continue
        meta: dict = {}
        body_lines: list[str] = []
        in_body = False
        for line in block.splitlines():
            if not in_body:
                m = _FRONTMATTER_RE.match(line.strip())
                if m:
                    meta[m.group(1)] = m.group(2).strip()
                    continue
                # baris kosong/non-frontmatter pertama → mulai body
                if line.strip() == "" and meta:
                    in_body = True
                    continue
                if not meta:
                    # blok tanpa frontmatter valid → abaikan
                    break
                in_body = True
            body_lines.append(line)
        name = meta.get("name", "").strip()
        if not name:
            continue
        skills.append(
            {
                "name": name,
                "role": meta.get("role", ""),
                "trigger_pattern": meta.get("trigger_pattern", "") or None,
                "generator_model": meta.get("generator_model", "") or None,
                "hash": meta.get("hash", ""),
                "content": "\n".join(body_lines).strip(),
            }
        )
    return skills


class SkillPack:
    """Ekspor/impor skill antar-instalasi. DB-bound; impor berlapis-keamanan."""

    def __init__(self, db: DatabaseManager, config: AppConfig = CONFIG):
        self.db = db
        self.config = config

    async def export_skills(self, role: str | None = None) -> str:
        """Render skill aktif (opsional per role) → satu pack Markdown.

        Hanya skill `active` diekspor (draft/archived tak layak dibagi). Urut nama.
        """
        if role:
            rows = await self.db.fetchall(
                """SELECT role, skill_name, trigger_pattern, skill_content,
                          generator_model, confidence
                   FROM skills WHERE status='active' AND role=? ORDER BY skill_name""",
                (role,),
            )
        else:
            rows = await self.db.fetchall(
                """SELECT role, skill_name, trigger_pattern, skill_content,
                          generator_model, confidence
                   FROM skills WHERE status='active' ORDER BY role, skill_name"""
            )
        if not rows:
            return ""
        return SKILL_DELIMITER.join(_render_skill(r) for r in rows)

    async def import_pack(self, text: str, target_role: str | None = None) -> dict:
        """Impor pack Markdown → DB sebagai skill `draft` (berlapis-keamanan §1).

        Mengembalikan ringkasan {imported, skipped, reasons}. Tidak pernah crash:
        tiap skill divalidasi sendiri; kegagalan satu tak menjatuhkan yang lain.
        target_role override role tiap skill (mis. impor ke role tertentu).
        """
        if len(text.encode("utf-8", "ignore")) > MAX_IMPORT_BYTES:
            return {"imported": 0, "skipped": 0, "error": "pack terlalu besar"}

        parsed = _parse_pack(text)
        imported = 0
        flagged: list[dict] = []  # diimpor TAPI scanner menandai risiko sedang
        skipped: list[dict] = []
        for sk in parsed:
            name = sk["name"]
            content = sk["content"]
            role = target_role or sk["role"]
            if not role or not content:
                skipped.append({"name": name, "reason": "role/konten kosong"})
                continue
            # Lapis 2: Shield scan konten (pola prompt-injection).
            safe, reason = Shield.scan_input(content)
            if not safe:
                skipped.append({"name": name, "reason": f"ditolak shield: {reason}"})
                log.warning("skill_import_blocked", skill=name, reason=reason)
                continue
            # Lapis 3: scanner skill (AST + pola) — risiko tinggi DITOLAK total (§1,
            # keputusan owner). Risiko sedang tetap impor tapi diberi label di temuan.
            scan = scan_skill(name, content)
            if scan.blocked:
                skipped.append(
                    {
                        "name": name,
                        "reason": f"ditolak scanner (skor {scan.score}): {scan.findings}",
                    }
                )
                log.warning(
                    "skill_import_unsafe", skill=name, score=scan.score, findings=scan.findings
                )
                continue
            if scan.verdict == "flag":
                flagged.append({"name": name, "score": scan.score, "findings": scan.findings})
                log.info(
                    "skill_import_flagged", skill=name, score=scan.score, findings=scan.findings
                )
            # Lapis 5: verifikasi hash bila pack menyertakannya (integritas).
            expected = sk.get("hash")
            actual = _skill_hash(name, content)
            if expected and expected != actual:
                skipped.append({"name": name, "reason": "hash tidak cocok"})
                continue
            # Lapis 3: impor sebagai DRAFT — tak auto-masuk context (get_active_skills
            # hanya ambil status='active'). User meninjau & mengaktifkan manual.
            try:
                await self.db.execute(
                    """INSERT INTO skills (role, skill_name, trigger_pattern, skill_content,
                                           visibility, status, confidence, generator_model,
                                           decay_score)
                       VALUES (?,?,?,?, 'inherited', 'draft', ?, ?, 1.0)
                       ON CONFLICT(role, skill_name) DO NOTHING""",
                    (
                        role,
                        name,
                        sk["trigger_pattern"],
                        content,
                        0.0,
                        sk["generator_model"],
                    ),
                )
                imported += 1
                await self._record_lock(name, actual)
            except Exception as e:  # noqa: BLE001 — satu skill gagal jangan jatuhkan impor
                skipped.append({"name": name, "reason": str(e)})
                log.error("skill_import_failed", skill=name, error=str(e))
        return {
            "imported": imported,
            "skipped": len(skipped),
            "reasons": skipped,
            "flagged": flagged,
        }

    async def import_url(self, url: str, target_role: str | None = None) -> dict:
        """Impor pack dari URL publik. Lapis 1: SSRF guard + scheme http(s)."""
        if not url.startswith(("http://", "https://")):
            return {"imported": 0, "skipped": 0, "error": "url harus http:// atau https://"}
        blocked = _ssrf_guard(url)
        if blocked:
            return {"imported": 0, "skipped": 0, "error": blocked}
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                text = resp.text[:MAX_IMPORT_BYTES]
        except httpx.HTTPError as e:
            return {"imported": 0, "skipped": 0, "error": f"fetch gagal: {e}"}
        return await self.import_pack(text, target_role)

    async def _record_lock(self, name: str, digest: str) -> None:
        """Catat hash skill impor ke skills-lock.json (integritas, mirip Multica).

        Fail-soft: lockfile adalah catatan, bukan jalur kritis. Disimpan di
        workspace_root agar bisa di-commit bersama proyek.
        """
        from infra.workspace import resolve_in_workspace

        try:
            path = resolve_in_workspace(LOCKFILE_NAME, self.config.workspace_root)
            data: dict = {"version": 1, "skills": {}}
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    data = {"version": 1, "skills": {}}
            data.setdefault("skills", {})[name] = {
                "hash": digest,
                "imported_at": datetime.now().isoformat(timespec="seconds"),
            }
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 — lockfile gagal jangan jatuhkan impor
            log.warning("skill_lock_write_failed", skill=name, error=str(e))
