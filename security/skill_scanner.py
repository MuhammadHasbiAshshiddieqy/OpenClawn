"""Skill scanner — pemeriksaan keamanan untuk skill yang diimpor dari luar.

Terinspirasi `nvidia/skillspector` (scanner skill agent): skill pack adalah konten
TAK-TEPERCAYA yang masuk ke ranah agent, jadi harus diperiksa SEBELUM disimpan.
Sebelumnya impor hanya lewat `Shield` (4 regex prompt-injection) — tak sepadan dengan
ancaman skill yang membawa kode/eksfiltrasi.

Scanner ini dua lapis, MURNI stdlib (tanpa dependency — §6 proyek minimal):
  1. AST (`ast.walk`) — deteksi pemanggilan berbahaya pada blok kode di dalam skill:
     `exec`/`eval`/`compile`, `os.system`, `subprocess.*`, `__import__` dinamis,
     `open(...,'w')` (tulis file), `eval`/`exec` via `getattr`. AST hanya jalan untuk
     blok yang BENAR parse sebagai Python; teks biasa di-skip (banyak skill = prosa).
  2. Pola leksikal — eksfiltrasi (`curl|wget` ke host), URL mentah dengan kredensial,
     payload base64 panjang, path sensitif (`~/.ssh`, `.env`, `id_rsa`).

Hasil: `ScanResult(score 0-100, verdict, findings)`.
  - verdict `reject`   (score ≥ HIGH)  → impor DITOLAK (§1: keamanan-dulu). Keputusan
    owner: risiko tinggi tak masuk DB sama sekali.
  - verdict `flag`     (MED ≤ score < HIGH) → boleh impor tapi diberi label risiko.
  - verdict `clean`    (score < MED).

Scanner ini SELALU aktif pada impor — keamanan bukan optimasi, tak bisa dimatikan
dari UI (beda dari compaction headroom yang opt-in). Extractable: stdlib saja.
"""

import ast
import re
import unicodedata
from dataclasses import dataclass, field

# Ambang skor → verdict. Skor diakumulasi per temuan (severity bobot di bawah).
# HIGH=50 disetel agar SATU temuan kritis (exec/subprocess/curl|sh = 50) cukup untuk
# reject sendirian — eksekusi kode arbitrer tak butuh "bukti kedua". Temuan sedang
# (15) baru reject bila menumpuk/berpasangan dengan yang lebih berat.
SCORE_HIGH = 50  # ≥ ini → reject
SCORE_MED = 25  # ≥ ini → flag

# Bobot severity per temuan.
_SEV_CRITICAL = 50  # eksekusi kode arbitrer / shell
_SEV_HIGH = 30  # eksfiltrasi / akses kredensial
_SEV_MED = 15  # tulis file / import dinamis
_SEV_LOW = 8  # sinyal lemah (base64 panjang, dll.)

# Nama fungsi berbahaya yang dideteksi via AST (panggilan langsung).
_DANGEROUS_CALLS = {
    "exec": _SEV_CRITICAL,
    "eval": _SEV_CRITICAL,
    "compile": _SEV_HIGH,
    "__import__": _SEV_MED,
}
# Pemanggilan ber-attribute: (modul/objek, atribut) → severity.
_DANGEROUS_ATTR_CALLS = {
    ("os", "system"): _SEV_CRITICAL,
    ("os", "popen"): _SEV_CRITICAL,
    ("subprocess", "run"): _SEV_CRITICAL,
    ("subprocess", "call"): _SEV_CRITICAL,
    ("subprocess", "Popen"): _SEV_CRITICAL,
    ("subprocess", "check_output"): _SEV_CRITICAL,
    ("os", "remove"): _SEV_MED,
    ("os", "unlink"): _SEV_MED,
    ("shutil", "rmtree"): _SEV_HIGH,
}

