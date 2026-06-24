"""Guardrails — rail input/output ringan, terinspirasi arsitektur NVIDIA NeMo Guardrails.

NeMo Guardrails (paket) bergantung pada LangChain + dependency berat → bertentangan
dengan prinsip proyek (CLAUDE.md §6 no-LangChain, §1.4 token-first, §8 minimal). Yang
kita adopsi adalah KONSEPNYA, bukan paketnya — sama seperti `skill_scanner` meniru
`nvidia/skillspector` tanpa mengimpornya.

Model rail (NeMo):
  - INPUT rails  : periksa pesan user SEBELUM masuk pipeline (mis. prompt-injection).
  - OUTPUT rails : periksa respons LLM SEBELUM sampai ke user/disimpan (mis. kebocoran
                   system-prompt, PII). Inilah gap terbesar OpenCLAWN sebelumnya.

Catatan kejujuran soal streaming: token di-stream real-time ke UI, jadi output rail
tak bisa "menarik kembali" token yang sudah tampil. Yang bisa dilakukan: MEMERIKSA
respons lengkap saat finalisasi, MEREDAKSI sebelum disimpan ke memori/history, dan
MEMANCARKAN peringatan. Deteksi + redaksi penyimpanan tetap bernilai (PII tak bocor
ke L1/L4; audit mencatat pelanggaran).

Murni stdlib (re, dataclasses) — extractable, tanpa dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from security.shield import Shield


class RailStage(Enum):
    INPUT = "input"
    OUTPUT = "output"


class RailAction(Enum):
    ALLOW = "allow"  # lolos tanpa perubahan
    REDACT = "redact"  # lolos, tapi teks dimodifikasi (mis. PII di-mask)
    BLOCK = "block"  # tolak total; teks diganti pesan penolakan


@dataclass
class RailResult:
    """Hasil satu rail atas sepotong teks."""

    rail: str
    action: RailAction
    text: str  # teks setelah rail (sama dengan input jika ALLOW)
    reason: str = ""
    findings: list[str] = field(default_factory=list)

    @property
    def triggered(self) -> bool:
        return self.action is not RailAction.ALLOW


@dataclass
class GuardrailOutcome:
    """Hasil agregat seluruh rail pada satu stage."""

    text: str  # teks final setelah semua rail
    blocked: bool
    block_reason: str
    results: list[RailResult] = field(default_factory=list)

    @property
    def modified(self) -> bool:
        return any(r.triggered for r in self.results)


class Rail:
    """Antarmuka rail. `check` murni (tanpa I/O) agar mudah ditest & extractable."""

    name: str = "rail"
    stage: RailStage = RailStage.INPUT

    def check(self, text: str) -> RailResult:  # pragma: no cover - abstract
        raise NotImplementedError


# ── INPUT rails ──────────────────────────────────────────────────────────────


class PromptInjectionRail(Rail):
    """Input: tolak pola prompt-injection. Membungkus `Shield` yang sudah ada (DRY)."""

    name = "prompt_injection"
    stage = RailStage.INPUT

    def check(self, text: str) -> RailResult:
        safe, reason = Shield.scan_input(text)
        if safe:
            return RailResult(self.name, RailAction.ALLOW, text)
        return RailResult(self.name, RailAction.BLOCK, text, reason=reason, findings=["injection"])


# ── OUTPUT rails ─────────────────────────────────────────────────────────────

# Penanda kebocoran instruksi sistem dalam respons. Konservatif: hanya frasa yang
# kuat menyiratkan model membocorkan prompt/peran internalnya.
_LEAK_PATTERNS = [
    r"you are a .{0,40} agent\.\s*your role",
    r"system prompt:",
    r"my instructions are",
    r"my system prompt",
    r"sebagai (agent|asisten) .{0,30}, peran saya",
]

# PII umum. Konservatif untuk hindari false-positive berlebih; redaksi, bukan blokir.
_PII_PATTERNS: list[tuple[str, str]] = [
    ("email", r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    # Kartu kredit 13-16 digit (boleh berspasi/strip). Luhr tidak dicek (heuristik).
    ("credit_card", r"\b(?:\d[ -]*?){13,16}\b"),
    # Kunci API umum (sk-..., ghp_..., AIza..., AKIA...).
    (
        "api_key",
        r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_\-]{20,}|AKIA[A-Z0-9]{16})\b",
    ),
]


class PromptLeakRail(Rail):
    """Output: blokir respons yang membocorkan system-prompt / peran internal."""

    name = "prompt_leak"
    stage = RailStage.OUTPUT

    def check(self, text: str) -> RailResult:
        for pat in _LEAK_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                return RailResult(
                    self.name,
                    RailAction.BLOCK,
                    text,
                    reason="Respons diblokir: terindikasi membocorkan instruksi sistem.",
                    findings=["system_prompt_leak"],
                )
        return RailResult(self.name, RailAction.ALLOW, text)


class PIIRail(Rail):
    """Output: redaksi PII (email, kartu kredit, kunci API) sebelum tampil/disimpan."""

    name = "pii"
    stage = RailStage.OUTPUT
    MASK = "[REDACTED]"

    def check(self, text: str) -> RailResult:
        findings: list[str] = []
        redacted = text
        for label, pat in _PII_PATTERNS:
            new = re.sub(pat, self.MASK, redacted)
            if new != redacted:
                findings.append(label)
                redacted = new
        if not findings:
            return RailResult(self.name, RailAction.ALLOW, text)
        return RailResult(
            self.name,
            RailAction.REDACT,
            redacted,
            reason=f"PII diredaksi: {', '.join(findings)}",
            findings=findings,
        )


# Registry rail bawaan, dipakai engine & config (nama → kelas).
BUILTIN_RAILS: dict[str, type[Rail]] = {
    PromptInjectionRail.name: PromptInjectionRail,
    PromptLeakRail.name: PromptLeakRail,
    PIIRail.name: PIIRail,
}

# Default aktif per stage bila tak ada config (semua nyala — keamanan dulu, §1).
DEFAULT_ENABLED: dict[str, bool] = {name: True for name in BUILTIN_RAILS}

# Pesan pengganti saat output diblokir total (jangan tampilkan teks asli ke user).
BLOCKED_OUTPUT_MESSAGE = "[Respons ditahan oleh guardrail keamanan.]"


class GuardrailEngine:
    """Menjalankan rail terurut pada satu stage. Murni & sinkron — tanpa I/O/LLM.

    enabled: peta nama_rail → bool. Rail yang dinonaktifkan dilewati. Bila None,
    pakai DEFAULT_ENABLED (semua aktif).
    """

    def __init__(self, enabled: dict[str, bool] | None = None):
        self.enabled = enabled if enabled is not None else dict(DEFAULT_ENABLED)
        self._rails: list[Rail] = [cls() for cls in BUILTIN_RAILS.values()]

    def _active(self, stage: RailStage) -> list[Rail]:
        return [r for r in self._rails if r.stage is stage and self.enabled.get(r.name, True)]

    def run(self, stage: RailStage, text: str) -> GuardrailOutcome:
        """Jalankan semua rail aktif untuk stage. BLOCK menghentikan rantai."""
        current = text
        results: list[RailResult] = []
        for rail in self._active(stage):
            res = rail.check(current)
            results.append(res)
            if res.action is RailAction.BLOCK:
                return GuardrailOutcome(
                    text=BLOCKED_OUTPUT_MESSAGE if stage is RailStage.OUTPUT else res.reason,
                    blocked=True,
                    block_reason=res.reason,
                    results=results,
                )
            if res.action is RailAction.REDACT:
                current = res.text  # teruskan teks teredaksi ke rail berikutnya
        return GuardrailOutcome(text=current, blocked=False, block_reason="", results=results)

    def check_input(self, text: str) -> GuardrailOutcome:
        return self.run(RailStage.INPUT, text)

    def check_output(self, text: str) -> GuardrailOutcome:
        return self.run(RailStage.OUTPUT, text)
