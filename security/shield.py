import re
import unicodedata

DANGER_PATTERNS = [
    # Prompt-injection klasik.
    r"ignore (previous|all) instructions",
    r"abaikan (instruksi|perintah) (sebelumnya|di atas)",
    r"system prompt",
    r"reveal your (instructions|prompt)",
    # Eksfiltrasi instruksi (variasi yang sering muncul di payload injection).
    r"print your (system )?(instructions|prompt|rules)",
    r"disregard (the above|previous|all)",
    r"you are now (in )?(developer|dan|jailbreak) mode",
]


class Shield:
    """
    Lapisan kosmetik — BUKAN pertahanan utama.
    Pertahanan utama tetap container isolation (lihat §16).
    """

    @staticmethod
    def scan_input(text: str) -> tuple[bool, str]:
        # Nit #4: normalisasi NFKD dulu untuk cegah homoglyph bypass (ìgnore → ignore)
        normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        for pat in DANGER_PATTERNS:
            if re.search(pat, normalized, re.IGNORECASE):
                return False, "Input ditolak: pola mencurigakan terdeteksi"
        return True, ""