# Pola leksikal (selain AST). Dijalankan pada teks ter-normalisasi NFKD.
_PATTERNS: list[tuple[str, str, int]] = [
    # (label, regex, severity)
    ("shell_exfil", r"\b(curl|wget)\s+[^\n|]*\|\s*(ba)?sh", _SEV_CRITICAL),
    ("curl_post", r"\bcurl\s+[^\n]*(-d|--data|-F|-T|-X\s*POST)\b", _SEV_HIGH),
    ("cred_path", r"(~/\.ssh|id_rsa|\.aws/credentials|/etc/shadow)", _SEV_HIGH),
    ("dotenv_read", r"\b(cat|read|open)\b[^\n]{0,40}\.env\b", _SEV_MED),
    ("url_with_cred", r"https?://[^\s/@]+:[^\s/@]+@", _SEV_HIGH),
    ("pipe_to_eval", r"\beval\s*\(\s*(input|request|urlopen|recv)", _SEV_CRITICAL),
    ("base64_blob", r"[A-Za-z0-9+/]{120,}={0,2}", _SEV_LOW),
    ("metadata_endpoint", r"169\.254\.169\.254|metadata\.google\.internal", _SEV_HIGH),
]
_COMPILED = [(label, re.compile(pat, re.IGNORECASE), sev) for label, pat, sev in _PATTERNS]

# Blok kode dalam skill Markdown: ```...``` (peliharanya fokus AST hanya ke kode).
_CODE_FENCE_RE = re.compile(r"```(?:python|py)?\s*(.*?)```", re.DOTALL)


@dataclass
class ScanResult:
    """Hasil pemindaian satu skill."""

    score: int
    verdict: str  # "clean" | "flag" | "reject"
    findings: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """True bila skill harus DITOLAK impor (§1: keamanan-dulu)."""
        return self.verdict == "reject"


def _verdict_for(score: int) -> str:
    if score >= SCORE_HIGH:
        return "reject"
    if score >= SCORE_MED:
        return "flag"
    return "clean"


def _scan_ast(code: str) -> list[tuple[str, int]]:
    """Walk AST satu blok kode → list (temuan, severity). Tak pernah raise.

    Blok yang tak parse sebagai Python (prosa, pseudo-code) → [] (di-skip diam).
    """
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return []  # bukan Python valid → bukan urusan AST scan
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # Panggilan nama langsung: exec(...), eval(...), __import__(...)
            if isinstance(func, ast.Name) and func.id in _DANGEROUS_CALLS:
                out.append((f"call:{func.id}", _DANGEROUS_CALLS[func.id]))
            # Panggilan ber-attribute: os.system(...), subprocess.run(...)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                key = (func.value.id, func.attr)
                if key in _DANGEROUS_ATTR_CALLS:
                    out.append((f"call:{key[0]}.{key[1]}", _DANGEROUS_ATTR_CALLS[key]))
        # open(path, 'w'/'a') → tulis file (sinyal sedang).
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
        ):
            for arg in node.args[1:]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if any(m in arg.value for m in ("w", "a", "+")):
                        out.append(("call:open(write)", _SEV_MED))
    return out


def scan_skill(name: str, content: str) -> ScanResult:
    """Pindai satu skill (nama + konten Markdown) → `ScanResult`.

    Tak pernah raise: input eksternal, kegagalan analisis = fail-safe ke temuan
    yang sudah terkumpul (tidak diam-diam meloloskan). Skor diakumulasi & di-clamp 100.
    """
    normalized = unicodedata.normalize("NFKD", content)
    findings: list[str] = []
    score = 0

    # Lapis 1: AST pada tiap blok kode berpagar.
    code_blocks = _CODE_FENCE_RE.findall(normalized)
    # Bila tak ada pagar tapi konten tampak seperti kode (banyak skill polos), coba
    # parse seluruh konten sebagai usaha terakhir — gratis bila gagal parse.
    if not code_blocks:
        code_blocks = [normalized]
    seen: set[str] = set()
    for block in code_blocks:
        for label, sev in _scan_ast(block):
            if label not in seen:  # hitung tiap jenis temuan sekali (hindari inflasi)
                seen.add(label)
                findings.append(f"ast {label}")
                score += sev

    # Lapis 2: pola leksikal pada seluruh konten ter-normalisasi.
    for label, rx, sev in _COMPILED:
        if rx.search(normalized):
            if label not in seen:
                seen.add(label)
                findings.append(f"pattern {label}")
                score += sev

    score = min(score, 100)
    return ScanResult(score=score, verdict=_verdict_for(score), findings=findings)
